# astrbot_plugin_epiddownloader

AstrBot 的 **P站（Pixiv）** 与 **E站（E-Hentai）** 资源下载插件。

- **`.pid <作品ID>`** → 下载 Pixiv 插画（含多图集），**默认逐张直发高清原图**
- **`.eid <画廊ID 或 完整URL>`** → 下载 E-Hentai 画廊，默认合并转发预览 + ZIP 压缩包
- 支持 PDF 输出、缓存、群限速、白名单

> ⚠️ **重要警告**
>
> - 本插件会**不可避免地使 Bot 发送 R18 内容**，请严格遵守相关法律法规及平台使用条款
> - 目前仅支持**单线程下载**，大群使用请**慎重**，建议善用**白名单功能**限制可用群组
> - Bot 账号因使用本插件被封禁，**与插件开发者无关**，使用者需自行承担风险

## 特性

- **`.pid <ID>`** 下载 Pixiv 作品，多图作品逐张获取，**默认逐张直发高清原图（不打包）**
- **`.eid <ID或URL>`** 下载 E-Hentai 画廊（裸 gid 自动搜索解析 token，完整 URL 最可靠）
- **P站直发原图**：下载后逐张把高清原图直接发送到聊天，不生成 PDF/ZIP
- **合并转发预览**：作品信息 + 前 N 张预览图（N 可配置，E站默认使用）
- **输出格式**：原图直发 / 仅预览 / ZIP（可选加密）/ PDF / 两者
- **缓存机制**：源图片、ZIP、PDF 均缓存，重复请求直接发送缓存
- **群组限速**：同一群默认 60 秒间隔，可配置
- **自动清理**：按保留天数 + 总大小上限自动清理过期缓存
- **手动清理**：`.pid del-cache`

## 指令列表

| 指令 | 说明 |
|------|------|
| `/pid 59580629` | 下载 Pixiv 作品 59580629 |
| `/pixiv 59580629` | 同上（别名） |
| `/eid 123456` | 下载 E-Hentai 画廊（需站点搜索解析 token） |
| `/eid https://e-hentai.org/g/123456/abcdef/` | 下载 E-Hentai 画廊（完整URL，最可靠） |
| `/ehentai ...` | 同上（别名） |
| `/ephelp` | 显示帮助 |
| `/pid del-cache` | 手动清除所有缓存 |
| `.` 前缀同样兼容 | — |

## 安装

### 依赖

```
python >= 3.10
```

### 安装插件

1. 在 AstrBot WebUI → 插件管理 → 安装插件：
   - **上传 zip 包**：选择打包好的 zip 文件上传
   - **从链接安装**：输入仓库地址
2. 重启 AstrBot 或重新加载插件

> **打包注意事项**：压缩包内文件应位于**根目录**（`main.py`、`metadata.yaml` 等在 zip 根目录下，而非外层文件夹）。

### 依赖安装（v1.03 起加载时自动校验）

v1.03 起，插件在**加载阶段**就导入 `pixivpy3`、`curl_cffi`、`bs4` 等依赖：

- 依赖齐全 → 插件正常加载
- 依赖缺失 → 插件**直接加载失败**，日志给出明确提示，同时可触发 AstrBot 的依赖自动安装；不会再出现"插件看起来正常、发 `/pid` 时才报 `No module named 'pixivpy3'`"

安装插件（上传 zip / 从链接安装）时，较新版本的 AstrBot 会自动执行 `requirements.txt`。若仍然提示缺包（常见于**手动拷贝插件目录**、**Docker 升级镜像后 pip 包丢失**），请在 AstrBot 运行环境执行：

