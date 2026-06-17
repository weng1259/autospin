"""Spike B — GantryBackend via OpenBuilds CONTROL socket.io.

1-hour spike for Phase 3.0 (see ADR-002 revision / ADR-004 / migration-checklist).

Assumes OpenBuilds CONTROL 1.0.390 is installed and running (Electron app bound
to http://localhost:3000). The app owns the USB port; our Python process talks
only to its socket.io surface.

Events consumed (per /tmp/cncjs-alternatives-research/openbuilds/index.js):
  client → server: connectTo / runCommand / jog / jogTo / stop / clearAlarm / resetMachine
  server → client: status (every 100ms, full status object) / data (gcode lines + tags)
                    / errorsCleared / grbl / sysinfo

API quirks that affect ADR-004 mapping (see writeup in ADR-002 末尾):
  - No `home` event; $H must go via runCommand (blocks queue)
  - jog uses CSV string "X,10,2000" not typed object
  - clearAlarm takes magic-number 1 (just $X) or 2 (drain + $X)
  - No per-command ack; we infer completion by polling status.comms.runStatus
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import socketio

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


class OpenBuildsGantryBackend:
    """Socket.io client to a running OpenBuilds CONTROL instance.

    Public surface mirrors ADR-004 `GantryBackend`. For this spike only
    home() + move_to() are fully implemented.
    """

    def __init__(
        self,
        *,
        ob_url: str = "http://localhost:3000",
        serial_port: str = "/dev/cu.wchusbserial110",
        serial_baud: int = 115200,
        dry_run: bool = False,
    ):
        self.ob_url = ob_url
        self.serial_port = serial_port
        self.serial_baud = serial_baud
        self.dry_run = dry_run

        self.sio = socketio.Client(logger=False, engineio_logger=False)
        self._status_lock = threading.Lock()
        self._last_status: dict[str, Any] = {}
        self._last_status_ts_ms: float = 0.0
        self._alarm_cleared_evt = threading.Event()
        self._idem_cache: dict[str, HomeResult | MoveResult] = {}
        self._is_homed: bool = False

        @self.sio.on("status")
        def _on_status(data):  # noqa: ANN001
            with self._status_lock:
                self._last_status = data
                self._last_status_ts_ms = now_ms()

        @self.sio.on("data")
        def _on_data(payload):  # noqa: ANN001
            # payload = {command, response, type}  type ∈ {info, error, ...}
            # OpenBuilds already filtered raw gcode; we only flag errors.
            if isinstance(payload, dict) and payload.get("type") == "error":
                # surfaced via next get_status() / caller's timeout
                pass

        @self.sio.on("errorsCleared")
        def _on_cleared(_):  # noqa: ANN001
            self._alarm_cleared_evt.set()

    # ── lifecycle ──

    def connect(self, *, connect_timeout_s: float = 10.0) -> None:
        if self.dry_run:
            return
        try:
            self.sio.connect(self.ob_url, wait_timeout=connect_timeout_s)
        except socketio.exceptions.ConnectionError as e:
            raise L3ConnectionError(
                human_message=f"无法连 OpenBuilds {self.ob_url}",
                agent_message=f"socket.io connect to {self.ob_url} failed: {e!r}. "
                              f"Verify OpenBuilds CONTROL is running (Electron window).",
            ) from e
        # ask OpenBuilds to open its own serial to Arduino
        self.sio.emit("connectTo", {
            "type": "usb",
            "port": self.serial_port,
            "baud": self.serial_baud,
        })
        # wait for first status frame
        deadline = time.time() + connect_timeout_s
        while time.time() < deadline:
            if self._last_status and self._last_status.get("comms", {}) \
                    .get("connectionStatus", 0) >= 1:
                return
            time.sleep(0.1)
        raise L3ConnectionError(
            human_message="OpenBuilds 未能打开串口",
            agent_message=f"connectTo {self.serial_port} did not raise "
                          f"connectionStatus >= 1 within {connect_timeout_s}s.",
        )

    def close(self) -> None:
        if self.sio.connected:
            self.sio.disconnect()

    # ── state queries ──

    def get_status(self) -> MachineStatus:
        if self.dry_run:
            return MachineStatus(
                state=MachineState.IDLE,
                position=Position(x_mm=0, y_mm=0, z_mm=0),
                is_homed=self._is_homed,
            )
        with self._status_lock:
            raw = dict(self._last_status)
            ts = self._last_status_ts_ms
        state = self._map_state(raw)
        pos = self._map_position(raw)
        comms = raw.get("comms", {})
        machine = raw.get("machine", {})
        return MachineStatus(
            state=state,
            position=pos,
            alarm_code=None,  # OpenBuilds stringifies; code not exposed
            is_homed=self._is_homed,
            planner_buffer_free=machine.get("bufferSpace"),
            rx_buffer_free=None,
            raw=str(comms.get("runStatus", "")),
            last_update_ms_ago=now_ms() - ts if ts else 0.0,
        )

    def get_position(self) -> Position:
        return self.get_status().position

    def is_connected(self) -> bool:
        if not self.sio.connected:
            return False
        return (
            self._last_status.get("comms", {}).get("connectionStatus", 0) >= 1
        )

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

        st = self.get_status()
        if st.state == MachineState.ALARM:
            # OpenBuilds exposes 2 magic numbers; 2 = drain + $X
            self._alarm_cleared_evt.clear()
            self.sio.emit("clearAlarm", 2)
            if not self._alarm_cleared_evt.wait(timeout=5.0):
                raise AlarmStateError(
                    human_message="clearAlarm 超时",
                    agent_message="OpenBuilds did not emit errorsCleared within 5s "
                                  "after clearAlarm(2).",
                )

        # $H via runCommand. OpenBuilds blocks its queue during $H.
        self.sio.emit("runCommand", "$H\n")

        # wait for Idle (runStatus becomes 'Stopped' + state Idle)
        self._wait_runstatus(expected={"Stopped", "Idle"}, timeout_s=90.0,
                             err_label="homing")
        self._is_homed = True
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
                human_message="机器未归零",
                agent_message="is_homed=False; call gantry.home() first.",
            )

        for axis, v, lo, hi in (
            ("x", target.x_mm, -280, 0),
            ("y", target.y_mm, -280, 0),
            ("z", target.z_mm, -95, 0),
        ):
            if not (lo <= v <= hi):
                raise SoftLimitExceededError(
                    human_message=f"{axis}={v} 超出 [{lo},{hi}]",
                    agent_message=f"Axis {axis}={v} outside [{lo},{hi}] mm.",
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

        self.sio.emit("runCommand", cmd)

        if wait_for_idle:
            self._wait_runstatus(expected={"Stopped", "Idle"}, timeout_s=timeout_s,
                                 err_label="move")

        return MoveResult(
            success=True,
            final_position=self.get_position(),
            duration_ms=now_ms() - t0,
            event_id=event_id,
        )

    # ── internals ──

    def _wait_runstatus(self, *, expected: set[str], timeout_s: float,
                        err_label: str) -> None:
        deadline = time.time() + timeout_s
        seen_running = False
        while time.time() < deadline:
            st_raw = self._last_status.get("comms", {}).get("runStatus", "")
            if st_raw == "Running":
                seen_running = True
            if seen_running and st_raw in expected:
                return
            # OpenBuilds emits alarm via data+status; detect via alarm string
            if self._last_status.get("comms", {}).get("alarm"):
                raise AlarmStateError(
                    human_message=f"{err_label} 进 alarm",
                    agent_message=f"OpenBuilds status.comms.alarm="
                                  f"{self._last_status['comms']['alarm']!r}; "
                                  f"call unlock_alarm() or resetMachine.",
                )
            time.sleep(0.1)
        raise HomingTimeoutError(
            human_message=f"{err_label} 未在 {timeout_s}s 内结束",
            agent_message=f"{err_label} did not reach any of {expected} in "
                          f"{timeout_s}s.",
        )

    @staticmethod
    def _map_state(raw: dict) -> MachineState:
        run_status = raw.get("comms", {}).get("runStatus", "").lower()
        alarm = raw.get("comms", {}).get("alarm", "")
        if alarm:
            return MachineState.ALARM
        return {
            "running": MachineState.RUN,
            "stopped": MachineState.IDLE,
            "idle": MachineState.IDLE,
            "paused": MachineState.HOLD,
        }.get(run_status, MachineState.UNKNOWN)

    @staticmethod
    def _map_position(raw: dict) -> Position:
        pos = raw.get("machine", {}).get("position", {}).get("work", {})
        try:
            return Position(
                x_mm=float(pos.get("x", 0.0)),
                y_mm=float(pos.get("y", 0.0)),
                z_mm=float(pos.get("z", 0.0)),
            )
        except Exception:
            return Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)


def _quickrun(ob_url: str, serial_port: str, real_home: bool = False) -> None:
    backend = OpenBuildsGantryBackend(ob_url=ob_url, serial_port=serial_port)
    try:
        backend.connect()
        print(f"[spike-b] connected to OpenBuilds at {ob_url} "
              f"with serial {serial_port}")
        status = backend.get_status()
        print(f"[spike-b] initial status: state={status.state.value} "
              f"pos=({status.position.x_mm:.1f},{status.position.y_mm:.1f},"
              f"{status.position.z_mm:.1f})")

        dry = OpenBuildsGantryBackend(ob_url=ob_url, serial_port=serial_port,
                                      dry_run=True)
        r = dry.home(idempotency_key="dry-1")
        print(f"[spike-b] dry-run home ok event={r.event_id[:8]}")

        if real_home:
            print("[spike-b] issuing real $H via OpenBuilds runCommand")
            r = backend.home(idempotency_key="quickrun-1")
            print(f"[spike-b] real home ok event={r.event_id[:8]} "
                  f"duration={r.duration_ms:.1f}ms")
    finally:
        backend.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--ob-url", default="http://localhost:3000")
    ap.add_argument("--serial-port", default="/dev/cu.wchusbserial110")
    ap.add_argument("--real-home", action="store_true")
    args = ap.parse_args()
    _quickrun(args.ob_url, args.serial_port, real_home=args.real_home)
