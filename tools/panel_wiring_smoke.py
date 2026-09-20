#!/usr/bin/env python3
"""Read-only wiring smoke through the maintained DeviceRegistry.

This tool opens configured devices and reads status only. It never homes,
moves, toggles a relay, writes a heater setpoint, or starts the spin motor.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.webapp import DeviceRegistry  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read configured hardware identity/status without actuation"
    )
    parser.add_argument(
        "--confirm-read-only-hardware",
        action="store_true",
        help=(
            "confirm that configured serial ports may be opened for read-only "
            "status checks"
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.confirm_read_only_hardware:
        print(
            "Refusing to open hardware. Re-run with "
            "--confirm-read-only-hardware after checking port mapping.",
            file=sys.stderr,
        )
        return 2

    registry = DeviceRegistry.from_config()
    failures: list[str] = []
    try:
        print("[registry] configured devices use one DeviceRegistry")
        print(f"[serial] resources before connect: {registry.serial_diagnostics()}")
        for name in ("relay", "gantry", "spincoater", "heater", "pipette", "linear_stage"):
            backend = getattr(registry, name, None)
            if backend is None:
                failures.append(f"{name}: unavailable")
                continue
            try:
                backend.connect()
                if name == "heater":
                    status = backend.read_pv()
                elif name == "gantry":
                    status = backend.get_status()
                elif name == "relay":
                    status = backend.get_state()
                elif hasattr(backend, "get_status"):
                    status = backend.get_status()
                else:
                    status = backend.status()
                rendered = (
                    status.model_dump(mode="json")
                    if hasattr(status, "model_dump")
                    else status
                )
                print(f"[{name}] OK {rendered}")
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"[{name}] FAIL {type(exc).__name__}: {exc}")

        print(f"[serial] resources after connect: {registry.serial_diagnostics()}")
        print(
            "[arch] relay/gantry/gripper and shared RS485 ownership are supplied "
            "by this single registry; see resource diagnostics above"
        )
    finally:
        registry.shutdown()

    if failures:
        print("Failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Read-only wiring smoke passed; no actuator command was issued.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
