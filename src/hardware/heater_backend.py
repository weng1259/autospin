"""AI-516P heating-stage backend over the shared Modbus RTU bus.

The register map and temperature conversion follow the validated legacy
implementation at
``autospin_system/hardware/heating_stage/heating_stage_controller.py``:

- lines 26-31: PV register 74, SV register 0, Srun register 27, scale 10;
- lines 133-146: write SV first, then write ``Srun=0`` to enter run mode.

Every Modbus request uses one :meth:`Rs485Bus.transaction`; the optional Srun
write therefore uses a second transaction rather than retaining the shared bus
lock across two device operations.
"""
from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..observable import observable
from .errors import ConnectionError as L3ConnectionError
from .errors import L3Error
from .rs485_bus import Rs485Bus


_DEVICE_NAME = "heater"
_READ_HOLDING_REGISTERS = 0x03
_WRITE_SINGLE_REGISTER = 0x06


class HeaterCommunicationError(L3Error):
    """AI-516P returned a timeout, malformed frame, CRC error, or exception."""

    error_code = "L3.HEATER_COMMUNICATION"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "Verify the AI-516P is online in standard Modbus mode (AFC=0), then "
        "reconnect the shared RS485 bus and retry."
    )
    suggested_action_zh = (
        "确认 AI-516P 已上电、AFC=0、地址和波特率正确，再重连共享 RS485 总线后重试。"
    )


class HeaterSetpointOutOfRangeError(L3Error):
    """Requested heater SV exceeds the configured hard safety boundary."""

    error_code = "L3.HEATER_SV_OUT_OF_RANGE"
    severity = "warning"
    recoverable = False
    suggested_action = "Choose a finite SV at or below heater.sv_max_c."
    suggested_action_zh = "将设定温度改为不超过 heater.sv_max_c 的有限数值。"


@dataclass(frozen=True)
class HeaterConfig:
    """AI-516P protocol constants plus its reviewed SV safety ceiling."""

    sv_max_c: float
    baudrate: int = 9600
    timeout_s: float = 3.0
    pv_register: int = 74
    sv_register: int = 0
    srun_register: int = 27
    run_on_sv_write: bool = True
    scale: float = 10.0


class HeaterStatus(BaseModel):
    """Latest in-memory PV/SV snapshot; :meth:`status` performs no I/O."""

    connected: bool
    pv_c: float | None = Field(
        default=None,
        description="Latest process value in degrees Celsius.",
    )
    sv_c: float | None = Field(
        default=None,
        description="Last successfully commanded set value in degrees Celsius.",
    )
    timestamp: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest successful PV read.",
    )
    last_update_ms_ago: float | None = Field(
        default=None,
        ge=0,
        description="Age of the latest successful PV read.",
    )


class HeaterActionResult(BaseModel):
    """Result or dry-run description returned by :meth:`set_sv`."""

    success: bool
    target_sv_c: float
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


