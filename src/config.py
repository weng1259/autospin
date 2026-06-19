"""从 constants.yaml 加载安全边界 / 运动默认值。

ADR-004 §原则 5：硬编码安全边界由配置文件提供，Agent 运行时不可改。

加载点：进程启动时 `CONFIG = load_config()`，模块级单例；GantryBackend 构造
时注入（参数形式，方便测试覆盖）。

`Position` 在 types.py 里**不**做 pydantic bounds 校验（读 grbl 状态时坐标可
任意）。软限位校验在 `move_to()` 方法入口做，抛 `SoftLimitExceededError`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from .hardware.errors import SoftLimitExceededError
from .hardware.types import Position

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "constants.yaml"


class SoftLimits(BaseModel):
    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float
    z_min_mm: float
    z_max_mm: float

    _EPSILON_MM = 0.01

    def contains(self, p: Position) -> bool:
        e = self._EPSILON_MM
        return (
            self.x_min_mm - e <= p.x_mm <= self.x_max_mm + e
            and self.y_min_mm - e <= p.y_mm <= self.y_max_mm + e
            and self.z_min_mm - e <= p.z_mm <= self.z_max_mm + e
        )

    def assert_contains(self, p: Position) -> None:
        e = self._EPSILON_MM
        for axis, v, lo, hi in (
            ("x", p.x_mm, self.x_min_mm, self.x_max_mm),
            ("y", p.y_mm, self.y_min_mm, self.y_max_mm),
            ("z", p.z_mm, self.z_min_mm, self.z_max_mm),
        ):
            if not (lo - e <= v <= hi + e):
                raise SoftLimitExceededError(
                    human_message=f"{axis.upper()} = {v} 超出 [{lo}, {hi}]，请调整坐标",
                    agent_message=(
                        f"Axis {axis}={v} mm outside soft limit "
                        f"[{lo}, {hi}]; clamp the target before retrying."
                    ),
                )


class MotionConfig(BaseModel):
    default_feed_mm_min: float = Field(..., gt=0)
    max_feed_mm_min: float = Field(..., gt=0)
    move_timeout_s: float = Field(..., gt=0)
    status_poll_interval_ms: int = Field(..., ge=50, le=1000)


class L3Config(BaseModel):
    soft_limits: SoftLimits
    motion: MotionConfig


def load_config(path: Optional[Path] = None) -> L3Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return L3Config.model_validate(raw)


# 进程级单例（仅在 constants.yaml 存在且合法时懒加载）
_CONFIG: Optional[L3Config] = None


def get_config() -> L3Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG
