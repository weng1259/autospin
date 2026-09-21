"""Web mock 模式使用的七个纯内存设备。

这些类只复用真实 backend 的 Pydantic 输入/输出模型，不构造硬件 backend，
也不启动后台任务。写操作在调用线程内立即更新缓存，供 OperationGate 和
StatusPoller 做浏览器活体演示。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from ..hardware.heater_backend import HeaterActionResult, HeaterStatus
from ..hardware.linearstage_backend import (
    LinearStageActionResult,
    LinearStageStatus,
)
from ..hardware.pipette_backend import PipetteActionResult, PipetteStatus
from ..hardware.spincoater_backend import SpinActionResult, SpinStatus
from ..hardware.types import (
    GrblSettingsSnapshot,
    GrblSettingsValidationResult,
    GripperActionPlan,
    GripperActionResult,
    GripperCommandedState,
    GripperState,
    HomePlan,
    HomeResult,
    MachineState,
    MachineStatus,
    MovePlan,
    MoveResult,
    Position,
    RecoveryResult,
    RelayActionPlan,
    RelayActionResult,
    RelayState,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_id(action: str) -> str:
    return f"mock:{action}"


class MockGantry:
    """立即完成运动的三轴内存模型。"""

    def __init__(self) -> None:
        self._connected = False
        self._state = MachineState.DISCONNECTED
        self._position = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
        self._homed = False

    def connect(self) -> None:
        self._connected = True
        self._state = MachineState.IDLE

    def close(self) -> None:
        self._connected = False
        self._state = MachineState.DISCONNECTED
        self._homed = False

    def get_status(self) -> MachineStatus:
        return MachineStatus(
            state=self._state,
            position=self._position.model_copy(deep=True),
            is_homed=self._homed,
            raw="mock",
            last_update_ms_ago=0.0,
        )

    def get_position(self) -> Position:
        return self._position.model_copy(deep=True)

    def is_connected(self) -> bool:
        return self._connected

    def is_homed(self) -> bool:
        return self._homed

    def home(
        self,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> HomeResult | HomePlan:
        del idempotency_key
        if dry_run:
            return HomePlan(
                sequence=["mock: set XYZ position to zero"],
                estimated_duration_s=0.0,
            )
        self._position = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
        self._homed = True
        self._state = MachineState.IDLE
        return HomeResult(
            success=True,
            position_after_pulloff=self._position.model_copy(deep=True),
            duration_ms=0.0,
            event_id=_event_id("gantry.home"),
        )

    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: float | None = None,
        wait_for_idle: bool = True,
        timeout_s: float | None = None,
        dry_run: bool = False,
    ) -> MoveResult | MovePlan:
        del wait_for_idle, timeout_s
        feed = 1000.0 if feed_mm_min is None else feed_mm_min
        current = self._position.model_copy(deep=True)
        distance = (
            (target.x_mm - current.x_mm) ** 2
            + (target.y_mm - current.y_mm) ** 2
            + (target.z_mm - current.z_mm) ** 2
        ) ** 0.5
        if dry_run:
            return MovePlan(
                target=target.model_copy(deep=True),
                feed_mm_min=feed,
                distance_mm=distance,
                estimated_duration_s=(
                    0.0 if distance == 0.0 else distance / (feed / 60.0)
                ),
                current_position=current,
            )
        self._position = target.model_copy(deep=True)
        self._state = MachineState.IDLE
        return MoveResult(
            success=True,
            final_position=self._position.model_copy(deep=True),
            duration_ms=0.0,
            event_id=_event_id("gantry.move_to"),
        )

    def jog(
        self,
        axis: str,
        distance_mm: float,
        feed_mm_min: float = 100.0,
    ) -> Position:
        del feed_mm_min
        axis_upper = axis.upper()
        values = {
            "X": self._position.x_mm,
            "Y": self._position.y_mm,
            "Z": self._position.z_mm,
        }
        if axis_upper not in values:
            raise ValueError(f"mock gantry axis must be X/Y/Z, got {axis!r}")
        values[axis_upper] += distance_mm
        self._position = Position(
            x_mm=values["X"],
            y_mm=values["Y"],
            z_mm=values["Z"],
        )
        self._state = MachineState.IDLE
        return self._position.model_copy(deep=True)

    def recover_from_alarm(
        self,
        *,
        idempotency_key: str,
        skip_rehome: bool = False,
    ) -> RecoveryResult:
        del idempotency_key
        entry_state = self._state
        actions: list[str] = []
        if entry_state != MachineState.IDLE:
            actions.extend(["soft_reset", "unlock_alarm"])
        if not skip_rehome:
            self._position = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
            self._homed = True
            actions.append("home")
        self._state = MachineState.IDLE
        return RecoveryResult(
            success=True,
            entry_state=entry_state,
            actions_taken=actions,
            final_status=self.get_status(),
            duration_ms=0.0,
            event_id=_event_id("gantry.recover_from_alarm"),
        )

    def validate_grbl_settings(self) -> GrblSettingsValidationResult:
        return GrblSettingsValidationResult(
            success=True,
            repaired=False,
            mismatches=[],
            snapshot=GrblSettingsSnapshot(settings={}),
            duration_ms=0.0,
            event_id=_event_id("gantry.validate_grbl_settings"),
        )

    def abort_motion_immediate(self) -> MachineStatus:
        self._homed = False
        self._state = (
            MachineState.IDLE if self._connected else MachineState.DISCONNECTED
        )
        return self.get_status()


class MockRelay:
    """八通道继电器命令缓存。"""

    def __init__(self) -> None:
        self._connected = False
        self._channels = {channel: False for channel in range(1, 9)}

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_state(self) -> RelayState:
        return RelayState(
            channels=dict(self._channels),
            last_update_ms_ago=0.0,
        )

    def ch_on(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> RelayActionResult | RelayActionPlan:
        del idempotency_key
        return self._set_channel(channel, target=True, dry_run=dry_run)

    def ch_off(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> RelayActionResult | RelayActionPlan:
        del idempotency_key
        return self._set_channel(channel, target=False, dry_run=dry_run)

    def _set_channel(
        self,
        channel: int,
        *,
        target: bool,
        dry_run: bool,
    ) -> RelayActionResult | RelayActionPlan:
        if channel not in self._channels:
            raise ValueError(f"mock relay channel must be in [1, 8], got {channel}")
        current = self._channels[channel]
        if dry_run:
            return RelayActionPlan(
                channel=channel,
                target_state=target,
                current_state=current,
                would_write=current != target,
            )
        self._channels[channel] = target
        return RelayActionResult(
            success=True,
            channel=channel,
            state_after=target,
            was_noop=current == target,
            duration_ms=0.0,
            event_id=_event_id("relay.ch_on" if target else "relay.ch_off"),
        )


class MockGripper:
    """只记录最后开合命令的夹爪模型。"""

    def __init__(self) -> None:
        self._commanded_state = GripperCommandedState.UNKNOWN
        self._has_command = False

    def get_state(self) -> GripperState:
        return GripperState(
            commanded_state=self._commanded_state,
            position_known=False,
            last_command_ms_ago=0.0 if self._has_command else None,
        )

    def open(
        self,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> GripperActionResult | GripperActionPlan:
        del idempotency_key
        return self._set_state(GripperCommandedState.OPEN, dry_run=dry_run)

    def close(
        self,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> GripperActionResult | GripperActionPlan:
        del idempotency_key
        return self._set_state(GripperCommandedState.CLOSED, dry_run=dry_run)

    def stop(self) -> GripperState:
        return self.get_state()

    def emergency_release(self) -> GripperActionResult:
        result = self._set_state(GripperCommandedState.OPEN, dry_run=False)
        assert isinstance(result, GripperActionResult)
        return result

    def _set_state(
        self,
        target: GripperCommandedState,
        *,
        dry_run: bool,
    ) -> GripperActionResult | GripperActionPlan:
        current = self._commanded_state
        if dry_run:
            return GripperActionPlan(
                target_state=target,
                current_state=current,
                would_activate_relay=current != target,
                underlying_relay_channel=1,
            )
        self._commanded_state = target
        self._has_command = True
        return GripperActionResult(
            success=True,
            commanded_state_after=target,
            was_noop=current == target,
            duration_ms=0.0,
            event_id=_event_id(f"gripper.{target.value}"),
        )


class MockHeater:
    """PV 会立即追平 SV 的加热台模型。"""

    def __init__(self) -> None:
        self._connected = False
        self._pv_c: float | None = None
        self._sv_c: float | None = None
        self._timestamp: datetime | None = None

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def read_pv(self) -> HeaterStatus:
        return self.status()

    def set_sv(
        self,
        sv_c: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> HeaterActionResult:
        del idempotency_key
        description = f"Mock heater sets SV and PV to {sv_c:g} °C immediately"
        if not dry_run:
            self._sv_c = sv_c
            self._pv_c = sv_c
            self._timestamp = _now()
        return HeaterActionResult(
            success=True,
            target_sv_c=sv_c,
            dry_run=dry_run,
            action_description=description,
            duration_ms=0.0,
            event_id=_event_id("heater.set_sv"),
        )

    def status(self) -> HeaterStatus:
        return HeaterStatus(
            connected=self._connected,
            pv_c=self._pv_c,
            sv_c=self._sv_c,
            timestamp=self._timestamp,
            last_update_ms_ago=(0.0 if self._timestamp is not None else None),
        )


class MockSpincoater:
    """立即启动或停止、无故障的旋涂模型。"""

    def __init__(self) -> None:
        self._connected = False
        self._running = False
        self._target_rpm: float | None = None
        self._brake_engaged: bool | None = None
        self._fault_register: int | None = None
        self._timestamp: datetime | None = None

    def connect(self) -> None:
        self._connected = True
        self._fault_register = 0
        self._timestamp = _now()

    def close(self) -> None:
        self._connected = False

    def start(
        self,
        rpm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        del idempotency_key
        command_value = max(0, min(0xFFFF, round(rpm)))
        if not dry_run:
            self._running = True
            self._target_rpm = rpm
            self._brake_engaged = False
        return SpinActionResult(
            success=True,
            action="start",
            target_rpm=rpm,
            command_value=command_value,
            brake_engaged=False,
            dry_run=dry_run,
            action_description=f"Mock spin starts at {rpm:g} RPM immediately",
            duration_ms=0.0,
            event_id=_event_id("spincoater.start"),
        )

    def stop(
        self,
        *,
        use_brake: bool = True,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        del idempotency_key
        if not dry_run:
            self._running = False
            self._target_rpm = 0.0
            self._brake_engaged = use_brake
        return SpinActionResult(
            success=True,
            action="stop",
            target_rpm=0.0,
            command_value=0,
            brake_engaged=use_brake,
            dry_run=dry_run,
            action_description="Mock spin stops immediately",
            duration_ms=0.0,
            event_id=_event_id("spincoater.stop"),
        )

    def read_fault(self) -> SpinStatus:
        self._fault_register = 0
        self._timestamp = _now()
        return self.status()

    def status(self) -> SpinStatus:
        return SpinStatus(
            connected=self._connected,
            running=self._running,
            target_rpm=self._target_rpm,
            brake_engaged=self._brake_engaged,
            fault_register=self._fault_register,
            fault_bits=[],
            timestamp=self._timestamp,
            last_update_ms_ago=(0.0 if self._timestamp is not None else None),
        )


PipetteAction = Literal["home", "aspirate", "dispense", "eject_tip", "stop"]


class MockPipette:
    """记录命令但不推造 µL 到柱塞 step 换算的移液模型。"""

    def __init__(self) -> None:
        self._connected = False
        self._homed = False
        self._status_word: int | None = 0
        self._position_steps: int | None = 0
        self._tip_present: bool | None = True
        self._timestamp: datetime | None = None
        self._last_action: PipetteAction | None = None
        self._last_volume_ul: float | None = None

    def connect(self) -> None:
        self._connected = True
        self._timestamp = _now()

    def close(self) -> None:
        self._connected = False

    def home(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if not dry_run:
            self._homed = True
            self._status_word = 0
            self._position_steps = 0
            self._timestamp = _now()
            self._last_action = "home"
            self._last_volume_ul = None
        return self._action_result("home", dry_run=dry_run)

    def aspirate(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if not dry_run:
            self._timestamp = _now()
            self._last_action = "aspirate"
            self._last_volume_ul = volume_ul
        return self._action_result(
            "aspirate",
            volume_ul=volume_ul,
            dry_run=dry_run,
        )

    def dispense(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if not dry_run:
            self._timestamp = _now()
            self._last_action = "dispense"
            self._last_volume_ul = volume_ul
        return self._action_result(
            "dispense",
            volume_ul=volume_ul,
            dry_run=dry_run,
        )

    def eject_tip(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if not dry_run:
            self._tip_present = False
            self._timestamp = _now()
            self._last_action = "eject_tip"
            self._last_volume_ul = None
        return self._action_result("eject_tip", dry_run=dry_run)

    def stop(self) -> PipetteActionResult:
        self._homed = False
        self._status_word = 0
        self._timestamp = _now()
        self._last_action = "stop"
        self._last_volume_ul = None
        return self._action_result("stop", dry_run=False)

    def status(self) -> PipetteStatus:
        return PipetteStatus(
            connected=self._connected,
            homed=self._homed,
            status_word=self._status_word,
            driver_fault=False,
            position_steps=self._position_steps,
            tip_present=self._tip_present,
            timestamp=self._timestamp,
            last_update_ms_ago=(0.0 if self._timestamp is not None else None),
        )

    def _action_result(
        self,
        action: PipetteAction,
        *,
        volume_ul: float | None = None,
        dry_run: bool,
    ) -> PipetteActionResult:
        return PipetteActionResult(
            success=True,
            action=action,
            volume_ul=volume_ul,
            dry_run=dry_run,
            action_description=f"Mock pipette {action} completes immediately",
            duration_ms=0.0,
            event_id=_event_id(f"pipette.{action}"),
        )


class MockLinearStage:
    """绝对位置直接写入缓存的丝杆滑台模型。"""

    def __init__(self) -> None:
        self._connected = False
        self._homed = False
        self._moving = False
        self._position_mm: float | None = 0.0
        self._flags_raw: int | None = 0x03
        self._home_status_raw: int | None = 0
        self._timestamp: datetime | None = None

    def connect(self) -> None:
        self._connected = True
        self._timestamp = _now()

    def close(self) -> None:
        self._connected = False
        self._homed = False
        self._moving = False

    def home(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        del idempotency_key
        if not dry_run:
            self._homed = True
            self._moving = False
            self._position_mm = 0.0
            self._timestamp = _now()
        return LinearStageActionResult(
            success=True,
            action="home",
            target_position_mm=0.0,
            final_position_mm=None if dry_run else 0.0,
            dry_run=dry_run,
            action_description="Mock linear stage homes immediately",
            duration_ms=0.0,
            event_id=_event_id("linear_stage.home"),
        )

    def move_to(
        self,
        position_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        del idempotency_key
        if not dry_run:
            self._moving = False
            self._position_mm = position_mm
            self._timestamp = _now()
        return LinearStageActionResult(
            success=True,
            action="move_to",
            target_position_mm=position_mm,
            final_position_mm=None if dry_run else position_mm,
            dry_run=dry_run,
            action_description=(
                f"Mock linear stage moves to {position_mm:g} mm immediately"
            ),
            duration_ms=0.0,
            event_id=_event_id("linear_stage.move_to"),
        )

    def stop(self) -> LinearStageActionResult:
        self._moving = False
        self._timestamp = _now()
        return LinearStageActionResult(
            success=True,
            action="stop",
            target_position_mm=None,
            final_position_mm=self._position_mm,
            dry_run=False,
            action_description="Mock linear stage stops immediately",
            duration_ms=0.0,
            event_id=_event_id("linear_stage.stop"),
        )

    def status(self) -> LinearStageStatus:
        flags = self._flags_raw
        return LinearStageStatus(
            connected=self._connected,
            homed=self._homed,
            moving=self._moving,
            position_mm=self._position_mm,
            flags_raw=flags,
            enabled=None if flags is None else bool(flags & 0x01),
            in_position=None if flags is None else bool(flags & 0x02),
            stalled=None if flags is None else bool(flags & 0x04),
            stall_protection_active=(
                None if flags is None else bool(flags & 0x08)
            ),
            home_status_raw=self._home_status_raw,
            timestamp=self._timestamp,
            last_update_ms_ago=(0.0 if self._timestamp is not None else None),
        )
