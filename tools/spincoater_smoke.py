#!/usr/bin/env python3
"""DBLS400 真机 smoke：仅 W5 gate 时由 PM 运行。

本脚本会连接 ``--port`` 指定的真实共享 RS485 适配器。默认只读故障状态；
提供 ``--rpm`` 时仍默认 dry-run，只有同时显式给出 ``--apply`` 才会真转，
并在 ``--hold-s`` 后用刹车停机。Codex 的 W1.2 开发与测试阶段禁止运行本脚本。
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.hardware.rs485_bus import get_bus  # noqa: E402
from src.hardware.spincoater_backend import (  # noqa: E402
    SpincoaterBackend,
    SpincoaterConfig,
)


def _load_spincoater_config() -> SpincoaterConfig:
    raw = yaml.safe_load((REPO_ROOT / "constants.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("spincoater"), dict):
        raise RuntimeError("constants.yaml 缺少 spincoater 配置段")
    spincoater = cast(dict[str, Any], raw["spincoater"])
    return SpincoaterConfig(
        max_rpm=float(spincoater["max_rpm"]),
        baudrate=int(spincoater["baudrate"]),
        timeout_s=float(spincoater["timeout_s"]),
        control_register=int(spincoater["control_register"]),
        speed_set_register=int(spincoater["speed_set_register"]),
        fault_register=int(spincoater["fault_register"]),
        pole_pairs=int(spincoater["pole_pairs"]),
        speed_factor=float(spincoater["speed_factor"]),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DBLS400 spincoater W5 smoke gate")
    parser.add_argument(
        "--port",
        required=True,
        help="真实共享 RS485 串口；仅由 PM 在 W5 gate 时指定",
    )
    parser.add_argument("--unit-id", type=int, default=2)
    parser.add_argument(
        "--rpm",
        type=float,
        default=None,
        help="可选目标转速；未给 --apply 时只做 dry-run",
    )
    parser.add_argument(
        "--hold-s",
        type=float,
        default=3.0,
        help="--apply 真转后的保持秒数，随后自动刹车停机",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="允许按 --rpm 真转；需 PM 完成清场、装夹和急停确认",
    )
    args = parser.parse_args()
    if args.apply and args.rpm is None:
        parser.error("--apply 必须同时提供 --rpm")
    if args.hold_s < 0:
        parser.error("--hold-s 不能为负数")
    return args


def main() -> None:
    args = _parse_args()
    backend = SpincoaterBackend(
        bus=get_bus(args.port),
        unit_id=args.unit_id,
        config=_load_spincoater_config(),
    )
    backend.connect()
    should_brake_on_exit = bool(args.apply and args.rpm is not None)
    try:
        print(backend.status().model_dump_json(indent=2))
        if args.rpm is not None:
            result = backend.start(
                args.rpm,
                idempotency_key=f"spincoater-smoke-start-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
            if args.apply:
                time.sleep(args.hold_s)
    finally:
        try:
            if should_brake_on_exit and backend.status().connected:
                stop_result = backend.stop(
                    use_brake=True,
                    idempotency_key=f"spincoater-smoke-stop-{uuid.uuid4()}",
                )
                print(stop_result.model_dump_json(indent=2))
        finally:
            backend.close()


if __name__ == "__main__":
    main()
