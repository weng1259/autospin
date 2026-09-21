#!/usr/bin/env python3
"""Configuration-wired Gantry smoke test with an explicit hardware gate."""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.hardware.types import Position  # noqa: E402
from src.webapp.registry import DeviceRegistry  # noqa: E402

SAFE_TARGET = Position(x_mm=-20.0, y_mm=-20.0, z_mm=-10.0)
HARDWARE_CONFIRMATION = "MOVE_GANTRY_TO_X-20_Y-20_Z-10"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the configured Gantry and optionally run a real hardware smoke test."
    )
    parser.add_argument(
        "--execute-hardware",
        action="store_true",
        help="Permit connection, homing, and movement on the configured real Gantry.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        metavar="TEXT",
        help=f"Required hardware confirmation text: {HARDWARE_CONFIRMATION}",
    )
    args = parser.parse_args(argv)
    if args.execute_hardware and args.confirm != HARDWARE_CONFIRMATION:
        parser.error(
            "--execute-hardware requires "
            f"--confirm {HARDWARE_CONFIRMATION}"
        )
    if not args.execute_hardware and args.confirm:
        parser.error("--confirm is only valid with --execute-hardware")
    return args


def _render(value: Any) -> str:
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json(indent=2)
    return str(value)


def run_smoke(*, execute_hardware: bool) -> None:
    print("Loading config/hardware.yaml through DeviceRegistry.from_config()")
    registry = DeviceRegistry.from_config()
    gantry = registry.gantry
    if gantry is None:
        raise RuntimeError("Configured DeviceRegistry did not create a GantryBackend")

    print(f"Safe target: {_render(SAFE_TARGET)}")
    if not execute_hardware:
        print(
            "DRY RUN: hardware execution flag absent; no serial connection, "
            "homing, or movement was attempted."
        )
        return

    connected = False
    try:
        print("Connecting Gantry")
        gantry.connect()
        connected = True

        print(f"Initial status:\n{_render(gantry.get_status())}")

        print("Homing Gantry")
        home_result = gantry.home(
            idempotency_key=f"gantry-runtime-smoke-home-{uuid.uuid4()}"
        )
        print(f"Home result:\n{_render(home_result)}")

        print(f"Moving Gantry to safe target:\n{_render(SAFE_TARGET)}")
        move_result = gantry.move_to(SAFE_TARGET)
        print(f"Move result:\n{_render(move_result)}")

        print(f"Final position:\n{_render(gantry.get_position())}")
    finally:
        if connected:
            print("Disconnecting Gantry")
            gantry.disconnect()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    run_smoke(execute_hardware=args.execute_hardware)


if __name__ == "__main__":
    main()
