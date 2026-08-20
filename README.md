# Tool Registry（MVP）

Tool 定义以各包导出的 Pydantic Schema 和 `ToolSpec` 为权威来源。

| Tool | 版本 | 类型 | 实现 |
|---|---:|---|---|
| `social.download_media` | `1.5.0` | read | [`social_content_crawler`](./social_content_crawler) |

新增 Tool 时应保持包边界独立，至少提供版本化 `ToolSpec`、结构化输入输出、Executor、权限/风险标签、审计和测试。
