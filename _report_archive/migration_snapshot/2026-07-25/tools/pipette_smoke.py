#!/usr/bin/env python3
"""28-series pipette real-hardware smoke: only PM may run it at the W5 gate.

The script connects to the real RS485 adapter selected by ``--port``.  With no
action flags it only reads status.  Requested actions remain dry-run unless
``--apply`` is also explicit.  Codex must not run this script during W1.3.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.hardware.pipette_backend import PipetteBackend, PipetteConfig  # noqa: E402
from src.hardware.rs485_bus import get_bus  # noqa: E402


def _load_pipette_config() -> PipetteConfig:
    raw = yaml.safe_load((REPO_ROOT / "constants.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("pipette"), dict):
        raise RuntimeError("constants.yaml 缺少 pipette 配置段")
    pipette = cast(dict[str, Any], raw["pipette"])
    return PipetteConfig(
        max_volume_ul=float(pipette["max_volume_ul"]),
        baudrate=int(pipette["baudrate"]),
        timeout_s=float(pipette["timeout_s"]),
        home_timeout_s=float(pipette["home_timeout_s"]),
        action_timeout_s=float(pipette["action_timeout_s"]),
        poll_interval_s=float(pipette["poll_interval_s"]),
        speed_01rps=int(pipette["speed_01rps"]),
        accel_01rpss=int(pipette["accel_01rpss"]),
        decel_01rpss=int(pipette["decel_01rpss"]),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="28-series pipette W5 smoke gate")
    parser.add_argument(
        "--port",
        required=True,
        help="真实共享 RS485 串口；仅由 PM 在 W5 gate 时指定",
    )
    parser.add_argument("--unit-id", type=int, default=1)
    parser.add_argument(
        "--home",
        action="store_true",
        help="规划归位；配合 --apply 才执行真实归位",
    )
    parser.add_argument(
        "--aspirate-ul",
        type=float,
        default=None,
        help="规划吸液体积；配合 --apply 才执行真实吸液",
    )
    parser.add_argument(
        "--dispense-ul",
        type=float,
        default=None,
        help="规划吐液体积；配合 --apply 才执行真实吐液",
    )
    parser.add_argument(
        "--eject-tip",
        action="store_true",
        help="规划退吸头；配合 --apply 才执行真实退吸头",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="允许执行所选真实动作；需 PM 在场并完成行程、tip 与液源确认",
    )
    args = parser.parse_args()
    if args.apply and not any(
        (
            args.home,
            args.aspirate_ul is not None,
            args.dispense_ul is not None,
            args.eject_tip,
        )
    ):
        parser.error("--apply 必须同时选择至少一个动作")
    return args


def main() -> None:
    args = _parse_args()
    backend = PipetteBackend(
        bus=get_bus(args.port),
        unit_id=args.unit_id,
        config=_load_pipette_config(),
    )
    backend.connect()
    try:
        print(backend.status().model_dump_json(indent=2))
        if args.home:
            result = backend.home(
                idempotency_key=f"pipette-smoke-home-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        if args.aspirate_ul is not None:
            result = backend.aspirate(
                args.aspirate_ul,
                idempotency_key=f"pipette-smoke-aspirate-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        if args.dispense_ul is not None:
            result = backend.dispense(
                args.dispense_ul,
                idempotency_key=f"pipette-smoke-dispense-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        if args.eject_tip:
            result = backend.eject_tip(
                idempotency_key=f"pipette-smoke-eject-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        print(backend.status().model_dump_json(indent=2))
    finally:
        backend.close()


if __name__ == "__main__":
    main()
