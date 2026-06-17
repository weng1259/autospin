"""`dry_run=True` 契约测试（ADR-004 §原则 7）。

验证：
1. `move_to(dry_run=True)` 返回 MovePlan，不触发串口发字节
2. `home(dry_run=True)` 返回 HomePlan，不释放/锁回 Z 刹车
3. soft_limits / feed 校验在 dry-run 时仍然执行（提前暴露错误给 Agent）
4. 未 connect 的 backend 也能 dry-run（plan 计算独立于 serial）

测试手法：用 `StubBackend` 替换真实 GantryBackend 构造，只替换
`_ser` / `_release_brake` / `_lock_brake` / `_send_line_blocking`
让我们可以断言"没被调用"。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config import L3Config, MotionConfig, SoftLimits
from src.hardware.errors import SoftLimitExceededError
from src.hardware.gantry_backend import GantryBackend
from src.hardware.types import HomePlan, MovePlan, Position


@pytest.fixture
def stub_config() -> L3Config:
    return L3Config(
        soft_limits=SoftLimits(
            x_min_mm=-300.0, x_max_mm=0.0,
            y_min_mm=-300.0, y_max_mm=0.0,
            z_min_mm=-100.0, z_max_mm=0.0,
        ),
        motion=MotionConfig(
            default_feed_mm_min=1000.0,
            max_feed_mm_min=5000.0,
            move_timeout_s=60.0,
            status_poll_interval_ms=200,
        ),
    )


@pytest.fixture
def stub_backend(stub_config: L3Config) -> GantryBackend:
    """构造一个不碰真实串口的 backend：`_ser` 为 None，发字节函数被
    MagicMock 替换以便断言"没被调用"。"""
    b = GantryBackend(config=stub_config, port="/dev/null")
    # 确保未 connect 状态
    b._ser = None
    b._send_line_blocking = MagicMock(side_effect=AssertionError("不应发任何字节"))  # type: ignore[method-assign]
    # Phase 3.3：Z 刹车通过 `_release_brake` / `_lock_brake` helper 走 RelayBackend。
    b._release_brake = MagicMock(side_effect=AssertionError("不应动刹车"))  # type: ignore[method-assign]
    b._lock_brake = MagicMock(side_effect=AssertionError("不应动刹车"))  # type: ignore[method-assign]
    return b


# ── home dry-run ──

def test_home_dry_run_returns_plan_no_hardware(stub_backend: GantryBackend) -> None:
    plan = stub_backend.home(idempotency_key="test-home-dry", dry_run=True)
    assert isinstance(plan, HomePlan)
    assert plan.estimated_duration_s > 0
    assert len(plan.sequence) >= 2  # 至少要有 release_z_brake + $H
    assert any("$H" in step for step in plan.sequence)
    # 验证 no-op
    stub_backend._send_line_blocking.assert_not_called()  # type: ignore[attr-defined]
    stub_backend._release_brake.assert_not_called()  # type: ignore[attr-defined]


def test_home_dry_run_works_without_connect(stub_backend: GantryBackend) -> None:
    """ADR-004 §原则 7：dry-run 不应要求 live connection，Agent 在规划阶段
    就能拿到 plan。"""
    assert stub_backend._ser is None
    plan = stub_backend.home(idempotency_key="no-conn", dry_run=True)
    assert isinstance(plan, HomePlan)


# ── move_to dry-run ──

def test_move_to_dry_run_returns_plan(stub_backend: GantryBackend) -> None:
    plan = stub_backend.move_to(
        Position(x_mm=-50, y_mm=-50, z_mm=-10),
        feed_mm_min=2000,
        dry_run=True,
    )
    assert isinstance(plan, MovePlan)
    assert plan.target.x_mm == -50
    assert plan.feed_mm_min == 2000.0
    # 未 connect → distance/duration/current_position 皆 None
    assert plan.distance_mm is None
    assert plan.estimated_duration_s is None
    assert plan.current_position is None


def test_move_to_dry_run_validates_soft_limits(stub_backend: GantryBackend) -> None:
    """dry-run 应提前暴露 soft_limit 错误给 Agent（ADR-004 §原则 7 的
    "规划阶段发现问题"要义）。"""
    with pytest.raises(SoftLimitExceededError):
        stub_backend.move_to(
            Position(x_mm=-400, y_mm=-50, z_mm=-10),  # x 越下界
            dry_run=True,
        )


def test_move_to_dry_run_validates_feed_range(stub_backend: GantryBackend) -> None:
    """feed 超限也应在 dry-run 时报错。"""
    with pytest.raises(Exception):  # L3Error
        stub_backend.move_to(
            Position(x_mm=-50, y_mm=-50, z_mm=-10),
            feed_mm_min=99999.0,  # 超过 max 5000
            dry_run=True,
        )


def test_move_to_dry_run_uses_default_feed(stub_backend: GantryBackend) -> None:
    """未传 feed_mm_min 时 plan 应用 config.motion.default_feed_mm_min。"""
    plan = stub_backend.move_to(
        Position(x_mm=-50, y_mm=-50, z_mm=-10),
        dry_run=True,
    )
    assert isinstance(plan, MovePlan)
    assert plan.feed_mm_min == 1000.0  # stub_config.motion.default_feed_mm_min


def test_move_to_dry_run_does_not_release_brake(stub_backend: GantryBackend) -> None:
    """dry-run 断言 no-op 到硬件：MagicMock side_effect 会 raise 如果被调用。"""
    plan = stub_backend.move_to(
        Position(x_mm=-50, y_mm=-50, z_mm=-10),
        dry_run=True,
    )
    assert isinstance(plan, MovePlan)
    stub_backend._release_brake.assert_not_called()  # type: ignore[attr-defined]
    stub_backend._send_line_blocking.assert_not_called()  # type: ignore[attr-defined]
