# Tool Registry（MVP）

Tool 定义以各包导出的 Pydantic Schema 和 `ToolSpec` 为权威来源。

| Tool | 版本 | 类型 | 实现 |
|---|---:|---|---|
| `social.browse_posts` | `1.0.0` | read | [`social_content_crawler`](./social_content_crawler) |
| `social.download_media` | `1.6.0` | read | [`social_content_crawler`](./social_content_crawler) |
| `media.analyze_content` | `1.1.1` | analysis | [`media_content_analyzer`](./media_content_analyzer) |
| `media.process_watermark` | `1.4.0` | analysis/transform | [`media_content_analyzer`](./media_content_analyzer) |
| `media.generate_post_copy` | `1.0.0` | generation | [`media_content_analyzer`](./media_content_analyzer) |

新增 Tool 时应保持包边界独立，至少提供版本化 `ToolSpec`、结构化输入输出、Executor、权限/风险标签、审计和测试。

## 统一桌面 GUI

所有可独立运行的 Tool Client 统一使用 PySide6/Qt 构建 macOS 和 Windows 原生桌面界面，不混用 Web UI 或 Electron。界面沿用统一的深色主题、拖放输入、后台 Worker、进度反馈、结果卡片和本地目录操作模式。`media.process_watermark` 的独立客户端为 `WatermarkStudio`。

## 本地 Agent Client

独立桌面 Agent 位于与本仓库并列的 [`social_agent`](https://github.com/kingview/social_agent) 项目，不属于 `tools` 仓库。它用会话式自然语言生成受限 `AgentPlan`，经用户确认后编排 `social.browse_posts`、`social.download_media` 和可选的 `media.process_watermark`。它不是新的平台连接器，也不会绕过各 Tool 的权限、会话、限额和审计边界。
