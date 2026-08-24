# media.analyze_content + media.process_watermark + media.generate_post_copy

下载后媒体内容理解 Tool。它接收 `social.download_media` 返回的 artifact 清单，对图片、音频和视频进行确定性预处理，并默认调用本机 Ollama 中的 Qwen3.5-9B 生成结构化摘要、标签、实体、主张、情感、安全标记和证据。生产环境也可以切换到 LiteLLM/vLLM。

## 能力

- 图片：Pillow/OpenCV 预处理、pHash、PaddleOCR、本地视觉模型理解。
- 视频：FFmpeg/ffprobe、PySceneDetect、关键帧 OCR、Whisper ASR、关键帧视觉理解。
- 音频：FFmpeg 标准化和 Whisper ASR。
- 统一输出：`ContentAnalysisOutput`，包含 summary、分类型 tags、topics、entities、claims、evidence、confidence 和 `needs_human_review`。
- 文案生成：以 `ContentAnalysisOutput` 为事实依据，按通用、抖音、小红书、B站、微博、Instagram、TikTok 的发布风格生成 1–5 条文案。
- 文案控制：支持自然、种草推荐、专业、幽默、情绪共鸣、暧昧吸睛（非露骨）语气，以及长度、发布目标、补充要求和话题标签开关。
- 水印处理：`media.process_watermark` 抽帧检测画面任意位置的固定水印，并通过归一化边缘描述子跨帧关联重复出现且位置变化的文字/Logo，自动识别高置信度动态水印。周期滚动水印不必从首个采样帧开始，视频也可以同时返回固定和动态区域。明确授权后，默认只为叠加层笔画生成细粒度 mask，以首次可靠出现的时间点建立模板，向前/向后双向跟踪并在循环跳转时全画面重新定位，再使用 OpenCV inpaint、光流对齐和时序融合修复画面；也可切换快速模式或接入独立 GPU 视频修复 Worker。手动框选保留为低置信度兜底，原文件永不覆盖。
- 安全：文件必须位于 Executor 配置的媒体根目录，大小和 SHA-256 必须与下载清单一致；OCR、字幕、帖子文本和图片都作为不可信数据传给模型。
- 运行：Agent Python API、CLI 和 PySide6 桌面 App 共用同一套 Tool、契约、缓存和审计实现。所有 Tool 客户端统一使用 PySide6/Qt 桌面框架，不混用 Web 或 Electron。

## 安装

最小安装可以进行图片元数据、哈希和基于现有文本的确定性分析：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

完整媒体处理和桌面界面依赖按需安装：

```bash
.venv/bin/pip install -e '.[image,video,ocr,desktop,dev]'
```

PaddleOCR 还需要按目标 CPU/GPU 和操作系统安装匹配的 PaddlePaddle。模型权重需要单独核对许可证并登记版本。
在 macOS ARM64 的 Python 3.12 环境可以安装 CPU 版本：

```bash
.venv/bin/pip install paddlepaddle
```

首次执行 OCR 时会下载 PaddleOCR 模型权重；之后使用本地缓存。桌面端默认从 ModelScope 下载，适合中国大陆网络；可以通过 `CONTENT_ANALYZER_OCR_MODEL_SOURCE` 覆盖来源。

首次执行语音识别时也会下载 Faster-Whisper 模型权重并缓存。默认使用 ModelScope 上的官方 Systran 镜像，适合中国大陆网络；如需改回 Hugging Face，可设置 `CONTENT_ANALYZER_ASR_MODEL_SOURCE=huggingface`，也可以把 `--whisper-model` 指向已有的本地模型目录。

## 桌面 App

桌面端使用 Qt for Python（PySide6），不是 Web 页面。它支持：

- 选择或拖入多个图片、视频和音频文件。
- 输入可选的帖子正文和语言提示。
- 开关摘要、标签、OCR、ASR 和 Qwen3.5-9B 视觉分析。
- 查看摘要、标签、实体、主张、OCR、字幕、证据、警告和完整 JSON。
- 分析完成后直接选择目标平台、语气、长度和数量，使用本地 Qwen 生成文案，并复制或保存文案 JSON。
- 将结构化结果保存为 JSON，并打开本地缓存和审计目录。

开发环境直接启动：

```bash
.venv/bin/media-content-analyzer-gui
```

构建 macOS App：

```bash
.venv/bin/pip install -e '.[image,video,ocr,desktop,build]'
.venv/bin/pip install paddlepaddle
./scripts/build_macos.sh
```

产物位于 `dist/PostInsight.app`。对外分发前还应完成 Apple Developer 签名和公证。

构建 Windows App：

```powershell
.venv\Scripts\pip install -e ".[image,video,ocr,desktop,build]"
.venv\Scripts\pip install paddlepaddle
.\scripts\build_windows.ps1
```

产物位于 `dist\PostInsight\PostInsight.exe`。macOS 不能直接交叉生成 Windows `.exe`，需要在 Windows 上构建。

