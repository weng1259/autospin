"""Small motion matrix for gantry alarm isolation.

This script homes grbl and then runs a conservative absolute-position matrix in
the same serial session. It does not soft-reset grbl, because soft-reset
invalidates position.

1. XY-only at Z=-5
2. Z-only down/up at low feed
3. XYZ diagonal at low feed

It listens for grbl reset banners, ALARM lines, Pn changes, and serial errors.
Z-brake CH2 is released only for moves that change Z and is locked in finally.
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from dataclasses import dataclass

import serial


GRBL_PORT = "/dev/cu.wchusbserial110"
RELAY_PORT = "/dev/cu.usbmodem6670E00119391"
GRBL_BAUD = 115200
RELAY_BAUD = 9600


def relay_frame(channel: int, on: bool) -> bytes:
    op = 0x01 if on else 0x00
    checksum = (0xA0 + channel + op) & 0xFF
    return bytes([0xA0, channel, op, checksum])


@dataclass(frozen=True)
class Target:
    label: str
    x: float
    y: float
    z: float
    feed: float


class MatrixRunner:
    def __init__(self) -> None:
        self.grbl = serial.Serial(GRBL_PORT, GRBL_BAUD, timeout=0.1)
        time.sleep(2.0)
        self.grbl.reset_input_buffer()
        self.relay = serial.Serial(RELAY_PORT, RELAY_BAUD, timeout=1.0)
        time.sleep(0.5)
        self.stats: Counter[str] = Counter()
        self.last_pn = ""
        self.last_state = ""

    def close(self) -> None:
        try:
            self.lock_brake()
        finally:
            self.relay.close()
            self.grbl.close()

    def release_brake(self) -> None:
        self.relay.write(relay_frame(2, True))
        self.relay.flush()
        time.sleep(0.5)

    def lock_brake(self) -> None:
        self.relay.write(relay_frame(2, False))
        self.relay.flush()
        time.sleep(0.5)

    def read_line(self, timeout_s: float = 0.1) -> str:
        old_timeout = self.grbl.timeout
        self.grbl.timeout = timeout_s
        try:
            return self.grbl.readline().decode("utf-8", errors="replace").strip()
        finally:
            self.grbl.timeout = old_timeout

    def classify(self, line: str) -> None:
        if not line:
            return
        if line.startswith("Grbl "):
            self.stats["banners"] += 1
            print(f"RST {line}")
            return
        if line.startswith("ALARM:"):
            self.stats["alarms"] += 1
            print(f"ALM {line}")
            return
        if line.startswith("error:"):
            self.stats["errors"] += 1
            print(f"ERR {line}")
            return
        if line == "ok":
            self.stats["ok"] += 1
            print("ACK ok")
            return
        if line.startswith("<"):
            self.stats["status"] += 1
            body = line.strip("<>")
            parts = body.split("|")
            state = parts[0] if parts else "?"
            pn = ""
            for part in parts[1:]:
                if part.startswith("Pn:"):
                    pn = part[3:]
                    break
            if state != self.last_state:
                print(f"STA {self.last_state or '-'} -> {state}  {line}")
                self.last_state = state
            if pn != self.last_pn:
                print(f"PIN {self.last_pn or '-'} -> {pn or '-'}  {line}")
                if pn:
                    self.stats["pn_hits"] += 1
                self.last_pn = pn
            return
        if line.startswith("[MSG:"):
            print(f"MSG {line}")

    def poll_status(self) -> str:
        self.grbl.write(b"?")
        self.grbl.flush()
        end = time.time() + 0.5
        last = ""
        while time.time() < end:
            line = self.read_line(0.1)
            self.classify(line)
            if line.startswith("<"):
                last = line
                break
        return last

    def require_idle(self) -> None:
        line = self.poll_status()
        if not line.startswith("<Idle"):
            raise RuntimeError(f"not idle before test: {line or '<no status>'}")

    def home(self) -> None:
        print("\n== Home first ==")
        self.grbl.write(b"$X\n")
        self.grbl.flush()
        end = time.time() + 3.0
        while time.time() < end:
            line = self.read_line(0.1)
            self.classify(line)
            if line == "ok":
                break

        self.release_brake()
        try:
            self.grbl.write(b"$H\n")
            self.grbl.flush()
            end = time.time() + 90.0
            got_ok = False
            while time.time() < end:
                line = self.read_line(0.1)
                self.classify(line)
                if line == "ok":
                    got_ok = True
                    break
                if self.stats["alarms"] or self.stats["errors"]:
                    raise RuntimeError("home failed")
                self.poll_status()
            if not got_ok:
                raise RuntimeError("timeout waiting ok for home")

            end = time.time() + 5.0
            while time.time() < end:
                line = self.poll_status()
                if line.startswith("<Idle"):
                    print("DONE home")
                    self.stats.clear()
                    self.last_pn = ""
                    self.last_state = ""
                    return
                time.sleep(0.1)
            raise RuntimeError("home completed but did not return to idle")
        finally:
            self.lock_brake()

    def send_jog_abs(self, target: Target, timeout_s: float) -> None:
        print(
            f"\n== {target.label}: X{target.x:.3f} Y{target.y:.3f} "
            f"Z{target.z:.3f} F{target.feed:.0f} =="
        )
        cmd = (
            f"$J=G90 X{target.x:.3f} Y{target.y:.3f} "
            f"Z{target.z:.3f} F{target.feed:.0f}\n"
        )
        self.grbl.write(cmd.encode("ascii"))
        self.grbl.flush()

        fail0 = self.stats["alarms"] + self.stats["banners"] + self.stats["errors"]
        got_ok = False
        end_ack = time.time() + 5.0
        while time.time() < end_ack:
            line = self.read_line(0.1)
            self.classify(line)
            if line == "ok":
                got_ok = True
                break
            fail_now = self.stats["alarms"] + self.stats["banners"] + self.stats["errors"]
            if fail_now > fail0:
                raise RuntimeError(f"grbl failed while acking {target.label}")
        if not got_ok:
            raise RuntimeError(f"timeout waiting ok for {target.label}")

        end = time.time() + timeout_s
        while time.time() < end:
            line = self.poll_status()
            fail_now = self.stats["alarms"] + self.stats["banners"] + self.stats["errors"]
            if fail_now > fail0:
                raise RuntimeError(f"grbl failed during {target.label}")
            if line.startswith("<Idle"):
                print(f"DONE {target.label}")
                return
            time.sleep(0.1)
        raise RuntimeError(f"timeout waiting idle for {target.label}")

    def run(self) -> None:
        self.home()
        self.require_idle()
        # Keep all test positions inside the verified safe work envelope.
        tests = [
            Target("XY-only out @1000", -50.0, -50.0, -5.0, 1000.0),
            Target("XY-only home-side @1000", -5.0, -5.0, -5.0, 1000.0),
            Target("Z-only down @300", -5.0, -5.0, -15.0, 300.0),
            Target("Z-only up @300", -5.0, -5.0, -5.0, 300.0),
            Target("XYZ diagonal @500", -50.0, -50.0, -10.0, 500.0),
            Target("XYZ diagonal back @500", -5.0, -5.0, -5.0, 500.0),
        ]

        current_z = -5.0
        for target in tests:
            z_changes = abs(target.z - current_z) > 0.01
            if z_changes:
                self.release_brake()
            try:
                self.send_jog_abs(target, timeout_s=45.0)
            finally:
                if z_changes:
                    self.lock_brake()
            current_z = target.z

    def summary(self) -> None:
        print("\n== Summary ==")
        for key in ("ok", "status", "banners", "alarms", "errors", "pn_hits"):
            print(f"{key}: {self.stats[key]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    runner = MatrixRunner()
    try:
        runner.run()
    finally:
        runner.summary()
        runner.close()


if __name__ == "__main__":
    main()
