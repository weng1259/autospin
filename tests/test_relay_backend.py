"""RelayBackend 单元测试（Phase 3.3 Task 3）。

覆盖 ADR-004 七原则在 RelayBackend 上的可验证落地：
- 协议帧 checksum 正确（DSTUR-T80 线协议）
- 通道号越界 → `RelayCommunicationError`
- `dry_run=True` 一行字节都不写
- **幂等 state-memo**：目标 state 已匹配时 `was_noop=True`（Issue #025
  brake-skip 修复的核心路径）
- **@observable 幂等缓存**：同 key 重放直接返回原 result，不触发写
- **USB 重连**：首次写 `SerialException` → close + reopen → 再写成功
- **USB 重连双失败**：抛 `RelayCommunicationError`，agent_message 含两次错误
- `get_state()` 不阻塞（无 `_write_lock`）

不测的：真实串口 IO（实机在 Phase 3.3 Task 7 Agent smoke 覆盖）。
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import serial

from src import observable as observable_mod
from src.hardware.errors import RelayCommunicationError
from src.hardware.relay_backend import (
    RelayBackend,
    _CHANNEL_MAX,
    _CHANNEL_MIN,
    _frame,
    _validate_channel,
)
from src.hardware.types import RelayActionPlan, RelayActionResult, RelayState


@pytest.fixture(autouse=True)
def _clear_idem_cache() -> None:
    """@observable 的 _IDEM_CACHE 是模块级单例，防测试间泄漏。"""
    observable_mod._IDEM_CACHE.clear()
    yield
    observable_mod._IDEM_CACHE.clear()


# ─────────────────────── 协议层 ───────────────────────


@pytest.mark.parametrize(
    "channel,on,expected",
    [
        (1, True, bytes([0xA0, 0x01, 0x01, 0xA2])),
        (1, False, bytes([0xA0, 0x01, 0x00, 0xA1])),
        (2, True, bytes([0xA0, 0x02, 0x01, 0xA3])),
        (2, False, bytes([0xA0, 0x02, 0x00, 0xA2])),
        (8, True, bytes([0xA0, 0x08, 0x01, 0xA9])),
        (8, False, bytes([0xA0, 0x08, 0x00, 0xA8])),
    ],
)
def test_frame_checksum(channel: int, on: bool, expected: bytes) -> None:
    assert _frame(channel, on) == expected


@pytest.mark.parametrize("bad_ch", [0, -1, 9, 100])
def test_validate_channel_rejects_out_of_range(bad_ch: int) -> None:
    with pytest.raises(RelayCommunicationError) as exc:
        _validate_channel(bad_ch)
    assert "越界" in exc.value.human_message or "out of range" in exc.value.human_message.lower()
    assert str(bad_ch) in exc.value.human_message


@pytest.mark.parametrize("ok_ch", [1, 2, 4, 8])
def test_validate_channel_accepts_valid(ok_ch: int) -> None:
    _validate_channel(ok_ch)  # no raise


# ─────────────────────── 连接生命周期 ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_connect_opens_serial(mock_serial_cls: MagicMock) -> None:
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser

    relay = RelayBackend(port="/dev/fake")
    assert not relay.is_connected()
    relay.connect()
    assert relay.is_connected()
    mock_serial_cls.assert_called_once_with("/dev/fake", 9600, timeout=1.0)


@patch("src.hardware.relay_backend.serial.Serial")
def test_connect_idempotent(mock_serial_cls: MagicMock) -> None:
    mock_serial_cls.return_value = MagicMock()
    relay = RelayBackend(port="/dev/fake")
    relay.connect()
    relay.connect()  # second call should be no-op
    mock_serial_cls.assert_called_once()


@patch("src.hardware.relay_backend.serial.Serial")
def test_connect_failure_raises_relay_error(mock_serial_cls: MagicMock) -> None:
    mock_serial_cls.side_effect = serial.SerialException("device busy")
    relay = RelayBackend(port="/dev/fake")
    with pytest.raises(RelayCommunicationError) as exc:
        relay.connect()
    assert "/dev/fake" in exc.value.human_message
    assert "device busy" in exc.value.agent_message


def test_close_without_connect_is_safe() -> None:
    relay = RelayBackend(port="/dev/fake")
    relay.close()  # no raise


# ─────────────────────── 读路径 get_state ───────────────────────


def test_initial_state_all_off() -> None:
    relay = RelayBackend(port="/dev/fake")
    state = relay.get_state()
    assert isinstance(state, RelayState)
    assert state.channels == {i: False for i in range(_CHANNEL_MIN, _CHANNEL_MAX + 1)}
    assert state.last_update_ms_ago == 0.0


@patch("src.hardware.relay_backend.serial.Serial")
def test_get_state_reflects_ch_on(mock_serial_cls: MagicMock) -> None:
    mock_serial_cls.return_value = MagicMock()
    relay = RelayBackend(port="/dev/fake")
    relay.connect()

    relay.ch_on(1, idempotency_key="k1")
    state = relay.get_state()
    assert state.channels[1] is True
    assert state.channels[2] is False
    assert state.last_update_ms_ago >= 0.0  # 已有 update 时间戳


# ─────────────────────── 写路径：dry_run ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_dry_run_does_not_write(mock_serial_cls: MagicMock) -> None:
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake")
    relay.connect()

    plan = relay.ch_on(1, idempotency_key="k-dryrun", dry_run=True)
    assert isinstance(plan, RelayActionPlan)
    assert plan.channel == 1
    assert plan.target_state is True
    assert plan.current_state is False
    assert plan.would_write is True
    mock_ser.write.assert_not_called()


@patch("src.hardware.relay_backend.serial.Serial")
def test_dry_run_plan_would_write_false_when_already_target(
    mock_serial_cls: MagicMock,
) -> None:
    mock_serial_cls.return_value = MagicMock()
    relay = RelayBackend(port="/dev/fake")
    relay.connect()
    relay.ch_on(1, idempotency_key="seed")  # put CH1 into ON

    plan = relay.ch_on(1, idempotency_key="k-dryrun", dry_run=True)
    assert isinstance(plan, RelayActionPlan)
    assert plan.would_write is False  # already on, noop


# ─────────────────────── 写路径：实发字节 ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_ch_on_writes_frame(mock_serial_cls: MagicMock) -> None:
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)  # 测速加速
    relay.connect()

    result = relay.ch_on(1, idempotency_key="k1")
    assert isinstance(result, RelayActionResult)
    assert result.success is True
    assert result.channel == 1
    assert result.state_after is True
    assert result.was_noop is False
    mock_ser.write.assert_called_once_with(bytes([0xA0, 0x01, 0x01, 0xA2]))
    mock_ser.flush.assert_called_once()


@patch("src.hardware.relay_backend.serial.Serial")
def test_ch_off_writes_frame(mock_serial_cls: MagicMock) -> None:
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()
    relay.ch_on(2, idempotency_key="seed")  # ON first
    mock_ser.write.reset_mock()

    result = relay.ch_off(2, idempotency_key="k-off")
    assert isinstance(result, RelayActionResult)
    assert result.state_after is False
    assert result.was_noop is False
    mock_ser.write.assert_called_once_with(bytes([0xA0, 0x02, 0x00, 0xA2]))


# ─────────────────────── 写路径：幂等 state-memo（brake-skip 核心） ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_state_memo_noop_when_already_on(mock_serial_cls: MagicMock) -> None:
    """Issue #025 brake-skip 修复路径：ch_on(ch) 目标 state 已匹配 → was_noop=True，不发字节。"""
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    relay.ch_on(1, idempotency_key="k-first")
    assert mock_ser.write.call_count == 1
    mock_ser.write.reset_mock()

    # 不同 key，但 state 已是 True → 走 state-memo 短路，不发字节
    result = relay.ch_on(1, idempotency_key="k-second")
    assert isinstance(result, RelayActionResult)
    assert result.was_noop is True
    assert result.state_after is True
    mock_ser.write.assert_not_called()


