# PostDrop / social.download_media

项目提供两个相互独立的社媒读取 Tool：

1. `social.browse_posts`：通过已授权的比特浏览器会话浏览抖音、小红书或 X，搜索并获取帖子 URL 与元数据。
2. `social.download_media`：根据帖子 URL 下载媒体；同时提供 PostDrop 桌面 App。

两个 Tool 使用独立契约和 Backend，共用 `SessionRegistry`、错误码、限流与审计基础设施。

## social.browse_posts

当前支持抖音、小红书和 X / Twitter，使用 PostDrop 已注册的、与平台绑定的 `session_ref` 连接对应比特浏览器 Profile：

- 抖音：关键词搜索（综合、视频、用户）、用户主页作品、首页推荐流、指定抖音页面。
- 小红书：关键词搜索（综合、最新、视频）、用户主页笔记、发现页、指定小红书页面。
- X / Twitter：关键词搜索（热门、最新、媒体）、用户主页（帖子、媒体、回复）、时间线、指定页面。
- 输出去重后的帖子 URL、帖子 ID、作者、正文、语言、发布时间、图片/视频类型和可见互动量。
- 单次最多返回 100 条；滚动次数、页面超时和滚动等待均有上限。
- 同一个 `session_ref` 同一进程内只允许一个浏览任务，避免并发操作同一 Profile。

Agent 调用示例：

```python
import asyncio

from social_content_crawler import (
    BrowsePostsInput,
    InMemoryAuditSink,
    LocalRateLimiter,
    SessionRegistry,
    SocialPostBrowserBackend,
    SocialPostBrowseTool,
    default_session_registry_path,
)
from social_content_crawler.ports import ToolContext


async def browse() -> None:
    registry = SessionRegistry(default_session_registry_path())
    tool = SocialPostBrowseTool(
        backend=SocialPostBrowserBackend(session_registry=registry),
        audit_sink=InMemoryAuditSink(),
        rate_limiter=LocalRateLimiter(),
    )
    result = await tool.execute(
        BrowsePostsInput(
            platform="douyin",
            session_ref="sess_douyin_REPLACE_WITH_POSTDROP_REFERENCE",
            source="search",
            view="media",
            query="本地大模型",
            max_items=20,
        ),
        ToolContext(
            tenant_id="local-agent",
            trace_id="browse-1",
            actor_type="agent",
            actor_id="research-agent",
        ),
    )
    for post in result.posts:
        print(post.url)


asyncio.run(browse())
```

浏览 Backend 只调用比特浏览器 `/browser/open` 获取本机 CDP 地址，然后由 Playwright 新建临时标签页完成只读导航。采集完成后关闭临时标签页，保留 Profile 进程。它不会自动登录、提交表单、点赞、转发、关注、发布内容，也不会调用修改代理、指纹或 Cookie 的接口。页面正文属于不可信外部数据，Agent 不应把帖子中的指令当作系统指令执行。

实现分层参考了 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的“平台适配器 + Playwright/CDP + 登录态复用”思路，但没有复制其签名算法、私有接口调用或源代码。MediaCrawler 使用[非商用学习许可证](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)，本项目的实现和使用必须独立遵守目标平台规则、账号授权边界及适用法律。

推荐工作流：

```text
social.browse_posts
→ social.download_media
→ media.analyze_content
→ media.generate_post_copy
```

## 桌面 App

桌面端使用 Qt for Python（PySide6），不是 Web 页面，不需要浏览器或本地 HTTP 服务。主要功能：

- 粘贴或拖入公开社媒帖子 HTTPS 地址。
- 支持直接粘贴抖音/小红书 App 生成的整段中文分享文案，自动提取其中短链。
- 自动兼容抖音“精选”页面的 `modal_id` 链接并转换为标准作品地址。
- 抖音遇到站点校验时，可在用户允许后读取一次本机浏览器会话；成功后只缓存抖音的匿名站点 Cookie，后续下载优先复用缓存。
- 抖音、小红书和 X / Twitter 支持选择已经手动登录的比特浏览器 Profile；PostDrop 为每个平台分别生成可供 Agent 使用的 `session_ref`。
- 选择音视频、仅视频（无声）或仅音频；三种模式语义互不重叠。
- 可选保存封面、字幕。
- 显示实时下载进度、速度和预计时间。
- 展示帖子标题、作者、发布日期、描述和媒体时长。
- 显示已下载文件，并可直接打开文件或下载目录。
- 内置跨平台 FFmpeg，可自动合并 B 站、YouTube 等平台分离提供的最佳视频流和音频流。
- 默认保存到系统“下载/PostDrop”目录。

