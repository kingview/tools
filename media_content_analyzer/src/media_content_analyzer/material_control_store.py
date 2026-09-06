"""Read-only adapter for the existing task database; no schema migration needed.

Only this adapter maps persisted command/state strings to the control contract.
It is vendored with plugins, so they do not import the host repository at runtime.
"""
from contextlib import closing
from pathlib import Path
import re
import sqlite3

from .material_control_contract import ControlAction, TaskControl


class SQLiteControlReader:
    def __init__(self, state_root, *, timeout=1):
        self.path = Path(state_root).resolve() / 'material-tasks.sqlite3'
        self.timeout = timeout

    def read(self, task_id: str) -> TaskControl:
        if not re.fullmatch('[a-f0-9]{32}', task_id):
            raise ValueError('无效任务 ID')
        if not self.path.is_file():
            return TaskControl(found=False)
        # SQLite's connection context only commits/rolls back; closing is explicit
        # because progress callbacks may read this frequently during a transfer.
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=self.timeout)) as db:
            row = db.execute('SELECT command,state FROM jobs WHERE id=?', (task_id,)).fetchone()
        if row is None:
            return TaskControl(found=False)
        command, state = row
        if command == 'stop' or state == '已停止':
            return TaskControl(True, ControlAction.STOP)
        if command == 'pause' or state == '已暂停':
            return TaskControl(True, ControlAction.PAUSE)
        return TaskControl(found=True)