@patch("src.hardware.relay_backend.serial.Serial")
def test_observable_cache_noop_on_same_key(mock_serial_cls: MagicMock) -> None:
    """同 idempotency_key 重放 → @observable 缓存返回原 result，不走 _set_channel。"""
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    result1 = relay.ch_on(1, idempotency_key="observable-key-unique")
    # 直接改内部 state 模拟"如果重新走 _set_channel 会是什么结果"
    relay._channels[1] = False  # type: ignore[attr-defined]
    mock_ser.write.reset_mock()

    # 同 key 重放：@observable 短路，返回缓存 result1（state_after=True, was_noop=False），
    # 不再读当前 _channels
    result2 = relay.ch_on(1, idempotency_key="observable-key-unique")
    assert result2.event_id == result1.event_id
    mock_ser.write.assert_not_called()


# ─────────────────────── 写路径：force（Issue #025 2026-04-24 新增） ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_ch_on_force_bypasses_state_memo(mock_serial_cls: MagicMock) -> None:
    """`_ch_on_force` 无视 state-memo——即使 memo 说已是 True，仍然写字节。

    这是 2026-04-24 pick-and-place 场景的修复核心：EMI/USB 抖让物理和 memo
    失同步后，普通 `ch_on` 会短路，`_ch_on_force` 保证物理一定被写。
    """
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    # 先 ch_on → memo 记录 True
    relay.ch_on(2, idempotency_key="seed")
    assert mock_ser.write.call_count == 1
    mock_ser.write.reset_mock()

    # 普通 ch_on 会 noop（state-memo 命中）
    relay.ch_on(2, idempotency_key="skip-1")
    mock_ser.write.assert_not_called()

    # _ch_on_force 强制写，即使 memo 已 True
    relay._ch_on_force(2)
    mock_ser.write.assert_called_once_with(bytes([0xA0, 0x02, 0x01, 0xA3]))
    # memo 仍保持 True
    assert relay.get_state().channels[2] is True