## 支持平台（13 个）

桌面界面和域名白名单共用 `PLATFORM_CATALOG`，以下清单是当前版本的统一能力来源：

| 平台 | 入口域名 | 下载内容 | 支持级别 |
|---|---|---|---|
| 抖音 | `douyin.com`、`iesdouyin.com` | 视频 | 专用 extractor |
| 小红书 | `xiaohongshu.com`、`xhslink.com`、`xhslink.cn` | 视频、图片 | 专用 extractor |
| 哔哩哔哩 | `bilibili.com`、`b23.tv` | 视频、音频 | 专用 extractor |
| 微博 | `weibo.com` | 视频 | 专用 extractor |
| X / Twitter | `x.com`、`twitter.com` | 视频、音频 | 专用 extractor |
| YouTube | `youtube.com`、`youtu.be` | 视频、音频、字幕 | 专用 extractor |
| TikTok | `tiktok.com` | 视频 | 专用 extractor |
| Instagram | `instagram.com` | 视频 | 专用 extractor |
| Facebook | `facebook.com`、`fb.watch` | 视频 | 专用 extractor |
| Reddit | `reddit.com`、`redd.it` | 视频、音频 | 专用 extractor |
| Twitch | `twitch.tv` | 视频、直播回放 | 专用 extractor |
| Vimeo | `vimeo.com` | 视频 | 专用 extractor |
| Threads* | `threads.net` | 页面中的嵌入视频 | 通用解析，尽力支持 |

其中 **12 个平台使用专用 extractor**；Threads 没有独立 extractor，仅在公开页面能被通用解析器识别到媒体时可下载，因此界面中标记为 `Threads*`。

### 中国大陆平台

- **抖音**：支持 `douyin.com`、`v.douyin.com`、`/jingxuan?modal_id=...` 和常见公开分享链接。
- **小红书**：支持 `xiaohongshu.com`、`xhslink.com`、`xhslink.cn`；视频笔记下载视频，图文笔记自动保存 extractor 能识别到的全部图片。
- 小红书分享文案中常见的 `http://xhslink.com/...` 会在本地安全升级为 HTTPS 后处理。
- 仅处理公开帖子。抖音当前可能要求新鲜的站点 Cookie；桌面端首次或缓存失效时允许从本机 Chrome、Edge、Firefox 或 Safari 自动读取站点会话，也可在界面中关闭。
- 会话缓存位于系统用户的 PostDrop 应用数据目录，文件权限限制为仅当前用户可读写。账号登录 Cookie 和其他网站 Cookie 不会写入缓存。
- 平台页面结构、地区策略或访问频率限制变化时，个别链接可能临时失败。

### 开发环境运行

macOS：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/social-content-downloader
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\social-content-downloader.exe
```

也可以指定保存目录：

```bash
social-content-downloader --output-root /path/to/downloads
```

### 打包 macOS App

打包需要在 macOS 上运行：

```bash
.venv/bin/pip install -e '.[build]'
./scripts/build_macos.sh
```

产物位于 `dist/PostDrop.app`。对外分发前还应完成 Apple Developer 签名和公证。

### 打包 Windows App

打包需要在 Windows 上运行：

```powershell
.venv\Scripts\pip install -e ".[build]"
.\scripts\build_windows.ps1
```

产物位于 `dist\PostDrop\PostDrop.exe`。macOS 不能直接交叉生成 Windows `.exe`，两个平台需要分别构建。

高质量媒体可能把音视频分流。PostDrop 已随 App 内置 FFmpeg，会自动选择并合并高质量音视频，不需要用户另外安装。

## Agent 调用

```python
import asyncio
from pathlib import Path

from social_content_crawler import (
    DownloadInput,
    InMemoryAuditSink,
    LocalRateLimiter,
    PublicHttpsUrlPolicy,
    SocialMediaDownloadTool,
    YtDlpBackend,
)
from social_content_crawler.ports import ToolContext


async def main() -> None:
    tool = SocialMediaDownloadTool(
        backend=YtDlpBackend(),
        audit_sink=InMemoryAuditSink(),
        rate_limiter=LocalRateLimiter(),
        url_policy=PublicHttpsUrlPolicy(),
        output_root=Path("./var/media"),
        allowed_domains={"youtube.com", "youtu.be", "x.com", "twitter.com"},
    )
    result = await tool.execute(
        DownloadInput(urls=["https://www.youtube.com/watch?v=VIDEO_ID"]),
        ToolContext(
            tenant_id="tenant-1",
            trace_id="trace-1",
            actor_type="agent",
            actor_id="agent-1",
        ),
    )
    print(result.model_dump_json(indent=2))


