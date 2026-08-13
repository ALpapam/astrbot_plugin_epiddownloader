"""Pixiv（P站）下载逻辑 — 基于 pixivpy3

- 支持 refresh_token 与账号密码两种登录方式（优先 refresh_token）
- 多图作品（meta_pages）逐张下载，ugora 动画取静态海报帧
- 图片缓存于 {cache}/pixiv/{pid}/，输出文件缓存于 {cache}/_output/
"""
import asyncio
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .base_handler import BaseDownloadMixin
from .paths import get_pixiv_cache_path


class PixivDownloadMixin(BaseDownloadMixin):
    """Pixiv 下载混入类"""

    # 共享 API 实例（pixivpy 会话非线程安全，串行访问）
    _pixiv_api = None

    # ---------- Pixiv API ----------
    def _get_pixiv_api(self):
        """构建并缓存 Pixiv API 实例，自动登录"""
        if PixivDownloadMixin._pixiv_api is not None:
            return PixivDownloadMixin._pixiv_api

        from pixivpy3 import AppPixivAPI

        kwargs = {}
        if self.ep_proxy:
            kwargs["proxies"] = {"http": self.ep_proxy, "https": self.ep_proxy}

        api = AppPixivAPI(**kwargs)
        api.set_accept_language("zh-cn")

        if self.ep_pixiv_refresh_token:
            logger.info("🔑 Pixiv 使用 refresh_token 认证")
            api.auth(refresh_token=self.ep_pixiv_refresh_token)
        elif self.ep_pixiv_username and self.ep_pixiv_password:
            logger.info("🔑 Pixiv 使用账号密码认证")
            api.auth(username=self.ep_pixiv_username, password=self.ep_pixiv_password)
        else:
            raise RuntimeError("未配置 Pixiv 登录信息（账号密码或 refresh_token 至少填一项）")

        PixivDownloadMixin._pixiv_api = api
        return api

    # ---------- 图片 URL 提取 ----------
    @staticmethod
    def _extract_pixiv_image_urls(illust) -> list[str]:
        """从 illust 对象提取全部图片原始 URL"""
        urls: list[str] = []
        illust_type = str(getattr(illust, "type", "") or "")

        # 动画（ugora）：仅取静态海报帧
        if illust_type == "ugoira":
            iu = getattr(illust, "image_urls", None)
            u = (
                getattr(iu, "large", None)
                or getattr(iu, "medium", None)
                or getattr(iu, "original", None)
            )
            if u:
                urls.append(u)
            return urls

        meta_pages = getattr(illust, "meta_pages", None)
        if meta_pages:
            for page in meta_pages:
                iu = getattr(page, "image_urls", None)
                u = (
                    getattr(iu, "original", None)
                    or getattr(iu, "large", None)
                    or getattr(iu, "medium", None)
                )
                if u:
                    urls.append(u)
        else:
            msp = getattr(illust, "meta_single_page", None)
            single_url = None
            if isinstance(msp, dict):
                single_url = msp.get("original_image_url") or msp.get("large_image_url")
            elif msp is not None:
                single_url = getattr(msp, "original_image_url", None) or getattr(
                    msp, "large_image_url", None
                )
            iu = getattr(illust, "image_urls", None)
            single_url = single_url or (
                getattr(iu, "original", None) or getattr(iu, "large", None)
            )
            if single_url:
                urls.append(single_url)

        return [u for u in urls if u]

    # ---------- 主流程 ----------
    def _pixiv_lock(self) -> asyncio.Lock:
        """懒创建 asyncio 锁（pixivpy 会话非线程安全，串行访问不阻塞事件循环）"""
        if not hasattr(self, "_pixiv_async_lock"):
            self._pixiv_async_lock = asyncio.Lock()
        return self._pixiv_async_lock

    async def _download_pixiv(self, event: AstrMessageEvent, pid: str) -> AsyncGenerator:
        process_start = time.perf_counter()
        self._refresh_config()

        allowed, wait_seconds = self._check_rate_limit(event)
        if not allowed:
            for result in self._yield_error(
                event,
                f"下载过于频繁，请等待 {wait_seconds:.0f} 秒后再试\n"
                f"本群限速间隔: {self.ep_rate_limit_seconds}秒",
            ):
                yield result
            return

        output_mode = self.ep_output_mode
        send_pdf = output_mode in ("PDF", "两者")
        send_zip = output_mode in ("压缩包", "两者")
        send_files = output_mode != "仅预览"

        # 1. 登录 + 2. 获取作品元信息（串行访问 pixiv API）
        async with self._pixiv_lock():
            try:
                api = self._get_pixiv_api()
            except Exception as exc:
                self._release_rate_limit(event)
                logger.error(f"Pixiv 登录失败: {exc}")
                for result in self._yield_error(
                    event,
                    f"Pixiv 登录失败\n"
                    f"原因: {str(exc)[:200]}\n"
                    f"请检查插件配置中的账号密码或 refresh_token",
                ):
                    yield result
                return

            try:
                result = await asyncio.to_thread(api.illust_detail, int(pid))
                illust = result.illust
            except Exception as exc:
                self._release_rate_limit(event)
                logger.error(f"获取 P站作品失败: {exc}")
                for result in self._yield_error(
                    event,
                    f"获取 P站作品失败\n"
                    f"pid: {pid}\n"
                    f"原因: {str(exc)[:200]}\n"
                    f"请检查作品ID是否正确、登录是否有效",
                ):
                    yield result
                return

        title = str(getattr(illust, "title", "") or "未知标题")
        user = getattr(illust, "user", None)
        author = str(getattr(user, "name", "") or "未知作者")
        tags_raw = getattr(illust, "tags", None) or []
        tags = []
        for t in tags_raw:
            name = getattr(t, "name", "") or ""
            if name:
                tags.append(name)
        page_count = int(getattr(illust, "page_count", 1) or 1)
        image_urls = self._extract_pixiv_image_urls(illust)

        logger.info(
            f"📖 P站作品: {title} | 作者: {author} | 页数: {page_count} | 提取URL: {len(image_urls)}"
        )

        if not image_urls:
            self._release_rate_limit(event)
            for result in self._yield_error(event, f"作品 {pid} 未提取到任何图片URL"):
                yield result
            return

        # 3. 缓存检查
        images_cached = self._check_cached_images_exist("pixiv", pid)
        cached_pdf, cached_zip = self._check_cached_outputs("pixiv", pid, title)

        need_zip = send_zip and cached_zip is None
        need_pdf = send_pdf and cached_pdf is None
        if send_files:
            output_cache_hit = not need_zip and not need_pdf
        else:
            # 仅预览模式：只要图片已缓存即可直接发预览，无需输出文件
            output_cache_hit = True

        all_image_paths: list[Path] = []
        if images_cached:
            all_image_paths = self._collect_all_cached_images("pixiv", pid)

        # 4. 完全缓存命中
        if images_cached and output_cache_hit and all_image_paths:
            logger.info(f"📦 P站缓存完全命中: pid={pid}, 跳过下载和生成")
            preview_paths = self._limit_preview(all_image_paths, event)
            yield event.plain_result(f"📥 开始获取「{title}」，请稍候...")
            nodes = self._build_merge_preview_nodes(
                event, "pixiv", pid, title, author, tags,
                [f"共{page_count}张图"], preview_paths,
            )
            yield event.chain_result([nodes])
            await asyncio.sleep(1)

            summary = (
                f"✅ 获取完成: {title}\n"
                f"作者: {author} | 图片: {len(all_image_paths)}张 | 耗时: {time.perf_counter() - process_start:.1f}s"
            )
            async for result in self._send_cached_outputs(
                event, "pixiv", pid, title, output_mode, summary
            ):
                yield result
            self._finalize_cache_cleanup()
            return

        # 5. 图片已缓存但需生成输出文件
        if images_cached and (need_zip or need_pdf) and all_image_paths:
            logger.info(f"📦 P站图片缓存存在，仅需生成输出: pid={pid}")
            preview_paths = self._limit_preview(all_image_paths, event)
            yield event.plain_result(f"📥 开始获取「{title}」，请稍候...")
            nodes = self._build_merge_preview_nodes(
                event, "pixiv", pid, title, author, tags,
                [f"共{page_count}张图"], preview_paths,
            )
            yield event.chain_result([nodes])
            await asyncio.sleep(1)

            summary = (
                f"✅ 获取完成: {title}\n"
                f"作者: {author} | 图片: {len(all_image_paths)}张 | 耗时: {time.perf_counter() - process_start:.1f}s"
            )
            async for result in self._generate_and_send_outputs(
                event, "pixiv", pid, title, all_image_paths,
                output_mode, summary,
            ):
                yield result
            self._finalize_cache_cleanup()
            return

        # 6. 全新下载
        yield event.plain_result(
            f"📥 开始获取「{title}」，共{len(image_urls)}张图，请稍候...\n"
            f"⚠️ 预览图片可能因数量过多而发送失败，属正常现象"
        )

        # 下载目录
        item_dir = get_pixiv_cache_path() / str(pid)
        item_dir.mkdir(parents=True, exist_ok=True)

        success_urls = 0
        fail_count = 0
        downloaded_paths: list[Path] = []
        try:
            async with self._pixiv_lock():
                for idx, url in enumerate(image_urls):
                    try:
                        ext = Path(urlparse(url).path).suffix.lstrip(".") or "jpg"
                        if ext.lower() not in ("jpg", "jpeg", "png", "gif", "webp"):
                            ext = "jpg"
                        fname = f"{idx+1:03d}.{ext}"
                        saved = await asyncio.to_thread(
                            api.download, url, "", str(item_dir), fname
                        )
                        local = item_dir / fname
                        if local.exists():
                            downloaded_paths.append(local)
                            success_urls += 1
                            logger.info(f"✅ [{idx+1}/{len(image_urls)}] 下载完成: {fname}")
                        elif saved:
                            downloaded_paths.append(local)
                            success_urls += 1
                        else:
                            fail_count += 1
                            logger.warning(f"⏭️ [{idx+1}/{len(image_urls)}] 下载跳过: {url}")
                    except Exception as exc:
                        fail_count += 1
                        logger.error(f"❌ 下载失败 [{idx+1}]: {url}: {exc}")
        except asyncio.CancelledError:
            logger.info(f"♻️ P站下载任务被中断: pid={pid}")
            raise
        except Exception as exc:
            logger.error(f"❌ P站下载任务异常: {exc}\n{traceback.format_exc()}")

        if not downloaded_paths:
            self._release_rate_limit(event)
            for result in self._yield_error(event, f"作品 {pid} 未成功下载任何图片"):
                yield result
            return

        all_image_paths = sorted(downloaded_paths)
        logger.info(
            f"🖼️ 共获取 {len(all_image_paths)} 张图片，准备发送合并转发"
        )

        preview_paths = self._limit_preview(all_image_paths, event)
        nodes = self._build_merge_preview_nodes(
            event, "pixiv", pid, title, author, tags,
            [f"共{page_count}张图"], preview_paths,
        )
        yield event.chain_result([nodes])
        await asyncio.sleep(1)

        total_time = time.perf_counter() - process_start
        summary = (
            f"✅ 获取完成: {title}\n"
            f"作者: {author} | 成功: {success_urls}张 | 失败: {fail_count}张 | 耗时: {total_time:.1f}s"
        )
        if send_files:
            async for result in self._generate_and_send_outputs(
                event, "pixiv", pid, title, all_image_paths,
                output_mode, summary,
            ):
                yield result
        else:
            yield event.plain_result(summary)

        self._log_download_complete("Pixiv", pid, title, len(all_image_paths), total_time)
        self._finalize_cache_cleanup()

    def _limit_preview(self, paths: list[Path], event: AstrMessageEvent) -> list[Path]:
        """按群聊/私聊限制预览图片数量"""
        limit = self._get_preview_limit(event)
        return paths[:limit]
