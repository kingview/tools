# PostDrop / social.download_media

项目提供四个相互独立的社媒 Tool：

1. `browser.operate`：在已授权的比特浏览器会话中观察页面、导航、点击、输入搜索词、按键、滚动和翻页。
2. `social.browse_posts`：通过已授权的比特浏览器会话浏览抖音、小红书、X 或 Telegram Web，搜索/遍历并获取帖子 URL 与元数据。
3. `social.download_media`：根据帖子 URL 下载媒体；同时提供 PostDrop 桌面 App。
4. `social.publish_x_post`：在用户确认 Agent 高风险计划后，通过已登录的比特浏览器发布一条 X 帖子。

四个 Tool 使用独立契约和 Backend，共用 `SessionRegistry`、错误码、限流与审计基础设施。

开发期异常日志：Agent 调用时写入 `SOCIAL_AGENT_LOG_DIR`（默认 Agent 输出目录的
`.social-agent-state/logs`）；独立 macOS GUI 默认 `~/Library/Logs/SocialAgent/`，
也可用 `SOCIAL_AGENT_STATE_ROOT` 指定状态目录。保存异常链、文件/行号/函数和可用 Trace ID；
X 发布超时、HTTP/GraphQL 拒绝也记录，但不改变发布行为、不自动重试。敏感凭据及 URL 查询参数脱敏，
不主动记录请求、Cookie 或局部变量。每进程 5 MiB + 3 备份，限制非活跃进程历史日志组，
文件仅当前用户读写。诊断模块及 MCP 适配器由 Agent 的 `scripts/sync_diagnostics.py` 在统一插件构建前同步，
随包分发，插件无需依赖 Agent Core。任务/步骤/调用 ID 通过每次 MCP 调用的元数据关联，
不作为业务参数或权限来源；每个错误只记录一次完整堆栈，上层使用同一个 `error_id` 关联。

## browser.operate

该 Tool 使用 `session_ref` 找到比特浏览器 Profile，通过官方 Local API 的
`/browser/open` 获取本机 CDP 地址，再由 Playwright 操作可见标签页。支持动作：

- `observe`：返回页面标题、正文摘要和最多 100 个可交互元素，元素以短期 `element_ref` 标识。
- `navigate`：打开公开、无凭据的 HTTPS 页面。
- `click`、`input`：可使用 `element_ref`、CSS selector、ARIA role/name 或精确文本定位。
- `press`、`scroll`：支持 Enter、PageUp/PageDown、方向键等受限按键和指定方向滚动。
- `swipe_up`、`swipe_down`：提供语义明确的上划、下划操作；幅度由 `scroll_y` 控制。
- `back`、`forward`、`reload`、`wait`：完成常见分页与页面状态等待。

推荐先 `observe`，再用返回的 `element_ref` 点击或输入。每个 `session_ref` 串行执行，
标签页和元素引用仅保存在当前进程内。Tool 拒绝密码和文件输入控件，并在点击前拦截
发布、点赞、评论、关注、交易、删除等外部写操作；它只用于搜索、浏览和翻页。

## social.publish_x_post

该 Tool 不使用 X 官方 API，而是用 X 专属 `session_ref` 打开已登录的比特浏览器
Profile，通过 Playwright 进入 `https://x.com/compose/post`，填写最终文案、可选上传最多
4 个图片或视频并提交。通用 `browser.operate` 仍然禁止发布按钮和文件输入，不能代替或
绕过该专用 Tool。

发布属于不可逆外部写操作，因此执行条件固定为：

- 用户文字明确要求发布到 X，并选择已登录的 X 会话。
- Social Agent 先展示动态计划，再由用户在独立高风险确认框中确认。
- 每次确认签发随机一次性令牌，核心 MCP 和插件各验证一次，并在浏览器操作前消费。
- 一次计划最多发布一条；成功、失败或 `unknown` 都不会自动重试。
- 发布媒体必须来自 Social Agent 输出目录，防止上传任意本地文件。

发布器只操作当前前景发帖弹框中的输入、上传和提交控件，不匹配背景时间线的同名控件。
填写文案后通过真实空格按键结束标签/提及输入，收起 X 可能残留的透明联想层；普通 `fill()`、
失焦不等价。不使用 Esc（可能触发保存草稿）、强制点击或删除遮罩，完成输入前后都校验文案
（仅忽略末尾空白）。发现富文本编辑器追加了旧草稿等不一致时停止发布。
弹框按规则分类处理（不是自动点击任意弹框的“确定”）：
- 功能介绍（含“可下载视频简介”）、Premium 推广、开启通知邀请、X 应用安装提示：识别标题后，
  仅点击该弹框内的“关闭 / 暂不 / 跳过 / No thanks”等拒绝或关闭按钮；功能介绍也允许“知道了”。