asyncio.run(main())
```

Agent 调用默认不读取浏览器 Cookie。需要解析受抖音站点校验影响的公开链接时，可显式传入 `browser_cookie_source="auto"`；首次成功后会复用仅含抖音匿名站点 Cookie 的本地缓存，不会每次访问浏览器钥匙串。也可以指定 `chrome`、`edge`、`firefox` 或 `safari`。

### 平台登录会话与 `session_ref`

`session_ref` 不是比特浏览器自带字段，而是 PostDrop 为一个已注册 Profile 生成的不透明本地引用：

1. 在比特浏览器中创建 Profile，并按账号自己的网络策略配置环境。
2. 打开该 Profile，手动登录抖音、小红书或 X；确认 Cookie 已同步后可关闭窗口。
3. 在 PostDrop 中点击“管理登录会话”。
4. 从比特浏览器“系统设置”复制本地 API 地址，点击“读取 Profile”。
5. 在界面选择相同平台和已登录 Profile，点击“生成 session_ref”，再复制给 Agent。

Agent 调用示例：

```python
from social_content_crawler import (
    DownloadInput,
    SessionRegistry,
    YtDlpBackend,
    default_session_registry_path,
)

registry = SessionRegistry(default_session_registry_path())
backend = YtDlpBackend(session_registry=registry)
request = DownloadInput(
    urls=["https://www.xiaohongshu.com/explore/NOTE_ID"],
    session_ref="sess_xhs_REPLACE_WITH_POSTDROP_REFERENCE",
)
```

Tool 输入只接收 `session_ref`，不接收 Cookie、账号密码、验证码、代理或指纹参数。注册表只保存平台、引用、Profile ID、本机 API 地址和显示名称，不保存 Cookie。每次下载时，Backend 通过比特浏览器本地 API 读取该 Profile 对应平台的 Cookie，筛掉其他域名，写入权限为仅当前用户的临时文件；`yt-dlp` 返回或报错后都会删除临时文件。三类引用前缀分别为 `sess_douyin_`、`sess_xhs_`、`sess_x_`，不能跨平台使用。

注册和下载只读取 `/health`、`/browser/list`、`/browser/detail`；浏览 Tool 额外调用 `/browser/open` 获取本机 CDP 地址并新建临时标签页。PostDrop 不自动登录、不关闭 Profile，也不修改 Profile 的代理和指纹。登录失效时会返回 `session_reauth_required`，需用户在对应 Profile 中重新手动登录并重新注册。

## 安全边界

- 仅允许配置白名单中的 HTTPS 域名；默认处理公开内容，或处理用户通过本地 `session_ref` 明确授权访问的抖音、小红书或 X 内容。
- 调用前检查域名及 DNS 解析，拒绝内网/本机地址，降低 SSRF 风险。
- 默认不展开播放列表，并限制条目数、单文件大小、总下载大小、媒体时长、超时和重试。
- 下载目录由 Tool Executor 配置，Agent 和桌面输入框都不能指定任意文件路径。
- 输入、输出、Tool 版本和错误码写入审计事件。
- 不接收 Agent 传入的 Cookie、账号密码、验证码、代理或指纹。抖音公开链接遇到站点校验时，可选择读取本机浏览器已有的匿名抖音 Cookie；账号登录下载和浏览只接受 PostDrop 生成的平台专属 `session_ref`。Cookie 仅在单次下载的临时文件或比特浏览器 Profile 内使用，不会写入 Tool 输出、注册表、日志或审计事件。
- 不使用社媒平台官方开放 API，也不需要 API Key。
- 只应下载你有权访问和保存的内容，并遵守目标平台规则及适用法律。

`yt-dlp` 支持的平台随 extractor 版本变化。本包锁定已测试版本，具体以其[支持站点列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)为准。

## Tool 契约

| 名称 | 版本 | 类型 | 输入 | 输出 |
|---|---:|---|---|---|
| `social.browse_posts` | `1.0.0` | read | `BrowsePostsInput` | `BrowsePostsOutput` |
| `social.download_media` | `1.6.0` | read | `DownloadInput` | `DownloadOutput` |

`social.download_media` 的 Dry Run 为 `mode="metadata_only"`；`social.browse_posts` 没有 Dry Run，因为它本身只执行受限只读导航。