```bash
# Docker 部署
docker exec -it <容器名> python -m pip install -r /AstrBot/data/plugins/astrbot_plugin_epiddownloader/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 源码 / venv 部署
pip install -r data/plugins/astrbot_plugin_epiddownloader/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

也可以在 AstrBot WebUI → 左侧 **平台日志** → 右上角 **安装pip库**，逐行输入以下内容并执行：

```
pixivpy3>=3.7.0
curl_cffi>=0.7.0
beautifulsoup4>=4.12.0
lxml>=4.9.0
Pillow>=10.0.0
reportlab>=4.0.0
pyzipper>=0.3.6
cryptography
```

> **Docker/Linux 部署**：以上库均有 manylinux 预编译轮子，`pip install` 即可，无需编译。若 AstrBot 运行在 Docker 容器内，请确认容器 Python 版本 ≥ 3.10。**每次 `docker compose pull` 升级镜像后建议重新安装一次依赖**（镜像层会覆盖 pip 包）。

### 打包

```bash
cd astrbot_plugin_epiddownloader
zip -r ../astrbot_plugin_epiddownloader.zip . -x "*.git/*" "__pycache__/*" "*.pyc" "data/*"
```

## Pixiv 配置（必填）

> ⚠️ Pixiv 官方 API 现在**强制要求登录**，即使公开作品也需要。不配置登录信息则无法使用 `.pid`。

在 AstrBot WebUI 插件配置的 `pixiv_*` 项中二选一（v1.03 起 `pixiv_login_method` 真正生效：按所选方式优先登录，未填写对应凭据时自动回退到另一种方式）：

**方式一：账号密码（简单，可能不稳定）**
- `pixiv_login_method` 选择 `密码`
- 填写 `pixiv_username`（邮箱/用户名）和 `pixiv_password`
- 部分账号可能触发 Pixiv 验证码导致登录失败，此时请改用方式二

**方式二：refresh_token（推荐，稳定，可访问 R18）**
- `pixiv_login_method` 选择 `refresh_token`
- 填写 `pixiv_refresh_token`
- 获取方式：使用浏览器抓包 Pixiv 登录返回的 `refresh_token`，或使用第三方工具（如 pixiv-token 相关开源项目）生成

> 更换 `pixiv_refresh_token` 后**无需重启 AstrBot**：v1.03 起插件检测到登录配置变化会自动重建登录会话。

**其他建议：**
- 中国大陆网络需配置 `proxy`（如 `http://127.0.0.1:7890`），确保能访问 Pixiv API
- R18 等需要账号权限的内容，需使用**已通过成人认证**的账号登录

**发送模式（v1.01 起）：**
- `pixiv_send_mode` 默认 **`原图直发`**：下载后把高清原图直接发送到聊天，**不生成 PDF/ZIP**
- **防刷屏（v1.02 起）**：原图直发时，图片张数超过 `pixiv_direct_merge_threshold`（默认 **3 张**）即改用**合并转发**批量发送；图片较少则逐张直发
- 如需打包可在 `pixiv_send_mode` 切换为 `压缩包`/`PDF`/`两者`

## E-Hentai 配置（可选但强烈建议）

- **裸 gid 解析**：`.eid 123456` 需要站点搜索来解析访问令牌，搜索结果可能不包含该画廊（已删除/未收录），此时请提供**完整画廊URL**：
  ```
  /eid https://e-hentai.org/g/123456/abcdef/
  ```
- **Cookie 配置**：在 `ehentai_cookies` 填入登录 e-hentai 后的 Cookie 字符串，如 `ipb_member_id=xxx; ipb_pass_hash=yyy`：
  - 能显著降低被 Cloudflare 拦截的概率
  - 可获得更高分辨率的原图
- **ExHentai**：若已配置有效 Cookie，可开启 `ehentai_use_exhentai` 走 exhentai.org 域名（需登录权限）
- 无 Cookie 时图片分辨率可能受限，属站点正常限制

## 配置项

在 AstrBot WebUI 插件配置中可修改以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `output_mode` | 下拉 | 压缩包 | E站发送模式：仅预览 / 压缩包 / PDF / 两者 |
| `pixiv_send_mode` | 下拉 | 原图直发 | P站发送模式：原图直发 / 压缩包 / PDF / 两者 / 仅预览 |
| `pixiv_direct_merge_threshold` | 整数 | 3 | 原图直发时图片超过此张数改用合并转发（避免刷屏），设很大值则始终逐张直发 |
| `zip_password` | 字符串 | (空) | ZIP 加密密码，留空则不加密 |
| `preview_images_group` | 整数 | 5 | 群聊合并转发预览图片数 |
| `preview_images_private` | 整数 | 50 | 私聊合并转发预览图片数 |
| `proxy` | 字符串 | (空) | HTTP 代理地址 |
| `pixiv_login_method` | 下拉 | refresh_token | Pixiv 登录方式：refresh_token / 密码（按所选方式优先，另一种方式兜底） |
| `pixiv_username` | 字符串 | (空) | Pixiv 账号 |
| `pixiv_password` | 字符串 | (空) | Pixiv 密码 |
| `pixiv_refresh_token` | 字符串 | (空) | Pixiv refresh_token（推荐） |
| `ehentai_cookies` | 字符串 | (空) | E-Hentai Cookie 字符串 |
| `ehentai_use_exhentai` | 布尔 | false | 使用 exhentai.org 域名 |
| `cache_retention_days` | 整数 | 3 | 缓存保留天数 |
| `cache_max_size_gb` | 浮点数 | 3.0 | 缓存总大小上限 (GB) |
| `rate_limit_seconds` | 整数 | 60 | 每群限速间隔 (秒) |
| `merge_send_as_sender` | 布尔 | false | 合并转发显示为发送者 |
| `whitelist_enabled` | 布尔 | true | 开启群白名单 |
| `whitelist_groups` | 字符串 | (空) | 白名单群号，逗号分隔 |
| `admin_qq` | 字符串 | (空) | 管理员QQ |
| `allow_private_chat` | 布尔 | true | 允许私聊下载 |
| `file_merge_forward_enabled` | 布尔 | true | 群聊文件合并转发 |
| `error_notify_mode` | 下拉 | 通知 | 错误通知模式：静默 / 通知 |