- 登录、验证码、账号异常、保存/丢弃草稿、付款、隐私设置，以及带输入/选择控件的弹框：停止发布，提示人工处理。
- 未知弹框、没有安全关闭按钮、连续关闭超过 3 个弹框：停止并保存诊断，不猜测操作。

初次打开、等待媒体上传或按钮就绪，以及点击前延迟出现的弹框均会检查。保留文案与附件；
不关闭发帖框、不强行越过遮罩、不重复点击发布。规则在 `x_dialog_rules.py`，浏览器执行在
`x_dialogs.py`，诊断输出在 `x_dialog_diagnostics.py`，可独立扩展及测试。
需要人工处理时写入 `x_publish.dialog_requires_attention` 日志；关闭失败记录
`x_publish.dismiss_information_dialog`。诊断保存在当前日志目录的 `x-dialogs/<时间-随机ID>/`，
包含脱敏、限长的 `dom.json` 和仅截取弹框区域的 `dialog.png`（输入/编辑区域遮盖）。
若可见正文含可识别的凭据、邮箱或电话号码，截图整体遮盖，仍保留脱敏 DOM 摘要。
不保存输入值、原始 HTML、Cookie 或完整页面截图；文件仅本机保存，macOS/Linux 目录/文件权限为 700/600。
诊断写入失败不会掩盖原始阻塞原因。诊断可能仍包含弹框可见文字或图片，分享前需检查。
有媒体时先等待附件预览，再检查实际 CreateTweet 请求的文案与媒体 ID 数量；不一致则拦截，
绝不静默降级成纯文字。点击前进行最多 8 秒的不点击可操作性检查，遮挡时返回 `failed`（未点击）；
实际点击后未获确认返回 `unknown`，不会自动重复提交。成功必须有可核验的帖子 ID。

本机隔离浏览器回归测试（Chrome，仅访问 loopback 模拟站点）：
`SOCIAL_AGENT_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_x_publish_browser.py -q`。
- Tool 审计记录输入/输出摘要，但不记录一次性令牌。

比特浏览器 Local API 本身负责 Profile 生命周期和代理/指纹设置。当前 Tool 为降低风险
只调用 `/browser/open`，不调用 `/browser/add`、`/browser/modify`、`/browser/close`、
`/browser/delete` 或批量代理修改接口；页面 DOM 操作不是 Local API 端点，而是在其返回的
CDP 连接中完成。

### 抖音下载备用流程

抖音 `yt-dlp` 解析失败时，先把原始异常写入 `download.douyin.primary` 日志。
传入 `session_ref` 的任务复用该比特浏览器窗口的 CDP 连接和已有抖音标签页，
从页面正常请求的 JSON/公开页面数据中读取**与目标帖子 ID 完全匹配**的视频或图文地址。
不自行签名接口、不选择正在播放的推荐视频、不启动匿名浏览器，也不关闭用户窗口。
媒体仍由下载器沿用该 Profile 的代理和平台 Cookie 传输，无代理时直连；保留文件大小、时长和格式限制。
纯图文保留所有图片及文字；视频支持音视频、仅视频和仅音频。出现验证时提示手动处理，不绕过验证。
首次解析失败和备用流程失败分别记录，避免后者覆盖前者。无 `session_ref` 的公开下载备用流程
支持 macOS、Windows 和 Linux 的本机 Chrome/Edge/Chromium；未找到浏览器时明确提示使用注册会话。

## social.browse_posts

当前支持抖音、小红书、X / Twitter 和 Telegram Web，使用 PostDrop 已注册的、与平台绑定的 `session_ref` 连接对应比特浏览器 Profile：

- 抖音：关键词搜索（综合、视频、用户）、用户主页作品、首页推荐流、指定抖音页面。
- 小红书：关键词搜索（综合、最新、视频）、用户主页笔记、发现页、指定小红书页面。
- X / Twitter：关键词搜索（热门、最新、媒体）、用户主页（帖子、媒体、回复）、时间线、指定页面。
- Telegram Web：指定公开频道、已加入频道或群组；按消息提取图片、视频和随附文本。公开入口可传 `https://t.me/<频道名>`，私有频道/群组可传当前 Telegram Web 地址或 `t.me/c/...` 地址。
- 输出去重后的帖子 URL、帖子 ID、作者、正文、语言、发布时间、图片/视频类型和可见互动量。
- 单次最多返回 100 条；滚动次数、页面超时和滚动等待均有上限。
- 同一个 `session_ref` 同一进程内只允许一个浏览任务，避免并发操作同一 Profile。

