#!/usr/bin/env python3
"""启动智能旋涂仪 Web 控制服务。W3.0 自测只允许 ``--mock``。"""
from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn  # noqa: E402

from src.webapp import DeviceRegistry, create_app  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动智能旋涂仪 Web 控制服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", default=8800, type=int, help="监听端口")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="使用不连接任何串口或真实硬件的 mock 组合根",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.host != "127.0.0.1":
        print(
            "WARNING: Web 控制服务正在绑定非 127.0.0.1 地址，API 将暴露给网络。",
            file=sys.stderr,
            flush=True,
        )

    registry = (
        DeviceRegistry.from_mocks()
        if args.mock
        else DeviceRegistry.from_config()
    )
    token = secrets.token_urlsafe(32)
    print(f"Bearer token: {token}", flush=True)

    app = create_app(registry, token=token)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
