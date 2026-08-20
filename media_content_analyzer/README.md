# media.analyze_content

下载后媒体内容理解 Tool。它接收 `social.download_media` 返回的 artifact 清单，对图片、音频和视频进行确定性预处理，并可调用本地 Qwen3-VL/LiteLLM 模型生成结构化摘要、标签、实体、主张、情感、安全标记和证据。

## 能力

- 图片：Pillow/OpenCV 预处理、pHash、PaddleOCR、本地视觉模型理解。
- 视频：FFmpeg/ffprobe、PySceneDetect、关键帧 OCR、Whisper ASR、关键帧视觉理解。
- 音频：FFmpeg 标准化和 Whisper ASR。
- 统一输出：`ContentAnalysisOutput`，包含 summary、分类型 tags、topics、entities、claims、evidence、confidence 和 `needs_human_review`。
- 安全：文件必须位于 Executor 配置的媒体根目录，大小和 SHA-256 必须与下载清单一致；OCR、字幕、帖子文本和图片都作为不可信数据传给模型。
- 运行：Agent Python API 与独立 CLI 共用同一套 Tool、契约、缓存和审计实现。

## 安装

最小安装可以进行图片元数据、哈希和基于现有文本的确定性分析：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

完整媒体处理依赖按需安装：

```bash
.venv/bin/pip install -e '.[image,video,ocr,dev]'
```

PaddleOCR 还需要按目标 CPU/GPU 和操作系统安装匹配的 PaddlePaddle。模型权重需要单独核对许可证并登记版本。

## 独立运行

无模型服务时仍会输出 OCR/ASR 和确定性摘要，并把低置信度结果标记为需要复核：

```bash
media-content-analyzer ./downloads/post.jpg \
  --post-text '帖子正文' \
  --language zh
```

接入 LiteLLM Proxy 或 vLLM 的 OpenAI-compatible 接口：

```bash
export CONTENT_ANALYZER_MODEL_BASE_URL=http://127.0.0.1:4000/v1
export CONTENT_ANALYZER_MODEL=content_understander
media-content-analyzer ./downloads/post.mp4 --language zh
```

也可以使用 `--model-base-url`、`--model` 和 `--model-api-key` 参数。模型接口只用于内容理解，不用于下载或社媒账号访问。

## Agent 调用

```python
import asyncio
from pathlib import Path

from media_content_analyzer import (
    AnalyzeContentInput,
    ArtifactRef,
    ToolContext,
    build_local_tool,
)


async def main() -> None:
    tool = build_local_tool(
        allowed_media_root=Path("./var/media"),
        state_root=Path("./var/media-analysis"),
    )
    result = await tool.execute(
        AnalyzeContentInput(
            artifacts=[
                ArtifactRef(
                    path="/absolute/executor-managed/path/post.mp4",
                    size_bytes=123456,
                    sha256="...64 hexadecimal characters...",
                    media_type="video/mp4",
                )
            ],
            post_text="帖子正文",
            language_hint="zh",
        ),
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

`ArtifactRef` 与下载器的 `DownloadedArtifact` 字段兼容，可直接使用 `DownloadedArtifact.model_dump()` 构建输入。生产环境应进一步把本地路径替换为 Media Service 管理的 `asset_id` 或对象存储引用。

## Tool 契约

- 名称：`media.analyze_content`
- 版本：`1.0.0`
- 类型：`analysis`
- 外部副作用：无
- 幂等：是，按 artifact hash、请求参数和流水线/模型版本缓存
- 人工批准：不需要
- 权限：`media.analyze`

固定媒体处理流程不使用 LangGraph。后续接入 Temporal 时，应将图片 CPU、视频 CPU、视觉 GPU 分别投递到 `media-image-cpu`、`media-video-cpu` 和 `media-vision-gpu` Task Queue。