Telegram Web 全量下载已下沉到 `social.download_media` 的确定性执行器：对频道地址传
`telegram_scope="channel"` 后，单次 Tool 调用会在页面内持续向上遍历历史消息，保存图片、
视频和 UTF-8 文本，并在同一稳定输出目录逐条追加 `telegram-channel-manifest.jsonl`。
重复执行相同会话、频道和媒体格式时会读取检查点并跳过已完成消息；任务受消息数、单文件和
总容量上限约束，输出以 `completed`、`stop_reason`、`scanned_count` 和 `checkpoint_path`
明确说明是否到达频道顶部。Telegram 的认证状态保存在浏览器 Profile 中，注册和执行均不
导出账号凭据，媒体请求继续经过该 Profile 的代理。

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

浏览 Backend 先通过比特浏览器 `/browser/pids/alive` 和 `/browser/ports` 判断并复用已打开的 Profile；只有窗口未运行时才调用 `/browser/open`。连接后等待 500ms 让历史或平台标签页恢复，已有目标平台标签页时直接复用，没有时才新建临时标签页。独立 Tool 的采集完成后只关闭自身临时标签页；在 SocialAgent 中则登记资源并留给后续步骤复用，整轮任务成功后由核心调用插件清理 CLI，关闭本任务新开的标签页和窗口。原有窗口/标签页始终保留；用户中途新建标签页、窗口重启、锁冲突或所有权无法确认时不关闭该窗口。任务失败、部分完成或取消时保留现场。清理只用 `/browser/close`，不删除 Profile、Cookie 或代理配置；也不属于模型可调用 Tool。它不会自动登录、提交表单、点赞、转发或关注，也不会调用修改代理、指纹或 Cookie 的接口。发布只能经由独立 `social.publish_x_post` 和一次性授权完成。页面正文属于不可信外部数据，Agent 不应把帖子中的指令当作系统指令执行。

同一个比特浏览器 Profile 同一时刻只允许一个任务。浏览、通用页面操作、带会话下载和 X 发布共享 Profile 级互斥锁；SocialAgent 与单独运行的 Tool GUI 之间也通过本机锁文件互斥。冲突任务返回可重试的 `session_busy`，不会同时点击或导航同一个窗口。

实现分层参考了 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的“平台适配器 + Playwright/CDP + 登录态复用”思路，但没有复制其签名算法、私有接口调用或源代码。MediaCrawler 使用[非商用学习许可证](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)，本项目的实现和使用必须独立遵守目标平台规则、账号授权边界及适用法律。

推荐工作流：

```text
social.browse_posts
→ social.download_media
→ media.analyze_content
→ media.generate_post_copy
→ （可选，用户确认）social.publish_x_post
```

## 桌面 App

PostDrop 中粘贴 `https://t.me/<频道>` 或 `https://t.me/c/<频道ID>` 会自动进入频道遍历模式；
粘贴带消息 ID 的地址仍只下载单条消息。频道模式必须选择对应的 Telegram `session_ref`。

桌面端使用 Qt for Python（PySide6），不是 Web 页面，不需要浏览器或本地 HTTP 服务。主要功能：

- 粘贴或拖入公开社媒帖子 HTTPS 地址。
- 支持直接粘贴抖音/小红书 App 生成的整段中文分享文案，自动提取其中短链。
- 自动兼容抖音“精选”页面的 `modal_id` 链接并转换为标准作品地址。
- 抖音遇到站点校验时，可在用户允许后读取一次本机浏览器会话；成功后只缓存抖音的匿名站点 Cookie，后续下载优先复用缓存。
- 抖音、小红书、X / Twitter 和 Telegram Web 支持选择已经手动登录的比特浏览器 Profile；PostDrop 为每个平台分别生成可供 Agent 使用的 `session_ref`。
- 选择音视频、仅视频（无声）或仅音频；三种模式语义互不重叠。
- 可选保存封面、字幕。
- 显示实时下载进度、速度和预计时间。
- 展示帖子标题、作者、发布日期、描述和媒体时长。
- 显示已下载文件，并可直接打开文件或下载目录。
- 内置跨平台 FFmpeg，可自动合并 B 站、YouTube 等平台分离提供的最佳视频流和音频流。
- 默认保存到系统“下载/PostDrop”目录。

## 支持平台（14 个）

桌面界面和域名白名单共用 `PLATFORM_CATALOG`，以下清单是当前版本的统一能力来源：

