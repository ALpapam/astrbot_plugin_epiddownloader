"""E-Hentai（E站）下载逻辑 — 自研下载器（curl_cffi + bs4）

- E-Hentai 由 Cloudflare 防护，使用 curl_cffi 进行 Chrome TLS 指纹伪装
- 支持两种输入：完整画廊URL（g/{gid}/{token}/，100%可靠）或裸 gid（站点搜索解析 token）
- 画廊元信息优先走官方 gdata API，失败回退页面解析
- 图片逐页解析 #img src（E-Hentai 每页一张图，N 张图需 N 次请求）
- 串行下载 + 礼貌延迟，避免触发站点反爬（可能封 IP）
"""
import asyncio
import re
import time
import traceback
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlparse

import curl_cffi.requests as creq
from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .base_handler import BaseDownloadMixin
from .paths import get_ehentai_cache_path

# 画廊 URL 正则：https://e-hentai.org/g/{gid}/{token}/
_GALLERY_URL_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org)/g/(\d+)/([0-9a-f]+)/?", re.I
)


class EHentaiDownloadMixin(BaseDownloadMixin):
    """E-Hentai 下载混入类"""

    # 页面请求间隔（秒），避免触发反爬
    _PAGE_DELAY = 0.3
    _IMAGE_DELAY = 0.1

    def _eh_lock(self) -> asyncio.Lock:
        """懒创建 asyncio 锁：E-Hentai 反爬严格，同一时间只处理一个下载任务（不阻塞事件循环）"""
        if not hasattr(self, "_eh_async_lock"):
            self._eh_async_lock = asyncio.Lock()
        return self._eh_async_lock

    # ---------- 会话 ----------
    def _get_session(self):
        """构建 curl_cffi 会话（Chrome 指纹伪装 + Cookie + 代理）"""
        session = creq.Session(impersonate="chrome")
        if self.ep_proxy:
            session.proxies = {"http": self.ep_proxy, "https": self.ep_proxy}
        if self.ep_ehentai_cookies:
            cookies = {}
            for part in self.ep_ehentai_cookies.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
            session.cookies.update(cookies)
        return session

    def _eh_host(self) -> str:
        """返回使用的站点域名"""
        return (
            "https://exhentai.org"
            if self.ep_ehentai_use_exhentai
            else "https://e-hentai.org"
        )

    # ---------- gid/token 解析 ----------
    @staticmethod
    def resolve_gallery_from_input(input_str: str) -> tuple[str, str]:
        """从输入解析 (gid, token)。仅处理 URL 与纯数字，不做网络请求。"""
        input_str = input_str.strip()
        m = _GALLERY_URL_RE.match(input_str)
        if m:
            return m.group(1), m.group(2)
        if input_str.isdigit():
            return input_str, ""
        raise ValueError("无法识别的输入，请输入 gid 数字或完整画廊URL")

    def _resolve_token_by_search(self, session, gid: str) -> str:
        """通过站点搜索解析 gid 对应的 token"""
        url = (
            "https://e-hentai.org/?f_doujinshi=1&f_manga=1&f_artistcg=1&f_gamecg=1"
            f"&f_western=1&f_non-h=1&f_imageset=1&f_cosplay=1&f_asianporn=1&f_misc=1"
            f"&f_search={gid}&f_apply=Apply+Filter"
        )
        resp = session.get(url, timeout=20)
        text = resp.text
        if not text or len(text) < 500 or "Just a moment" in text or "cf-browser-verification" in text:
            raise RuntimeError(
                "E-Hentai 返回空响应（可能被 Cloudflare 拦截）\n"
                "请配置 ehentai_cookies，或直接提供完整画廊URL"
            )
        m = re.search(
            r"https?://(?:e-hentai\.org|exhentai\.org)/g/(\d+)/([0-9a-f]+)/", text
        )
        if not m:
            raise RuntimeError(
                f"未能在搜索中找到画廊 {gid}\n"
                "画廊可能已删除、搜索未收录，或需完整URL：https://e-hentai.org/g/{gid}/token/"
            )
        return m.group(2)

    # ---------- 画廊元信息 ----------
    @staticmethod
    def parse_gallery_meta_from_api(gmeta: dict) -> dict:
        """从 gdata API 返回的 gmetadata 解析元信息"""
        info = {
            "title": "未知标题",
            "page_count": 0,
            "tags": [],
            "category": "",
            "rating": "",
            "uploader": "",
            "author": "未知作者",
        }
        title = gmeta.get("title") or gmeta.get("title_jpn") or "未知标题"
        info["title"] = str(title).strip()
        try:
            info["page_count"] = int(gmeta.get("filecount") or 0)
        except (TypeError, ValueError):
            info["page_count"] = 0
        info["category"] = str(gmeta.get("category") or "")
        info["rating"] = str(gmeta.get("rating") or "")
        info["uploader"] = str(gmeta.get("uploader") or "")
        tags = []
        for t in gmeta.get("tags", []):
            # 每个 tag 形如 {"male": "xxx"} 或 {"female": "yyy"}
            if isinstance(t, dict):
                for v in t.values():
                    if v:
                        tags.append(str(v))
        info["tags"] = tags
        if info["uploader"]:
            info["author"] = info["uploader"]
        return info

    def _fetch_gallery_meta(
        self, session, gid: str, token: str, title_hint: str = ""
    ) -> dict:
        """获取画廊元信息：优先 gdata API，失败回退页面解析"""
        info = {
            "title": title_hint or "未知标题",
            "page_count": 0,
            "tags": [],
            "category": "",
            "rating": "",
            "uploader": "",
            "author": "未知作者",
        }
        try:
            resp = session.post(
                f"{self._eh_host()}/api.php",
                json={
                    "method": "gdata",
                    "gidlist": [[int(gid), token, 0]],
                    "namespace": 1,
                },
                headers={
                    "Content-Type": "application/json",
                    "Referer": f"{self._eh_host()}/",
                },
                timeout=20,
            )
            if resp.content:
                data = resp.json()
                gmeta = (data.get("gmetadata") or [{}])[0]
                if gmeta:
                    info = self.parse_gallery_meta_from_api(gmeta)
                    logger.info(
                        f"📖 E站画廊(API): {info['title']} | {info['page_count']}张 | "
                        f"分类: {info['category']}"
                    )
                    return info
        except Exception as exc:
            logger.warning(f"gdata API 获取失败，回退页面解析: {exc}")

        return self._fetch_gallery_meta_by_page(session, gid, token, title_hint)

    def _fetch_gallery_meta_by_page(
        self, session, gid: str, token: str, title_hint: str = ""
    ) -> dict:
        """回退：从画廊首页 HTML 解析元信息"""
        info = {
            "title": title_hint or "未知标题",
            "page_count": 0,
            "tags": [],
            "category": "",
            "rating": "",
            "uploader": "",
            "author": "未知作者",
        }
        url = f"{self._eh_host()}/g/{gid}/{token}/"
        resp = session.get(url, timeout=20)
        soup = BeautifulSoup(resp.text, "lxml")

        h1 = soup.find("h1", id="gj")
        if h1:
            info["title"] = h1.get_text(strip=True)

        gsc = soup.find("span", id="gsc")
        if gsc:
            try:
                info["page_count"] = int(gsc.get_text(strip=True) or 0)
            except ValueError:
                pass

        taglist = soup.find("div", id="taglist")
        if taglist:
            for td in taglist.select("td a"):
                t = td.get_text(strip=True)
                if t:
                    info["tags"].append(t)

        rating_label = soup.find("span", id="rating_label")
        if rating_label:
            info["rating"] = rating_label.get_text(strip=True)

        gdc = soup.find("div", id="gdc")
        if gdc:
            info["category"] = gdc.get_text(strip=True)

        logger.info(
            f"📖 E站画廊(页面): {info['title']} | {info['page_count']}张 | "
            f"分类: {info['category']}"
        )
        return info

    # ---------- 图片 URL 提取 ----------
    @staticmethod
    def extract_image_url_from_page(text: str) -> str | None:
        """从画廊单页 HTML 提取当前图片 URL"""
        soup = BeautifulSoup(text, "lxml")
        img = soup.find("img", id="img")
        if img:
            src = img.get("src") or img.get("data-src")
            if src:
                return src.strip()
        img = soup.select_one("#i3 img")
        if img:
            src = img.get("src") or img.get("data-src")
            if src:
                return src.strip()
        return None

    @staticmethod
    def _extract_next_href(text: str) -> str | None:
        """从画廊单页 HTML 提取下一页链接（<a id="next">）"""
        soup = BeautifulSoup(text, "lxml")
        a = soup.find("a", id="next")
        if a and a.get("href"):
            return a["href"].strip()
        return None

    def _is_blocked(self, text: str) -> bool:
        """判断响应是否为 Cloudflare 拦截/空响应"""
        return not text or len(text) < 500 or "Just a moment" in text or "cf-browser-verification" in text

    def _fetch_by_pagination(
        self, session, gid: str, token: str, page_count: int
    ) -> list[str]:
        """方式一：已知页数时按 ?p= 分页直接抓取（gdata API 正常时最快）"""
        urls: list[str] = []
        base = f"{self._eh_host()}/g/{gid}/{token}/"
        for n in range(page_count):
            page_url = base + (f"?p={n}" if n > 0 else "")
            try:
                resp = session.get(page_url, timeout=20)
                if self._is_blocked(resp.text):
                    raise RuntimeError(
                        "E-Hentai 返回空响应（可能被 Cloudflare 拦截）\n"
                        "请配置 ehentai_cookies 后重试"
                    )
                img_url = self.extract_image_url_from_page(resp.text)
                if img_url:
                    urls.append(img_url)
                else:
                    logger.warning(f"⚠️ 第{n+1}页未解析到图片URL: {page_url}")
            except Exception as exc:
                logger.warning(f"❌ 第{n+1}页解析失败: {exc}")
            time.sleep(self._PAGE_DELAY)
        return urls

    def _fetch_by_next_chain(self, session, gid: str, token: str) -> list[str]:
        """方式二：跟随站点原生 #next 链接链抓取（页数未知时的兜底，最可靠）"""
        from urllib.parse import urljoin

        urls: list[str] = []
        errors: list[str] = []
        current = f"{self._eh_host()}/g/{gid}/{token}/"
        visited = 0
        while current and visited < 5000:
            try:
                resp = session.get(current, timeout=20)
                if self._is_blocked(resp.text):
                    raise RuntimeError(
                        "E-Hentai 返回空响应（可能被 Cloudflare 拦截）\n"
                        "请配置 ehentai_cookies 后重试"
                    )
                img_url = self.extract_image_url_from_page(resp.text)
                next_href = self._extract_next_href(resp.text)
                if img_url:
                    urls.append(img_url)
                if not next_href:
                    break
                current = urljoin(current, next_href)
            except Exception as exc:
                errors.append(str(exc))
                break
            visited += 1
            time.sleep(self._PAGE_DELAY)

        if not urls and errors:
            raise RuntimeError("所有页面均解析失败\n" + errors[0])
        return urls

    def _fetch_image_urls(
        self, session, gid: str, token: str, page_count: int
    ) -> list[str]:
        """解析画廊全部图片 URL：优先 ?p= 分页，失败/页数未知时回退 next 链接链"""
        if page_count and page_count > 0:
            urls = self._fetch_by_pagination(session, gid, token, page_count)
            if urls:
                return urls
            logger.warning("?p= 分页解析失败，回退 next 链接链方式...")
        return self._fetch_by_next_chain(session, gid, token)

    # ---------- 下载 ----------
    def _download_images(
        self, session, gid: str, image_urls: list[str]
    ) -> list[Path]:
        """下载全部图片到缓存目录"""
        item_dir = get_ehentai_cache_path() / str(gid)
        item_dir.mkdir(parents=True, exist_ok=True)
        referer = f"{self._eh_host()}/"
        downloaded: list[Path] = []

        for i, url in enumerate(image_urls):
            try:
                ext = Path(urlparse(url).path).suffix.lstrip(".") or "jpg"
                if ext.lower() not in ("jpg", "jpeg", "png", "gif", "webp"):
                    ext = "jpg"
                fname = f"{i+1:03d}.{ext}"
                local = item_dir / fname
                if local.exists():
                    downloaded.append(local)
                    continue
                resp = session.get(
                    url, headers={"Referer": referer}, timeout=30
                )
                if resp.status_code == 200 and resp.content:
                    local.write_bytes(resp.content)
                    downloaded.append(local)
                    logger.info(f"✅ [{i+1}/{len(image_urls)}] 下载完成: {fname}")
                else:
                    logger.warning(
                        f"⏭️ [{i+1}/{len(image_urls)}] 下载失败 HTTP {resp.status_code}"
                    )
            except Exception as exc:
                logger.error(f"❌ 下载失败 [{i+1}]: {url}: {exc}")
            time.sleep(self._IMAGE_DELAY)

        return downloaded

    # ---------- 主流程 ----------
    async def _download_ehentai(
        self, event: AstrMessageEvent, input_str: str
    ) -> AsyncGenerator:
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

        # 1. 解析 gid/token
        try:
            gid, token = self.resolve_gallery_from_input(input_str)
        except ValueError as exc:
            self._release_rate_limit(event)
            for result in self._yield_error(event, str(exc)):
                yield result
            return

        session = self._get_session()

        # 2. 解析 token（纯数字输入时需要搜索）
        if not token:
            yield event.plain_result(
                f"🔍 正在解析画廊 {gid} 的访问令牌，请稍候..."
            )
            try:
                token = await asyncio.to_thread(
                    self._resolve_token_by_search, session, gid
                )
            except Exception as exc:
                self._release_rate_limit(event)
                for result in self._yield_error(event, str(exc)):
                    yield result
                return

        # 3. 获取元信息
        try:
            info = await asyncio.to_thread(
                self._fetch_gallery_meta, session, gid, token
            )
        except Exception as exc:
            self._release_rate_limit(event)
            logger.error(f"获取 E站画廊信息失败: {exc}\n{traceback.format_exc()}")
            for result in self._yield_error(
                event, f"获取 E站画廊信息失败\n原因: {str(exc)[:200]}"
            ):
                yield result
            return

        title = info["title"]
        page_count = info["page_count"]
        author = info["author"]
        tags = info["tags"]
        category = info["category"]
        rating = info["rating"]

        if page_count <= 0:
            page_count = 0  # 未获取到页数时，走逐页兜底（见第6步）

        # 4. 缓存检查
        images_cached = self._check_cached_images_exist("ehentai", gid)
        cached_pdf, cached_zip = self._check_cached_outputs("ehentai", gid, title)

        need_zip = send_zip and cached_zip is None
        need_pdf = send_pdf and cached_pdf is None
        if send_files:
            output_cache_hit = not need_zip and not need_pdf
        else:
            # 仅预览模式：只要图片已缓存即可直接发预览，无需输出文件
            output_cache_hit = True

        all_image_paths: list[Path] = []
        if images_cached:
            all_image_paths = self._collect_all_cached_images("ehentai", gid)

        # 5. 完全缓存命中
        if images_cached and output_cache_hit and all_image_paths:
            logger.info(f"📦 E站缓存完全命中: gid={gid}, 跳过下载和生成")
            preview_paths = self._limit_preview(all_image_paths, event)
            yield event.plain_result(f"📥 开始获取「{title}」，请稍候...")
            nodes = self._build_merge_preview_nodes(
                event, "ehentai", gid, title, author, tags,
                [f"共{len(all_image_paths)}张图"], preview_paths,
            )
            yield event.chain_result([nodes])
            await asyncio.sleep(1)

            summary = (
                f"✅ 获取完成: {title}\n"
                f"作者: {author} | 图片: {len(all_image_paths)}张 | "
                f"耗时: {time.perf_counter() - process_start:.1f}s"
            )
            async for result in self._send_cached_outputs(
                event, "ehentai", gid, title, output_mode, summary
            ):
                yield result
            self._finalize_cache_cleanup()
            return

        # 6. 图片已缓存但需生成输出文件
        if images_cached and (need_zip or need_pdf) and all_image_paths:
            logger.info(f"📦 E站图片缓存存在，仅需生成输出: gid={gid}")
            preview_paths = self._limit_preview(all_image_paths, event)
            yield event.plain_result(f"📥 开始获取「{title}」，请稍候...")
            nodes = self._build_merge_preview_nodes(
                event, "ehentai", gid, title, author, tags,
                [f"共{len(all_image_paths)}张图"], preview_paths,
            )
            yield event.chain_result([nodes])
            await asyncio.sleep(1)

            summary = (
                f"✅ 获取完成: {title}\n"
                f"作者: {author} | 图片: {len(all_image_paths)}张 | "
                f"耗时: {time.perf_counter() - process_start:.1f}s"
            )
            async for result in self._generate_and_send_outputs(
                event, "ehentai", gid, title, all_image_paths,
                output_mode, summary,
            ):
                yield result
            self._finalize_cache_cleanup()
            return

        # 7. 全新下载
        extra = f" | 分类: {category}" if category else ""
        if rating:
            extra += f" | 评分: {rating}"
        yield event.plain_result(
            f"📥 开始获取「{title}」{extra}，请稍候...\n"
            f"⚠️ E站需逐页解析，张数较多时耗时较长，请耐心等待"
        )

        try:
            async with self._eh_lock():
                # 7.1 逐页解析图片URL（页数未知时先解析首页兜底）
                if page_count <= 0:
                    logger.info(f"E站未获取到页数，尝试解析首页...")
                    resp = await asyncio.to_thread(
                        session.get, f"{self._eh_host()}/g/{gid}/{token}/", timeout=20
                    )
                    if resp and resp.text:
                        page_count = 0
                        gsc = BeautifulSoup(resp.text, "lxml").find("span", id="gsc")
                        if gsc:
                            try:
                                page_count = int(gsc.get_text(strip=True))
                            except ValueError:
                                page_count = 0

                image_urls = await asyncio.to_thread(
                    self._fetch_image_urls, session, gid, token, page_count
                )
                if not image_urls:
                    self._release_rate_limit(event)
                    for result in self._yield_error(
                        event, f"画廊 {gid} 未解析到任何图片URL"
                    ):
                        yield result
                    return

                # 7.2 下载图片
                downloaded_paths = await asyncio.to_thread(
                    self._download_images, session, gid, image_urls
                )
        except asyncio.CancelledError:
            logger.info(f"♻️ E站下载任务被中断: gid={gid}")
            raise
        except Exception as exc:
            self._release_rate_limit(event)
            logger.error(f"❌ E站下载任务异常: {exc}\n{traceback.format_exc()}")
            for result in self._yield_error(
                event, f"E站下载异常: {str(exc)[:200]}"
            ):
                yield result
            return

        if not downloaded_paths:
            self._release_rate_limit(event)
            for result in self._yield_error(event, f"画廊 {gid} 未成功下载任何图片"):
                yield result
            return

        all_image_paths = sorted(downloaded_paths)
        logger.info(
            f"🖼️ 共获取 {len(all_image_paths)} 张图片，准备发送合并转发"
        )

        preview_paths = self._limit_preview(all_image_paths, event)
        nodes = self._build_merge_preview_nodes(
            event, "ehentai", gid, title, author, tags,
            [f"共{len(all_image_paths)}张图"], preview_paths,
        )
        yield event.chain_result([nodes])
        await asyncio.sleep(1)

        total_time = time.perf_counter() - process_start
        summary = (
            f"✅ 获取完成: {title}\n"
            f"作者: {author} | 图片: {len(all_image_paths)}张 | 耗时: {total_time:.1f}s"
        )
        if send_files:
            async for result in self._generate_and_send_outputs(
                event, "ehentai", gid, title, all_image_paths,
                output_mode, summary,
            ):
                yield result
        else:
            yield event.plain_result(summary)

        self._log_download_complete("E-Hentai", gid, title, len(all_image_paths), total_time)
        self._finalize_cache_cleanup()
