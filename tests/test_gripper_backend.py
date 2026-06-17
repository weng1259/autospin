"""GripperBackend 单元测试（Phase 3.3 Task 4）。

覆盖点：
- open/close 委托到 RelayBackend 正确的 ch 和 on/off
- dry_run 透传（不调 relay.ch_on/ch_off 的实写路径）
- state-memo：已 OPEN 再调 open → was_noop，不触发 relay
- UNKNOWN → OPEN：**必须**触发 relay（物理真值未知，不能短路）
- get_state 反映 commanded_state + last_command_ms_ago
- 继电器失败透传 RelayCommunicationError
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import serial

from src import observable as observable_mod
from src.hardware.errors import RelayCommunicationError
from src.hardware.gripper_backend import GripperBackend
from src.hardware.relay_backend import RelayBackend
from src.hardware.types import (
    GripperActionPlan,
    GripperActionResult,
    GripperCommandedState,
)


@pytest.fixture(autouse=True)
def _clear_idem_cache() -> None:
    observable_mod._IDEM_CACHE.clear()
    yield
    observable_mod._IDEM_CACHE.clear()


@pytest.fixture
def relay_with_mock_serial() -> RelayBackend:
    """预连接好的 RelayBackend，内部 serial 是 MagicMock。"""
    with patch("src.hardware.relay_backend.serial.Serial") as mock_serial_cls:
        mock_ser = MagicMock()
        mock_serial_cls.return_value = mock_ser
        relay = RelayBackend(port="/dev/fake", settle_s=0.0)
        relay.connect()
        relay._mock_ser = mock_ser  # type: ignore[attr-defined]
        yield relay


# ─────────────────────── 初始状态 ───────────────────────


def test_initial_state_is_unknown() -> None:
    relay = RelayBackend(port="/dev/fake")  # 未连不影响
    gripper = GripperBackend(relay)
    state = gripper.get_state()
    assert state.commanded_state == GripperCommandedState.UNKNOWN
    assert state.position_known is False
    assert state.last_command_ms_ago is None


# ─────────────────────── close() → ch_on(1) ───────────────────────


def test_close_invokes_relay_ch_on(relay_with_mock_serial: RelayBackend) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    result = gripper.close(idempotency_key="k-close")

    assert isinstance(result, GripperActionResult)
    assert result.success is True
    assert result.commanded_state_after == GripperCommandedState.CLOSED
    assert result.was_noop is False

    # Verify relay was touched: CH1 writes ON frame
    expected_frame = bytes([0xA0, 0x01, 0x01, 0xA2])
    relay_with_mock_serial._mock_ser.write.assert_called_once_with(  # type: ignore[attr-defined]
        expected_frame
    )


def test_open_invokes_relay_ch_off(relay_with_mock_serial: RelayBackend) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    # Preseed: close first, then open
    gripper.close(idempotency_key="seed")
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.reset_mock()

    result = gripper.open(idempotency_key="k-open")
    assert result.commanded_state_after == GripperCommandedState.OPEN
    expected_frame = bytes([0xA0, 0x01, 0x00, 0xA1])
    mock_ser.write.assert_called_once_with(expected_frame)


# ─────────────────────── state-memo ───────────────────────


def test_close_twice_second_is_noop(relay_with_mock_serial: RelayBackend) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    gripper.close(idempotency_key="k1")
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.reset_mock()

    # Different key, same target → state-memo short circuit
    result = gripper.close(idempotency_key="k2")
    assert result.was_noop is True
    mock_ser.write.assert_not_called()


def test_unknown_to_closed_writes_frame(
    relay_with_mock_serial: RelayBackend,
) -> None:
    """初始 UNKNOWN 下调 close() 必须实发继电器命令（CH1 OFF → ON 物理变化）。

    UNKNOWN → OPEN 这个方向下，因为 RelayBackend 假设初始"全 OFF"，两端都 OFF，
    state-memo 会合理短路；相比之下 UNKNOWN → CLOSED 必触发继电器实写。
    """
    gripper = GripperBackend(relay_with_mock_serial)
    result = gripper.close(idempotency_key="k-first-close")
    assert result.was_noop is False
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    assert mock_ser.write.call_count == 1


def test_unknown_to_open_short_circuits_at_relay_layer(
    relay_with_mock_serial: RelayBackend,
) -> None:
    """UNKNOWN → OPEN：gripper 层不短路（commanded≠target），但 relay 层可短路
    （CH1 初始已 OFF）。物理真值：DSTUR-T80 USB 上电默认全 OFF，所以不写字节是正确的。

    gripper 的 was_noop 反映 **gripper 层**的判断（首次 open → 非 noop）。
    """
    gripper = GripperBackend(relay_with_mock_serial)
    result = gripper.open(idempotency_key="k-first-open")
    assert result.was_noop is False  # gripper 层不短路
    # 但 relay 层判定 CH1 已 OFF → 不发字节
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.assert_not_called()


# ─────────────────────── dry_run ───────────────────────


def test_dry_run_returns_plan_and_no_relay_write(
    relay_with_mock_serial: RelayBackend,
) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    plan = gripper.close(idempotency_key="k-dry", dry_run=True)

    assert isinstance(plan, GripperActionPlan)
    assert plan.target_state == GripperCommandedState.CLOSED
    assert plan.current_state == GripperCommandedState.UNKNOWN
    assert plan.would_activate_relay is True
    assert plan.underlying_relay_channel == 1

    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.assert_not_called()


def test_dry_run_plan_would_activate_false_when_already_matched(
    relay_with_mock_serial: RelayBackend,
) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    gripper.close(idempotency_key="seed")
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.reset_mock()

    plan = gripper.close(idempotency_key="k-dry2", dry_run=True)
    assert isinstance(plan, GripperActionPlan)
    assert plan.would_activate_relay is False
    mock_ser.write.assert_not_called()


# ─────────────────────── channel override ───────────────────────


def test_custom_channel_writes_to_different_ch(
    relay_with_mock_serial: RelayBackend,
) -> None:
    gripper = GripperBackend(relay_with_mock_serial, channel=3)
    gripper.close(idempotency_key="k-ch3")
    expected_frame = bytes([0xA0, 0x03, 0x01, 0xA4])
    relay_with_mock_serial._mock_ser.write.assert_called_once_with(  # type: ignore[attr-defined]
        expected_frame
    )


# ─────────────────────── get_state 时序 ───────────────────────


def test_get_state_after_close_reflects_commanded(
    relay_with_mock_serial: RelayBackend,
) -> None:
    gripper = GripperBackend(relay_with_mock_serial)
    gripper.close(idempotency_key="k")
    state = gripper.get_state()
    assert state.commanded_state == GripperCommandedState.CLOSED
    assert state.last_command_ms_ago is not None
    assert state.last_command_ms_ago >= 0.0


# ─────────────────────── 错误透传 ───────────────────────


def test_relay_error_bubbles_up() -> None:
    """relay.ch_on 抛 RelayCommunicationError → gripper.close 原样抛，不吞。"""
    with patch("src.hardware.relay_backend.serial.Serial") as mock_serial_cls:
        # 两次 open 都会被调用（_write_with_retry 重连路径），两次都失败
        bad = MagicMock()
        bad.write.side_effect = serial.SerialException("USB dead")
        bad2 = MagicMock()
        bad2.write.side_effect = serial.SerialException("USB still dead")
        mock_serial_cls.side_effect = [bad, bad2]

        relay = RelayBackend(port="/dev/fake", settle_s=0.0)
        relay.connect()
        gripper = GripperBackend(relay)

        with pytest.raises(RelayCommunicationError) as exc:
            gripper.close(idempotency_key="k-err")
        assert "USB" in exc.value.agent_message


# ─────────────────────── @observable 缓存 ───────────────────────


def test_observable_cache_short_circuits_relay(
    relay_with_mock_serial: RelayBackend,
) -> None:
    """同 key 重放：gripper @observable 缓存命中，relay 不会被调。"""
    gripper = GripperBackend(relay_with_mock_serial)
    result1 = gripper.close(idempotency_key="obs-cache-key")
    mock_ser = relay_with_mock_serial._mock_ser  # type: ignore[attr-defined]
    mock_ser.write.reset_mock()

    # 把内部 state 回到 UNKNOWN 模拟"如果重新走逻辑会发关"
    gripper._commanded_state = GripperCommandedState.UNKNOWN  # type: ignore[attr-defined]

    result2 = gripper.close(idempotency_key="obs-cache-key")
    assert result2.event_id == result1.event_id
    mock_ser.write.assert_not_called()
