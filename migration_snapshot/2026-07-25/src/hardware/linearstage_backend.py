"""ZDT Emm RS485 screw-stage backend on the shared half-duplex bus.

Protocol bytes are copied from the hardware-validated reference snapshot at
``docs/references/bro-linear-stage-20260716/linear_stage.py``:

- lines 108-150 define ``address + function + payload + 0x6B`` frames and
  fixed-tail response validation;
- lines 174-184 define position (0x36), motor flags (0x3A), and native-home
  status (0x3B) reads;
- lines 200-240 define immediate stop and sensorless-home configuration;
- lines 242-267 define native collision homing and its bounded status cycle;
- lines 269-323 define the relative 0xFD payload used to implement absolute
  :meth:`move_to` calls.

Every request or write owns exactly one :meth:`Rs485Bus.transaction`.  Polling
sleeps only after that transaction has ended, so the shared heater/spincoater/
pipette bus is never locked while this stage waits for motion.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, Field

from ..observable import observable
from .errors import ConnectionError as L3ConnectionError
from .errors import L3Error, MachineNotHomedError
from .rs485_bus import Rs485Bus


_DEVICE_NAME = "linear_stage"
_FIXED_CHECKSUM = 0x6B

# Emm command/query bytes, reference lines 174-247 and 269-302.
_READ_POSITION = 0x36
_READ_FLAGS = 0x3A
_READ_HOME_STATUS = 0x3B
_CONFIGURE_HOME = 0x4C
_ENABLE = 0xF3
_TRIGGER_HOME = 0x9A
_MOVE_RELATIVE = 0xFD
_STOP = 0xFE
_RESET_PROTECTION = 0x0E  # 参考 203-204 行：0x0E 0x52 清堵转保护锁存

# 0x3A flag layout used by reference lines 159 and 191-195: bit0 is enabled
# and bit1 is in-position (the reference mock reports enabled | 0x02).  The
# remaining documented motion alarms are stall and stall-protection trip.
_FLAG_ENABLED = 0x01
_FLAG_IN_POSITION = 0x02
_FLAG_STALLED = 0x04
_FLAG_STALL_PROTECTION = 0x08

# 0x3B native-home flags, reference lines 258-264.
_HOME_ACTIVE = 0x04
_HOME_FAILED = 0x08


class LinearStageCommunicationError(L3ConnectionError):
    """Emm stage returned a timeout, malformed frame, or invalid 0x6B tail."""

    error_code = "L3.LINEAR_STAGE_COMMUNICATION"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Verify the Emm stage is powered at this address and 115200 baud, then "
        "reconnect the shared RS485 bus and retry."
    )
    suggested_action_zh = (
        "确认 Emm 滑台已上电、站号和 115200 波特率正确，再重连共享 RS485 总线后重试。"
    )


class LinearStagePositionOutOfRangeError(L3Error):
    """Requested absolute position is outside the configured travel boundary."""

    error_code = "L3.LINEAR_STAGE_POSITION_OUT_OF_RANGE"
    severity = "warning"
    recoverable = False
    suggested_action = (
        "Choose a finite position_mm between 0 and linear_stage.travel_mm."
    )
    suggested_action_zh = "将目标位置改为 0 到 linear_stage.travel_mm 之间的有限数值。"


class LinearStageNotHomedError(MachineNotHomedError):
    """Absolute stage motion was requested before native home succeeded."""

    error_code = "L3.LINEAR_STAGE_NOT_HOMED"
    severity = "warning"
    recoverable = True
    suggested_action = "Call linear_stage.home() before move_to()."
    suggested_action_zh = "先调用滑台 home() 完成碰撞归零，再执行绝对位置移动。"


class LinearStageActionTimeoutError(L3Error):
    """Home or move exceeded its bounded wait and was immediately stopped."""

    error_code = "L3.LINEAR_STAGE_ACTION_TIMEOUT"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Inspect stage power and mechanics, home again if needed, then retry "
        "with a new idempotency key."
    )
    suggested_action_zh = "检查滑台供电与机构；必要时重新归零，再用新的幂等键重试。"

    def __init__(self, action: str, timeout_s: float) -> None:
        self.action = action
        self.timeout_s = timeout_s
        super().__init__(
            human_message=f"丝杆滑台 {action} 在 {timeout_s:g}s 内未完成，已发送立即停止",
            agent_message=(
                f"Linear-stage action {action!r} did not complete within "
                f"{timeout_s:g}s. The backend sent the Emm 0xFE immediate-stop "
                "frame before raising this error."
            ),
        )


class LinearStageFaultError(L3Error):
    """Emm motor flags report disabled power, stall, or stall protection."""

    error_code = "L3.LINEAR_STAGE_FAULT"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Keep the stage stopped, inspect motor power and mechanical obstruction, "
        "then home again before retrying motion."
    )
    suggested_action_zh = "保持滑台停止，检查电机供电和机械堵转，排障后重新归零再移动。"

    def __init__(self, flags_raw: int, fault_reasons: tuple[str, ...]) -> None:
        self.flags_raw = flags_raw
        self.fault_reasons = fault_reasons
        decoded = ", ".join(fault_reasons) if fault_reasons else "unknown flag fault"
        super().__init__(
            human_message=f"丝杆滑台状态异常 flags=0x{flags_raw:02X}（{decoded}），已停止",
            agent_message=(
                f"Emm motor flags 0x{flags_raw:02X} indicate: {decoded}. "
                "The backend sent the immediate-stop frame before raising."
            ),
        )


class LinearStageHomingError(L3Error):
    """Emm native sensorless-home status reports an explicit failure flag."""

    error_code = "L3.LINEAR_STAGE_HOMING_FAILED"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Inspect the collision-homing direction, speed, current, and obstruction; "
        "then retry home with a new idempotency key."
    )
    suggested_action_zh = "检查碰撞归零方向、速度、电流和机械阻挡，再用新的幂等键重试。"

    def __init__(
        self, home_status_raw: int, *, position_mm: float | None = None
    ) -> None:
        self.home_status_raw = home_status_raw
        self.position_mm = position_mm
        if position_mm is None:
            human = f"丝杆滑台原生碰撞归零失败，状态 0x{home_status_raw:02X}"
            agent = (
                f"Emm native-home status 0x{home_status_raw:02X} has failure "
                "bit 0x08 set. The stage remains not homed."
            )
        else:
            # 闭环假设校验失败：归零流程报完成，但设备 0x36 位置计数没有回零。
            human = (
                f"丝杆滑台归零后位置计数未回零（读到 {position_mm:.3f} mm），"
                "闭环等待无法工作"
            )
            agent = (
                f"Native homing reported completion but the 0x36 position "
                f"counter reads {position_mm:.3f} mm (beyond tolerance). "
                "Closed-loop move waits would use a wrong origin; the stage "
                "remains not homed."
            )
        super().__init__(human_message=human, agent_message=agent)


@dataclass(frozen=True)
class LinearStageConfig:
    """Emm geometry, protocol timing, and hard travel/motion boundaries."""

    travel_mm: float
    baudrate: int = 115200
    timeout_s: float = 0.5
    lead_mm: float = 2.0
    microsteps: int = 16
    motor_step_deg: float = 1.8
    default_speed_rpm: int = 2000
    default_acceleration: int = 150
    home_direction: int = 1
    home_speed_rpm: int = 300
    sensorless_timeout_ms: int = 10000
    collision_rpm: int = 300
    collision_current_ma: int = 800
    collision_time_ms: int = 60
    home_timeout_s: float = 35.0
    move_timeout_s: float = 15.0
    position_tolerance_mm: float = 0.25
    poll_interval_s: float = 0.1


class LinearStageStatus(BaseModel):
    """Latest cached Emm position/flag snapshot; :meth:`status` performs no I/O."""

    connected: bool
    homed: bool
    moving: bool
    position_mm: float | None = Field(
        default=None,
        description="Latest signed position decoded from Emm query 0x36.",
    )
    flags_raw: int | None = Field(
        default=None,
        ge=0,
        le=0xFF,
        description="Latest raw enable/in-position/stall flags from query 0x3A.",
    )
    enabled: bool | None = None
    in_position: bool | None = None
    stalled: bool | None = None
    stall_protection_active: bool | None = None
    home_status_raw: int | None = Field(
        default=None,
        ge=0,
        le=0xFF,
        description="Latest native sensorless-home status from query 0x3B.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest successful live feedback read.",
    )
    last_update_ms_ago: float | None = Field(
        default=None,
        ge=0,
        description="Age of the latest successful live feedback read.",
    )


class LinearStageActionResult(BaseModel):
    """Result or dry-run description returned by a stage physical action."""

    success: bool
    action: Literal["home", "move_to", "stop"]
    target_position_mm: float | None = None
    final_position_mm: float | None = None
    dry_run: bool
    action_description: str
    duration_ms: float = Field(ge=0)
    event_id: str


class LinearStageBackend:
    """Agent-facing absolute-position backend for one ZDT Emm screw stage."""

    def __init__(
        self,
        bus: Rs485Bus,
        address: int,
        config: LinearStageConfig,
        controller_adapter: Any | None = None,
    ) -> None:
        if not 1 <= address <= 0xFF:
            raise ValueError(f"address must be in [1, 255], got {address}")
        finite_positive = (
            ("travel_mm", config.travel_mm),
            ("timeout_s", config.timeout_s),
            ("lead_mm", config.lead_mm),
            ("motor_step_deg", config.motor_step_deg),
            ("home_timeout_s", config.home_timeout_s),
            ("move_timeout_s", config.move_timeout_s),
            ("position_tolerance_mm", config.position_tolerance_mm),
            ("poll_interval_s", config.poll_interval_s),
        )
        for name, value in finite_positive:
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive, got {value!r}")
        if config.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        if config.position_tolerance_mm > config.travel_mm:
            raise ValueError("position_tolerance_mm must not exceed travel_mm")
        if config.microsteps <= 0:
            raise ValueError("microsteps must be positive")
        if not 1 <= config.default_speed_rpm <= 3000:
            raise ValueError("default_speed_rpm must be in [1, 3000]")
        if not 0 <= config.default_acceleration <= 0xFF:
            raise ValueError("default_acceleration must be in [0, 255]")
        if config.home_direction not in (0, 1):
            raise ValueError("home_direction must be 0 (CW) or 1 (CCW)")
        if not 0 <= config.home_speed_rpm <= 300:
            raise ValueError("home_speed_rpm must be in [0, 300]")
        if not 0 <= config.collision_rpm <= 300:
            raise ValueError("collision_rpm must be in [0, 300]")
        for name, value, maximum in (
            ("sensorless_timeout_ms", config.sensorless_timeout_ms, 0xFFFFFFFF),
            ("collision_current_ma", config.collision_current_ma, 0xFFFF),
            ("collision_time_ms", config.collision_time_ms, 0xFFFF),
        ):
            if not 0 <= value <= maximum:
                raise ValueError(f"{name} must be in [0, {maximum}], got {value}")

        pulses_at_limit = round(config.travel_mm * self._pulses_per_mm_for(config))
        if not 0 <= pulses_at_limit <= 0xFFFFFFFF:
            raise ValueError("travel_mm cannot be encoded in the Emm 32-bit pulse field")

        self._bus = bus
        self._address = address
        self._config = config
        self._controller_adapter = controller_adapter
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False
        self._homed = False
        self._moving = False
        self._position_mm: float | None = None
        self._flags_raw: int | None = None
        self._home_status_raw: int | None = None
        self._status_timestamp: datetime | None = None
        self._status_monotonic: float | None = None

    @property
    def pulses_per_mm(self) -> float:
        """Pulse conversion copied from reference lines 59-61."""
        return self._pulses_per_mm_for(self._config)

    def connect(self) -> None:
        """Connect idempotently and read flags plus position to verify online."""
        if self._controller_adapter is not None:
            with self._lifecycle_lock:
                with self._state_lock:
                    if self._connected:
                        return
                self._controller_adapter.connect()
                self._sync_from_adapter_status(self._controller_adapter.status())
                with self._state_lock:
                    self._connected = True
                    self._homed = False
                    self._moving = False
                return

        with self._lifecycle_lock:
            with self._state_lock:
                if self._connected:
                    return
            self._bus.connect()
            with self._state_lock:
                self._connected = True
                # A new transport session cannot prove that native home occurred
                # since power-up; only this backend's successful home() may set it.
                self._homed = False
                self._moving = False
            try:
                self._read_flags()
                self._read_position_mm()
            except L3ConnectionError:
                self._mark_disconnected()
                raise

    def close(self) -> None:
        """Mark this device disconnected; never close the shared RS485 bus."""
        if self._controller_adapter is not None:
            with self._lifecycle_lock:
                self._controller_adapter.close()
                with self._state_lock:
                    self._connected = False
                    self._homed = False
                    self._moving = False
                return

        with self._lifecycle_lock:
            with self._state_lock:
                self._connected = False
                self._homed = False
                self._moving = False

    def disconnect(self) -> None:
        self.close()

    def shutdown(self) -> None:
        self.close()

    @observable(idempotency_ttl_s=24 * 3600)
    def home(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        """Run native collision home; any failure resets the trusted homed flag."""
        action_description = (
            f"Configure native collision home direction={self._config.home_direction}, "
            f"speed={self._config.home_speed_rpm} RPM, enable the motor, trigger "
            "0x9A, and wait for home-active then home-idle"
        )
        if dry_run:
            return self._action_result(
                action="home",
                target_position_mm=0.0,
                final_position_mm=None,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
            )

        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.home(
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            with self._state_lock:
                self._homed = True
                self._moving = False
            return result

        self._ensure_connected()
        started = time.monotonic()
        deadline = started + self._config.home_timeout_s
        with self._state_lock:
            self._homed = False
            self._moving = True
        try:
            # 归零自带恢复语义：先清堵转保护锁存位（参考 203-204 行 0x0E 0x52）。
            # 否则真机碰撞触发保护后，公开 API 没有任何解除路径（审查 P1-2）。
            # 参考同款只写不读 ack；随后睡一拍，让可能存在的迟到 ack 先到达、
            # 被下一次事务的 reset_input_buffer 清掉。
            self._write_only(self._frame(_RESET_PROTECTION, bytes([0x52])))
            self._sleep_until_next_poll(deadline)
            self._configure_sensorless_home()
            self._enable_for_motion(
                deadline, action="home", timeout_s=self._config.home_timeout_s
            )
            # Reference line 247: trigger is write-only.
            self._write_only(self._frame(_TRIGGER_HOME, bytes([0x02, 0x00])))
            self._wait_for_home(deadline)
            # 闭环假设校验（审查 P2-2）：本 backend 的 move 等待拿 0x36 对目标
            # 比对，前提是原生归零把设备位置计数清零。真机若不清零，这里立即
            # 暴露，而不是让之后每次 move 都 15s 超时。W5 真机 gate 首验项。
            final_position = self._read_position_mm()
            if abs(final_position) > self._config.position_tolerance_mm:
                raise LinearStageHomingError(0, position_mm=final_position)
        except Exception as exc:
            with self._state_lock:
                self._homed = False
                self._moving = False
            if isinstance(exc, L3ConnectionError):
                self._mark_disconnected()
            raise

        with self._state_lock:
            self._homed = True
            self._moving = False
            self._position_mm = final_position
            self._touch_status_locked()
        return self._action_result(
            action="home",
            target_position_mm=0.0,
            final_position_mm=final_position,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    @observable
    def move_to(
        self,
        position_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        """Move absolutely and wait for position tolerance with live fault checks."""
        target = self._validate_position(position_mm)
        dry_description = (
            f"Move Emm stage to absolute {target:g} mm at "
            f"{self._config.default_speed_rpm} RPM and wait within "
            f"±{self._config.position_tolerance_mm:g} mm"
        )
        if dry_run:
            return self._action_result(
                action="move_to",
                target_position_mm=target,
                final_position_mm=None,
                dry_run=True,
                action_description=dry_description,
                duration_ms=0.0,
            )

        if self._controller_adapter is not None:
            self._ensure_connected()
            self._ensure_homed()
            result = self._controller_adapter.move_to(
                target,
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result

        self._ensure_connected()
        self._ensure_homed()
        with self._state_lock:
            current = self._position_mm
        if current is None:
            current = self._read_position_mm()

        distance = target - current
        if abs(distance) <= self._config.position_tolerance_mm:
            return self._action_result(
                action="move_to",
                target_position_mm=target,
                final_position_mm=current,
                dry_run=False,
                action_description=(
                    f"Already within ±{self._config.position_tolerance_mm:g} mm of "
                    f"absolute target {target:g} mm; no motion frame was needed"
                ),
                duration_ms=0.0,
            )

        pulses = round(abs(distance) * self.pulses_per_mm)
        direction = 0x00 if distance >= 0 else 0x01
        payload = bytes([direction])
        payload += self._config.default_speed_rpm.to_bytes(2, "big")
        payload += bytes([self._config.default_acceleration])
        payload += pulses.to_bytes(4, "big") + bytes([0x00, 0x00])
        move_frame = self._frame(_MOVE_RELATIVE, payload)
        action_description = (
            f"Move from {current:g} to {target:g} mm using direction={direction}, "
            f"speed={self._config.default_speed_rpm} RPM, pulses={pulses}; then "
            f"poll position and flags within ±{self._config.position_tolerance_mm:g} mm"
        )

        started = time.monotonic()
        deadline = started + self._config.move_timeout_s
        with self._state_lock:
            self._moving = True
        try:
            # 参考 297-298 行：每次移动前重新使能（审查 P2-1）——电机中途失能
            # 时在发运动帧之前暴露，而不是发一条无效指令后靠轮询超时兜底。
            self._enable_for_motion(
                deadline, action="move_to", timeout_s=self._config.move_timeout_s
            )
            # Reference lines 293-300: 0xFD uses a four-byte acknowledgement.
            self._command(move_frame, expected_response_length=4)
            final_position = self._wait_for_position(target, deadline)
        except Exception as exc:
            with self._state_lock:
                self._moving = False
            if isinstance(exc, L3ConnectionError):
                self._mark_disconnected()
            raise

        with self._state_lock:
            self._moving = False
        return self._action_result(
            action="move_to",
            target_position_mm=target,
            final_position_mm=final_position,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def move_absolute(
        self,
        position_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        return self.move_to(
            position_mm,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def move_relative(
        self,
        delta_mm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> LinearStageActionResult:
        if not math.isfinite(delta_mm):
            raise LinearStagePositionOutOfRangeError(
                human_message=f"丝杆滑台相对位移 {delta_mm!r} mm 无效",
                agent_message=f"Requested relative move {delta_mm!r} mm is not finite.",
            )
        if dry_run:
            with self._state_lock:
                current = self._position_mm
            target = None if current is None else current + float(delta_mm)
            if target is not None:
                self._validate_position(target)
            return self._action_result(
                action="move_to",
                target_position_mm=target,
                final_position_mm=None,
                dry_run=True,
                action_description=f"Dry-run relative move by {delta_mm:g} mm.",
                duration_ms=0.0,
            )
        if self._controller_adapter is not None:
            self._ensure_connected()
            self._ensure_homed()
            result = self._controller_adapter.move_relative(
                delta_mm,
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result
        self._ensure_connected()
        self._ensure_homed()
        with self._state_lock:
            current = self._position_mm
        if current is None:
            current = self._read_position_mm()
        return self.move_to(
            current + float(delta_mm),
            idempotency_key=idempotency_key,
            dry_run=False,
        )

    @observable
    def stop(self) -> LinearStageActionResult:
        """Send the immediate 0xFE stop frame; this method has no idem cache key."""
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.stop()
            self._sync_from_adapter_status(self._controller_adapter.status())
            with self._state_lock:
                self._moving = False
            return result

        self._ensure_connected()
        started = time.monotonic()
        try:
            self._send_stop_frame()
        except L3ConnectionError:
            self._mark_disconnected()
            raise
        with self._state_lock:
            final_position = self._position_mm
            self._moving = False
        return self._action_result(
            action="stop",
            target_position_mm=None,
            final_position_mm=final_position,
            dry_run=False,
            action_description="Send Emm immediate-stop frame 0xFE 0x98 0x00",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def status(self) -> LinearStageStatus:
        """Return the latest cached snapshot without touching the RS485 bus."""
        with self._state_lock:
            connected = self._connected
            homed = self._homed
            moving = self._moving
            position_mm = self._position_mm
            flags_raw = self._flags_raw
            home_status_raw = self._home_status_raw
            timestamp = self._status_timestamp
            last_monotonic = self._status_monotonic

        age_ms = (
            None
            if last_monotonic is None
            else max(0.0, (time.monotonic() - last_monotonic) * 1000.0)
        )
        return LinearStageStatus(
            connected=connected,
            homed=homed,
            moving=moving,
            position_mm=position_mm,
            flags_raw=flags_raw,
            enabled=None if flags_raw is None else bool(flags_raw & _FLAG_ENABLED),
            in_position=(
                None if flags_raw is None else bool(flags_raw & _FLAG_IN_POSITION)
            ),
            stalled=None if flags_raw is None else bool(flags_raw & _FLAG_STALLED),
            stall_protection_active=(
                None
                if flags_raw is None
                else bool(flags_raw & _FLAG_STALL_PROTECTION)
            ),
            home_status_raw=home_status_raw,
            timestamp=timestamp,
            last_update_ms_ago=age_ms,
        )

    def get_status(self) -> LinearStageStatus:
        return self.status()

    def get_position(self) -> float | None:
        if self._controller_adapter is not None:
            self._ensure_connected()
            status = self._controller_adapter.status()
            self._sync_from_adapter_status(status)
            return status.position_mm
        self._ensure_connected()
        return self._read_position_mm()

    def reset_protection(self) -> LinearStageActionResult:
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.reset_protection()
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result
        self._ensure_connected()
        started = time.monotonic()
        self._write_only(self._frame(_RESET_PROTECTION, bytes([0x52])))
        return self._action_result(
            action="stop",
            target_position_mm=None,
            final_position_mm=self.status().position_mm,
            dry_run=False,
            action_description="Sent Emm reset-protection frame 0x0E 0x52.",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def _sync_from_adapter_status(self, status: LinearStageStatus) -> None:
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            self._connected = status.connected
            self._homed = status.homed
            self._moving = status.moving
            self._position_mm = status.position_mm
            self._flags_raw = status.flags_raw
            self._home_status_raw = status.home_status_raw
            self._status_timestamp = status.timestamp or now_utc
            self._status_monotonic = now_monotonic

    def _ensure_connected(self) -> None:
        with self._state_lock:
            connected = self._connected
        if not connected:
            raise L3ConnectionError(
                human_message="丝杆滑台未连接",
                agent_message=(
                    "LinearStageBackend is disconnected; call connect() before retrying."
                ),
            )

    def _ensure_homed(self) -> None:
        with self._state_lock:
            homed = self._homed
        if not homed:
            raise LinearStageNotHomedError(
                human_message="丝杆滑台尚未归零，请先执行 home",
                agent_message=(
                    "Linear-stage homed=false; call "
                    "linear_stage.home(idempotency_key=...) before move_to(). "
                    "No RS485 motion bytes were sent."
                ),
            )

    def _mark_disconnected(self) -> None:
        with self._state_lock:
            self._connected = False
            self._homed = False
            self._moving = False

    def _validate_position(self, position_mm: float) -> float:
        if (
            not math.isfinite(position_mm)
            or position_mm < 0.0
            or position_mm > self._config.travel_mm
        ):
            raise LinearStagePositionOutOfRangeError(
                human_message=(
                    f"丝杆滑台目标 {position_mm!r} mm 超出 [0, "
                    f"{self._config.travel_mm:g}] mm"
                ),
                agent_message=(
                    f"Requested linear-stage position {position_mm!r} mm is not "
                    f"finite or outside [0, {self._config.travel_mm:g}]. "
                    "The request was rejected; no RS485 bytes were sent."
                ),
            )
        return float(position_mm)

    def _configure_sensorless_home(self) -> None:
        # Reference lines 233-240, byte-for-byte.
        payload = bytes([0xAE, 0x00, 0x02, self._config.home_direction])
        payload += self._config.home_speed_rpm.to_bytes(2, "big")
        payload += self._config.sensorless_timeout_ms.to_bytes(4, "big")
        payload += self._config.collision_rpm.to_bytes(2, "big")
        payload += self._config.collision_current_ma.to_bytes(2, "big")
        payload += self._config.collision_time_ms.to_bytes(2, "big")
        payload += bytes([0x00])
        self._command(
            self._frame(_CONFIGURE_HOME, payload),
            expected_response_length=4,
        )

    def _enable_for_motion(
        self, deadline: float, *, action: str, timeout_s: float
    ) -> None:
        # Reference lines 186-198: enable is write-only, then query 0x3A up to
        # five times.  Each query is a new transaction; sleeps hold no bus lock.
        self._write_only(self._frame(_ENABLE, bytes([0xAB, 0x01, 0x00])))
        last_flags = 0
        for _ in range(5):
            self._sleep_until_next_poll(deadline)
            if time.monotonic() >= deadline:
                self._stop_after_timeout(action, timeout_s)
            last_flags = self._read_flags()
            if last_flags & _FLAG_ENABLED:
                reasons = self._fault_reasons(last_flags, require_enabled=False)
                if reasons:
                    self._stop_after_fault(last_flags, reasons)
                return
        self._stop_after_fault(
            last_flags,
            ("motor_disabled_or_power_lost",),
        )

    def _wait_for_home(self, deadline: float) -> None:
        # Reference lines 252-267: completion is active -> inactive; an initial
        # inactive sample is not accepted as a completed home.
        action_started = False
        while True:
            # 参考 255-257 行：先睡一拍再读——0x9A 触发若有迟到 ack，会在这一
            # 拍内到达、被下一次事务的 reset_input_buffer 清掉（审查 P1-1：
            # 触发后立即读 0x3B 会把迟到 ack 当响应，撞上严格功能码校验）。
            self._sleep_until_next_poll(deadline)
            home_status = self._read_home_status()
            if home_status & _HOME_FAILED:
                raise LinearStageHomingError(home_status)
            if home_status & _HOME_ACTIVE:
                action_started = True
            elif action_started:
                return
            if time.monotonic() >= deadline:
                self._stop_after_timeout("home", self._config.home_timeout_s)

    def _wait_for_position(self, target: float, deadline: float) -> float:
        while True:
            # Reference get_status lines 339-345 reads position before flags.
            # Each helper opens and releases its own transaction.
            position = self._read_position_mm()
            flags = self._read_flags()
            reasons = self._fault_reasons(flags, require_enabled=True)
            if reasons:
                self._stop_after_fault(flags, reasons)
            if abs(position - target) <= self._config.position_tolerance_mm:
                return position
            if time.monotonic() >= deadline:
                self._stop_after_timeout("move_to", self._config.move_timeout_s)
            self._sleep_until_next_poll(deadline)

    def _read_position_mm(self) -> float:
        response = self._command(
            self._frame(_READ_POSITION),
            expected_response_length=8,
        )
        sign = -1.0 if response[2] else 1.0
        raw = int.from_bytes(response[3:7], "big")
        position = sign * raw / 65536.0 * self._config.lead_mm
        with self._state_lock:
            self._position_mm = position
            self._touch_status_locked()
        return position

    def _read_flags(self) -> int:
        response = self._command(
            self._frame(_READ_FLAGS),
            expected_response_length=4,
        )
        flags = response[2]
        with self._state_lock:
            self._flags_raw = flags
            self._touch_status_locked()
        return flags

    def _read_home_status(self) -> int:
        response = self._command(
            self._frame(_READ_HOME_STATUS),
            expected_response_length=4,
        )
        home_status = response[2]
        with self._state_lock:
            self._home_status_raw = home_status
            self._touch_status_locked()
        return home_status

    def _send_stop_frame(self) -> None:
        # Reference lines 200-201: write-only 0xFE 0x98 0x00.
        self._write_only(self._frame(_STOP, bytes([0x98, 0x00])))
        with self._state_lock:
            self._moving = False

    def _stop_after_timeout(self, action: str, timeout_s: float) -> NoReturn:
        self._send_stop_frame()
        raise LinearStageActionTimeoutError(action, timeout_s)

    def _stop_after_fault(
        self,
        flags_raw: int,
        fault_reasons: tuple[str, ...],
    ) -> NoReturn:
        self._send_stop_frame()
        raise LinearStageFaultError(flags_raw, fault_reasons)

    @staticmethod
    def _fault_reasons(flags: int, *, require_enabled: bool) -> tuple[str, ...]:
        reasons: list[str] = []
        if require_enabled and not flags & _FLAG_ENABLED:
            reasons.append("motor_disabled_or_power_lost")
        if flags & _FLAG_STALLED:
            reasons.append("motor_stalled")
        if flags & _FLAG_STALL_PROTECTION:
            reasons.append("stall_protection_active")
        return tuple(reasons)

    def _frame(self, function: int, payload: bytes = b"") -> bytes:
        # Reference lines 108-109.
        return bytes([self._address, function]) + payload + bytes([_FIXED_CHECKSUM])

    def _write_only(self, request: bytes) -> None:
        self._exchange(request, expected_response_length=0)

    def _command(self, request: bytes, *, expected_response_length: int) -> bytes:
        response = self._exchange(
            request,
            expected_response_length=expected_response_length,
        )
        if expected_response_length == 4 and response[2] == 0xE2:
            self._raise_protocol_error(
                f"motor rejected parameters (E2) for request {request.hex()}"
            )
        if expected_response_length == 4 and response[2] == 0xEE:
            self._raise_protocol_error(
                f"motor rejected command format (EE) for request {request.hex()}"
            )
        return response

    def _exchange(self, request: bytes, *, expected_response_length: int) -> bytes:
        response = b""
        try:
            with self._bus.transaction(
                _DEVICE_NAME,
                self._config.baudrate,
                timeout_s=self._config.timeout_s,
            ) as serial_port:
                serial_port.reset_input_buffer()
                written = serial_port.write(request)
                if written != len(request):
                    self._raise_protocol_error(
                        f"short serial write: expected {len(request)} bytes, "
                        f"wrote {written}"
                    )
                serial_port.flush()
                if expected_response_length:
                    response = serial_port.read(expected_response_length)
        except (TimeoutError, OSError) as exc:
            raise LinearStageCommunicationError(
                human_message="丝杆滑台 RS485 通信超时或串口异常",
                agent_message=(
                    f"Emm stage address {self._address} failed during one RS485 "
                    f"request-response exchange: {exc!r}."
                ),
            ) from exc

        if expected_response_length == 0:
            return b""
        if len(response) != expected_response_length:
            raise LinearStageCommunicationError(
                human_message="丝杆滑台 RS485 响应超时或帧长度错误",
                agent_message=(
                    f"Emm stage address {self._address} returned {len(response)} "
                    f"bytes; expected {expected_response_length}. Partial frame="
                    f"{response.hex()!r}."
                ),
            )
        if response[0] != self._address:
            self._raise_protocol_error(
                f"response address {response[0]} did not match {self._address}"
            )
        if response[1] != request[1]:
            self._raise_protocol_error(
                f"response function 0x{response[1]:02X} did not match "
                f"0x{request[1]:02X}"
            )
        if response[-1] != _FIXED_CHECKSUM:
            self._raise_protocol_error(
                f"invalid fixed checksum tail in frame {response.hex()}"
            )
        return response

    def _raise_protocol_error(self, detail: str) -> NoReturn:
        raise LinearStageCommunicationError(
            human_message="丝杆滑台 RS485 协议响应无效",
            agent_message=(
                f"Emm stage address {self._address} protocol error: {detail}."
            ),
        )

    def _sleep_until_next_poll(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(self._config.poll_interval_s, remaining))

    def _touch_status_locked(self) -> None:
        self._status_monotonic = time.monotonic()
        self._status_timestamp = datetime.now(timezone.utc)

    @staticmethod
    def _pulses_per_mm_for(config: LinearStageConfig) -> float:
        return (360.0 / config.motor_step_deg * config.microsteps) / config.lead_mm

    @staticmethod
    def _action_result(
        *,
        action: Literal["home", "move_to", "stop"],
        target_position_mm: float | None,
        final_position_mm: float | None,
        dry_run: bool,
        action_description: str,
        duration_ms: float,
    ) -> LinearStageActionResult:
        return LinearStageActionResult(
            success=True,
            action=action,
            target_position_mm=target_position_mm,
            final_position_mm=final_position_mm,
            dry_run=dry_run,
            action_description=action_description,
            duration_ms=duration_ms,
            event_id="",
        )
