from __future__ import annotations

from .diagnostics import install_exception_hooks, record_exception

import argparse
import asyncio
import hashlib
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .contracts import (
    ArtifactRef,
    ProcessWatermarkInput,
    WatermarkMode,
    WatermarkRepairQuality,
)
from .errors import AnalyzerError
from .ports import ToolContext
from .watermark_runtime import build_local_watermark_tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-watermark-processor",
        description="Detect video watermarks and optionally create authorized derivatives.",
    )
    parser.add_argument("artifacts", nargs="+", help="Video files to inspect")
    parser.add_argument("--media-root", type=Path, help="Allowed media root")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".media-content-analyzer",
    )
    parser.add_argument("--remove", action="store_true", help="Create a derivative when a confident static watermark is detected")
    parser.add_argument(
        "--authorized",
        action="store_true",
        help="Confirm that you own or are authorized to modify these media files",
    )
    parser.add_argument("--minimum-confidence", type=float, default=0.72)
    parser.add_argument(
        "--sample-frames",
        type=int,
        default=None,
        help="Override automatic duration-aware sampling (advanced)",
    )
    parser.add_argument(
        "--repair-quality",
        choices=[value.value for value in WatermarkRepairQuality],
        default=WatermarkRepairQuality.AUTO.value,
        help="Repair strategy: auto, fast, balanced, or high",
    )
    parser.add_argument(
        "--no-temporal-consistency",
        action="store_true",
        help="Disable optical-flow temporal stabilization in balanced repair",
    )
    parser.add_argument("--output", type=Path, help="Write JSON result to this file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    install_exception_hooks("media-content")
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (AnalyzerError, ValueError) as exc:
        record_exception("media-content", "watermark_cli.handled", exc)
        print(f"ERROR [{getattr(exc, 'code', 'invalid_input')}]: {exc}", file=sys.stderr)
        return 2


async def _run(args: argparse.Namespace) -> int:
    paths = [Path(value).expanduser().resolve(strict=True) for value in args.artifacts]
    media_root = args.media_root.expanduser().resolve() if args.media_root else Path(
        os.path.commonpath([str(path.parent) for path in paths])
    ).resolve()
    manifests = [
        ArtifactRef(
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
            media_type=mimetypes.guess_type(path.name)[0],
        )
        for path in paths
    ]
    request = ProcessWatermarkInput(
        artifacts=manifests,
        mode=(WatermarkMode.REMOVE_IF_PRESENT if args.remove else WatermarkMode.DETECT_ONLY),
        authorization_confirmed=args.authorized,
        minimum_confidence=args.minimum_confidence,
        sample_frames=args.sample_frames,
        repair_quality=WatermarkRepairQuality(args.repair_quality),
        temporal_consistency=not args.no_temporal_consistency,
    )
    tool = build_local_watermark_tool(
        allowed_media_root=media_root,
        state_root=args.state_root,
        output_root=args.output_root,
    )
    result = await tool.execute(
        request,
        ToolContext(
            tenant_id="local",
            trace_id=uuid.uuid4().hex,
            actor_type="user",
            actor_id=os.getenv("USER") or os.getenv("USERNAME") or "local-user",
        ),
    )
    rendered = result.model_dump_json(indent=2)
    if args.output:
        args.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
        print(str(args.output))
    else:
        print(rendered)
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
