"""状态查询不阻塞测试（ADR-004 §原则 4 + §测试标准 6）。

ADR-004 测试标准 §6：
    "get_status 在机器执行 30s 长运动时可以连续调 100 次，每次 < 50ms 响应。"

关键不变量：`get_status()` 是只读快照复制 —— 它**不走串口、不持 `_lock`**。
所以无论 `home()` / `move_to()` 内部把 `_lock` 持续占多久（$H 真实场景约
30s），Agent 或 UI 轮询 `get_status()` 都不应被阻塞。

实施思路：
- StubBackend: `_ser` / `_brake._ser` 用 MagicMock 让 None 检查通过（但
  get_status 本身不访问 `_ser.write/read`，只读 `self._status` 字段）
- Mock `_send_line_blocking` 改成"持 `_lock` 然后 sleep LONG_OP_DURATION_S"，
  精准模拟真实 `$H` 阶段的锁持续占用。用 2s 代替 30s 只是让 CI 快 —— 关键
  不变量是"锁被持续占用"，与具体时长解耦
- Mock `release_z_brake` / `lock_z_brake` / `_poll_status_sync` 为 no-op，
  避免真 DSTUR / 真串口 IO
- 另起线程跑 `home()`，主线程循环 100 次 `get_status()` 记录延迟
- 断言 max / p95 / p99 全部 < 50ms

不启后台 poller —— 测的是 get_status **主路径**不阻塞于 home 持锁。Poller
线程是另一个关注点（由覆盖率报告兜，且 Phase 3.1 Slice 3 的真硬件 UI 已
间接验证过）。
"""
from __future__ import annotations

import statistics
import threading
import time
from typing import Callable
from unittest.mock import MagicMock

import pytest

from src.config import L3Config, MotionConfig, SoftLimits
from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.gantry_backend import GantryBackend


LONG_OP_DURATION_S = 2.0  # 模拟真实 $H 的锁持续占用（2s vs 真实 30s 等价，见 docstring）


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
    """不碰真实硬件的 backend：满足各处 `_ser is None` 检查，关键 IO 全 mock。"""
    b = GantryBackend(config=stub_config, port="/dev/null")
    # `_ser` 存在（is_open 属性真值）即可通过 None 检查；get_status 不会调 write/read
    b._ser = MagicMock()
    b._ser.is_open = True
    # Phase 3.3：Z 刹车走 `_release_brake` / `_lock_brake` helper（内部 RelayBackend）
    b._release_brake = MagicMock()  # type: ignore[method-assign]
    b._lock_brake = MagicMock()  # type: ignore[method-assign]
    # 防 home() 尾端的同步 poll 走真串口
    b._poll_status_sync = MagicMock()  # type: ignore[method-assign]
    return b


def _make_lock_holding_send_line(
    backend: GantryBackend, duration_s: float
) -> Callable[..., None]:
    """模拟真实 `_send_line_blocking` 的阻塞：持 `_lock` 然后 sleep duration_s。

    这条路径下 `$H` 发出后 Mega 要 ~30s 完成归零，期间 grbl 在 ack 里持续
    回传 `<Home|...>` 状态。真实 backend 里 _send_line_blocking 会一直持
    `_lock` 等 "ok" —— 所以 get_status 并发调用不应被 `_lock` 拦下
    （事实上 get_status 就不碰 _lock）。
    """
    def _fn(line: str, *, timeout_s: float, timeout_msg: str) -> None:
        with backend._lock:
            time.sleep(duration_s)
    return _fn


def test_get_status_nonblocking_during_long_home(
    stub_backend: GantryBackend,
) -> None:
    """home() 持 `_lock` LONG_OP_DURATION_S 期间，get_status 100 次 <50ms。"""
    stub_backend._send_line_blocking = _make_lock_holding_send_line(  # type: ignore[method-assign]
        stub_backend, LONG_OP_DURATION_S
    )

    home_exc: list[BaseException] = []

    def _home() -> None:
        try:
            stub_backend.home(idempotency_key="concurrency-test")
        except BaseException as e:  # 捕所有异常，测试末尾统一断言
            home_exc.append(e)

    home_thread = threading.Thread(target=_home, daemon=True)
    home_thread.start()
    # 等 home 真正进入锁持续占用阶段（connect 检查 + brake release 几 ms）
    time.sleep(0.1)

    # 验证锁此时确实被持住 —— 证明"持锁 30s"场景被精准模拟
    assert not stub_backend._lock.acquire(blocking=False), (
        "前置：home 应持 _lock；如果能拿到说明 mock 配错"
    )

    latencies_ms: list[float] = []
    for _ in range(100):
        t0 = time.perf_counter()
        _ = stub_backend.get_status()
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.005)  # 5ms 间隔，不 busy loop

    home_thread.join(timeout=LONG_OP_DURATION_S + 2.0)
    assert not home_thread.is_alive(), "home 未能退出"
    assert not home_exc, f"home 抛错：{home_exc}"

    max_ms = max(latencies_ms)
    p50 = statistics.median(latencies_ms)
    p95 = sorted(latencies_ms)[int(0.95 * len(latencies_ms))]
    p99 = sorted(latencies_ms)[int(0.99 * len(latencies_ms))]

    # ADR-004 §测试标准 6：每次 < 50ms
    assert max_ms < 50.0, (
        f"get_status max={max_ms:.2f}ms 超 50ms —— 期望不阻塞于 home 持锁。"
        f"p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}"
    )


def test_get_status_latency_baseline(stub_backend: GantryBackend) -> None:
    """无并发时的 get_status 基线延迟 —— 证明主路径本身就是亚毫秒级。"""
    latencies_ms: list[float] = []
    for _ in range(1000):
        t0 = time.perf_counter()
        _ = stub_backend.get_status()
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    p99 = sorted(latencies_ms)[int(0.99 * len(latencies_ms))]
    assert p99 < 5.0, (
        f"baseline p99={p99:.2f}ms —— 纯内存 model_copy 应亚毫秒级；"
        f"异常提示 MachineStatus 模型被改得很重或 GC 压力"
    )


def test_get_status_returns_fresh_snapshot_during_lock_hold(
    stub_backend: GantryBackend,
) -> None:
    """锁被占用期间 get_status 仍能返回 `last_update_ms_ago`。

    这是 Agent 判断"快照陈旧度"的依据（ADR-004 §原则 4 "返回时间戳"）。
    """
    stub_backend._status_ts_ms = (time.time() - 0.1) * 1000.0  # 100ms 前的快照

    # 模拟锁被别的线程持住
    stub_backend._lock.acquire()
    try:
        st = stub_backend.get_status()
        assert st.last_update_ms_ago >= 100.0
        assert st.last_update_ms_ago < 200.0  # 宽松上界
    finally:
        stub_backend._lock.release()


def test_get_status_raises_fast_when_disconnected(
    stub_backend: GantryBackend,
) -> None:
    """disconnect 分支也应快速抛错，不引入阻塞。"""
    stub_backend._ser = None
    t0 = time.perf_counter()
    with pytest.raises(L3ConnectionError):
        stub_backend.get_status()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 5.0, f"disconnect 路径 {elapsed_ms:.2f}ms —— 应 <5ms"
