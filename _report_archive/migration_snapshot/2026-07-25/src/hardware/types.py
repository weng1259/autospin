"""共享 pydantic 类型 — Position / MachineStatus / *Result。

ADR-004 §原则 1：所有 L3 公共 API 的参数和返回值都应是类型化的 pydantic
模型，而非裸 dict。

注意：`Position` 在此层**不**做软限位校验 —— 读取 grbl 状态时坐标可能在
任意值（未归零 / 上一次断电残留）。软限位校验放在 `move_to` 运行时（Slice 3
起），配置读自 constants.yaml。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MachineState(str, Enum):
    """grbl 状态机（`<state>` 字段）。"""

    IDLE = "idle"
    RUN = "run"
    JOG = "jog"
    HOME = "home"
    ALARM = "alarm"
    HOLD = "hold"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class Position(BaseModel):
    x_mm: float
    y_mm: float
    z_mm: float


class MachineStatus(BaseModel):
    state: MachineState
    position: Position
    alarm_code: Optional[int] = None
    is_homed: bool = False
    limit_pins: list[str] = Field(default_factory=list)
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


class MovePlan(BaseModel):
    """`move_to(..., dry_run=True)` 返回：预计轨迹 + 时长，**不发任何字节**。

    为 Phase 4 旋涂 recipe 做模板（ADR-004 §原则 7）：破坏性操作可由 Agent
    / UI 先拿 plan 给人审批再 commit。

    v0 字段最小集：
    - `target` / `feed_mm_min`：真正会下发的参数（defaults 已解析）
    - `distance_mm`：从 `current_position` 到 target 的欧氏距离；
      若 backend 未 connect 则 None（拿不到 current）
    - `estimated_duration_s`：distance_mm / (feed/60)；未知距离时 None
    - `current_position`：dry-run 时刻的位置快照；未 connect 时 None
    """

    target: Position
    feed_mm_min: float
    distance_mm: Optional[float] = None
    estimated_duration_s: Optional[float] = None
    current_position: Optional[Position] = None


class HomePlan(BaseModel):
    """`home(..., dry_run=True)` 返回：归零动作序列 + 预计时长。

    v0：序列和时长都是经验值硬编码。Phase 4 若接 recipe 引擎可按当前位置
    优化估算。
    """

    sequence: list[str]
    estimated_duration_s: float


class RecoveryResult(BaseModel):
    """`recover_from_alarm()` 的返回（Slice 5）。

    `entry_state`：进入 recover 时的 MachineState；用于 PM/Agent 事后理解
    这次恢复面对的是什么场景（Alarm / Hold / Idle）。
    `actions_taken`：按执行顺序的 step 名字，例如
    `["soft_reset", "unlock_alarm", "home"]`；Idle 入口时为空列表。
    `final_status`：恢复后的完整状态快照（含 state / position / is_homed）。
    """

    success: bool
    entry_state: MachineState
    actions_taken: list[str]
    final_status: MachineStatus
    duration_ms: float
    event_id: str


class GrblSettingMismatch(BaseModel):
    """单条 grbl `$` 参数不一致的快照。"""

    key: str
    expected: str
    actual: Optional[str] = None


class GrblSettingsSnapshot(BaseModel):
    """grbl 当前 settings dump 的结构化快照。"""

    settings: dict[str, str]


class GrblSettingsValidationResult(BaseModel):
    """grbl settings 检查 / 修复结果。"""

    success: bool
    repaired: bool
    mismatches: list[GrblSettingMismatch]
    snapshot: GrblSettingsSnapshot
    duration_ms: float
    event_id: str


# ─────────────────────────── Phase 3.3：RelayBackend ───────────────────────────


class RelayState(BaseModel):
    """DSTUR-T80 USB 继电器 8 通道状态快照。

    `channels`：通道号（1-8）→ 是否 ON。True = 24V 输出有效。

    **注意**：这是 backend 记忆的"已下发"state，**不是**硬件反馈测量——
    DSTUR-T80 协议无状态回读命令。连线松动 / 继电器本身失效不会被发现。
    实际语义：「上次成功 write 的值，假设命令真的生效了」。
    """

    channels: dict[int, bool]
    last_update_ms_ago: float = 0.0


class RelayActionResult(BaseModel):
    """`ch_on()` / `ch_off()` 完成后返回。

    `was_noop=True` 表示幂等命中——目标 state 已匹配，未实际写串口。这是
    brake-skip 优化的核心路径：GantryBackend 纯 XY 移动时 Z 刹车已 released，
    `ch_on(2)` 立即 noop 返回，避开 EMI 风险（Issue #025）。
    """

    success: bool
    channel: int
    state_after: bool
    was_noop: bool
    duration_ms: float
    event_id: str


class RelayActionPlan(BaseModel):
    """`ch_on(..., dry_run=True)` / `ch_off(..., dry_run=True)` 返回。"""

    channel: int
    target_state: bool
    current_state: bool
    would_write: bool  # false 意味 dry_run 执行会是 noop（目标已匹配）


# ─────────────────────────── Phase 3.3：GripperBackend ──────────────────────────


class GripperCommandedState(str, Enum):
    """夹爪命令状态——backend 记忆的最后一次开合命令。

    **不是真实位置**：继电器只控 24V 通断，无位置反馈。真实位置要 RS485
    查（Phase 3.5+ 的 `set_force` / 完整状态查询才引入）。

    `UNKNOWN` 用在 connect 前 / 未发任何开合命令的初始态。
    """

    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class GripperState(BaseModel):
    """夹爪状态快照。

    `position_known` 在 Phase 3.3 永远为 `False`（RS485 未集成）。Phase 3.5
    接 RS485 后会加 `actual_position_mm` 字段并把此标志切到 True；
    保留字段是让 Agent / UI 现在就写好分支逻辑。
    """

    commanded_state: GripperCommandedState
    position_known: bool = False
    last_command_ms_ago: Optional[float] = None


class GripperActionResult(BaseModel):
    """`open()` / `close()` 完成后返回。"""

    success: bool
    commanded_state_after: GripperCommandedState
    was_noop: bool
    duration_ms: float
    event_id: str


class GripperActionPlan(BaseModel):
    """dry_run 返回。

    `underlying_relay_channel` 让调用方看到开合命令底层走的继电器通道
    （默认 CH1），便于 Agent 理解跨 backend 副作用。
    """

    target_state: GripperCommandedState
    current_state: GripperCommandedState
    would_activate_relay: bool
    underlying_relay_channel: int
