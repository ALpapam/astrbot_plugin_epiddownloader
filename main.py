import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star, register

from .core.ehentai_handler import EHentaiDownloadMixin
from .core.pixiv_handler import PixivDownloadMixin

_active_tasks: dict[str, bool] = {}

# 指令关键词集合（用于从消息体剥离指令名）
_COMMAND_NAMES = {"pid", "pixiv", "eid", "ehentai", "ep", "ephelp"}
_CLEAR_KEYWORDS = ("del-cache", "delcache", "clearcache", "clear")


@register(
    "astrbot_plugin_epiddownloader",
    "ncy",
    "P站/E站资源下载：.pid <作品ID> 下载Pixiv插画，.eid <画廊ID或URL> 下载E-Hentai画廊",
    "1.0.0",
)
class EpidDownloader(PixivDownloadMixin, EHentaiDownloadMixin, Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        self._refresh_config()
        logger.info("🖼️ P/E站下载插件已加载")

    # ---------- 通用指令解析+执行 ----------
    async def _parse_and_execute(
        self, event: AstrMessageEvent, source: str
    ):
        """source: "pixiv" 或 "ehentai" """
        user_id = str(event.get_sender_id())

        if _active_tasks.get(user_id, False):
            yield event.plain_result("⏳ 上一个请求还在处理中，请稍候...")
            return

        _active_tasks[user_id] = True

        try:
            # 访问控制（白名单 / 私聊开关）
            deny_message = self._check_access(event)
            if deny_message:
                yield event.plain_result(deny_message)
                return

            body = (event.message_str or "").strip()
            parts = body.split()

            # 剥离指令名（兼容 .pid 123 / /pid 123 / pid 123 及装饰器已剥离的情况）
            if parts and parts[0].lstrip("./").lower() in _COMMAND_NAMES:
                parts = parts[1:]

            if not parts:
                yield event.plain_result(
                    f"❗ 格式: {'pid' if source == 'pixiv' else 'eid'} <ID>\n"
                    f"示例: {'pid 59580629' if source == 'pixiv' else 'eid https://e-hentai.org/g/123456/abcdef/'}"
                )
                return

            # 清理缓存
            if parts[0].lower() in _CLEAR_KEYWORDS:
                async for result in self._handle_clear_cache(event):
                    yield result
                return

            arg = parts[0]

            if source == "pixiv":
                if not arg.isdigit():
                    yield event.plain_result(
                        "❗ 格式: pid <作品ID>\n示例: pid 59580629"
                    )
                    return
                logger.info(f"📥 P站指令: pid={arg}")
                async for result in self._download_pixiv(event, arg):
                    yield result
            else:
                logger.info(f"📥 E站指令: {arg}")
                async for result in self._download_ehentai(event, arg):
                    yield result

        except Exception as exc:
            logger.error(f"❌ P/E站插件异常: {exc}\n{traceback.format_exc()}")
            yield event.plain_result(f"❌ 插件出错: {str(exc)[:200]}")
        finally:
            _active_tasks.pop(user_id, None)

    # ---------- 指令入口 ----------
    @filter.command("pid")
    async def on_pid_command(self, event: AstrMessageEvent):
        """.pid <作品ID> — 下载Pixiv插画（含多图集）"""
        async for r in self._parse_and_execute(event, "pixiv"):
            yield r

    @filter.command("pixiv")
    async def on_pixiv_command(self, event: AstrMessageEvent):
        """.pixiv <作品ID> — 下载Pixiv插画（别名）"""
        async for r in self._parse_and_execute(event, "pixiv"):
            yield r

    @filter.command("eid")
    async def on_eid_command(self, event: AstrMessageEvent):
        """.eid <画廊ID或完整URL> — 下载E-Hentai画廊"""
        async for r in self._parse_and_execute(event, "ehentai"):
            yield r

    @filter.command("ehentai")
    async def on_ehentai_command(self, event: AstrMessageEvent):
        """.ehentai <画廊ID或完整URL> — 下载E-Hentai画廊（别名）"""
        async for r in self._parse_and_execute(event, "ehentai"):
            yield r

    @filter.command("ephelp")
    async def on_ep_help(self, event: AstrMessageEvent):
        """.ephelp — 以合并转发形式显示插件使用帮助"""
        sender_uin = str(event.get_self_id())
        nodes = Nodes([])

        help_sections = [
            (
                "📥 下载P站作品",
                "主要使用 / 前缀（兼容 . 前缀）\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• /pid <作品ID>\n"
                "• 示例: /pid 59580629\n"
                "• 自动获取作品全部图片（含多图集）\n"
                "• 也支持 /pixiv 别名"
            ),
            (
                "📥 下载E站画廊",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• /eid <画廊ID 或 完整URL>\n"
                "• 示例: /eid 123456\n"
                "       /eid https://e-hentai.org/g/123456/abcdef/\n"
                "• 推荐提供完整URL（裸gid需站点搜索，可能失败）\n"
                "• 也支持 /ehentai 别名"
            ),
            (
                "📤 输出格式",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• 每次下载先发送合并转发预览\n"
                "• 预览后按配置发送 ZIP 压缩包 / PDF\n"
                "• 默认: 合并转发预览 + ZIP压缩包\n"
                "• 可在 WebUI 配置中切换发送模式\n"
                "• ZIP 可设置加密密码（默认不加密）"
            ),
            (
                "🔑 Pixiv 配置",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• 必须在 WebUI 配置登录信息\n"
                "• 方式一: 账号密码（可能触发验证码）\n"
                "• 方式二: refresh_token（推荐，更稳定）\n"
                "• 获取 token 方法见插件 README"
            ),
            (
                "🍪 E站配置",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• 被 Cloudflare 拦截时需配置 Cookie\n"
                "• 填写 ehentai_cookies 可获更高分辨率\n"
                "• 建议提供完整画廊URL（裸gid解析可能失败）"
            ),
            (
                "💾 缓存机制",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• 下载的源图片、ZIP、PDF 均缓存\n"
                "• 重复请求直接发送缓存\n"
                "• 自动清理: 保留天数 / 总大小上限\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "/pid del-cache 或 /eid del-cache 手动清空"
            ),
            (
                "🧪 其他指令",
                "━━━━━━━━━━━━━━━━━━━━\n"
                "• /ephelp — 显示本帮助\n"
                "• /pid del-cache — 清空缓存\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🐧 {}".format(
                    ("有需求请联系管理员：" + getattr(self, "ep_admin_qq", ""))
                    if getattr(self, "ep_admin_qq", "")
                    else "请联系本群管理员"
                )
            ),
        ]

        for title, content in help_sections:
            nodes.nodes.append(
                Node(
                    uin=sender_uin,
                    content=[Plain(f"【{title}】\n{content}")],
                )
            )

        yield event.chain_result([nodes])
