"""Data-only control interface shared by the host and independent Tool plugins."""
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ControlAction(str, Enum):
    RUN = 'run'
    PAUSE = 'pause'
    STOP = 'stop'


@dataclass(frozen=True)
class TaskControl:
    found: bool
    action: ControlAction = ControlAction.RUN


class ControlReader(Protocol):
    def read(self, task_id: str) -> TaskControl: ...
