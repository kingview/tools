# PostDrop / social.download_media

项目的第一个只读 Tool，同时提供两个入口：

1. Agent 通过 Python 调用 `social.download_media`。
2. 用户在 macOS 或 Windows 上运行 PostDrop 桌面 App，粘贴公开社媒帖子地址并点击下载。

两个入口共用 `SocialMediaDownloadTool`、Pydantic 契约、`yt-dlp` Backend、安全策略、限流和审计逻辑，不存在两套下载实现。

## 桌面 App

桌面端使用 Qt for Python（PySide6），不是 Web 页面，不需要浏览器或本地 HTTP 服务。主要功能：

- 粘贴或拖入公开社媒帖子 HTTPS 地址。
- 支持直接粘贴抖音/小红书 App 生成的整段中文分享文案，自动提取其中短链。
- 自动兼容抖音“精选”页面的 `modal_id` 链接并转换为标准作品地址。
- 抖音遇到站点校验时，可在用户允许后读取一次本机浏览器会话；成功后只缓存抖音的匿名站点 Cookie，后续下载优先复用缓存。
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

## 安全边界

- 仅允许配置白名单中的公开 HTTPS 域名。
- 调用前检查域名及 DNS 解析，拒绝内网/本机地址，降低 SSRF 风险。
- 默认不展开播放列表，并限制条目数、单文件大小、总下载大小、媒体时长、超时和重试。
- 下载目录由 Tool Executor 配置，Agent 和桌面输入框都不能指定任意文件路径。
- 输入、输出、Tool 版本和错误码写入审计事件。
- 不接收 Agent 传入的 Cookie、账号密码、验证码或代理。抖音需要站点校验时，可选择读取本机浏览器已有的抖音 Cookie；缓存会排除账号登录 Cookie，其他域名 Cookie 也不会缓存。Cookie 不会写入 Tool 输出、日志或审计事件。
- 不使用社媒平台官方开放 API，也不需要 API Key。
- 只应下载你有权访问和保存的内容，并遵守目标平台规则及适用法律。

`yt-dlp` 支持的平台随 extractor 版本变化。本包锁定已测试版本，具体以其[支持站点列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)为准。

## Tool 契约

- 名称：`social.download_media`
- 版本：`1.5.0`
- 类型：`read`
- 输入：`DownloadInput.model_json_schema()`
- 输出：`DownloadOutput.model_json_schema()`
- Dry Run：`mode="metadata_only"`