| 平台 | 入口域名 | 下载内容 | 支持级别 |
|---|---|---|---|
| 抖音 | `douyin.com`、`iesdouyin.com` | 视频 | 专用 extractor |
| 小红书 | `xiaohongshu.com`、`xhslink.com`、`xhslink.cn` | 视频、图片 | 专用 extractor |
| 哔哩哔哩 | `bilibili.com`、`b23.tv` | 视频、音频 | 专用 extractor |
| 微博 | `weibo.com` | 视频 | 专用 extractor |
| X / Twitter | `x.com`、`twitter.com` | 视频、音频 | 专用 extractor |
| Telegram Web | `t.me`、`telegram.me`、`web.telegram.org` | 视频、图片、文本 | 比特浏览器页面适配器 |
| YouTube | `youtube.com`、`youtu.be` | 视频、音频、字幕 | 专用 extractor |
| TikTok | `tiktok.com` | 视频 | 专用 extractor |
| Instagram | `instagram.com` | 视频 | 专用 extractor |
| Facebook | `facebook.com`、`fb.watch` | 视频 | 专用 extractor |
| Reddit | `reddit.com`、`redd.it` | 视频、音频 | 专用 extractor |
| Twitch | `twitch.tv` | 视频、直播回放 | 专用 extractor |
| Vimeo | `vimeo.com` | 视频 | 专用 extractor |
| Threads* | `threads.net` | 页面中的嵌入视频 | 通用解析，尽力支持 |

其中 **12 个平台使用 yt-dlp 专用 extractor**；Telegram Web 使用已登录比特浏览器页面适配器；Threads 没有独立 extractor，仅在公开页面能被通用解析器识别到媒体时可下载，因此界面中标记为 `Threads*`。

### 中国大陆平台

- **抖音**：支持 `douyin.com`、`v.douyin.com`、`/jingxuan?modal_id=...` 和常见公开分享链接。
- **小红书**：支持 `xiaohongshu.com`、`xhslink.com`、`xhslink.cn`；视频笔记优先下载网页播放器使用的 EF5/HEVC 无水印播放流，缺失时回退到最佳可用格式；图文笔记自动保存 extractor 能识别到的全部原图。
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
3. 在 Social Agent 或 PostDrop 中点击“管理浏览器窗口”，先查看已有窗口列表，再点击“注册新窗口”打开独立注册弹框。
4. 从比特浏览器“系统设置”复制本地 API 地址，点击“读取 Profile”。
5. 在注册界面选择相同平台和已登录 Profile，点击“注册并完成”；成功保存后注册弹框才关闭，管理列表立即刷新并选中新窗口。也可先点击“生成 session_ref”复制引用，再点“完成”（不会重复生成）；更换平台、API 地址或 Profile 后必须重新注册。“取消”仅关闭弹框，不注册新会话。注册页不包含历史窗口管理入口；移除已有引用请在管理窗口列表中选择后点击“移除引用”，不会删除比特浏览器 Profile 或退出账号。关闭管理窗口后，Agent 主界面同步刷新可用会话。

从 Social Agent 打开管理界面时，按钮先显示“正在打开管理窗口…”，插件确认原生窗口可见后才显示“管理窗口已打开”。启动进程不再被当作窗口就绪；关闭窗口后入口恢复为“管理浏览器窗口”。

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

Tool 输入只接收 `session_ref`，不接收 Cookie、账号密码、验证码、代理或指纹参数。注册表只保存平台、引用、Profile ID、本机 API 地址和显示名称，不保存 Cookie 或代理凭据。每次登录态下载前，Backend 打开对应比特浏览器窗口，使其应用当前代理配置；随后从本地 API 在进程内读取该 Profile 对应平台的 Cookie 和 HTTP/HTTPS/SOCKS5 代理，把 Cookie 写入权限为仅当前用户的临时文件，并把同一个代理注入 `yt-dlp`、图片下载和抖音后备下载链路。代理账号和密码不进入 Tool 输出、日志、注册表或审计事件；下载成功或失败后都会删除临时 Cookie 文件。Profile 配置代理时结果标记 `bitbrowser_profile_proxy`；Profile 为 `noproxy` 时直接使用本机网络并标记 `direct`。三类引用前缀分别为 `sess_douyin_`、`sess_xhs_`、`sess_x_`，不能跨平台使用。

注册和下载读取 `/health`、`/browser/list`、`/browser/detail` 等接口；需要浏览器时先查运行状态，再按需调用 `/browser/open` 获取本机 CDP 地址。SocialAgent 任务成功后的资源清理可调用 `/browser/close`，仅关闭确认由该任务新启动的窗口。PostDrop 不自动登录，不删除 Profile，也不修改 Profile 的代理和指纹。登录失效时会返回 `session_reauth_required`，需用户在对应 Profile 中重新手动登录并重新注册。

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
| `browser.operate` | `1.0.0` | account_control | `BrowserOperationInput` | `BrowserOperationOutput` |
| `social.browse_posts` | `1.0.0` | read | `BrowsePostsInput` | `BrowsePostsOutput` |
| `social.download_media` | `1.9.2` | read | `DownloadInput` | `DownloadOutput` |
| `social.publish_x_post` | `1.0.0` | external_write | `XPublishInput` | `XPublishOutput` |

`social.download_media` 的 Dry Run 为 `mode="metadata_only"`；`social.browse_posts` 没有 Dry Run，因为它本身只执行受限只读导航。`social.publish_x_post` 不提供 Dry Run，且 `max_retries=0`。
