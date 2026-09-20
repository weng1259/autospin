"""28-series electric-pipette backend over the shared Modbus RTU bus.

The protocol follows the authoritative legacy behavior source at
``AutoSpinmotorSystem/hardware/pipette/pipette_controller.py``:

- lines 59-89 define the corrected one-based action codes and the reviewed
  50/1250/1250 motion defaults;
- lines 170-180 read the signed 32-bit position atomically;
- lines 253-292 trigger home with ``IDLE -> HOME`` and stop on timeout;
- lines 294-366 and 378-385 define aspirate, dispense, and tip-eject writes.

Every Modbus request uses one :meth:`Rs485Bus.transaction`.  Polling sleeps
only after that transaction has ended so a long pipette action never owns the
shared RS485 lock while waiting.
"""
from __future__ import annotations

import math
import struct
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


_DEVICE_NAME = "pipette"
_READ_INPUT_REGISTERS = 0x04
_WRITE_SINGLE_REGISTER = 0x06
_WRITE_MULTIPLE_REGISTERS = 0x10

# Input registers, pipette_controller.py:14-29.
_REG_STATUS = 0x00
_REG_HOMED = 0x01
_REG_DRIVER_FAULT = 0x02
_REG_POS_H = 0x03
_REG_ASPIRATE_STATE = 0x09
_REG_DISPENSE_STATE = 0x0A
_REG_TIP_PRESENT = 0x0D

# Holding registers, pipette_controller.py:31-50.
_REG_CTRL = 0x00
_REG_VELOCITY = 0x03
_REG_ACCELERATION = 0x04
_REG_DECELERATION = 0x05
_REG_VOLUME_H = 0x06

# Corrected action codes, pipette_controller.py:59-79.  The low half is
# one-based: 0x00 is idle/no command, not home.  Hardware gate 2026-06-29
# verified HOME=0x01 and IMM_STOP=0x08.
_ACTION_IDLE = 0x00
_ACTION_HOME = 0x01
_ACTION_IMMEDIATE_STOP = 0x08
_ACTION_ASPIRATE = 0x0A
_ACTION_DISPENSE = 0x0B
_ACTION_EJECT_TIP = 0x0C
_TIP_ABSENCE_CONFIRM_READS = 3

# Manual-recommended values, pipette_controller.py:82-89.  The legacy
# 10/20/20 values made a real home exceed its 30-second timeout.
_DEFAULT_SPEED_01RPS = 50
_DEFAULT_ACCEL_01RPSS = 1250
_DEFAULT_DECEL_01RPSS = 1250


class PipetteCommunicationError(L3ConnectionError):
    """Pipette returned a timeout, malformed frame, CRC error, or exception."""

    error_code = "L3.PIPETTE_COMMUNICATION"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Verify the pipette is powered and configured for this unit id and "
        "115200 baud, then reconnect the shared RS485 bus and retry."
    )
    suggested_action_zh = (
        "确认移液枪已上电、站号和 115200 波特率正确，再重连共享 RS485 总线后重试。"
    )


class PipetteVolumeOutOfRangeError(L3Error):
    """Requested volume exceeds the configured pipette safety boundary."""

    error_code = "L3.PIPETTE_VOLUME_OUT_OF_RANGE"
    severity = "warning"
    recoverable = False
    suggested_action = (
        "Choose a finite volume between 0 and pipette.max_volume_ul."
    )
    suggested_action_zh = "将体积改为 0 到 pipette.max_volume_ul 之间的有限数值。"


class PipetteNotHomedError(MachineNotHomedError):
    """Aspirate or dispense was requested before pipette home completed."""

    error_code = "L3.PIPETTE_NOT_HOMED"
    severity = "warning"
    recoverable = True
    suggested_action = "Call pipette.home() before aspirate() or dispense()."
    suggested_action_zh = "先调用移液枪 home() 完成归位，再执行吸液或吐液。"


class PipetteTipMissingError(L3Error):
    """A liquid-transfer action was requested without a detected pipette tip."""

    error_code = "L3.PIPETTE_TIP_MISSING"
    severity = "warning"
    recoverable = True
    suggested_action = "Install a compatible tip, verify tip_present, then retry."
    suggested_action_zh = "安装兼容吸头并确认 tip_present 后重试。"


