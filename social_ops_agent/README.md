# Social Agent

`Social Agent` 是一个可单独启动的本地桌面 Agent，也提供 Python Runtime。它通过会话式自然语言把任务编排为现有 Tool 调用，不直接实现平台爬取或下载逻辑。

当前支持：

- 使用 PostDrop 注册的抖音、小红书、X / Twitter `session_ref`。
- 关键词搜索、用户主页、推荐流/时间线、指定页面。
- 只获取帖子 URL，或继续批量下载图片/视频。
- 单次计划最多 100 条；下载固定拆成每批最多 20 个 URL。
- 默认跨批次总下载预算为 5 GB，不会把每批上限无限累加。
- 会话中继续说“改成前50条”等方式调整上一个计划。
- 计划生成和执行分为两个阶段，点击“确认并执行计划”前不会访问平台。
- 当前批次完成后停止、实时进度、结果数量和下载目录。
- 可在任务中明确要求“有水印就去水印”；Agent 会调用独立的 `media.process_watermark`，原视频始终保留。

示例：

```text
通过关键词“web3”在抖音上搜索并下载前100个帖子
```

需要水印处理时：

```text
通过关键词“web3”在抖音搜索并下载前100个帖子，有水印就去水印
```

对应的确定性执行图：

```text
自然语言
→ AgentPlan（平台、session_ref、搜索条件、数量、是否下载）
→ 用户确认
→ social.browse_posts（最多 100 条）
→ social.download_media（每批最多 20 条，共最多 5 批）
→ AgentRunResult
```

## 本地运行

macOS：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ../social_content_crawler -e '../media_content_analyzer[image]' -e '.[dev,build]'
.venv/bin/social-ops-agent
```

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\pip install -e ..\social_content_crawler -e "..\media_content_analyzer[image]" -e ".[dev,build]"
.venv\Scripts\social-ops-agent.exe
```

默认规划器先使用确定性中文解析；无法覆盖的表达会尝试本机 Ollama 的 `qwen3.5:9b`：

```text
SOCIAL_AGENT_OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
SOCIAL_AGENT_OLLAMA_MODEL=qwen3.5:9b
```

模型只能提出白名单字段，最终计划仍必须经过 Pydantic 契约、平台会话匹配、数量上限和 Tool Call 预算校验。

## 安全边界

- Agent 只允许浏览和本地下载，不支持自动登录、点赞、评论、关注、私信、转发或发布。
- `session_ref` 只映射用户已手动登录的比特浏览器 Profile；不向模型发送 Cookie、密码、代理或指纹参数。
- Profile 的代理和指纹在比特浏览器中预先配置，Agent 不轮换或修改它们。
- 页面内容是不可信数据，不作为 Agent 指令执行。
- 只应访问和保存有权使用的内容，并遵守平台规则与适用法律。

## LangGraph

当前执行路径是固定且有界的，因此采用有限状态 Runtime。加入“下载后逐帖分析、根据分析继续搜索、失败自动改计划、多模型复核”等动态循环时，保持 `AgentPlan`、`AgentRunResult` 和 Tool 契约不变，将 Runtime 替换为 LangGraph。
