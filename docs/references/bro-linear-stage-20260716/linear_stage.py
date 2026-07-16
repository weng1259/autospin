"""ZDT Emm RS485 screw-stage controller with verified status feedback."""

from __future__ import annotations

import logging
import time
from typing import Any


class LinearStageError(RuntimeError):
    """Raised when the stage rejects a command or fails status verification."""


class EmmLinearStage:
    CHECKSUM = 0x6B

    def __init__(
        self,
        port: str,
        *,
        address: int = 4,
        baudrate: int = 115200,
        timeout: float = 0.5,
        lead_mm: float = 2.0,
        microsteps: int = 16,
        motor_step_deg: float = 1.8,
        travel_mm: float = 100.0,
        default_speed_rpm: int = 2000,
        default_acceleration: int = 150,
        home_direction: int = 1,
        home_speed_rpm: int = 300,
        mock: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        if home_direction not in (0, 1):
            raise ValueError("home_direction must be 0 (CW) or 1 (CCW)")
        if not 0 <= home_speed_rpm <= 300:
            raise ValueError("native Emm homing speed must be in 0..300 RPM")
        self.port = str(port)
        self.address = int(address)
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.lead_mm = float(lead_mm)
        self.microsteps = int(microsteps)
        self.motor_step_deg = float(motor_step_deg)
        self.travel_mm = float(travel_mm)
        self.default_speed_rpm = int(default_speed_rpm)
        self.default_acceleration = int(default_acceleration)
        self.home_direction = int(home_direction)
        self.home_speed_rpm = int(home_speed_rpm)
        self.mock = bool(mock)
        self.logger = logger or logging.getLogger(__name__)
        self._serial: Any | None = None
        self._connected = False
        self._commanded_position_mm = 0.0
        self._mock_enabled = False
        self._mock_homing = False

    @property
    def pulses_per_mm(self) -> float:
        return (360.0 / self.motor_step_deg * self.microsteps) / self.lead_mm

    def connect(self) -> bool:
        if self.mock:
            self._connected = True
            return True
        try:
            import serial

            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self._connected = True
            # Opening a Linux tty only proves that the USB adapter exists.
            # Require a valid Emm status frame before reporting the stage as
            # connected, otherwise the Web UI would show a false positive when
            # A/B wiring, baudrate, power, or station address is wrong.
            self.read_flags()
            return True
        except Exception as exc:
            self.logger.error("Linear stage connection failed on %s: %s", self.port, exc)
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            self._connected = False
            return False

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and (self.mock or bool(self._serial and self._serial.is_open))

    def _frame(self, function: int, payload: bytes = b"") -> bytes:
        return bytes([self.address, function]) + payload + bytes([self.CHECKSUM])

    def _write(self, function: int, payload: bytes = b"") -> bytes:
        frame = self._frame(function, payload)
        if self.mock:
            self.logger.info("[MOCK] Linear stage TX: %s", frame.hex(" ").upper())
            return frame
        if not self.is_connected() or self._serial is None:
            raise LinearStageError("linear stage is not connected")
        self._serial.reset_output_buffer()
        self._serial.write(frame)
        self._serial.flush()
        self.logger.info("Linear stage TX: %s", frame.hex(" ").upper())
        return frame

    def _command(self, function: int, payload: bytes = b"", *, response_length: int = 4) -> bytes:
        frame = self._frame(function, payload)
        if self.mock:
            self._write(function, payload)
            return bytes([self.address, function, 0x02, self.CHECKSUM]) if response_length else b""
        if not self.is_connected() or self._serial is None:
            raise LinearStageError("linear stage is not connected")
        self._serial.reset_input_buffer()
        self._write(function, payload)
        if response_length == 0:
            return b""
        response = self._serial.read(response_length)
        self._validate_response(frame, response, response_length)
        if response_length == 4 and response[2] == 0xE2:
            raise LinearStageError(f"motor rejected parameters (E2): {frame.hex(' ').upper()}")
        if response_length == 4 and response[2] == 0xEE:
            raise LinearStageError(f"motor rejected command format (EE): {frame.hex(' ').upper()}")
        return response

    def _validate_response(self, request: bytes, response: bytes, length: int) -> None:
        if len(response) != length:
            raise LinearStageError(
                f"incomplete response ({len(response)}/{length}) to "
                f"{request.hex(' ').upper()}: {response.hex(' ').upper()}"
            )
        if response[0] != self.address or response[-1] != self.CHECKSUM:
            raise LinearStageError(f"invalid response: {response.hex(' ').upper()}")

    def _query(self, function: int, response_length: int, payload: bytes = b"") -> bytes:
        if self.mock:
            if function == 0x36:
                raw = round(abs(self._commanded_position_mm) / self.lead_mm * 65536)
                sign = 1 if self._commanded_position_mm < 0 else 0
                return bytes([self.address, function, sign]) + raw.to_bytes(4, "big") + bytes([self.CHECKSUM])
            if function == 0x3A:
                flags = int(self._mock_enabled) | 0x02
                return bytes([self.address, function, flags, self.CHECKSUM])
            if function == 0x3B:
                return bytes([self.address, function, 0x04 if self._mock_homing else 0x00, self.CHECKSUM])
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._command(function, payload, response_length=response_length)
            except LinearStageError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.15)
        assert last_error is not None
        raise last_error

    def read_position_mm(self) -> float:
        response = self._query(0x36, 8)
        sign = -1.0 if response[2] else 1.0
        raw = int.from_bytes(response[3:7], "big")
        return sign * raw / 65536.0 * self.lead_mm

    def read_flags(self) -> int:
        return self._query(0x3A, 4)[2]

    def read_home_status(self) -> int:
        return self._query(0x3B, 4)[2]

    def enable(self, enabled: bool = True) -> bytes:
        self._command(0xF3, bytes([0xAB, int(bool(enabled)), 0x00]), response_length=0)
        if self.mock:
            self._mock_enabled = bool(enabled)
        last_response = b""
        for _ in range(5):
            time.sleep(0.1)
            last_response = self._query(0x3A, 4)
            if bool(last_response[2] & 0x01) is bool(enabled):
                return last_response
        raise LinearStageError(
            f"enable state verification failed: {last_response.hex(' ').upper()}"
        )

    def stop(self) -> bytes:
        return self._write(0xFE, bytes([0x98, 0x00]))

    def reset_protection(self) -> bytes:
        return self._write(0x0E, bytes([0x52]))

    def set_response_mode(self, mode: int = 1, *, permanent: bool = True) -> bytes:
        if mode not in (0, 1, 2, 3, 4):
            raise ValueError("response mode must be in 0..4")
        if self.mock:
            return self._command(0x48, bytes([0xD1, int(permanent)]) + bytes(30))
        response = self._query(0x42, 33, bytes([0x6C]))
        config = bytearray(response[2:32])
        config[20] = mode
        payload = bytes([0xD1, int(permanent)]) + bytes(config)
        return self._command(0x48, payload)

    def configure_sensorless_home(
        self,
        *,
        direction: int | None = None,
        homing_rpm: int | None = None,
        timeout_ms: int = 10000,
        collision_rpm: int = 300,
        collision_current_ma: int = 800,
        collision_time_ms: int = 60,
    ) -> bytes:
        direction = self.home_direction if direction is None else int(direction)
        homing_rpm = self.home_speed_rpm if homing_rpm is None else int(homing_rpm)
        if direction not in (0, 1):
            raise ValueError("homing direction must be 0 (CW) or 1 (CCW)")
        if not 0 <= homing_rpm <= 300:
            raise ValueError("native Emm homing speed must be in 0..300 RPM")
        payload = bytes([0xAE, 0x00, 0x02, direction])
        payload += homing_rpm.to_bytes(2, "big")
        payload += int(timeout_ms).to_bytes(4, "big")
        payload += int(collision_rpm).to_bytes(2, "big")
        payload += int(collision_current_ma).to_bytes(2, "big")
        payload += int(collision_time_ms).to_bytes(2, "big")
        payload += bytes([0x00])
        return self._command(0x4C, payload)

    def home(self, *, direction: int | None = None, timeout_s: float = 30.0) -> bytes:
        """Run native sensorless homing; default direction is CCW (1)."""
        direction = self.home_direction if direction is None else int(direction)
        self.configure_sensorless_home(direction=direction)
        self.enable(True)
        trigger = self._write(0x9A, bytes([0x02, 0x00]))
        if self.mock:
            self._mock_homing = True
            self._commanded_position_mm = 0.0
            return trigger
        started = False
        last_status = 0
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            time.sleep(0.1)
            last_status = self.read_home_status()
            if last_status & 0x08:
                raise LinearStageError("motor reports homing failure")
            if last_status & 0x04:
                started = True
            elif started:
                self._commanded_position_mm = 0.0
                return trigger
        if started:
            raise LinearStageError(f"motor homing did not finish; status=0x{last_status:02X}")
        raise LinearStageError(f"motor did not enter homing state; status=0x{last_status:02X}")

    def move_relative(
        self,
        distance_mm: float,
        *,
        speed_rpm: int | None = None,
        acceleration: int | None = None,
    ) -> bytes:
        distance = float(distance_mm)
        speed = self.default_speed_rpm if speed_rpm is None else int(speed_rpm)
        acc = self.default_acceleration if acceleration is None else int(acceleration)
        if distance == 0:
            raise ValueError("distance_mm must not be zero")
        if abs(distance) > self.travel_mm:
            raise ValueError(f"absolute move distance must be <= {self.travel_mm} mm")
        if not 1 <= speed <= 3000:
            raise ValueError("speed_rpm must be in 1..3000")
        if not 0 <= acc <= 255:
            raise ValueError("acceleration must be in 0..255")
        target = self._commanded_position_mm + distance
        if not 0.0 <= target <= self.travel_mm:
            raise ValueError(
                f"commanded target {target:.3f} mm is outside 0..{self.travel_mm:.3f} mm; "
                "home first or reverse direction"
            )
        pulses = round(abs(distance) * self.pulses_per_mm)
        direction = 0x00 if distance >= 0 else 0x01
        payload = bytes([direction]) + speed.to_bytes(2, "big") + bytes([acc])
        payload += pulses.to_bytes(4, "big") + bytes([0x00, 0x00])
        self.enable(True)
        time.sleep(0.1)
        self._command(0xFD, payload)
        frame = self._frame(0xFD, payload)
        self._commanded_position_mm = target
        return frame

    def move_absolute(
        self,
        target_mm: float,
        *,
        speed_rpm: int | None = None,
        acceleration: int | None = None,
    ) -> bytes:
        target = float(target_mm)
        if not 0.0 <= target <= self.travel_mm:
            raise ValueError(
                f"absolute target {target:.3f} mm is outside 0..{self.travel_mm:.3f} mm"
            )
        distance = target - self._commanded_position_mm
        if abs(distance) < 1e-9:
            return b""
        return self.move_relative(
            distance,
            speed_rpm=speed_rpm,
            acceleration=acceleration,
        )

    def get_status(self, *, live: bool = False) -> dict[str, Any]:
        status = {
            "connected": self.is_connected(),
            "port": self.port,
            "address": self.address,
            "baudrate": self.baudrate,
            "commanded_position_mm": self._commanded_position_mm,
            "position_verified": False,
            "response_mode": "query_verified" if live else "session_snapshot",
            "home_direction": "CW" if self.home_direction == 0 else "CCW",
            "home_speed_rpm": self.home_speed_rpm,
            "default_speed_rpm": self.default_speed_rpm,
            "default_acceleration": self.default_acceleration,
        }
        if live and self.is_connected():
            try:
                status.update({
                    "actual_position_mm": self.read_position_mm(),
                    "flags_raw": self.read_flags(),
                    "home_status_raw": self.read_home_status(),
                    "position_verified": True,
                    "feedback_error": None,
                })
            except Exception as exc:
                status["feedback_error"] = str(exc)
        return status
