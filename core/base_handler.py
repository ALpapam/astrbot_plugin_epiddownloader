"""通用下载流程 Mixin：限速 / 白名单 / 缓存检查 / 合并转发预览 / 打包发送 / 错误通知

Pixiv 与 E-Hentai 两个下载器共享此基类，只实现各自的「获取元信息 + 下载图片」逻辑。
"""
import asyncio
import shutil
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Node, Nodes, Plain

from .cache import ensure_cache_limits
from .paths import (
    get_cache_path,
    get_cached_output_path,
    get_source_cache_path,
)
from .rate_limiter import RateLimiter
from .tools import images_to_pdf_async, images_to_zip_async

_rate_limiter = RateLimiter()

# 来源类型显示名
_KIND_LABELS = {"pixiv": "P站", "ehentai": "E站"}


class BaseDownloadMixin:
    """P/E 站下载通用混入类，提供限速、缓存、预览、打包、发送能力"""

    # ---------- 配置获取 ----------
    def _get_config_value(self, key: str, default):
        keys = key.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def _refresh_config(self) -> None:
        """刷新插件配置"""
        self.ep_output_mode = str(
            self._get_config_value("ep_settings.output_mode", "压缩包")
        ).strip()
        self.ep_pixiv_send_mode = str(
            self._get_config_value("ep_settings.pixiv_send_mode", "原图直发")
        ).strip()
        self.ep_pixiv_direct_merge_threshold = max(
            1,
            int(self._get_config_value("ep_settings.pixiv_direct_merge_threshold", 3)),
        )
        self.ep_zip_password = str(
            self._get_config_value("ep_settings.zip_password", "")
        ).strip()
        self.ep_preview_images_group = max(
            1, int(self._get_config_value("ep_settings.preview_images_group", 5))
        )
        self.ep_preview_images_private = max(
            1, int(self._get_config_value("ep_settings.preview_images_private", 50))
        )
        self.ep_proxy = str(
            self._get_config_value("ep_settings.proxy", "")
        ).strip()
        self.ep_pixiv_login_method = str(
            self._get_config_value("ep_settings.pixiv_login_method", "密码")
        ).strip()
        self.ep_pixiv_username = str(
            self._get_config_value("ep_settings.pixiv_username", "")
        ).strip()
        self.ep_pixiv_password = str(
            self._get_config_value("ep_settings.pixiv_password", "")
        ).strip()
        self.ep_pixiv_refresh_token = str(
            self._get_config_value("ep_settings.pixiv_refresh_token", "")
        ).strip()
        self.ep_ehentai_cookies = str(
            self._get_config_value("ep_settings.ehentai_cookies", "")
        ).strip()
        self.ep_ehentai_use_exhentai = bool(
            self._get_config_value("ep_settings.ehentai_use_exhentai", False)
        )
        self.ep_cache_retention_days = max(
            1, int(self._get_config_value("ep_settings.cache_retention_days", 3))
        )
        self.ep_cache_max_size_gb = max(
            0.0, float(self._get_config_value("ep_settings.cache_max_size_gb", 3.0))
        )
        self.ep_rate_limit_seconds = max(
            0, int(self._get_config_value("ep_settings.rate_limit_seconds", 60))
        )
        self.ep_merge_send_as_sender = bool(
            self._get_config_value("ep_settings.merge_send_as_sender", False)
        )
        self.ep_whitelist_enabled = bool(
            self._get_config_value("ep_settings.whitelist_enabled", True)
        )
        self.ep_whitelist_set = set()
        whitelist_raw = str(
            self._get_config_value("ep_settings.whitelist_groups", "")
        ).strip()
        if whitelist_raw:
            for g in whitelist_raw.split(","):
                g = g.strip()
                if g:
                    self.ep_whitelist_set.add(g)
        self.ep_admin_qq = str(
            self._get_config_value("ep_settings.admin_qq", "")
        ).strip()
        self.ep_file_merge_forward = bool(
            self._get_config_value("ep_settings.file_merge_forward_enabled", True)
        )
        self.ep_allow_private_chat = bool(
            self._get_config_value("ep_settings.allow_private_chat", True)
        )
        self.ep_error_notify = str(
            self._get_config_value("ep_settings.error_notify_mode", "通知")
        ).strip()

    # ---------- 小工具 ----------
    def _get_merge_sender_uin(self, event: AstrMessageEvent) -> str:
        """根据配置决定合并转发使用的 uin"""
        if self.ep_merge_send_as_sender:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
        return str(event.get_self_id())

    def _get_kind_label(self, kind: str) -> str:
        return _KIND_LABELS.get(kind, kind)

    def _get_preview_limit(self, event: AstrMessageEvent) -> int:
        """根据群聊/私聊返回预览图片上限"""
        if event.get_group_id():
            return self.ep_preview_images_group
        return self.ep_preview_images_private

    # ---------- 访问控制 ----------
    def _check_access(self, event: AstrMessageEvent) -> str | None:
        """白名单 + 私聊开关检查

        Returns:
            None 表示允许；否则返回拒绝提示消息文本
        """
        self._refresh_config()
        if self.ep_whitelist_enabled:
            gid = event.get_group_id()
            if gid and gid not in self.ep_whitelist_set:
                admin_line = (
                    f"🐧 有需求请联系管理员：{self.ep_admin_qq}"
                    if self.ep_admin_qq
                    else "🐧 请联系本群管理员"
                )
                return f"⛔ 本群未在白名单中\n{admin_line}"

        if not event.get_group_id() and not self.ep_allow_private_chat:
            return "⛔ 私聊下载已被管理员关闭"
        return None

    def _check_rate_limit(self, event: AstrMessageEvent) -> tuple[bool, float]:
        """群级限速检查"""
        group_id = event.get_group_id() or "private"
        return _rate_limiter.check_and_use(group_id, self.ep_rate_limit_seconds)

    def _release_rate_limit(self, event: AstrMessageEvent) -> None:
        group_id = event.get_group_id() or "private"
        _rate_limiter.release(group_id)

    # ---------- 错误通知 ----------
    def _yield_error(self, event: AstrMessageEvent, message: str):
        """yield 错误信息"""
        if self.ep_error_notify != "通知":
            yield event.plain_result(f"❌ P/E站下载失败: {message[:200]}")
            return
        yield event.plain_result(f"❌ P/E站下载失败\n{'─' * 20}\n{message}")

    # ---------- 缓存检查 ----------
    def _check_cached_outputs(
        self, kind: str, item_id: str, title: str
    ) -> tuple[Path | None, Path | None]:
        """检查是否有缓存的 PDF 和 ZIP 输出文件

        Returns:
            (pdf_path, zip_path) — 不存在的为 None
        """
        pdf_path = get_cached_output_path(kind, item_id, title, "pdf")
        zip_path = get_cached_output_path(kind, item_id, title, "zip")

        return (
            pdf_path if pdf_path.exists() else None,
            zip_path if zip_path.exists() else None,
        )

    def _check_cached_images_exist(self, kind: str, item_id: str) -> bool:
        """检查指定作品是否所有图片都缓存完毕"""
        item_dir = get_source_cache_path(kind) / str(item_id)
        if not item_dir.exists():
            return False
        for ext in ("*.webp", "*.jpg", "*.png", "*.gif"):
            if list(item_dir.glob(ext)):
                return True
        return False

    def _collect_all_cached_images(self, kind: str, item_id: str) -> list[Path]:
        """从缓存目录收集该作品已下载的所有图片（不限制数量）"""
        item_dir = get_source_cache_path(kind) / str(item_id)
        if not item_dir.exists():
            return []

        image_paths: list[Path] = []
        for ext in ("*.webp", "*.jpg", "*.png", "*.gif"):
            for f in sorted(item_dir.rglob(ext)):
                if ".preview." not in f.name:
                    image_paths.append(f)

        logger.info(
            f"📦 从缓存收集到 {len(image_paths)} 张图片 ({kind}/{item_id})"
        )
        return image_paths

    # ---------- 合并转发预览 ----------
    def _build_merge_preview_nodes(
        self,
        event: AstrMessageEvent,
        kind: str,
        item_id: str,
        title: str,
        author: str,
        tags: list[str],
        extra_lines: list[str],
        image_paths: list[Path],
    ) -> Nodes:
        """构建合并转发的 Nodes 对象

        Node[0]: "{item_id} 鉴"
        Node[1]: 标题 / 作者 / 标签 / 附加信息
        Node[2..N+1]: 预览图片
        """
        nodes = Nodes([])
        sender_uin = self._get_merge_sender_uin(event)

        nodes.nodes.append(
            Node(uin=sender_uin, content=[Plain(f"{item_id} 鉴")])
        )

        tag_str = ", ".join(tags) if tags else "无"
        lines = [
            f"标题: {title}",
            f"作者: {author}",
            f"标签: {tag_str}",
        ]
        lines.extend(extra_lines)
        lines.append(f"预览前{len(image_paths)}张，可发送压缩包获取全部")
        info_text = "\n".join(lines)
        nodes.nodes.append(
            Node(uin=sender_uin, content=[Plain(info_text)])
        )

        for img_path in image_paths:
            if not img_path.exists():
                continue
            display_path = self._ensure_preview_compatible(img_path)
            nodes.nodes.append(
                Node(
                    uin=sender_uin,
                    content=[Image.fromFileSystem(str(display_path.resolve()))],
                )
            )

        logger.info(
            f"📋 合并转发构建完成: kind={kind}, id={item_id}, "
            f"节点数={len(nodes.nodes)} (1鉴+1信息+{len(image_paths)}图)"
        )
        return nodes

    @staticmethod
    def _ensure_preview_compatible(img_path: Path) -> Path:
        """将可能不被 QQ 合并转发支持的格式（webp/gif）转为 jpg 预览图"""
        if img_path.suffix.lower() in (".webp", ".gif", ".bmp"):
            jpg_path = img_path.with_suffix(".preview.jpg")
            if not jpg_path.exists():
                try:
                    img = PILImage.open(str(img_path))
                    if img.mode in ("RGBA", "P", "LA"):
                        img = img.convert("RGB")
                    img.save(str(jpg_path), "JPEG", quality=95)
                except Exception:
                    jpg_path = img_path
            return jpg_path
        return img_path

    # ---------- 打包发送 ----------
    async def _generate_and_send_outputs(
        self,
        event: AstrMessageEvent,
        kind: str,
        item_id: str,
        title: str,
        image_paths: list[Path],
        output_mode: str,
        summary: str,
    ) -> AsyncGenerator:
        """根据图片列表生成 ZIP/PDF 并发送，生成的文件缓存到 _output 目录"""
        send_pdf = output_mode in ("PDF", "两者")
        send_zip = output_mode in ("压缩包", "两者")

        zip_password = self.ep_zip_password if send_zip else ""
        prompt_parts = []
        if send_pdf and send_zip:
            prompt_parts.append("📄 正在准备PDF/ZIP，请稍候...")
        elif send_pdf:
            prompt_parts.append("📄 正在准备PDF，请稍候...")
        else:
            prompt_parts.append("📦 正在准备ZIP，请稍候...")
        if zip_password:
            prompt_parts.append(f"🔑 ZIP密码: {zip_password}")
        yield event.plain_result("\n".join(prompt_parts))

        pdf_ready = None
        if send_pdf:
            pdf_path = get_cached_output_path(kind, item_id, title, "pdf")
            if pdf_path.exists():
                logger.info(f"📄 PDF缓存已存在: {pdf_path}")
                pdf_ready = pdf_path
            else:
                success = await images_to_pdf_async(image_paths, str(pdf_path))
                if success and pdf_path.exists():
                    logger.info(f"📄 PDF生成完成: {pdf_path.name}")
                    pdf_ready = pdf_path
                else:
                    logger.error(f"PDF 生成失败: kind={kind}, id={item_id}")
                    yield event.plain_result("❌ PDF 生成失败")

        zip_ready = None
        if send_zip:
            zip_path = get_cached_output_path(kind, item_id, title, "zip")
            if zip_path.exists():
                logger.info(f"📦 ZIP缓存已存在: {zip_path}")
                zip_ready = zip_path
            else:
                success = await images_to_zip_async(
                    image_paths, str(zip_path), password=zip_password
                )
                if success and zip_path.exists():
                    logger.info(f"📦 ZIP生成完成: {zip_path.name}")
                    zip_ready = zip_path
                else:
                    logger.error(f"ZIP 生成失败: kind={kind}, id={item_id}")
                    yield event.plain_result("❌ ZIP 生成失败")

        is_group = bool(event.get_group_id())
        use_merge = is_group and self.ep_file_merge_forward

        if use_merge:
            merge_nodes = Nodes([])
            sender_uin = str(event.get_self_id())
            if pdf_ready:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[File(name=pdf_ready.name, file=str(pdf_ready))])
                )
            if zip_ready:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[File(name=zip_ready.name, file=str(zip_ready))])
                )
            if summary:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[Plain(summary)])
                )
            if merge_nodes.nodes:
                yield event.chain_result([merge_nodes])
            elif summary:
                yield event.plain_result(summary)
        else:
            if pdf_ready:
                yield event.chain_result([File(name=pdf_ready.name, file=str(pdf_ready))])
                await asyncio.sleep(1)
            if zip_ready:
                yield event.chain_result([File(name=zip_ready.name, file=str(zip_ready))])
            if summary:
                yield event.plain_result(summary)

    async def _send_cached_outputs(
        self,
        event: AstrMessageEvent,
        kind: str,
        item_id: str,
        title: str,
        output_mode: str,
        summary: str,
    ) -> AsyncGenerator:
        """缓存命中时直接发送缓存的输出文件"""
        cached_pdf, cached_zip = self._check_cached_outputs(kind, item_id, title)
        send_pdf = output_mode in ("PDF", "两者")
        send_zip = output_mode in ("压缩包", "两者")

        is_group = bool(event.get_group_id())
        use_merge = is_group and self.ep_file_merge_forward

        if use_merge:
            merge_nodes = Nodes([])
            sender_uin = str(event.get_self_id())
            if send_pdf and cached_pdf:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[File(name=cached_pdf.name, file=str(cached_pdf))])
                )
            if send_zip and cached_zip:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[File(name=cached_zip.name, file=str(cached_zip))])
                )
            if summary:
                merge_nodes.nodes.append(
                    Node(uin=sender_uin, content=[Plain(summary)])
                )
            if merge_nodes.nodes:
                yield event.chain_result([merge_nodes])
        else:
            if send_pdf and cached_pdf:
                yield event.chain_result([File(name=cached_pdf.name, file=str(cached_pdf))])
                await asyncio.sleep(1)
            if send_zip and cached_zip:
                yield event.chain_result([File(name=cached_zip.name, file=str(cached_zip))])
            if summary:
                yield event.plain_result(summary)

    # ---------- 缓存清理 ----------
    async def _handle_clear_cache(self, event: AstrMessageEvent) -> AsyncGenerator:
        """清理整个插件缓存目录（包括图片、ZIP、PDF）"""
        cache_dir = get_cache_path()
        if not cache_dir.exists():
            yield event.plain_result("📭 缓存目录不存在，无需清理")
            return

        try:
            total_bytes = 0
            file_count = 0
            for f in cache_dir.rglob("*"):
                if f.is_file():
                    try:
                        total_bytes += f.stat().st_size
                        file_count += 1
                    except OSError:
                        pass

            shutil.rmtree(str(cache_dir))
            cache_dir.mkdir(parents=True, exist_ok=True)

            size_mb = total_bytes / (1024 * 1024)
            yield event.plain_result(
                f"🧹 P/E站缓存已清理\n"
                f"删除文件: {file_count} 个\n"
                f"释放空间: {size_mb:.1f} MB"
            )
            logger.info(f"🧹 P/E站缓存清理完成: 删除 {file_count} 个文件, {size_mb:.1f} MB")
        except Exception as exc:
            logger.error(f"❌ 缓存清理失败: {exc}")
            yield event.plain_result(f"❌ 缓存清理失败: {str(exc)[:200]}")

    def _finalize_cache_cleanup(self) -> None:
        """每次下载完成后检查缓存上限"""
        try:
            ensure_cache_limits(
                self.ep_cache_retention_days, self.ep_cache_max_size_gb
            )
        except Exception as exc:
            logger.warning(f"缓存清理失败: {exc}")

    def _log_download_complete(self, kind: str, item_id: str, title: str,
                               image_count: int, elapsed: float) -> None:
        logger.info(
            f"📊 {kind}下载完成: id={item_id} | {title} | "
            f"图片: {image_count}张 | 耗时: {elapsed:.1f}s"
        )