### 水印处理独立 App

`Watermark Studio` 与其他 Tool 客户端统一使用 PySide6/Qt。它支持拖入最多 20 个视频、仅检测、批量生成无水印副本、全画面固定水印检测、动态水印自动识别与跟踪、首帧手动框选兜底、检测区域预览、JSON 结果保存和输出目录打开。桌面客户端按本地自有或已获授权媒体场景运行，去除模式自动提交授权状态，不再重复显示确认复选框；Agent Tool 契约仍要求调用方显式传入授权。检测抽样按每个视频时长自动计算（每秒 2 帧，最低 36、最高 120），无需手工选择。修复质量可选“自动选择”“快速修复”“本机时序修复”和“AI 高质量修复（Apple/NVIDIA）”，并可控制时序一致性；原文件不会被覆盖。

开发环境启动：

```bash
.venv/bin/media-watermark-processor-gui
```

构建 macOS App：

```bash
./scripts/build_watermark_macos.sh
```

产物位于 `dist/WatermarkStudio.app`。发布构建会同时内置 LaMa ONNX 模型，
接收方无需安装 Python、ONNX Runtime 或另行下载模型即可离线运行。

构建 Windows App：

```powershell
.\scripts\build_watermark_windows.ps1
```

产物位于 `dist\WatermarkStudio\WatermarkStudio.exe`。

## 命令行运行

桌面端默认连接 `http://127.0.0.1:11434/v1`，使用已经安装到 Ollama 的 `qwen3.5:9b`：

```bash
ollama list
media-content-analyzer ./downloads/post.jpg \
  --post-text '帖子正文' \
  --language zh
```

如果 Ollama 不可用，Tool 会保留确定性 OCR/ASR 结果并给出警告。使用 `--no-vision` 可以显式禁用模型调用。

模型连接可以通过环境变量覆盖。例如接入 LiteLLM Proxy 或 vLLM 的 OpenAI-compatible 接口：

```bash
export CONTENT_ANALYZER_MODEL_BASE_URL=http://127.0.0.1:4000/v1
export CONTENT_ANALYZER_MODEL=content_understander
media-content-analyzer ./downloads/post.mp4 --language zh
```

也可以使用 `--model-base-url`、`--model` 和 `--model-api-key` 参数。省略 `/v1` 时会自动补齐。设置 `CONTENT_ANALYZER_MODEL_BASE_URL` 为空字符串，或传入 `--no-vision`，可以关闭模型调用。模型接口只用于内容理解，不用于下载或社媒账号访问。

水印检测可以独立运行，不需要 Ollama：

```bash
media-watermark-processor ./downloads/post.mp4
```

对自有或已获授权的视频生成去水印副本：

```bash
media-watermark-processor ./downloads/post.mp4 --remove --authorized
```

默认 `--repair-quality auto` 会根据水印面积和运动状态选择修复路径。也可以显式选择：

```bash
media-watermark-processor ./downloads/post.mp4 --remove --authorized \
  --repair-quality balanced
```

### Apple / NVIDIA 高质量修复 Worker

高质量模式采用独立的 LaMa ONNX Worker：同一份代码在 Apple Silicon 上自动选择 ONNX Runtime CoreML，在 NVIDIA 机器上自动选择 CUDA，不需要更改 Tool 或 GUI。主程序通过 schema `1.2` JSON 传入输入、精确输出路径、检测区域、每个区域是否需要动态跟踪和进度文件路径；Worker 结合现有双向跟踪、细粒度 mask、LaMa 图像补全和光流时序融合生成指定输出文件。Worker 会逐帧回报当前帧、总帧数、完成比例和预计剩余时间，GUI 将高质量阶段映射到总任务的 45%–94%。

开发环境安装与预下载模型（约 107 MB，首次运行也会自动下载）：

```bash
pip install -e '.[repair]'
video-repair-worker --download-model
video-repair-worker --health
media-watermark-processor ./downloads/post.mp4 --remove --authorized \
  --repair-quality high
```

Watermark Studio 的 macOS/Windows 安装包已内置 Worker 可执行文件，模型默认缓存在 `~/.cache/social-agent/video-repair/`。可用以下环境变量覆盖设备或模型位置：

```bash
export VIDEO_REPAIR_DEVICE=auto       # auto | coreml | cuda | cpu
export VIDEO_REPAIR_MODEL_PATH=/models/lama_512_fp16.onnx
```

NVIDIA 节点使用相同 Worker，安装 CUDA 版 ONNX Runtime extra（不要和 `repair` extra 同时安装）：

```bash
pip install -e '.[repair-nvidia]'
video-repair-worker --health
```

`--health` 应显示 `device: cuda` 和 `CUDAExecutionProvider`。如需把 Worker 部署为单独进程，或替换为已审核的其他实现，可设置：

```bash
export WATERMARK_HIGH_QUALITY_COMMAND='/opt/video-repair-worker/bin/repair-worker'
media-watermark-processor ./downloads/post.mp4 --remove --authorized \
  --repair-quality high
```

