"""智能旋涂仪 Web 控制服务。"""

from __future__ import annotations

from typing import Any

from .registry import DeviceRegistry

__all__ = ["DeviceRegistry", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
