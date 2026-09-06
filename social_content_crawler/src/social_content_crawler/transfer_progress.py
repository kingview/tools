"""Per-execution transfer telemetry; contains no URLs, cookies or post text."""
from __future__ import annotations

import contextvars
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import current_context, redact
from .errors import CrawlerError, ErrorCode
from .material_control import check_material_control, MaterialControlInterrupted

_active = contextvars.ContextVar('transfer_reporter', default=None)


class TransferReporter:
    def __init__(self, root: Path, execution_id: str, policy_path: Path | None = None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', execution_id):
            raise ValueError('Invalid transfer execution ID')
        self.execution_id = execution_id
        self.path = root / 'transfer-progress' / f'{execution_id}.json'
        self.log = root / 'logs' / f'transfer-{execution_id}.jsonl'
        self.policy_path = policy_path
        self.filename = ''
        self.started = time.monotonic()
        self.last_write = 0.0
        self.last_log = 0.0
        self.start_bytes = 0
        self.completed = set()
        self.sequence = 0
        self.state = {}

    def check_active(self):
        check_material_control()
        if self.policy_path is None:
            return
        try:
            policy = json.loads(self.policy_path.read_text())
            valid = policy.get('execution_id') == self.execution_id
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise CrawlerError(ErrorCode.DOWNLOAD_FAILED, '任务授权已撤销，已停止下载并保留现有文件。')

    def report(self, event):
        self.check_active()
        now = time.monotonic()
        status = str(event.get('status', 'downloading'))
        filename = Path(str(event['filename'])).name if 'filename' in event else self.filename
        changed = filename != self.filename
        if changed:
            self.filename = filename
            self.started = now
            self.start_bytes = max(0, int(event.get('downloaded_bytes') or 0))
        if status == 'finished' and filename:
            self.completed.add(filename)
        size = max(0, int(event.get('downloaded_bytes') or 0))
        total = max(0, int(event.get('total_bytes') or 0))
        self.state.update({key:event[key] for key in ('post_index', 'post_total', 'media_index', 'media_total') if key in event})
        if not changed and status == 'downloading' and now - self.last_write < 1:
            return
        self.sequence += 1
        self.state.update(execution_id=self.execution_id, sequence=self.sequence,
            updated_at=time.time(), status=status, filename=filename,
            downloaded_bytes=size, total_bytes=total, files_completed=len(self.completed),
            speed_bps=max(0, size-self.start_bytes) / max(now-self.started, 1),
            message=redact(str(event.get('message') or ''))[:500])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False))
        temporary.replace(self.path)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        if changed or status != 'downloading' or now-self.last_log >= 5:
            with self.log.open('a') as stream:
                stream.write(json.dumps(self.state, ensure_ascii=False) + '\n')
            self.last_log = now
        self.last_write = now


@contextmanager
def transfer_scope():
    root = os.getenv('SOCIAL_AGENT_STATE_ROOT')
    execution_id = current_context().get('execution_id')
    reporter = None
    if root and execution_id:
        policy = os.getenv('SOCIAL_AGENT_EXECUTION_POLICY_PATH')
        reporter = TransferReporter(Path(root), execution_id, Path(policy) if policy else None)
    token = _active.set(reporter)
    try:
        if reporter:
            reporter.report({'status':'preparing'})
        yield
    except Exception as exc:
        if reporter and not isinstance(exc, MaterialControlInterrupted):
            # Report the failure even when cancellation revoked the grant.
            reporter.policy_path = None
            reporter.report({'status':'failed', 'message':str(exc)})
        raise
    else:
        if reporter:
            reporter.report({'status':'completed'})
    finally:
        _active.reset(token)


def report_transfer(event):
    reporter = _active.get()
    if reporter:
        reporter.report(event)


def check_transfer_active():
    check_material_control()
    reporter = _active.get()
    if reporter:
        reporter.check_active()
