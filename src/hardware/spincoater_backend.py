"""DBLS400 spin-coater backend over the shared Modbus RTU bus.

The protocol bytes follow the hardware-validated legacy implementation at
``autospin_system/hardware/spin_motor/``:

- ``driver_communication.py:141-159`` reads DBLS400 register words low-byte
  first, and lines 164-180 write register values in the same byte order;
- ``motor_controller.py:95-119,192-226`` builds the control word and performs
  the start/speed writes; lines 205-213 define brake/coast stop control words;
- ``motor_controller.py:215-226`` ``set_speed()`` writes raw RPM to 0x8005 with
  no conversion; ``motor_controller.py:228-239`` applies ``speed_factor = 2.5``
  only when decoding actual-speed readback from 0x8018 (raw*20/pole_count).

Every Modbus request uses one :meth:`Rs485Bus.transaction`.  This backend does
not wait while holding the shared-bus lock; any future speed-ramp polling must
open one transaction per sample and wait between samples outside the lock.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..observable import observable
from .errors import ConnectionError as L3ConnectionError
from .errors import L3Error
from .rs485_bus import Rs485Bus


_DEVICE_NAME = "spincoater"
_READ_HOLDING_REGISTERS = 0x03
_WRITE_SINGLE_REGISTER = 0x06

# 真机验证事实（2026-06-20）：本机是 4 对极电机，寄存器指令值到实际
# RPM 的换算系数为 2.5。该系数只对 4 对极电机成立，换电机时不能沿用。
_VALIDATED_POLE_PAIRS = 4
_VALIDATED_SPEED_FACTOR = 2.5

# DBLS400 0x801B 低字节故障位，来源为已核对的驱动器手册与真机 bring-up。
# 任务卡规定 0x801B 非零即视为驱动器故障；高字节若非零也保留在解码结果中。
_FAULT_BIT_NAMES: tuple[tuple[str, str], ...] = (
    ("stall", "堵转"),
    ("overcurrent", "过流"),
    ("hall_sensor_fault", "霍尔异常"),
    ("bus_undervoltage", "母线欠压"),
    ("bus_overvoltage", "母线过压"),
    ("current_peak_alarm", "电流峰值报警"),
    ("reserved_bit_6", "保留位6"),
    ("reserved_bit_7", "保留位7"),
)


class SpincoaterCommunicationError(L3ConnectionError):
    """DBLS400 returned a timeout, malformed frame, CRC error, or exception."""

    error_code = "L3.SPINCOATER_COMMUNICATION"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Verify the DBLS400 is powered and address/baudrate are correct, then "
        "reconnect the shared RS485 bus and retry."
    )
    suggested_action_zh = (
        "确认 DBLS400 已上电、站号和波特率正确，再重连共享 RS485 总线后重试。"
    )


class SpincoaterRpmOutOfRangeError(L3Error):
    """Requested spin speed exceeds the configured hard safety boundary."""

    error_code = "L3.SPINCOATER_RPM_OUT_OF_RANGE"
    severity = "warning"
    recoverable = False
    suggested_action = "Choose a finite positive RPM at or below spincoater.max_rpm."
    suggested_action_zh = "将目标转速改为大于 0 且不超过 spincoater.max_rpm 的有限数值。"


class SpincoaterFaultError(L3Error):
    """DBLS400 fault register 0x801B is non-zero, with active bits decoded."""

    error_code = "L3.SPINCOATER_FAULT"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Keep the spin coater stopped, inspect the decoded DBLS400 fault bits, "
        "correct the physical cause, then reconnect and call read_fault()."
    )
    suggested_action_zh = (
        "保持旋涂电机停机，按解码故障位检查堵转、供电、霍尔与接线；排除原因后重连并复查。"
    )

    def __init__(self, fault_register: int, fault_bits: tuple[str, ...]) -> None:
        self.fault_register = fault_register
        self.fault_bits = fault_bits
        decoded = ", ".join(fault_bits) if fault_bits else "未识别故障位"
        super().__init__(
            human_message=(
                f"旋涂驱动器故障寄存器 0x801B=0x{fault_register:04X}（{decoded}）"
            ),
            agent_message=(
                f"DBLS400 fault register 0x801B returned 0x{fault_register:04X}; "
                f"decoded active bits: {decoded}. The motor command was rejected."
            ),
        )


@dataclass(frozen=True)
class SpincoaterConfig:
    """DBLS400 protocol constants plus the reviewed hard RPM ceiling."""

    max_rpm: float
    baudrate: int = 9600
    timeout_s: float = 0.5
    control_register: int = 0x8000
    speed_set_register: int = 0x8005
    fault_register: int = 0x801B
    pole_pairs: int = _VALIDATED_POLE_PAIRS
    speed_factor: float = _VALIDATED_SPEED_FACTOR


class SpinStatus(BaseModel):
    """Latest in-memory spin/fault snapshot; :meth:`status` performs no I/O."""

    connected: bool
    running: bool
    target_rpm: float | None = Field(
        default=None,
        description="Last successfully commanded actual speed in RPM.",
    )
    direction: Literal["ccw"] = Field(
        default="ccw",
        description="Fixed v1 direction, validated as smooth CCW from above.",
    )
    brake_engaged: bool | None = Field(
        default=None,
        description="Last successfully commanded brake state; unknown before stop/start.",
    )
    fault_register: int | None = Field(
        default=None,
        ge=0,
        le=0xFFFF,
        description="Latest raw DBLS400 0x801B value.",
    )
    fault_bits: list[str] = Field(
        default_factory=list,
        description="Decoded active bits from the latest 0x801B read.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest successful 0x801B register read.",
    )
    last_update_ms_ago: float | None = Field(
        default=None,
        ge=0,
        description="Age of the latest successful 0x801B register read.",
    )


class SpinActionResult(BaseModel):
    """Result or dry-run description returned by :meth:`start` or :meth:`stop`."""

    success: bool
    action: Literal["start", "stop"]
    target_rpm: float
    command_value: int = Field(
        ge=0,
        le=0xFFFF,
        description="DBLS400 register command value after speed-factor conversion.",
    )
    direction: Literal["ccw"] = "ccw"
    brake_engaged: bool
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


def _decode_fault_bits(value: int) -> tuple[str, ...]:
    active = [
        f"bit{bit}:{code}({name_zh})"
        for bit, (code, name_zh) in enumerate(_FAULT_BIT_NAMES)
        if value & (1 << bit)
    ]
    run_state = (value >> 8) & 0xFF
    if run_state:
        active.append(f"high_byte:run_state_0x{run_state:02X}(运行状态字节)")
    return tuple(active)


class SpincoaterBackend:
    """Agent-facing DBLS400 speed-control backend.

    物理限制：DBLS400 无角度反馈，只能调速，不能定向停转。v1 方向固定为
    真机验证平稳的 CCW，不暴露方向参数。
    """

    def __init__(
        self,
        bus: Rs485Bus,
        unit_id: int,
        config: SpincoaterConfig,
    ) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError(f"unit_id must be in [1, 247], got {unit_id}")
        if (
            config.baudrate <= 0
            or config.timeout_s <= 0
            or not math.isfinite(config.max_rpm)
            or config.max_rpm <= 0
            or not math.isfinite(config.speed_factor)
            or config.speed_factor <= 0
        ):
            raise ValueError(
                "spincoater baudrate, timeout_s, max_rpm, and speed_factor "
                "must be finite and positive"
            )
        if (
            config.pole_pairs != _VALIDATED_POLE_PAIRS
            or config.speed_factor != _VALIDATED_SPEED_FACTOR
        ):
            raise ValueError(
                "speed_factor=2.5 is validated only for this 4-pole-pair motor"
            )
        for name, register in (
            ("control_register", config.control_register),
            ("speed_set_register", config.speed_set_register),
            ("fault_register", config.fault_register),
        ):
            if not 0 <= register <= 0xFFFF:
                raise ValueError(f"{name} must be in [0, 65535], got {register}")

        self._bus = bus
        self._unit_id = unit_id
        self._config = config
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False
        self._running = False
        self._target_rpm: float | None = None
        self._brake_engaged: bool | None = None
        self._fault_register: int | None = None
        self._fault_bits: tuple[str, ...] = ()
        self._fault_timestamp: datetime | None = None
        self._fault_monotonic: float | None = None

    def connect(self) -> None:
        """Connect idempotently and read 0x801B once to verify the DBLS400."""
        with self._lifecycle_lock:
            with self._state_lock:
                already_connected = self._connected
            if already_connected:
                self._ensure_no_known_fault()
                return
            self._bus.connect()
            with self._state_lock:
                self._connected = True
            try:
                self.read_fault()
            except SpincoaterFaultError:
                # A decoded device fault proves the DBLS400 is online.  Preserve
                # connected=True so status/close remain truthful and usable.
                raise
            except L3ConnectionError:
                self._mark_disconnected()
                raise

    def close(self) -> None:
        """Mark this backend disconnected; never close the shared bus.

        Rs485Bus 是按物理口的进程级单例，加热台/移液枪同挂——任何单设备
        close 都不许关口（2026-07-16 审查修正）。总线生命周期归组合根管。
        """
        with self._lifecycle_lock:
            with self._state_lock:
                self._connected = False

    @observable
    def start(
        self,
        rpm: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        """Start at validated RPM in fixed CCW; 无角度反馈，不能定向停转。"""
        command_value = self._validate_and_encode_rpm(rpm)
        self._ensure_no_known_fault()
        control_value = self._control_word(run=True, brake=False)
        action_description = (
            f"Start DBLS400 CCW with control word 0x{control_value:04X}, then "
            f"write target {rpm:g} RPM as command {command_value} "
            "(0x8005 takes raw RPM)"
        )
        if dry_run:
            return SpinActionResult(
                success=True,
                action="start",
                target_rpm=rpm,
                command_value=command_value,
                brake_engaged=False,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
                event_id="",
            )

        self._ensure_connected()
        started = time.monotonic()
        try:
            # Reference order: motor_controller.py:192-226 starts first, then
            # sets speed.  Each write gets its own shared-bus transaction.
            self._write_single_register(self._config.control_register, control_value)
            self._write_single_register(self._config.speed_set_register, command_value)
        except L3ConnectionError:
            self._mark_disconnected()
            raise
        duration_ms = (time.monotonic() - started) * 1000.0

        with self._state_lock:
            self._running = True
            self._target_rpm = rpm
            self._brake_engaged = False
        return SpinActionResult(
            success=True,
            action="start",
            target_rpm=rpm,
            command_value=command_value,
            brake_engaged=False,
            dry_run=False,
            action_description=action_description,
            duration_ms=duration_ms,
            event_id="",
        )

    @observable
    def stop(
        self,
        *,
        use_brake: bool = True,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SpinActionResult:
        """Stop with brake by default; 无角度反馈，不能定向停转。"""
        control_value = self._control_word(run=False, brake=use_brake)
        stop_mode = "brake" if use_brake else "coast"
        action_description = (
            f"Stop DBLS400 using {stop_mode} mode with control word "
            f"0x{control_value:04X}; final angle is not controllable"
        )
        if dry_run:
            return SpinActionResult(
                success=True,
                action="stop",
                target_rpm=0.0,
                command_value=0,
                brake_engaged=use_brake,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
                event_id="",
            )

        self._ensure_connected()
        started = time.monotonic()
        try:
            self._write_single_register(self._config.control_register, control_value)
        except L3ConnectionError:
            self._mark_disconnected()
            raise
        duration_ms = (time.monotonic() - started) * 1000.0

        with self._state_lock:
            self._running = False
            self._target_rpm = 0.0
            self._brake_engaged = use_brake
        return SpinActionResult(
            success=True,
            action="stop",
            target_rpm=0.0,
            command_value=0,
            brake_engaged=use_brake,
            dry_run=False,
            action_description=action_description,
            duration_ms=duration_ms,
            event_id="",
        )

    def read_fault(self) -> SpinStatus:
        """Read and decode DBLS400 fault register 0x801B in one transaction."""
        self._ensure_connected()
        try:
            raw_fault = self._read_holding_register(self._config.fault_register)
        except L3ConnectionError:
            self._mark_disconnected()
            raise

        fault_bits = _decode_fault_bits(raw_fault)
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            self._fault_register = raw_fault
            self._fault_bits = fault_bits
            self._fault_timestamp = now_utc
            self._fault_monotonic = now_monotonic

        # 0x801B 低字节 = 故障位（Bit0-7），高字节 = 电机运行状态（手册 §寄存器表）。
        # 只有低字节非零才是故障；运行状态字节在正常旋转期间就会非零，不能计入。
        if raw_fault & 0xFF:
            raise SpincoaterFaultError(raw_fault, fault_bits)
        return self.status()

    def status(self) -> SpinStatus:
        """Return cached state without bus I/O; 无角度反馈，不能定向停转。"""
        with self._state_lock:
            connected = self._connected
            running = self._running
            target_rpm = self._target_rpm
            brake_engaged = self._brake_engaged
            fault_register = self._fault_register
            fault_bits = list(self._fault_bits)
            timestamp = self._fault_timestamp
            last_monotonic = self._fault_monotonic

        age_ms = (
            None
            if last_monotonic is None
            else max(0.0, (time.monotonic() - last_monotonic) * 1000.0)
        )
        return SpinStatus(
            connected=connected,
            running=running,
            target_rpm=target_rpm,
            brake_engaged=brake_engaged,
            fault_register=fault_register,
            fault_bits=fault_bits,
            timestamp=timestamp,
            last_update_ms_ago=age_ms,
        )

    def _ensure_connected(self) -> None:
        with self._state_lock:
            connected = self._connected
        if not connected:
            raise L3ConnectionError(
                human_message="旋涂驱动器未连接",
                agent_message=(
                    "SpincoaterBackend is disconnected; call connect() before retrying."
                ),
            )

    def _mark_disconnected(self) -> None:
        with self._state_lock:
            self._connected = False

    def _ensure_no_known_fault(self) -> None:
        with self._state_lock:
            fault_register = self._fault_register
            fault_bits = self._fault_bits
        # 同 read_fault：只有低字节（故障位）非零才拦；高字节是运行状态。
        if fault_register is not None and fault_register & 0xFF:
            raise SpincoaterFaultError(fault_register, fault_bits)

    def _validate_and_encode_rpm(self, rpm: float) -> int:
        if not math.isfinite(rpm) or rpm <= 0 or rpm > self._config.max_rpm:
            raise SpincoaterRpmOutOfRangeError(
                human_message=(
                    f"旋涂目标转速 {rpm!r} RPM 超出安全范围 "
                    f"(0, {self._config.max_rpm:g}] RPM)"
                ),
                agent_message=(
                    f"Requested spin speed {rpm!r} RPM is not finite, not positive, "
                    f"or exceeds hard limit spincoater.max_rpm="
                    f"{self._config.max_rpm:g} RPM. No Modbus bytes were sent."
                ),
            )

        # 0x8005 直接收 RPM 原值：手册"通讯举例"写 485 速度 E8 03 = 1000 RPM 原值，
        # motor_controller.set_speed() 也是 write_register(REG_SPEED_SET, target_rpm)
        # 无换算。speed_factor=2.5 只是 0x8018 实际转速回读的解码系数（raw*20/8极），
        # 与写路径无关——2026-07-16 审查纠正（原任务卡把它写成写路径换算，属卡文错误）。
        command_value = int(round(rpm))
        if not 1 <= command_value <= 0xFFFF:
            raise SpincoaterRpmOutOfRangeError(
                human_message=f"旋涂目标转速 {rpm!r} RPM 无法编码为有效指令值",
                agent_message=(
                    f"Requested spin speed {rpm!r} RPM rounds to DBLS400 command "
                    f"{command_value}; the valid 16-bit command range is "
                    "[1, 65535]. No bytes were sent."
                ),
            )
        return command_value

    def _control_word(self, *, run: bool, brake: bool) -> int:
        # motor_controller.py:95-119：Bit3 恒置 1，Bit0=运行，Bit2=刹车，
        # 极对数放高字节。forward/Bit1=0 已由真机确认是从上方观察 CCW。
        control_bits = 0x08
        if run:
            control_bits |= 0x01
        if brake:
            control_bits |= 0x04
        return (self._config.pole_pairs << 8) | control_bits

    def _read_holding_register(self, register: int) -> int:
        request = _append_crc(
            bytes(
                [
                    self._unit_id,
                    _READ_HOLDING_REGISTERS,
                    (register >> 8) & 0xFF,
                    register & 0xFF,
                    0x00,
                    0x01,
                ]
            )
        )
        response = self._exchange(
            request,
            expected_function=_READ_HOLDING_REGISTERS,
            expected_response_length=7,
        )
        if response[0] != self._unit_id or response[1] != _READ_HOLDING_REGISTERS:
            self._raise_protocol_error("response unit id or function did not match request")
        if response[2] != 2:
            self._raise_protocol_error(
                f"read response byte count must be 2, got {response[2]}"
            )
        # driver_communication.py:153-158：DBLS400 数据低字节在前。
        return int.from_bytes(response[3:5], byteorder="little", signed=False)

    def _write_single_register(self, register: int, value: int) -> None:
        # driver_communication.py:170-180：地址仍为大端，寄存器值低字节在前。
        request = _append_crc(
            bytes(
                [
                    self._unit_id,
                    _WRITE_SINGLE_REGISTER,
                    (register >> 8) & 0xFF,
                    register & 0xFF,
                    value & 0xFF,
                    (value >> 8) & 0xFF,
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
            raise SpincoaterCommunicationError(
                human_message="旋涂驱动器 Modbus 通信超时或串口异常",
                agent_message=(
                    f"DBLS400 unit {self._unit_id} failed during one Modbus RTU "
                    f"request-response exchange: {exc!r}."
                ),
            ) from exc

        if len(response) == 5 and response[1:2] == bytes([expected_function | 0x80]):
            self._require_valid_crc(response)
            exception_code = response[2]
            raise SpincoaterCommunicationError(
                human_message=f"旋涂驱动器返回 Modbus 异常码 {exception_code}",
                agent_message=(
                    f"DBLS400 unit {self._unit_id} returned Modbus exception "
                    f"0x{exception_code:02X} for function 0x{expected_function:02X}."
                ),
            )

        if len(response) != expected_response_length:
            raise SpincoaterCommunicationError(
                human_message="旋涂驱动器 Modbus 响应超时或帧长度错误",
                agent_message=(
                    f"DBLS400 unit {self._unit_id} returned {len(response)} bytes; "
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
            raise SpincoaterCommunicationError(
                human_message="旋涂驱动器 Modbus 响应 CRC 校验失败",
                agent_message=(
                    f"DBLS400 unit {self._unit_id} returned invalid CRC: "
                    f"received=0x{received_crc:04X}, calculated=0x{calculated_crc:04X}, "
                    f"frame={response.hex()}."
                ),
            )

    def _raise_protocol_error(self, detail: str) -> None:
        raise SpincoaterCommunicationError(
            human_message="旋涂驱动器 Modbus 协议响应无效",
            agent_message=f"DBLS400 unit {self._unit_id} protocol error: {detail}.",
        )
