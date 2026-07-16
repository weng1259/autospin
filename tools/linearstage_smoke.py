#!/usr/bin/env python3
"""ZDT Emm linear-stage smoke; only PM may run it at the hardware gate.

This script connects to the real shared RS485 adapter selected by ``--port``.
With no action flags it only reads the connect-time status snapshot.  Home and
move remain dry-run unless ``--apply`` is explicit; immediate stop requires
``--apply`` because the public stop API is intentionally always physical.
Codex must only write, and must not run, this script during W1.4.
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

from src.hardware.linearstage_backend import (  # noqa: E402
    LinearStageBackend,
    LinearStageConfig,
)
from src.hardware.rs485_bus import get_bus  # noqa: E402


def _load_linear_stage_config() -> LinearStageConfig:
    raw = yaml.safe_load((REPO_ROOT / "constants.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("linear_stage"), dict):
        raise RuntimeError("constants.yaml 缺少 linear_stage 配置段")
    stage = cast(dict[str, Any], raw["linear_stage"])
    return LinearStageConfig(
        travel_mm=float(stage["travel_mm"]),
        baudrate=int(stage["baudrate"]),
        timeout_s=float(stage["timeout_s"]),
        lead_mm=float(stage["lead_mm"]),
        microsteps=int(stage["microsteps"]),
        motor_step_deg=float(stage["motor_step_deg"]),
        default_speed_rpm=int(stage["default_speed_rpm"]),
        default_acceleration=int(stage["default_acceleration"]),
        home_direction=int(stage["home_direction"]),
        home_speed_rpm=int(stage["home_speed_rpm"]),
        sensorless_timeout_ms=int(stage["sensorless_timeout_ms"]),
        collision_rpm=int(stage["collision_rpm"]),
        collision_current_ma=int(stage["collision_current_ma"]),
        collision_time_ms=int(stage["collision_time_ms"]),
        home_timeout_s=float(stage["home_timeout_s"]),
        move_timeout_s=float(stage["move_timeout_s"]),
        position_tolerance_mm=float(stage["position_tolerance_mm"]),
        poll_interval_s=float(stage["poll_interval_s"]),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZDT Emm linear-stage W5 smoke gate")
    parser.add_argument(
        "--port",
        required=True,
        help="真实共享 RS485 串口；仅由 PM 在硬件 gate 时指定",
    )
    parser.add_argument("--address", type=int, default=4)
    parser.add_argument(
        "--home",
        action="store_true",
        help="规划碰撞归零；配合 --apply 才执行真实归零",
    )
    parser.add_argument(
        "--move-to-mm",
        type=float,
        default=None,
        help="规划绝对位置；真实移动必须同时指定 --home --apply",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="发送真实立即停止；必须同时指定 --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="允许执行所选真实动作；需 PM 在场并确认行程无障碍",
    )
    args = parser.parse_args()
    selected_action = args.home or args.move_to_mm is not None or args.stop
    if args.apply and not selected_action:
        parser.error("--apply 必须同时选择至少一个动作")
    if args.stop and not args.apply:
        parser.error("--stop 是立即物理动作，必须同时指定 --apply")
    if args.apply and args.move_to_mm is not None and not args.home:
        parser.error("新 smoke 会话不信任历史归零态；真实移动必须同时指定 --home")
    return args


def main() -> None:
    args = _parse_args()
    bus = get_bus(args.port)
    backend = LinearStageBackend(
        bus=bus,
        address=args.address,
        config=_load_linear_stage_config(),
    )
    try:
        backend.connect()
        print(backend.status().model_dump_json(indent=2))
        if args.home:
            result = backend.home(
                idempotency_key=f"linear-stage-smoke-home-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        if args.move_to_mm is not None:
            result = backend.move_to(
                args.move_to_mm,
                idempotency_key=f"linear-stage-smoke-move-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
        if args.stop:
            print(backend.stop().model_dump_json(indent=2))
        print(backend.status().model_dump_json(indent=2))
    finally:
        backend.close()
        # This CLI is the composition root and therefore owns the shared bus
        # lifecycle.  Device backends themselves deliberately never close it.
        bus.close()


if __name__ == "__main__":
    main()