class PipetteActionTimeoutError(L3Error):
    """A pipette action exceeded its bounded wait and was immediately stopped."""

    error_code = "L3.PIPETTE_ACTION_TIMEOUT"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Inspect the pipette mechanism and sensor state, call home(), then retry "
        "the action with a new idempotency key."
    )
    suggested_action_zh = "检查移液机构和传感器，重新归位后用新的幂等键重试。"

    def __init__(self, action: str, timeout_s: float) -> None:
        self.action = action
        self.timeout_s = timeout_s
        super().__init__(
            human_message=f"移液枪 {action} 在 {timeout_s:g}s 内未完成，已发送立即停止",
            agent_message=(
                f"Pipette action {action!r} did not complete within {timeout_s:g}s. "
                "The backend sent CTRL=0x08 (IMM_STOP) before raising this error."
            ),
        )


@dataclass(frozen=True)
class PipetteConfig:
    """28-series protocol timing, motion defaults, and volume hard limit."""

    max_volume_ul: float
    baudrate: int = 115200
    timeout_s: float = 2.0
    home_timeout_s: float = 30.0
    action_timeout_s: float = 10.0
    poll_interval_s: float = 0.1
    speed_01rps: int = _DEFAULT_SPEED_01RPS
    accel_01rpss: int = _DEFAULT_ACCEL_01RPSS
    decel_01rpss: int = _DEFAULT_DECEL_01RPSS


class PipetteStatus(BaseModel):
    """Latest cached pipette snapshot; :meth:`status` performs no bus I/O."""

    connected: bool
    homed: bool
    status_word: int | None = Field(
        default=None,
        ge=0,
        le=0xFFFF,
        description="Latest raw motion-state input register (0 means idle).",
    )
    driver_fault: bool | None = Field(
        default=None,
        description="Latest input-register driver-fault flag.",
    )
    position_steps: int | None = Field(
        default=None,
        ge=-(2**31),
        le=2**31 - 1,
        description="Latest atomically read signed 32-bit plunger position.",
    )
    tip_present: bool | None = Field(
        default=None,
        description="Latest input-register tip-presence flag.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest successful status snapshot.",
    )
    last_update_ms_ago: float | None = Field(
        default=None,
        ge=0,
        description="Age of the latest successful status snapshot.",
    )


class PipetteActionResult(BaseModel):
    """Result or dry-run description returned by a pipette physical action."""

    success: bool
    action: Literal[
        "home",
        "aspirate",
        "dispense",
        "eject_tip",
        "blowout",
        "liquid_detect",
        "reset",
        "stop",
    ]
    volume_ul: float | None = Field(
        default=None,
        ge=0,
        description="Requested volume for liquid actions, in microlitres.",
    )
    dry_run: bool
    action_description: str
    duration_ms: float = Field(ge=0)
    event_id: str