@patch("src.hardware.relay_backend.serial.Serial")
def test_ch_off_force_bypasses_state_memo(mock_serial_cls: MagicMock) -> None:
    """`_ch_off_force` 对称：memo 已 False 时仍写字节。"""
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    # memo 默认 False，普通 ch_off noop
    relay.ch_off(2, idempotency_key="seed")
    mock_ser.write.assert_not_called()

    # force 版本强制写
    relay._ch_off_force(2)
    mock_ser.write.assert_called_once_with(bytes([0xA0, 0x02, 0x00, 0xA2]))
    assert relay.get_state().channels[2] is False


@patch("src.hardware.relay_backend.serial.Serial")
def test_ch_on_force_rejects_bad_channel(mock_serial_cls: MagicMock) -> None:
    """force 路径也走 `_validate_channel`。"""
    mock_ser = MagicMock()
    mock_serial_cls.return_value = mock_ser
    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    with pytest.raises(RelayCommunicationError):
        relay._ch_on_force(99)


# ─────────────────────── 写路径：USB 重连（Issue #025 第二 bug） ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_reconnect_succeeds_on_stale_fd(mock_serial_cls: MagicMock) -> None:
    """首次写 SerialException → reconnect → 二次成功。"""
    # 第一次 Serial() 返回"旧 fd"，write 抛；第二次 Serial() 返回新 mock，write 成功
    stale_ser = MagicMock()
    stale_ser.write.side_effect = serial.SerialException("Device not configured")
    fresh_ser = MagicMock()
    mock_serial_cls.side_effect = [stale_ser, fresh_ser]

    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()  # 得到 stale_ser
    assert relay._ser is stale_ser  # type: ignore[attr-defined]

    result = relay.ch_on(1, idempotency_key="k-reconnect")
    assert isinstance(result, RelayActionResult)
    assert result.success is True
    assert result.state_after is True
    assert result.was_noop is False
    # 旧 fd 尝试 close；新 fd write 成功
    stale_ser.close.assert_called()
    fresh_ser.write.assert_called_once_with(bytes([0xA0, 0x01, 0x01, 0xA2]))


@patch("src.hardware.relay_backend.serial.Serial")
def test_reconnect_fails_both_writes(mock_serial_cls: MagicMock) -> None:
    """重连后二次写仍失败 → 抛 RelayCommunicationError，agent_message 含两次错误。"""
    first = MagicMock()
    first.write.side_effect = serial.SerialException("Device not configured")
    second = MagicMock()
    second.write.side_effect = serial.SerialException("still broken")
    mock_serial_cls.side_effect = [first, second]

    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    with pytest.raises(RelayCommunicationError) as exc:
        relay.ch_on(1, idempotency_key="k-double-fail")
    assert "still broken" in exc.value.agent_message
    assert "Device not configured" in exc.value.agent_message


@patch("src.hardware.relay_backend.serial.Serial")
def test_reconnect_open_fails(mock_serial_cls: MagicMock) -> None:
    """首次写失败 → reconnect 时 open 都抛 → RelayCommunicationError。"""
    stale = MagicMock()
    stale.write.side_effect = serial.SerialException("Device not configured")
    # 第一次 Serial() OK；第二次 Serial() 抛
    mock_serial_cls.side_effect = [stale, serial.SerialException("no such device")]

    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    with pytest.raises(RelayCommunicationError) as exc:
        relay.ch_on(1, idempotency_key="k-open-fail")
    assert "重连失败" in exc.value.human_message or "reconnect" in exc.value.agent_message.lower()


def test_call_before_connect_raises() -> None:
    relay = RelayBackend(port="/dev/fake")
    with pytest.raises(RelayCommunicationError) as exc:
        relay.ch_on(1, idempotency_key="k-unconnected")
    assert "未连接" in exc.value.human_message or "not connected" in exc.value.agent_message.lower()


# ─────────────────────── 并发 ───────────────────────


@patch("src.hardware.relay_backend.serial.Serial")
def test_get_state_does_not_block_during_write(
    mock_serial_cls: MagicMock,
) -> None:
    """写串口时 `get_state()` 应立即返回（不抢 `_write_lock`）。"""
    blocked_ser = MagicMock()

    # 让 write 阻塞 0.5s 模拟真实串口慢写
    write_started = threading.Event()

    def slow_write(data: bytes) -> None:
        write_started.set()
        time.sleep(0.5)

    blocked_ser.write.side_effect = slow_write
    mock_serial_cls.return_value = blocked_ser

    relay = RelayBackend(port="/dev/fake", settle_s=0.0)
    relay.connect()

    t = threading.Thread(target=relay.ch_on, args=(1,), kwargs={"idempotency_key": "bg"})
    t.start()
    assert write_started.wait(timeout=1.0), "write 未启动"

    # 并发读 100 次，断最长耗时 < 50ms（参考 test_get_status_concurrency 的阈值）
    times_ms = []
    for _ in range(100):
        t0 = time.time()
        _ = relay.get_state()
        times_ms.append((time.time() - t0) * 1000.0)
    t.join()

    assert max(times_ms) < 50.0, f"get_state 最大耗时 {max(times_ms):.2f}ms 超阈值"
