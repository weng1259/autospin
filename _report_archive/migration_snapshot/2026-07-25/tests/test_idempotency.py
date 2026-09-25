"""幂等 + TTL 分级契约测试（ADR-004 §原则 3）。

核心验证：
1. 同 `idempotency_key` 连调 3 次 → 方法体只跑 1 次 → runlog 只入 1 条
2. 不同 key 连调 → 每次都跑
3. TTL 分级：默认 5 min（move_to 级秒级操作）/ 显式 24h（home /
   recover_from_alarm 高物理代价动作）各自的过期与不过期行为
4. GantryBackend 的 `home` / `recover_from_alarm` 实际挂了 24h TTL，
   `halt` / `unlock_alarm` 等挂了 5min 默认 TTL（通过 `__observable_ttl_s__`
   introspect）

测试手法：
- FakeBackend with @observable 方法，避免真实硬件依赖
- 用 `monkeypatch.setattr(observable, "time", FakeTime)` 操控时间，
  而非实际 sleep——测跑 24h TTL 不至于等 24h
- 每个测试独立 runlog DB（tmp_path） + 清空 `_IDEM_CACHE` 防泄漏
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src import observable as observable_mod
from src.hardware.gantry_backend import GantryBackend
from src.observable import observable
from src.runlog import RunLog


# ── 共享 fixture ──

class _FakeClock:
    """受控时钟，用 monkeypatch 替换 observable.time 模块。"""

    def __init__(self, initial: float = 1_700_000_000.0) -> None:
        self.now = initial

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """把 observable 模块里的 time.time 换成 FakeClock.time。

    `observable.py` 通过 `import time` 再用 `time.time()`，所以替换
    observable_mod.time 整个子模块。"""
    c = _FakeClock()

    class _TimeProxy:
        time = staticmethod(c.time)

        # 保留其它属性回原模块，防止 observable 用到别的 time.* 函数
        def __getattr__(self, name: str) -> Any:
            import time as _real
            return getattr(_real, name)

    monkeypatch.setattr(observable_mod, "time", _TimeProxy())
    return c


@pytest.fixture
def isolated_runlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> RunLog:
    """每个 test 独立 DB，避免 runtime/runlog.db 被污染或跨 test 泄漏。"""
    r = RunLog(db_path=tmp_path / "test_runlog.db")
    monkeypatch.setattr(observable_mod, "RUNLOG", r)
    # 清空全局幂等缓存（模块单例）
    observable_mod._IDEM_CACHE.clear()
    yield r
    observable_mod._IDEM_CACHE.clear()


# ── FakeBackend：两种 TTL ──

class FakeBackend:
    """无硬件依赖的 backend，用来打 @observable 装饰器单元测试。"""

    def __init__(self) -> None:
        self.short_runs = 0
        self.long_runs = 0

    @observable
    def short_op(self, *, idempotency_key: str) -> str:
        """默认 5 min TTL（模拟 move_to / halt / unlock_alarm 级操作）。"""
        self.short_runs += 1
        return f"short_result_{self.short_runs}"

    @observable(idempotency_ttl_s=24 * 3600)
    def long_op(self, *, idempotency_key: str) -> str:
        """显式 24h TTL（模拟 home / recover_from_alarm 级操作）。"""
        self.long_runs += 1
        return f"long_result_{self.long_runs}"


# ── 核心合同测试 ──

def test_same_key_three_times_runs_method_once(
    isolated_runlog: RunLog,
) -> None:
    b = FakeBackend()
    r1 = b.short_op(idempotency_key="k1")
    r2 = b.short_op(idempotency_key="k1")
    r3 = b.short_op(idempotency_key="k1")
    assert r1 == r2 == r3 == "short_result_1"
    assert b.short_runs == 1
    # runlog 只入一条 completed 事件（started 不入库）
    events = isolated_runlog.query_recent()
    short_completed = [
        e for e in events
        if e["method"].endswith(".short_op") and e["phase"] == "completed"
    ]
    assert len(short_completed) == 1


def test_different_keys_each_run(isolated_runlog: RunLog) -> None:
    b = FakeBackend()
    for k in ("a", "b", "c"):
        b.short_op(idempotency_key=k)
    assert b.short_runs == 3
    events = isolated_runlog.query_recent()
    short_completed = [
        e for e in events
        if e["method"].endswith(".short_op") and e["phase"] == "completed"
    ]
    assert len(short_completed) == 3


def test_short_ttl_expires_at_5min(
    clock: _FakeClock, isolated_runlog: RunLog,
) -> None:
    b = FakeBackend()
    b.short_op(idempotency_key="k")  # runs
    # 4 分钟内仍命中缓存
    clock.advance(4 * 60)
    b.short_op(idempotency_key="k")
    assert b.short_runs == 1
    # 5 min + 1s 后 miss，重新跑
    clock.advance(60 + 1)
    b.short_op(idempotency_key="k")
    assert b.short_runs == 2


def test_long_ttl_survives_beyond_5min(
    clock: _FakeClock, isolated_runlog: RunLog,
) -> None:
    """24h TTL：5 min 后一定不失效——这正是 home/recover 要的。"""
    b = FakeBackend()
    b.long_op(idempotency_key="k")
    clock.advance(10 * 60)  # 10 min
    b.long_op(idempotency_key="k")
    assert b.long_runs == 1


def test_long_ttl_survives_up_to_24h(
    clock: _FakeClock, isolated_runlog: RunLog,
) -> None:
    b = FakeBackend()
    b.long_op(idempotency_key="k")
    clock.advance(23 * 3600 + 59 * 60)  # 23h59m 仍命中
    b.long_op(idempotency_key="k")
    assert b.long_runs == 1
    # 24h + 1s 后 miss
    clock.advance(2 * 60)
    b.long_op(idempotency_key="k")
    assert b.long_runs == 2


# ── GantryBackend 实际装饰 introspect ──

def test_gantry_home_has_24h_ttl() -> None:
    """ADR-004 §原则 3 + 2026-04-22 架构审阅 A3：home 必须挂长 TTL。"""
    ttl = getattr(GantryBackend.home, "__observable_ttl_s__", None)
    assert ttl == 24 * 3600, (
        f"GantryBackend.home 应为 24h TTL，实测 {ttl}s — 回归：agent 断线"
        "重连同 key 会触发第二次 30s 归零，磨损电机 + 刹车"
    )


def test_gantry_recover_from_alarm_has_24h_ttl() -> None:
    ttl = getattr(GantryBackend.recover_from_alarm, "__observable_ttl_s__", None)
    assert ttl == 24 * 3600, (
        f"GantryBackend.recover_from_alarm 应为 24h TTL，实测 {ttl}s"
    )


def test_gantry_other_observables_use_default_ttl() -> None:
    """halt / unlock_alarm / soft_reset 等秒级操作应用默认 5min。

    注：这些方法当前未带 idempotency_key，不会触发缓存；但 TTL 设定应该
    正确防止未来加 key 时出现预期外的长缓存。"""
    for method_name in ("halt", "unlock_alarm", "soft_reset", "move_to"):
        method = getattr(GantryBackend, method_name)
        ttl = getattr(method, "__observable_ttl_s__", None)
        if ttl is None:
            # 不是 @observable 的方法，跳过
            continue
        assert ttl == 5 * 60, (
            f"GantryBackend.{method_name} 的 TTL 应为默认 5min，实测 {ttl}s"
        )
