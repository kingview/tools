"""Read-only cooperative control contract vendored into independent plugins.

Only the host's diagnostic task ID and configured state root are used; callers
cannot supply arbitrary database paths through tool arguments.
"""
import os
import re
import subprocess
import time

from .diagnostics import current_context
from .material_control_contract import ControlAction, ControlReader
from .material_control_store import SQLiteControlReader


class MaterialControlInterrupted(RuntimeError):
    pass


def check_material_control(*, reader: ControlReader | None = None):
    task_id = current_context().get('task_id', '')
    root = os.environ.get('SOCIAL_AGENT_STATE_ROOT')
    if not root or not re.fullmatch('[a-f0-9]{32}', task_id):
        return
    control = (reader if reader is not None else SQLiteControlReader(root)).read(task_id)
    if control.found and control.action != ControlAction.RUN:
        raise MaterialControlInterrupted('素材任务已请求暂停或停止，检查点已保留')


def run_material_process(args, *, timeout):
    """Stop only our own transcoder/decoder, never a user's browser or LLM server."""
    check_material_control()
    with subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
        deadline=time.monotonic()+timeout
        try:
            while True:
                check_material_control()
                remaining=deadline-time.monotonic()
                if remaining<=0: raise subprocess.TimeoutExpired(args,timeout)
                try:
                    stdout,stderr=process.communicate(timeout=min(.3,remaining))
                    return subprocess.CompletedProcess(args,process.returncode,stdout,stderr)
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate()
            raise