默认模型为 [`g-ronimo/lama/lama_512_fp16.onnx`](https://huggingface.co/g-ronimo/lama)，模型卡和 [LaMa 上游](https://github.com/advimman/lama) 均为 Apache-2.0。它是逐帧图像补全模型，视频一致性由 Tool 的双向水印跟踪和光流融合增强。ProPainter、DiffuEraser 等原生视频修复模型仍可通过同一 JSON sidecar 协议替换，但需要单独审核其权重、依赖许可证和 CUDA 资源；未找到可用 Worker 时，高质量模式会明确记录警告并回退到本机时序修复。

重复图案在至少 35% 的采样帧中出现、位置发生变化且跨帧相似度达到高置信阈值时，会被自动判定为动态水印并逐帧跟踪。低置信度、间歇出现或复杂形变的候选不会自动修改，可在 GUI 中框选，或通过 Tool 的 `manual_regions` 传入人工确认的首帧像素区域，并设置 `track_manual_regions=true` 处理。

可选资源预算环境变量：

```bash
export CONTENT_ANALYZER_MODEL_TIMEOUT=180
export CONTENT_ANALYZER_MODEL_MAX_IMAGES=16
export CONTENT_ANALYZER_MODEL_MAX_OUTPUT_TOKENS=4096
```

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

生成文案是独立、可审计的 Agent Tool，直接接收上一步的分析结果：

```python
from media_content_analyzer import (
    CopyPlatform,
    CopyTone,
    GeneratePostCopyInput,
    build_local_copy_tool,
)

copy_tool = build_local_copy_tool(state_root=Path("./var/media-analysis"))
copy_result = await copy_tool.execute(
    GeneratePostCopyInput(
        analysis=result,
        platform=CopyPlatform.XIAOHONGSHU,
        tone=CopyTone.RECOMMENDATION,
        language="zh",
        variant_count=3,
        max_characters=300,
        objective="提升收藏和评论",
    ),
    ToolContext(
        tenant_id="tenant-1",
        trace_id="trace-copy-1",
        actor_type="agent",
        actor_id="agent-1",
    ),
)
```

`ArtifactRef` 与下载器的 `DownloadedArtifact` 字段兼容，可直接使用 `DownloadedArtifact.model_dump()` 构建输入。生产环境应进一步把本地路径替换为 Media Service 管理的 `asset_id` 或对象存储引用。

水印 Tool 调用示例：

```python
from media_content_analyzer import ProcessWatermarkInput, build_local_watermark_tool

watermark_tool = build_local_watermark_tool(
    allowed_media_root=Path("./var/media"),
    state_root=Path("./var/media-analysis"),
)
watermark_result = await watermark_tool.execute(
    ProcessWatermarkInput(
        artifacts=[ArtifactRef.model_validate(downloaded_artifact.model_dump())],
        mode="remove_if_present",
        authorization_confirmed=True,
    ),
    ToolContext(
        tenant_id="tenant-1",
        trace_id="trace-watermark-1",
        actor_type="agent",
        actor_id="agent-1",
    ),
)
```

## Tool 契约

- 名称：`media.analyze_content`
- 版本：`1.1.1`
- 类型：`analysis`
- 外部副作用：无
- 幂等：是，按 artifact hash、请求参数和流水线/模型版本缓存
- 人工批准：不需要
- 权限：`media.analyze`

文案生成 Tool：

- 名称：`media.generate_post_copy`
- 版本：`1.2.0`
- 类型：`generation`
- 外部副作用：无，只返回候选文案
- 幂等：否，同一输入允许生成不同候选
- 人工批准：不需要；真正发布仍须经过平台写操作的权限和审批链
- 权限：`media.generate_copy`
- 安全边界：暧昧语气仅允许非露骨、成年人、自愿场景；检测到未成年人、年龄不明、非自愿或偷拍信号时拒绝生成

水印处理 Tool：

- 名称：`media.process_watermark`
- 版本：`1.4.3`
- 类型：`analysis` + 本地媒体衍生处理
- 原文件：强制保留，输出包含 `derived_from_sha256`
- 人工批准：去除模式需要；仅检测模式不修改文件
- 权限：`media.analyze`、`media.transform`
- 当前自动处理范围：画面任意位置的高置信度固定水印，以及跨帧重复、位置变化、外观稳定的动态文字/Logo；默认使用细粒度笔画 mask 和光流时序融合修复，也支持 Apple CoreML / NVIDIA CUDA 的 LaMa ONNX 高质量 Worker；低置信度、间歇出现、复杂形变或大面积候选转人工复核，也可手动框选后跟踪

固定媒体处理流程不使用 LangGraph。后续接入 Temporal 时，应将图片 CPU、视频 CPU、视觉 GPU 分别投递到 `media-image-cpu`、`media-video-cpu` 和 `media-vision-gpu` Task Queue。