## 工作流程

1. 用户发送 `.pid <ID>` 或 `.eid <ID/URL>`
2. 群组限速检查 → 白名单/私聊检查 → 通过后获取作品元信息
3. 缓存检查（命中则直接发送）
4. 否则逐张下载图片 → 合并转发预览 → 生成 ZIP/PDF → 发送
5. 缓存自动清理检查

## 项目结构

```
astrbot_plugin_epiddownloader/
├── main.py                  # 插件入口，指令监听
├── metadata.yaml            # 插件元信息
├── requirements.txt         # Python 依赖
├── _conf_schema.json        # WebUI 配置项
├── README.md
└── core/
    ├── __init__.py
    ├── paths.py             # 缓存路径管理
    ├── rate_limiter.py      # 群级限速
    ├── cache.py             # 缓存清理策略
    ├── tools.py             # ZIP/PDF 生成
    ├── base_handler.py      # 通用下载流程
    ├── pixiv_handler.py     # Pixiv 下载逻辑
    └── ehentai_handler.py   # E-Hentai 下载逻辑
```

## 依赖库

- [pixivpy3](https://github.com/upbit/pixivpy) — Pixiv API
- [curl_cffi](https://github.com/lexiforest/curl_cffi) — E-Hentai 请求（TLS 指纹伪装）
- [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) / [lxml](https://lxml.de/) — HTML 解析
- [Pillow](https://python-pillow.org/) — 图片格式转换
- [reportlab](https://www.reportlab.com/) — PDF 生成
- [pyzipper](https://github.com/danifus/pyzipper) — ZIP 加密

## 常见问题

**P站登录失败？**
- 确认账号密码正确，或改用 refresh_token
- 部分账号有验证码/地区限制，建议使用已过成人认证的日本区域账号
- 服务器无法访问 Pixiv、代理不可用也会表现为登录失败，请检查 `proxy` 配置

**提示 `No module named 'pixivpy3'` / `缺少 Python 依赖`？**
- 这不是 token 或账号的问题，是 AstrBot 运行环境缺少 Python 依赖
- 按上文「依赖安装」在 AstrBot 所在环境执行 `pip install -r requirements.txt`，再重载插件或重启 AstrBot

**E站返回空响应/被拦截？**
- 配置 `ehentai_cookies`（登录后从浏览器复制 Cookie）
- 使用完整画廊 URL 而非裸 gid

**合并转发预览发送失败？**
- 预览图片过多导致，降低 `preview_images_group`（建议 ≤ 20）

## 版本历史

### v1.03
- **依赖改为加载时导入**：`pixivpy3`、`curl_cffi`、`bs4`、`PIL` 等不再懒加载，缺包时插件加载即失败并给出明确提示，也可触发 AstrBot 的插件依赖自动安装；不会再出现"插件正常但 `/pid` 报 `No module named 'pixivpy3'`"
- **错误提示区分类型**：缺依赖时明确提示安装 `requirements.txt`；登录失败时补充"代理/网络不可达也会登录失败"的说明，不再把所有异常都归咎于账号密码
- **修复 `pixiv_login_method` 实际不生效**：现在按所选登录方式优先认证，另一种方式作为兜底，老配置不受影响（默认值改为 `refresh_token`）
- **更换 refresh_token 无需重启**：登录配置变化时自动重建 API 会话
- **修复 `metadata.yaml` 的 `repo` 指向了别人的仓库**，改为本插件仓库地址
- `pyzipper` 改为加载时探测：缺失时仅退化为无密码 ZIP，不影响插件加载

### v1.02
- 原图直发新增**合并转发阈值**：图片超过 `pixiv_direct_merge_threshold`（默认3张）时改用合并转发批量发送，避免逐张刷屏；阈值可在 WebUI 配置
- 合并转发按每批最多 50 个节点分批发送，超长图集不会超限

### v1.01
- P站 `.pid` 默认改为**原图直发**：下载后逐张发送高清原图到聊天，不再打包 PDF/ZIP
- 新增 `pixiv_send_mode` 配置项（默认「原图直发」，可切换回 压缩包/PDF/两者/仅预览）
- 新增「原图直发」发送模式：逐张发送，带 0.5s 间隔避免触发频率限制

### v1.0.0
- 初始版本：`.pid` 下载 Pixiv 插画、`.eid` 下载 E-Hentai 画廊，合并转发预览 + 压缩包/PDF

## 许可证

MIT

## 作者

ncy
