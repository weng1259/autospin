"""ADR-004 shared types for the Phase 3.0 spikes.

Both spikes implement the same `GantryBackend.home()` + `move_to(Position)`
signatures so we can evaluate how naturally each path maps to the 7 principles
in ADR-004 (typed API, structured errors, idempotency, observable state,
safety, observability, dry-run).
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MachineState(str, Enum):
    IDLE = "idle"
    RUN = "run"
    JOG = "jog"
    HOME = "home"
    ALARM = "alarm"
    HOLD = "hold"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class Position(BaseModel):
    x_mm: float = Field(..., ge=-280.0, le=0.0)
    y_mm: float = Field(..., ge=-280.0, le=0.0)
    z_mm: float = Field(..., ge=-95.0, le=0.0)


class MachineStatus(BaseModel):
    state: MachineState
    position: Position
    alarm_code: Optional[int] = None
    is_homed: bool = False
    planner_buffer_free: Optional[int] = None
    rx_buffer_free: Optional[int] = None
    raw: str = ""
    last_update_ms_ago: float = 0.0


class HomeResult(BaseModel):
    success: bool
    position_after_pulloff: Position
    duration_ms: float
    event_id: str


class MoveResult(BaseModel):
    success: bool
    final_position: Position
    duration_ms: float
    event_id: str


class L3Error(Exception):
    error_code: str = "L3.UNKNOWN"
    recoverable: bool = False
    suggested_action: str = ""

    def __init__(self, human_message: str, agent_message: str = ""):
        self.human_message = human_message
        self.agent_message = agent_message or human_message
        super().__init__(human_message)


class MachineNotHomedError(L3Error):
    error_code = "L3.MACHINE_NOT_HOMED"
    recoverable = True
    suggested_action = "Call gantry.home() before attempting motion commands."


class AlarmStateError(L3Error):
    error_code = "L3.ALARM_STATE"
    recoverable = True
    suggested_action = "Call unlock_alarm() or home() to recover."


class SoftLimitExceededError(L3Error):
    error_code = "L3.SOFT_LIMIT_EXCEEDED"
    recoverable = False
    suggested_action = "Target outside work envelope; adjust Position fields."


class HomingTimeoutError(L3Error):
    error_code = "L3.HOMING_TIMEOUT"
    recoverable = True
    suggested_action = "Homing did not complete; check brake release & sensors."


class ConnectionError(L3Error):
    error_code = "L3.CONNECTION"
    recoverable = True
    suggested_action = "Check USB cable and port; reconnect."


def new_event_id() -> str:
    return str(uuid.uuid4())


def now_ms() -> float:
    return time.time() * 1000.0
