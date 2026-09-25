"""Spike A — GantryBackend via direct pyserial + bCNC-style char-counting.

1-hour spike for Phase 3.0 (see ADR-002 revision / ADR-004 / migration-checklist).

Char-counting algorithm ported from bCNC `Sender.py` L644-860 + `_GenericController.py`
L247-293:
    cline = list of in-flight command byte lengths
    sline = list of in-flight command strings (for error attribution)
    precondition to send tosend: sum(cline) + len(tosend) <= RX_BUFFER_SIZE (128)
    on 'ok'     : del cline[0], del sline[0]
    on 'error:' : del cline[0], errline = sline.pop(0), set _alarm, _stop
    on 'ALARM:' : del cline[0], errline = sline.pop(0), set _alarm, _stop
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    AlarmStateError,
    ConnectionError as L3ConnectionError,
    HomeResult,
    HomingTimeoutError,
    L3Error,
    MachineNotHomedError,
    MachineState,
    MachineStatus,
    MoveResult,
    Position,
    SoftLimitExceededError,
    new_event_id,
    now_ms,
)

RX_BUFFER_SIZE = 128  # grbl AVR RX buffer in bytes
STATUS_REGEX = re.compile(
    r"<(?P<state>[A-Za-z]+)(?::\d+)?"
    r"\|(?:MPos|WPos):(?P<mx>-?[\d.]+),(?P<my>-?[\d.]+),(?P<mz>-?[\d.]+)"
    r"(?:\|Bf:(?P<bf_planner>\d+),(?P<bf_rx>\d+))?"
    r".*?>"
)
ALARM_REGEX = re.compile(r"ALARM:(\d+)")


class GrblDirectGantryBackend:
    """Direct pyserial GantryBackend with bCNC char-counting flow control.

    Public surface mirrors ADR-004 `GantryBackend`. For this spike only home()
    and move_to() are fully implemented; other methods are stubs / raise.
    """

    def __init__(self, port: str, baud: int = 115200, dry_run: bool = False):
        self.port = port
        self.baud = baud
        self.dry_run = dry_run
        self._ser: Optional[serial.Serial] = None
        self._cline: list[int] = []
        self._sline: list[str] = []
        self._lock = threading.Lock()
        self._status: MachineStatus = MachineStatus(
            state=MachineState.DISCONNECTED,
            position=Position(x_mm=0, y_mm=0, z_mm=0),
        )
        self._status_ts_ms: float = 0.0
        self._is_homed: bool = False
        self._alarm_code: Optional[int] = None
        self._idem_cache: dict[str, HomeResult | MoveResult] = {}

    # ── lifecycle ──

    def connect(self) -> None:
        if self.dry_run:
            return
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except serial.SerialException as e:
            raise L3ConnectionError(
                human_message=f"无法打开串口 {self.port}",
                agent_message=f"serial.open failed: {e!r}. Verify {self.port} exists "
                              f"and no other process holds it (cncjs/OpenBuilds).",
            ) from e
        time.sleep(2.0)  # AVR resets on DTR; give grbl time to print banner
        self._ser.reset_input_buffer()
        self._poll_status(timeout_s=2.0)

    def close(self) -> None:
        if self._ser:
            self._ser.close()
            self._ser = None

    # ── state queries (read-only, ADR-004 原则 4) ──

    def get_status(self) -> MachineStatus:
        if self.dry_run:
            return self._status.model_copy(update={"last_update_ms_ago": 0.0})
        self._poll_status(timeout_s=1.0)
        return self._status.model_copy(
            update={"last_update_ms_ago": now_ms() - self._status_ts_ms}
        )

    def get_position(self) -> Position:
        return self.get_status().position

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def is_homed(self) -> bool:
        return self._is_homed

    # ── actions ──

    def home(self, *, idempotency_key: str) -> HomeResult:
        if idempotency_key in self._idem_cache:
            cached = self._idem_cache[idempotency_key]
            if isinstance(cached, HomeResult):
                return cached

        t0 = now_ms()
        event_id = new_event_id()

        if self.dry_run:
            result = HomeResult(
                success=True,
                position_after_pulloff=Position(x_mm=0, y_mm=0, z_mm=0),
                duration_ms=0.0,
                event_id=event_id,
            )
            self._idem_cache[idempotency_key] = result
            return result

        # safety: ALARM must be cleared before $H (except alarm 11 "homing required")
        st = self.get_status()
        if st.state == MachineState.ALARM and st.alarm_code not in (None, 11):
            raise AlarmStateError(
                human_message=f"机器处于 alarm {st.alarm_code}，先 unlock",
                agent_message=f"grbl in ALARM:{st.alarm_code}; call unlock_alarm() "
                              f"before home().",
            )

        # $H blocks for the entire homing cycle. Send it as a line and wait
        # for 'ok'; enforce 90s hard timeout.
        self._send_line_blocking("$H\n", timeout_s=90.0,
                                 timeout_err="Homing did not complete in 90s")
        self._is_homed = True
        self._alarm_code = None
        final_pos = self.get_position()
        result = HomeResult(
            success=True,
            position_after_pulloff=final_pos,
            duration_ms=now_ms() - t0,
            event_id=event_id,
        )
        self._idem_cache[idempotency_key] = result
        return result

    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: float = 2000,
        wait_for_idle: bool = True,
        timeout_s: float = 60.0,
    ) -> MoveResult:
        if not self._is_homed and not self.dry_run:
            raise MachineNotHomedError(
                human_message="机器未归零，不能绝对移动",
                agent_message="is_homed=False; call gantry.home() first.",
            )

        # pydantic Field bounds already enforce [-280,0]/[-95,0] but we
        # double-check in case limits shrink in constants.yaml.
        for axis, v, lo, hi in (
            ("x", target.x_mm, -280, 0),
            ("y", target.y_mm, -280, 0),
            ("z", target.z_mm, -95, 0),
        ):
            if not (lo <= v <= hi):
                raise SoftLimitExceededError(
                    human_message=f"{axis}={v} 超出 [{lo},{hi}]",
                    agent_message=f"Axis {axis}={v} mm outside soft limit "
                                  f"[{lo},{hi}]; adjust Position.",
                )

        t0 = now_ms()
        event_id = new_event_id()
        cmd = (
            f"G90 G1 X{target.x_mm:.3f} Y{target.y_mm:.3f} "
            f"Z{target.z_mm:.3f} F{feed_mm_min:.0f}\n"
        )

        if self.dry_run:
            return MoveResult(
                success=True,
                final_position=target,
                duration_ms=0.0,
                event_id=event_id,
            )

        self._send_line_blocking(cmd, timeout_s=timeout_s,
                                 timeout_err=f"Move did not ack in {timeout_s}s")
        if wait_for_idle:
            self._wait_idle(timeout_s=timeout_s)

        return MoveResult(
            success=True,
            final_position=self.get_position(),
            duration_ms=now_ms() - t0,
            event_id=event_id,
        )

    # ── internals: bCNC char-counting ──

    def _send_line_blocking(
        self, line: str, *, timeout_s: float, timeout_err: str
    ) -> None:
        """Send one line, return only when grbl replies 'ok' or raise on error.

        Uses the bCNC cline/sline trick: buffer-full backpressure is satisfied
        by `sum(cline) + len(line) <= RX_BUFFER_SIZE`; for single-line sends
        that's trivially true (128 >> typical 30-byte gcode line).
        """
        assert self._ser is not None
        with self._lock:
            # wait until RX buffer has room
            deadline = time.time() + timeout_s
            while sum(self._cline) + len(line) > RX_BUFFER_SIZE:
                if time.time() > deadline:
                    raise L3Error("RX buffer full for too long",
                                  f"cline={self._cline}")
                self._drain_once()

            self._ser.write(line.encode("ascii"))
            self._cline.append(len(line))
            self._sline.append(line.rstrip())

            # wait for matching 'ok' or error
            while self._sline and self._sline[0] == line.rstrip():
                if time.time() > deadline:
                    raise HomingTimeoutError(
                        human_message=timeout_err,
                        agent_message=f"{timeout_err} (line={line.strip()!r})",
                    )
                self._drain_once()

    def _drain_once(self) -> None:
        """Read available lines, update cline/sline on 'ok' / errors."""
        assert self._ser is not None
        if self._ser.in_waiting == 0:
            # ask for status so we keep self._status fresh, but cheap
            time.sleep(0.01)
            return
        raw = self._ser.readline().decode("ascii", "ignore").strip()
        if not raw:
            return
        if raw.startswith("<"):
            self._parse_status_line(raw)
        elif "ok" in raw:
            if self._cline:
                self._cline.pop(0)
            if self._sline:
                self._sline.pop(0)
        elif "error:" in raw or "ALARM:" in raw:
            errline = self._sline.pop(0) if self._sline else "<unknown>"
            if self._cline:
                self._cline.pop(0)
            m = ALARM_REGEX.search(raw)
            self._alarm_code = int(m.group(1)) if m else None
            self._status = self._status.model_copy(
                update={"state": MachineState.ALARM,
                        "alarm_code": self._alarm_code}
            )
            raise AlarmStateError(
                human_message=f"grbl 报错: {raw} (line={errline})",
                agent_message=f"grbl returned {raw!r} for {errline!r}; "
                              f"alarm_code={self._alarm_code}. Call unlock_alarm().",
            )

    def _poll_status(self, timeout_s: float = 1.0) -> None:
        assert self._ser is not None
        # retry a few times: grbl may swallow the first ? while it's
        # still printing boot banners, or we may read a stale 'ok'.
        deadline = time.time() + timeout_s
        retries = 0
        while time.time() < deadline:
            self._ser.write(b"?")
            self._ser.flush()
            inner_deadline = min(deadline, time.time() + 0.3)
            while time.time() < inner_deadline:
                raw = self._ser.readline().decode("ascii", "ignore").strip()
                if raw.startswith("<"):
                    self._parse_status_line(raw)
                    return
            retries += 1

    def _parse_status_line(self, raw: str) -> None:
        m = STATUS_REGEX.match(raw)
        if not m:
            return
        state_str = m.group("state").lower()
        try:
            state = MachineState(state_str)
        except ValueError:
            state = MachineState.UNKNOWN
        self._status = MachineStatus(
            state=state,
            position=Position(
                x_mm=float(m.group("mx")),
                y_mm=float(m.group("my")),
                z_mm=float(m.group("mz")),
            ),
            alarm_code=self._alarm_code,
            is_homed=self._is_homed,
            planner_buffer_free=int(m.group("bf_planner")) if m.group("bf_planner") else None,
            rx_buffer_free=int(m.group("bf_rx")) if m.group("bf_rx") else None,
            raw=raw,
        )
        self._status_ts_ms = now_ms()

    def _wait_idle(self, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self.get_status()
            if st.state == MachineState.IDLE:
                return
            if st.state == MachineState.ALARM:
                raise AlarmStateError(
                    human_message=f"运动后进 alarm: {st.alarm_code}",
                    agent_message=f"Post-move state=ALARM:{st.alarm_code}; "
                                  f"call unlock_alarm().",
                )
            time.sleep(0.1)
        raise HomingTimeoutError(
            human_message=f"运动未在 {timeout_s}s 内结束",
            agent_message=f"Motion did not reach Idle within {timeout_s}s.",
        )


# ── quickrun: connect, dry-run home, optionally real home ──

def _quickrun(port: str, real_home: bool = False) -> None:
    backend = GrblDirectGantryBackend(port=port)
    backend.connect()
    try:
        print(f"[spike-a] connected to {port}")
        status = backend.get_status()
        print(f"[spike-a] initial status: state={status.state.value} "
              f"pos=({status.position.x_mm:.1f},{status.position.y_mm:.1f},"
              f"{status.position.z_mm:.1f}) "
              f"Bf={status.planner_buffer_free}/{status.rx_buffer_free}")
        # dry-run home always works
        dry = GrblDirectGantryBackend(port=port, dry_run=True)
        r = dry.home(idempotency_key="dry-1")
        print(f"[spike-a] dry-run home ok event={r.event_id[:8]} "
              f"duration={r.duration_ms:.1f}ms")

        if real_home:
            print("[spike-a] issuing real $H (z first, then x/y). Ctrl-C aborts.")
            r = backend.home(idempotency_key="quickrun-1")
            print(f"[spike-a] real home ok event={r.event_id[:8]} "
                  f"duration={r.duration_ms:.1f}ms "
                  f"pos={r.position_after_pulloff}")
    finally:
        backend.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.wchusbserial110")
    ap.add_argument("--real-home", action="store_true")
    args = ap.parse_args()
    _quickrun(args.port, real_home=args.real_home)
