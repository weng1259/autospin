#!/usr/bin/env python3
"""AI-516P 真机 smoke：仅 W5 gate 时由 PM 运行。

本脚本会连接 ``--port`` 指定的真实 RS485 适配器。默认只读 PV；提供
``--set-sv`` 时仍默认 dry-run，只有同时显式给出 ``--apply`` 才写入真机。
Codex 的 W1.1 开发与测试阶段禁止运行本脚本。
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

from src.hardware.heater_backend import HeaterBackend, HeaterConfig  # noqa: E402
from src.hardware.rs485_bus import get_bus  # noqa: E402


def _load_heater_config() -> HeaterConfig:
    raw = yaml.safe_load((REPO_ROOT / "constants.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("heater"), dict):
        raise RuntimeError("constants.yaml 缺少 heater 配置段")
    heater = cast(dict[str, Any], raw["heater"])
    return HeaterConfig(
        sv_max_c=float(heater["sv_max_c"]),
        baudrate=int(heater["baudrate"]),
        timeout_s=float(heater["timeout_s"]),
        pv_register=int(heater["pv_register"]),
        sv_register=int(heater["sv_register"]),
        srun_register=int(heater["srun_register"]),
        run_on_sv_write=bool(heater["run_on_sv_write"]),
        scale=float(heater["scale"]),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-516P heater W5 smoke gate")
    parser.add_argument(
        "--port",
        required=True,
        help="真实共享 RS485 串口；仅由 PM 在 W5 gate 时指定",
    )
    parser.add_argument("--unit-id", type=int, default=3)
    parser.add_argument(
        "--set-sv",
        type=float,
        default=None,
        help="可选目标温度；未给 --apply 时只做 dry-run",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="允许把 --set-sv 真正写入设备",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    backend = HeaterBackend(
        bus=get_bus(args.port),
        unit_id=args.unit_id,
        config=_load_heater_config(),
    )
    backend.connect()
    try:
        print(backend.status().model_dump_json(indent=2))
        if args.set_sv is not None:
            result = backend.set_sv(
                args.set_sv,
                idempotency_key=f"heater-smoke-{uuid.uuid4()}",
                dry_run=not args.apply,
            )
            print(result.model_dump_json(indent=2))
    finally:
        backend.close()


if __name__ == "__main__":
    main()