class HeaterBackend:
    """Agent-facing AI-516P backend using device-semantic temperature APIs."""

    def __init__(self, bus: Rs485Bus, unit_id: int, config: HeaterConfig) -> None:
        if not 1 <= unit_id <= 247:
            raise ValueError(f"unit_id must be in [1, 247], got {unit_id}")
        if config.baudrate <= 0 or config.timeout_s <= 0 or config.scale <= 0:
            raise ValueError("heater baudrate, timeout_s, and scale must be positive")
        for name, register in (
            ("pv_register", config.pv_register),
            ("sv_register", config.sv_register),
            ("srun_register", config.srun_register),
        ):
            if not 0 <= register <= 0xFFFF:
                raise ValueError(f"{name} must be in [0, 65535], got {register}")

        self._bus = bus
        self._unit_id = unit_id
        self._config = config
        self._lifecycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = False
        self._last_pv_c: float | None = None
        self._last_sv_c: float | None = None
        self._last_pv_timestamp: datetime | None = None
        self._last_pv_monotonic: float | None = None

    def connect(self) -> None:
        """Connect idempotently and read one PV value to verify the heater."""
        with self._lifecycle_lock:
            with self._state_lock:
                if self._connected:
                    return
            self._bus.connect()
            with self._state_lock:
                self._connected = True
            try:
                self.read_pv()
            except L3Error:
                self._mark_disconnected()
                raise

    def close(self) -> None:
        """Mark this backend disconnected; never close the shared bus.

        Rs485Bus 是按物理口的进程级单例，旋涂/移液同挂——任何单设备
        close 都不许关口（2026-07-16 审查修正）。总线生命周期归组合根管。
        """
        with self._lifecycle_lock:
            with self._state_lock:
                self._connected = False

    def read_pv(self) -> HeaterStatus:
        """Read one current-temperature snapshot from PV register 74."""
        self._ensure_connected()
        try:
            raw = self._read_holding_register(self._config.pv_register)
        except L3Error:
            self._mark_disconnected()
            raise

        pv_c = raw / self._config.scale
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._state_lock:
            self._last_pv_c = pv_c
            self._last_pv_timestamp = now_utc
            self._last_pv_monotonic = now_monotonic
        return self.status()

    @observable
    def set_sv(
        self,
        sv_c: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> HeaterActionResult:
        """Validate and set SV; dry-run returns the exact planned writes only."""
        raw_sv = self._validate_and_encode_sv(sv_c)
        action_description = (
            f"Write SV={sv_c:g} °C (raw={raw_sv}) to holding register "
            f"{self._config.sv_register}"
        )
        if self._config.run_on_sv_write:
            action_description += (
                f", then write Srun=0 to holding register "
                f"{self._config.srun_register}"
            )

        if dry_run:
            return HeaterActionResult(
                success=True,
                target_sv_c=sv_c,
                dry_run=True,
                action_description=action_description,
                duration_ms=0.0,
                event_id="",
            )

        self._ensure_connected()
        started = time.monotonic()
        try:
            self._write_single_register(self._config.sv_register, raw_sv)
            # SV 寄存器写成功即更新 memo：后面 Srun 写失败时设备 SV 已经变了，
            # status() 不能还报旧值（2026-07-16 审查修正）。
            with self._state_lock:
                self._last_sv_c = sv_c
            if self._config.run_on_sv_write:
                self._write_single_register(self._config.srun_register, 0)
        except L3Error:
            self._mark_disconnected()
            raise
        duration_ms = (time.monotonic() - started) * 1000.0
        return HeaterActionResult(
            success=True,
            target_sv_c=sv_c,
            dry_run=False,
            action_description=action_description,
            duration_ms=duration_ms,
            event_id="",
        )

    def status(self) -> HeaterStatus:
        """Return the latest PV/SV snapshot without touching the RS485 bus."""
        with self._state_lock:
            connected = self._connected
            pv_c = self._last_pv_c
            sv_c = self._last_sv_c
            timestamp = self._last_pv_timestamp
            last_monotonic = self._last_pv_monotonic

        age_ms = (
            None
            if last_monotonic is None
            else max(0.0, (time.monotonic() - last_monotonic) * 1000.0)
        )
        return HeaterStatus(
            connected=connected,
            pv_c=pv_c,
            sv_c=sv_c,
            timestamp=timestamp,
            last_update_ms_ago=age_ms,
        )

    def _ensure_connected(self) -> None:
        with self._state_lock:
            connected = self._connected
        if not connected:
            raise L3ConnectionError(
                human_message="加热台未连接",
                agent_message="HeaterBackend is disconnected; call connect() before retrying.",
            )

    def _mark_disconnected(self) -> None:
        with self._state_lock:
            self._connected = False

    def _validate_and_encode_sv(self, sv_c: float) -> int:
        # 下限暂取 0℃（本机加热台无制冷，负值必是笔误）。TODO PM 定 sv_min_c
        # 后改为配置项（2026-07-16 审查补充）。
        if not math.isfinite(sv_c) or sv_c > self._config.sv_max_c or sv_c < 0.0:
            raise HeaterSetpointOutOfRangeError(
                human_message=(
                    f"加热台设定温度 {sv_c!r} ℃ 超出安全上限 "
                    f"{self._config.sv_max_c:g} ℃"
                ),
                agent_message=(
                    f"Requested heater SV {sv_c!r} °C is not finite or exceeds "
                    f"the hard limit heater.sv_max_c={self._config.sv_max_c:g} °C. "
                    "The value was rejected; no Modbus bytes were sent."
                ),
            )

        raw = int(round(sv_c * self._config.scale))
        if not -0x8000 <= raw <= 0x7FFF:
            raise HeaterSetpointOutOfRangeError(
                human_message=f"加热台设定温度 {sv_c!r} ℃ 无法编码为 16 位寄存器",
                agent_message=(
                    f"Requested heater SV {sv_c!r} °C encodes to {raw}, outside "
                    "the signed 16-bit AI-516P register range; no bytes were sent."
                ),
            )
        return raw & 0xFFFF

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
        value = int.from_bytes(response[3:5], byteorder="big", signed=False)
        return value - 0x10000 if value >= 0x8000 else value

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
        except TimeoutError as exc:
            raise HeaterCommunicationError(
                human_message="加热台 Modbus 通信超时",
                agent_message=(
                    f"AI-516P unit {self._unit_id} timed out during one Modbus RTU "
                    f"request-response exchange: {exc!r}."
                ),
            ) from exc

        if len(response) == 5 and response[1:2] == bytes([expected_function | 0x80]):
            self._require_valid_crc(response)
            exception_code = response[2]
            raise HeaterCommunicationError(
                human_message=f"加热台返回 Modbus 异常码 {exception_code}",
                agent_message=(
                    f"AI-516P unit {self._unit_id} returned Modbus exception "
                    f"0x{exception_code:02X} for function 0x{expected_function:02X}."
                ),
            )

        if len(response) != expected_response_length:
            raise HeaterCommunicationError(
                human_message="加热台 Modbus 响应超时或帧长度错误",
                agent_message=(
                    f"AI-516P unit {self._unit_id} returned {len(response)} bytes; "
                    f"expected {expected_response_length}. Partial frame={response.hex()!r}."
                ),
            )
        self._require_valid_crc(response)
        return response

    def _require_valid_crc(self, response: bytes) -> None:
        received_crc = int.from_bytes(response[-2:], byteorder="little")
        calculated_crc = _crc16(response[:-2])
        if received_crc != calculated_crc:
            raise HeaterCommunicationError(
                human_message="加热台 Modbus 响应 CRC 校验失败",
                agent_message=(
                    f"AI-516P unit {self._unit_id} returned invalid CRC: "
                    f"received=0x{received_crc:04X}, calculated=0x{calculated_crc:04X}, "
                    f"frame={response.hex()}."
                ),
            )

    def _raise_protocol_error(self, detail: str) -> None:
        raise HeaterCommunicationError(
            human_message="加热台 Modbus 协议响应无效",
            agent_message=f"AI-516P unit {self._unit_id} protocol error: {detail}.",
        )