def _crc16(data: bytes) -> int:
    """Return the standard Modbus RTU CRC-16 value for *data*."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _append_crc(payload: bytes) -> bytes:
    return payload + struct.pack("<H", _crc16(payload))


class PipetteBackend:
    """Agent-facing 28-series pipette backend with bounded physical actions."""

    def __init__(
        self,
        bus: Rs485Bus,
        unit_id: int,
        config: PipetteConfig,
        controller_adapter: Any | None = None,
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError(f"unit_id must be in [1, 247], got {unit_id}")
        if (
            config.baudrate <= 0
            or not math.isfinite(config.max_volume_ul)
            or config.max_volume_ul <= 0
            or config.max_volume_ul > 0xFFFFFFFF
            or not math.isfinite(config.timeout_s)
            or config.timeout_s <= 0
            or not math.isfinite(config.home_timeout_s)
            or config.home_timeout_s <= 0
            or not math.isfinite(config.action_timeout_s)
            or config.action_timeout_s <= 0
            or not math.isfinite(config.poll_interval_s)
            or config.poll_interval_s <= 0
        ):
            raise ValueError(
                "pipette baudrate, max_volume_ul, timeouts, and poll interval "
                "must be finite and positive"
            )
        for name, value in (
            ("speed_01rps", config.speed_01rps),
            ("accel_01rpss", config.accel_01rpss),
            ("decel_01rpss", config.decel_01rpss),
        ):
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"{name} must be in [0, 65535], got {value}")

        self._bus = bus
        self._unit_id = unit_id
        self._config = config
        self._controller_adapter = controller_adapter
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False
        self._homed = False
        self._status_word: int | None = None
        self._driver_fault: bool | None = None
        self._position_steps: int | None = None
        self._tip_present: bool | None = None
        self._status_timestamp: datetime | None = None
        self._status_monotonic: float | None = None

    def connect(self) -> None:
        """Connect idempotently and read one complete snapshot to verify online."""
        if self._controller_adapter is not None:
            with self._lifecycle_lock:
                with self._state_lock:
                    if self._connected:
                        return
                self._controller_adapter.connect()
                self._sync_from_adapter_status(self._controller_adapter.status())
                with self._state_lock:
                    self._connected = True
                return

        with self._lifecycle_lock:
            with self._state_lock:
                if self._connected:
                    return
            self._bus.connect()
            with self._state_lock:
                self._connected = True
            try:
                self._refresh_status_snapshot()
            except L3ConnectionError:
                self._mark_disconnected()
                raise

    def close(self) -> None:
        """Mark this backend disconnected; never close the shared bus.

        Rs485Bus 是按物理口的进程级单例，加热台/旋涂同挂——任何单设备
        close 都不许关口（2026-07-16 审查修正）。总线生命周期归组合根管。
        """
        if self._controller_adapter is not None:
            with self._lifecycle_lock:
                self._controller_adapter.close()
                with self._state_lock:
                    self._connected = False
                    self._homed = False
                return

        with self._lifecycle_lock:
            with self._state_lock:
                self._connected = False

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
    ) -> PipetteActionResult:
        """Apply reviewed motion defaults, home, and IMM_STOP on bounded timeout."""
        action_description = (
            f"Write VEL/ACC/DEC={self._config.speed_01rps}/"
            f"{self._config.accel_01rpss}/{self._config.decel_01rpss}, then write "
            "CTRL 0x00 -> 0x01 and poll homed until idle"
        )
        if dry_run:
            return self._action_result(
                action="home",
                volume_ul=None,
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
            return result

        self._ensure_connected()
        started = time.monotonic()
        try:
            # Manual defaults (50/1250/1250) must be applied before home; the
            # legacy 10/20/20 values caused a real 30-second false timeout.
            self._write_single_register(_REG_VELOCITY, self._config.speed_01rps)
            self._write_single_register(_REG_ACCELERATION, self._config.accel_01rpss)
            self._write_single_register(_REG_DECELERATION, self._config.decel_01rpss)
            # pipette_controller.py:261-268: value-change edge is required.
            self._write_single_register(_REG_CTRL, _ACTION_IDLE)
            self._write_single_register(_REG_CTRL, _ACTION_HOME)
            self._wait_for_home()
        except L3ConnectionError:
            self._mark_disconnected()
            raise

        with self._state_lock:
            self._homed = True
            self._status_word = 0
            self._position_steps = 0
        return self._action_result(
            action="home",
            volume_ul=None,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    @observable
    def aspirate(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        """Aspirate a validated volume after successful home."""
        encoded_volume = self._validate_volume(volume_ul)
        action_description = (
            f"Write {encoded_volume} uL to VOL_H/VOL_L, write CTRL=0x0A, "
            "then wait for aspirate state active -> idle"
        )
        if dry_run:
            return self._action_result(
                action="aspirate",
                volume_ul=volume_ul,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
            )

        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.aspirate(
                volume_ul,
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result

        self._ensure_connected()
        self._ensure_homed()
        started = time.monotonic()
        try:
            self._ensure_tip_present_live()
            self._write_volume(encoded_volume)
            self._write_single_register(_REG_CTRL, _ACTION_ASPIRATE)
            self._wait_for_action_cycle(
                register=_REG_ASPIRATE_STATE,
                active_mask=0x01,
                action="aspirate",
            )
        except L3ConnectionError:
            self._mark_disconnected()
            raise

        return self._action_result(
            action="aspirate",
            volume_ul=volume_ul,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    @observable
    def dispense(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        """Dispense a validated volume after successful home."""
        encoded_volume = self._validate_volume(volume_ul)
        action_description = (
            f"Write {encoded_volume} uL to VOL_H/VOL_L, write CTRL=0x0B, "
            "then wait for dispense state active -> idle"
        )
        if dry_run:
            return self._action_result(
                action="dispense",
                volume_ul=volume_ul,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
            )

        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.dispense(
                volume_ul,
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result

        self._ensure_connected()
        self._ensure_homed()
        started = time.monotonic()
        try:
            self._ensure_tip_present_live()
            self._write_volume(encoded_volume)
            self._write_single_register(_REG_CTRL, _ACTION_DISPENSE)
            self._wait_for_action_cycle(
                register=_REG_DISPENSE_STATE,
                active_mask=0xFFFF,
                action="dispense",
            )
        except L3ConnectionError:
            self._mark_disconnected()
            raise

        return self._action_result(
            action="dispense",
            volume_ul=volume_ul,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    @observable
    def eject_tip(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        """Eject the installed tip and wait for both tip-absent and idle state."""
        action_description = (
            "If a tip is present, write CTRL=0x0C and wait for tip_present=false "
            "and status_word=0"
        )
        if dry_run:
            return self._action_result(
                action="eject_tip",
                volume_ul=None,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
            )

        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.tip_eject(
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result

        self._ensure_connected()
        started = time.monotonic()
        try:
            if not self._read_and_cache_tip_present():
                return self._action_result(
                    action="eject_tip",
                    volume_ul=None,
                    dry_run=False,
                    action_description="No tip detected; eject was already complete",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                )
            self._write_single_register(_REG_CTRL, _ACTION_EJECT_TIP)
            self._wait_for_tip_eject()
        except L3ConnectionError:
            self._mark_disconnected()
            raise

        return self._action_result(
            action="eject_tip",
            volume_ul=None,
            dry_run=False,
            action_description=action_description,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def tip_eject(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        """Business-name alias for the verified controller's tip_eject method."""
        return self.eject_tip(idempotency_key=idempotency_key, dry_run=dry_run)

    @observable
    def blowout(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        if dry_run:
            return self._action_result(
                action="blowout",
                volume_ul=None,
                dry_run=True,
                action_description="Dry-run pipette blowout; no bytes sent.",
                duration_ms=0.0,
            )
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.blowout(
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result
        self._ensure_connected()
        self._ensure_homed()
        started = time.monotonic()
        self._write_single_register(_REG_CTRL, _ACTION_DISPENSE)
        self._wait_for_action_cycle(
            register=_REG_DISPENSE_STATE,
            active_mask=0xFFFF,
            action="dispense",
        )
        return self._action_result(
            action="blowout",
            volume_ul=None,
            dry_run=False,
            action_description="Wrote CTRL=0x0B for pipette blowout.",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    @observable
    def liquid_detect(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        if dry_run:
            return self._action_result(
                action="liquid_detect",
                volume_ul=None,
                dry_run=True,
                action_description="Dry-run pipette liquid detect; no bytes sent.",
                duration_ms=0.0,
            )
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.liquid_detect(
                idempotency_key=idempotency_key,
                dry_run=False,
            )
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result
        self._ensure_connected()
        started = time.monotonic()
        self._write_single_register(_REG_CTRL, 0x09)
        return self._action_result(
            action="liquid_detect",
            volume_ul=None,
            dry_run=False,
            action_description="Wrote CTRL=0x09 for pipette liquid detect.",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def stop(self) -> PipetteActionResult:
        """立即写 IMM_STOP（0x08），急停级动作：无幂等缓存、不等待、不做 dry_run。

        柱塞停在当前位置后位置不再可信，homed 复位——继续吸/排前必须重新
        home。供 SystemEstop 与面板急停调用。
        """
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.stop()
            self._sync_from_adapter_status(self._controller_adapter.status())
            with self._state_lock:
                self._homed = False
            return result

        self._ensure_connected()
        started = time.monotonic()
        try:
            self._write_single_register(_REG_CTRL, _ACTION_IMMEDIATE_STOP)
        except L3ConnectionError:
            self._mark_disconnected()
            raise
        with self._state_lock:
            self._homed = False
        return self._action_result(
            action="stop",
            volume_ul=None,
            dry_run=False,
            action_description="Wrote CTRL=0x08 (IMM_STOP); plunger position "
            "is now untrusted and the pipette requires re-homing",
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    def reset(self) -> PipetteActionResult:
        if self._controller_adapter is not None:
            self._ensure_connected()
            result = self._controller_adapter.reset()
            self._sync_from_adapter_status(self._controller_adapter.status())
            return result
        self.stop()
        return self.home(idempotency_key=None, dry_run=False)

    def status(self) -> PipetteStatus:
        """Return the latest status snapshot without touching the RS485 bus."""
        with self._state_lock:
            connected = self._connected
            homed = self._homed
            status_word = self._status_word
            driver_fault = self._driver_fault
            position_steps = self._position_steps
            tip_present = self._tip_present
            timestamp = self._status_timestamp
            last_monotonic = self._status_monotonic

        age_ms = (
            None
            if last_monotonic is None
            else max(0.0, (time.monotonic() - last_monotonic) * 1000.0)
        )
        return PipetteStatus(
            connected=connected,
            homed=homed,
            status_word=status_word,
            driver_fault=driver_fault,
            position_steps=position_steps,
            tip_present=tip_present,
            timestamp=timestamp,
            last_update_ms_ago=age_ms,
        )

    def get_status(self) -> PipetteStatus:
        return self.status()

    def refresh_status(self) -> PipetteStatus:
        """Refresh live tip/action feedback after a mechanical tip mount."""
        self._ensure_connected()
        if self._controller_adapter is not None:
            status = self._controller_adapter.refresh_status()
            self._sync_from_adapter_status(status)
        else:
            self._refresh_status_snapshot()
        return self.status()

    def _sync_from_adapter_status(self, status: PipetteStatus) -> None:
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            self._connected = status.connected
            self._homed = status.homed
            self._status_word = status.status_word
            self._driver_fault = status.driver_fault
            self._position_steps = status.position_steps
            self._tip_present = status.tip_present
            self._status_timestamp = status.timestamp or now_utc
            self._status_monotonic = now_monotonic

    def _ensure_connected(self) -> None:
        with self._state_lock:
            connected = self._connected
        if not connected:
            raise L3ConnectionError(
                human_message="移液枪未连接",
                agent_message=(
                    "PipetteBackend is disconnected; call connect() before retrying."
                ),
            )

    def _mark_disconnected(self) -> None:
        with self._state_lock:
            self._connected = False

    def _ensure_homed(self) -> None:
        with self._state_lock:
            homed = self._homed
        if not homed:
            raise PipetteNotHomedError(
                human_message="移液枪未归位，请先执行 home",
                agent_message=(
                    "Pipette homed=false; call pipette.home(idempotency_key=...) "
                    "before aspirate() or dispense(). No Modbus bytes were sent."
                ),
            )

    def _ensure_tip_present_live(self) -> None:
        for attempt in range(_TIP_ABSENCE_CONFIRM_READS):
            if self._read_and_cache_tip_present():
                return
            if attempt + 1 < _TIP_ABSENCE_CONFIRM_READS:
                time.sleep(self._config.poll_interval_s)
        if self._tip_present is not True:
            raise PipetteTipMissingError(
                human_message="未检测到移液吸头",
                agent_message=(
                    "The live pipette tip-present register remained 0 for three "
                    "consecutive reads. Install a compatible tip before retrying; "
                    "no liquid-motion command was sent."
                ),
            )

    def _validate_volume(self, volume_ul: float) -> int:
        if (
            not math.isfinite(volume_ul)
            or volume_ul < 0
            or volume_ul > self._config.max_volume_ul
        ):
            raise PipetteVolumeOutOfRangeError(
                human_message=(
                    f"移液体积 {volume_ul!r} uL 超出 [0, "
                    f"{self._config.max_volume_ul:g}]"
                ),
                agent_message=(
                    f"Requested pipette volume {volume_ul!r} uL is not finite or "
                    f"outside [0, {self._config.max_volume_ul:g}]. "
                    "The request was rejected; no Modbus bytes were sent."
                ),
            )
        # The device stores whole microlitres in VOL_H/VOL_L.  Match the other
        # L3 numeric backends by rounding a validated float to the nearest
        # representable command while retaining the requested value in result.
        encoded = int(round(volume_ul))
        if not 0 <= encoded <= 0xFFFFFFFF:
            raise PipetteVolumeOutOfRangeError(
                human_message=f"移液体积 {volume_ul!r} uL 无法编码为 32 位运动量",
                agent_message=(
                    f"Requested pipette volume {volume_ul!r} uL is outside the "
                    "unsigned 32-bit VOL_H/VOL_L range; no bytes were sent."
                ),
            )
        return encoded

    def _refresh_status_snapshot(self) -> None:
        # One logical snapshot uses three short transactions.  POS_H/POS_L are
        # deliberately read together (reference lines 170-180) to avoid torn
        # values while the plunger crosses a 16-bit boundary.
        state = self._read_input_registers(_REG_STATUS, 3)
        position_words = self._read_input_registers(_REG_POS_H, 2)
        tip_words = self._read_input_registers(_REG_TIP_PRESENT, 1)
        homed = self._decode_flag("homed", state[1])
        tip_present = self._decode_flag("tip_present", tip_words[0])
        position_steps = self._decode_signed_position(position_words)
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            self._status_word = state[0]
            self._homed = homed
            self._driver_fault = state[2] != 0
            self._position_steps = position_steps
            self._tip_present = tip_present
            self._status_timestamp = now_utc
            self._status_monotonic = now_monotonic

    def _read_home_state(self) -> tuple[int, bool]:
        values = self._read_input_registers(_REG_STATUS, 2)
        status_word = values[0]
        homed = self._decode_flag("homed", values[1])
        self._update_live_state(status_word=status_word, homed=homed)
        return status_word, homed

    def _read_and_cache_tip_present(self) -> bool:
        value = self._read_input_registers(_REG_TIP_PRESENT, 1)[0]
        tip_present = self._decode_flag("tip_present", value)
        self._update_live_state(tip_present=tip_present)
        return tip_present

    def _read_and_cache_status_word(self) -> int:
        status_word = self._read_input_registers(_REG_STATUS, 1)[0]
        self._update_live_state(status_word=status_word)
        return status_word

    def _update_live_state(
        self,
        *,
        status_word: int | None = None,
        homed: bool | None = None,
        tip_present: bool | None = None,
    ) -> None:
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            if status_word is not None:
                self._status_word = status_word
            if homed is not None:
                self._homed = homed
            if tip_present is not None:
                self._tip_present = tip_present
            self._status_timestamp = now_utc
            self._status_monotonic = now_monotonic

    def _wait_for_home(self) -> None:
        # pipette_controller.py:253-292: completion requires observing action
        # start first, then homed=1 and status=IDLE.  Every sample releases the
        # transaction lock before this loop sleeps.
        deadline = time.monotonic() + self._config.home_timeout_s
        action_started = False
        while True:
            status_word, homed = self._read_home_state()
            if not action_started and (not homed or status_word != 0):
                action_started = True
            elif action_started and homed and status_word == 0:
                return
            if time.monotonic() >= deadline:
                self._stop_after_timeout("home", self._config.home_timeout_s)
            self._sleep_until_next_poll(deadline)

    def _wait_for_action_cycle(
        self,
        *,
        register: int,
        active_mask: int,
        action: Literal["aspirate", "dispense"],
    ) -> None:
        # pipette_controller.py:217-235: do not accept the initial idle sample
        # as completion; require active -> idle from the action-specific input.
        deadline = time.monotonic() + self._config.action_timeout_s
        action_started = False
        while True:
            state = self._read_input_registers(register, 1)[0]
            active = bool(state & active_mask)
            if active:
                action_started = True
            elif action_started:
                return
            if time.monotonic() >= deadline:
                self._stop_after_timeout(action, self._config.action_timeout_s)
            self._sleep_until_next_poll(deadline)

    def _wait_for_tip_eject(self) -> None:
        deadline = time.monotonic() + self._config.action_timeout_s
        action_started = False
        while True:
            status_word = self._read_and_cache_status_word()
            tip_present = self._read_and_cache_tip_present()
            if status_word != 0 or not tip_present:
                action_started = True
            if action_started and status_word == 0 and not tip_present:
                return
            if time.monotonic() >= deadline:
                self._stop_after_timeout("eject_tip", self._config.action_timeout_s)
            self._sleep_until_next_poll(deadline)

    def _sleep_until_next_poll(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(self._config.poll_interval_s, remaining))

    def _stop_after_timeout(self, action: str, timeout_s: float) -> NoReturn:
        # pipette_controller.py:286-292 and 418-421: a timeout must actively
        # write corrected IMM_STOP=0x08 before reporting failure.
        self._write_single_register(_REG_CTRL, _ACTION_IMMEDIATE_STOP)
        raise PipetteActionTimeoutError(action, timeout_s)

    def _write_volume(self, volume_ul: int) -> None:
        self._write_multiple_registers(
            _REG_VOLUME_H,
            [(volume_ul >> 16) & 0xFFFF, volume_ul & 0xFFFF],
        )

    def _read_input_registers(self, register: int, count: int) -> list[int]:
        request = _append_crc(
            bytes(
                [
                    self._unit_id,
                    _READ_INPUT_REGISTERS,
                    (register >> 8) & 0xFF,
                    register & 0xFF,
                    (count >> 8) & 0xFF,
                    count & 0xFF,
                ]
            )
        )
        expected_length = 5 + count * 2
        response = self._exchange(
            request,
            expected_function=_READ_INPUT_REGISTERS,
            expected_response_length=expected_length,
        )
        if response[0] != self._unit_id or response[1] != _READ_INPUT_REGISTERS:
            self._raise_protocol_error("response unit id or function did not match request")
        expected_byte_count = count * 2
        if response[2] != expected_byte_count:
            self._raise_protocol_error(
                f"read response byte count must be {expected_byte_count}, got {response[2]}"
            )
        return [
            int.from_bytes(response[3 + index * 2 : 5 + index * 2], "big")
            for index in range(count)
        ]

    def _write_single_register(self, register: int, value: int) -> None:
        request = _append_crc(
            bytes(
                [
                    self._unit_id,
                    _WRITE_SINGLE_REGISTER,
                    (register >> 8) & 0xFF,
                    register & 0xFF,
                    (value >> 8) & 0xFF,
                    value & 0xFF,
                ]
            )
        )
        response = self._exchange(
            request,
            expected_function=_WRITE_SINGLE_REGISTER,
            expected_response_length=8,
        )
        if response != request:
            self._raise_protocol_error(
                f"write echo mismatch: sent={request.hex()} received={response.hex()}"
            )

    def _write_multiple_registers(self, register: int, values: list[int]) -> None:
        quantity = len(values)
        value_bytes = b"".join(value.to_bytes(2, "big") for value in values)
        payload = bytes(
            [
                self._unit_id,
                _WRITE_MULTIPLE_REGISTERS,
                (register >> 8) & 0xFF,
                register & 0xFF,
                (quantity >> 8) & 0xFF,
                quantity & 0xFF,
                len(value_bytes),
            ]
        ) + value_bytes
        request = _append_crc(payload)
        response = self._exchange(
            request,
            expected_function=_WRITE_MULTIPLE_REGISTERS,
            expected_response_length=8,
        )
        expected_response = _append_crc(payload[:6])
        if response != expected_response:
            self._raise_protocol_error(
                f"multi-write echo mismatch: expected={expected_response.hex()} "
                f"received={response.hex()}"
            )

    def _exchange(
        self,
        request: bytes,
        *,
        expected_function: int,
        expected_response_length: int,
    ) -> bytes:
        try:
            with self._bus.transaction(
                _DEVICE_NAME,
                self._config.baudrate,
                timeout_s=self._config.timeout_s,
            ) as serial_port:
                # 发请求前清输入缓冲：上一笔超时的迟到应答若残留在 OS 缓冲，
                # 会被下一次 read 当成新鲜数据（同 unit id/长度/CRC 全合法）。
                serial_port.reset_input_buffer()
                written = serial_port.write(request)
                if written != len(request):
                    self._raise_protocol_error(
                        f"short serial write: expected {len(request)} bytes, wrote {written}"
                    )
                serial_port.flush()
                response = serial_port.read(expected_response_length)
        except (TimeoutError, OSError) as exc:
            raise PipetteCommunicationError(
                human_message="移液枪 Modbus 通信超时或串口异常",
                agent_message=(
                    f"Pipette unit {self._unit_id} failed during one Modbus RTU "
                    f"request-response exchange: {exc!r}."
                ),
            ) from exc

        if len(response) == 5 and response[1:2] == bytes([expected_function | 0x80]):
            self._require_valid_crc(response)
            exception_code = response[2]
            raise PipetteCommunicationError(
                human_message=f"移液枪返回 Modbus 异常码 {exception_code}",
                agent_message=(
                    f"Pipette unit {self._unit_id} returned Modbus exception "
                    f"0x{exception_code:02X} for function 0x{expected_function:02X}."
                ),
            )

        if len(response) != expected_response_length:
            raise PipetteCommunicationError(
                human_message="移液枪 Modbus 响应超时或帧长度错误",
                agent_message=(
                    f"Pipette unit {self._unit_id} returned {len(response)} bytes; "
                    f"expected {expected_response_length}. Partial frame="
                    f"{response.hex()!r}."
                ),
            )
        self._require_valid_crc(response)
        return response

    def _require_valid_crc(self, response: bytes) -> None:
        received_crc = int.from_bytes(response[-2:], byteorder="little")
        calculated_crc = _crc16(response[:-2])
        if received_crc != calculated_crc:
            raise PipetteCommunicationError(
                human_message="移液枪 Modbus 响应 CRC 校验失败",
                agent_message=(
                    f"Pipette unit {self._unit_id} returned invalid CRC: "
                    f"received=0x{received_crc:04X}, calculated=0x{calculated_crc:04X}, "
                    f"frame={response.hex()}."
                ),
            )

    def _raise_protocol_error(self, detail: str) -> NoReturn:
        raise PipetteCommunicationError(
            human_message="移液枪 Modbus 协议响应无效",
            agent_message=f"Pipette unit {self._unit_id} protocol error: {detail}.",
        )

    @staticmethod
    def _decode_flag(name: str, value: int) -> bool:
        # 参考实现语义（pipette_controller.is_homed 用 `== 1`）：非 0/1 值当
        # "未就位"继续轮询，不当协议错误——严格 0/1 校验会在柱塞运动中途
        # 抛错弃控且该路径不发 IMM_STOP（2026-07-16 审查修正）。
        del name  # 保留签名以便未来按 flag 名细分语义
        return value == 1

    @staticmethod
    def _decode_signed_position(words: list[int]) -> int:
        value = (words[0] << 16) | words[1]
        return value - 2**32 if value >= 2**31 else value

    @staticmethod
    def _action_result(
        *,
        action: Literal[
            "home",
            "aspirate",
            "dispense",
            "eject_tip",
            "blowout",
            "liquid_detect",
            "reset",
            "stop",
        ],
        volume_ul: float | None,
        dry_run: bool,
        action_description: str,
        duration_ms: float,
    ) -> PipetteActionResult:
        return PipetteActionResult(
            success=True,
            action=action,
            volume_ul=volume_ul,
            dry_run=dry_run,
            action_description=action_description,
            duration_ms=duration_ms,
            event_id="",
        )
