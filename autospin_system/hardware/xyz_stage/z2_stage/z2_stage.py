"""Standalone Z2/A-axis controller for the grbl Arduino Mega.

This module is intentionally separate from the existing XYZStage wrapper. It is
for bring-up of the new single Z2 rail mapped to grbl's A axis, especially the
first no-limit-switch test sequence:

    $X
    $21=0
    G91
    G0 A1 F100
    G0 A-1 F100
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import serial

try:
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ModuleNotFoundError:  # pragma: no cover - direct-script execution
    from config.hardware_config import CONFIG


STATUS_RE = re.compile(r"<(?P<state>[A-Za-z]+)(?::\d+)?\|(?:MPos|WPos):(?P<pos>[-\d.,]+).*?>")


@dataclass(frozen=True)
class Z2Status:
    state: str
    x_mm: Optional[float]
    y_mm: Optional[float]
    z_mm: Optional[float]
    a_mm: Optional[float]
    raw: str


class Z2Stage:
    """Direct serial control for the added Z2 rail exposed as grbl axis A."""

    def __init__(
        self,
        port: str | None = None,
        baudrate: int | None = None,
        timeout: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        stage_cfg = CONFIG.get("devices", {}).get("xyz_stage", {})
        comm_cfg = CONFIG.get("communication", {})

        self.port = port or comm_cfg.get("gantry_port", "COM6")
        self.baudrate = int(baudrate or stage_cfg.get("baudrate", 115200))
        self.timeout = float(timeout or stage_cfg.get("timeout", 0.5))
        self.logger = logger or logging.getLogger(__name__)
        self._ser: serial.Serial | None = None

    def connect(self) -> bool:
        if self.is_connected():
            return True
        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            # Opening an Arduino serial port resets AVR grbl.
            time.sleep(2.0)
            self._ser.reset_input_buffer()
            self.logger.info("Z2Stage connected to %s @ %s", self.port, self.baudrate)
            return True
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            self.logger.error("Z2Stage serial open failed: %s (%s)", self.port, exc)
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    disconnect = close

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def unlock(self) -> None:
        self._send_line_wait_ok("$X", timeout_s=3.0)

    def soft_reset(self) -> None:
        ser = self._require_serial()
        ser.write(b"\x18")
        ser.flush()
        time.sleep(2.0)
        ser.reset_input_buffer()

    def set_hard_limits(self, enabled: bool) -> None:
        self._send_line_wait_ok(f"$21={1 if enabled else 0}", timeout_s=3.0)

    def set_relative_mode(self) -> None:
        self._send_line_wait_ok("G91", timeout_s=3.0)

    def set_absolute_mode(self) -> None:
        self._send_line_wait_ok("G90", timeout_s=3.0)

    def move_relative(
        self,
        distance_mm: float,
        *,
        feed_mm_min: float = 100.0,
        wait_for_idle: bool = True,
        timeout_s: float = 30.0,
    ) -> Z2Status | None:
        """Move only the grbl A axis by a relative distance in millimetres."""
        if feed_mm_min <= 0:
            raise ValueError("feed_mm_min must be positive")
        self.set_relative_mode()
        self._send_line_wait_ok(
            f"G0 A{float(distance_mm):.3f} F{float(feed_mm_min):.0f}",
            timeout_s=timeout_s,
        )
        if wait_for_idle:
            return self.wait_idle(timeout_s=timeout_s)
        return None

    def jog_test(
        self,
        *,
        distance_mm: float = 1.0,
        feed_mm_min: float = 100.0,
        disable_hard_limits: bool = True,
    ) -> None:
        """Run the first safe bring-up sequence: unlock, G91, A+, A-."""
        self.unlock()
        if disable_hard_limits:
            self.set_hard_limits(False)
        self.set_relative_mode()
        self.move_relative(distance_mm, feed_mm_min=feed_mm_min)
        self.move_relative(-distance_mm, feed_mm_min=feed_mm_min)

    def get_status(self) -> Z2Status:
        ser = self._require_serial()
        ser.write(b"?")
        ser.flush()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            raw = ser.readline().decode("ascii", "ignore").strip()
            if raw.startswith("<"):
                return self._parse_status(raw)
        raise TimeoutError("grbl did not return a status line within 2s")

    def get_settings(self) -> dict[str, str]:
        lines = self._send_line_collect("$$", timeout_s=5.0)
        settings: dict[str, str] = {}
        for line in lines:
            if line.startswith("$") and "=" in line:
                key, value = line[1:].split("=", 1)
                settings[key] = value
        return settings

    def has_a_axis_settings(self) -> bool:
        settings = self.get_settings()
        return all(key in settings for key in ("103", "113", "123", "133"))

    def wait_idle(self, *, timeout_s: float = 30.0) -> Z2Status:
        deadline = time.time() + timeout_s
        last_status: Z2Status | None = None
        while time.time() < deadline:
            last_status = self.get_status()
            if last_status.state.lower() == "idle":
                return last_status
            if last_status.state.lower() == "alarm":
                raise RuntimeError(f"grbl entered Alarm: {last_status.raw}")
            time.sleep(0.05)
        raise TimeoutError(
            f"Z2/A move did not reach Idle within {timeout_s}s; "
            f"last_status={last_status.raw if last_status else '<none>'}"
        )

    def halt(self) -> None:
        ser = self._require_serial()
        ser.write(b"\x85")
        ser.flush()

    def _require_serial(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            raise RuntimeError("Z2Stage is not connected")
        return self._ser

    def _send_line_wait_ok(self, line: str, *, timeout_s: float) -> None:
        lines = self._send_line_collect(line, timeout_s=timeout_s)
        for raw in lines:
            low = raw.lower()
            if low.startswith("error:") or raw.startswith("ALARM:"):
                raise RuntimeError(f"grbl rejected {line!r}: {raw}")

    def _send_line_collect(self, line: str, *, timeout_s: float) -> list[str]:
        ser = self._require_serial()
        ser.write((line.rstrip() + "\n").encode("ascii"))
        ser.flush()

        deadline = time.time() + timeout_s
        collected: list[str] = []
        while time.time() < deadline:
            raw = ser.readline().decode("ascii", "ignore").strip()
            if not raw:
                continue
            collected.append(raw)
            low = raw.lower()
            if low == "ok":
                return collected
            if low.startswith("error:") or raw.startswith("ALARM:"):
                raise RuntimeError(f"grbl returned {raw!r} for {line!r}")
        raise TimeoutError(f"grbl did not acknowledge {line!r} within {timeout_s}s")

    def _parse_status(self, raw: str) -> Z2Status:
        match = STATUS_RE.match(raw)
        if not match:
            return Z2Status("Unknown", None, None, None, None, raw)
        values = [float(part) for part in match.group("pos").split(",") if part]
        return Z2Status(
            state=match.group("state"),
            x_mm=values[0] if len(values) > 0 else None,
            y_mm=values[1] if len(values) > 1 else None,
            z_mm=values[2] if len(values) > 2 else None,
            a_mm=values[3] if len(values) > 3 else None,
            raw=raw,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    z2 = Z2Stage()
    if not z2.connect():
        raise SystemExit(1)
    try:
        print("A-axis settings present:", z2.has_a_axis_settings())
        z2.jog_test()
        print("Final status:", z2.get_status())
    finally:
        z2.close()
