"""GantryBackend 公共 API 验证路径单测（"骨干"覆盖补充）。

目标：Phase 3.2 §整体验收 覆盖率 > 70%。真实串口 IO 层通过 Phase 3.1 Slice
1-5 + Phase 3.2 Agent smoke 实机验收覆盖，本文件专注**不依赖真硬件**的路径：
- 各方法 `_ser is None` 早退路径
- `move_to` / `start_move_async` 的参数校验早退（soft_limit / 未归零 / 重叠）
- `recover_from_alarm` 的状态分支（Idle no-op / Run 冲突）
- `_parse_status_line` 纯字符串解析
- 访问器 `is_connected` / `is_homed` / `get_position`
- 异步 move orchestration accessor

不测的：`_send_line_blocking` / `_poll_status_sync` / `_poller_loop` /
`_drain_once` / `connect` / `close` —— 这些是串口协议胶水，实机覆盖。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.config import L3Config, MotionConfig, SoftLimits
from src.hardware.errors import (
    ConnectionError as L3ConnectionError,
    GrblConfigMismatchError,
    HomingTimeoutError,
    MachineNotHomedError,
    OperationConflictError,
    SoftLimitExceededError,
)
from src.hardware.gantry_backend import GantryBackend
from src.hardware.types import MachineState, MachineStatus, Position


# ── fixtures ──

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
def disconnected_backend(stub_config: L3Config) -> GantryBackend:
    """`_ser=None` backend —— 用于测早退路径。"""
    b = GantryBackend(config=stub_config, port="/dev/null")
    b._ser = None
    return b


@pytest.fixture
def fake_serial_backend(stub_config: L3Config) -> GantryBackend:
    """带 MagicMock `_ser` 但不真发命令 —— 测需要"连着"的早退路径（NotHomed / SoftLimit 等）。"""
    b = GantryBackend(config=stub_config, port="/dev/null")
    b._ser = MagicMock()
    b._ser.is_open = True
    # Phase 3.3：Z 刹车通过 `_release_brake` / `_lock_brake` helper 走 RelayBackend。
    # 测试 mock helper 本身，绕开 relay 的 pyserial 细节。
    b._release_brake = MagicMock()  # type: ignore[method-assign]
    b._lock_brake = MagicMock()  # type: ignore[method-assign]
    return b


# ── 访问器 ──

def test_is_connected_false_when_ser_none(disconnected_backend: GantryBackend) -> None:
    assert disconnected_backend.is_connected() is False


def test_is_connected_true_when_ser_open(fake_serial_backend: GantryBackend) -> None:
    assert fake_serial_backend.is_connected() is True


def test_is_homed_default_false(disconnected_backend: GantryBackend) -> None:
    assert disconnected_backend.is_homed() is False


def test_is_homed_true_after_flag(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._is_homed = True
    assert fake_serial_backend.is_homed() is True


def test_get_position_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.get_position()


def test_get_position_returns_snapshot(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={"position": Position(x_mm=-10.0, y_mm=-20.0, z_mm=-5.0)}
    )
    p = fake_serial_backend.get_position()
    assert p.x_mm == -10.0 and p.y_mm == -20.0 and p.z_mm == -5.0


# ── halt 早退 ──

def test_halt_raises_when_disconnected(disconnected_backend: GantryBackend) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.halt()


def test_halt_writes_feedhold_and_cancel(fake_serial_backend: GantryBackend) -> None:
    """happy path：MagicMock 记录两次 write (b'!' + b'\\x85')。"""
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={"state": MachineState.IDLE}
    )
    result = fake_serial_backend.halt()
    assert isinstance(result, MachineStatus)
    # 验证两次字节写入
    writes = [call.args[0] for call in fake_serial_backend._ser.write.call_args_list]
    assert b"!" in writes
    assert b"\x85" in writes
    assert fake_serial_backend._halt_requested is True


# ── soft_reset 早退 ──

def test_soft_reset_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.soft_reset()


def test_soft_reset_clears_homed_and_sends_ctrl_x(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._is_homed = True
    fake_serial_backend._alarm_code = 11
    # 防内部 _poll_status_sync 真调串口
    fake_serial_backend._poll_status_sync = MagicMock()  # type: ignore[method-assign]
    fake_serial_backend.soft_reset()
    writes = [c.args[0] for c in fake_serial_backend._ser.write.call_args_list]
    assert b"\x18" in writes
    assert fake_serial_backend._is_homed is False
    assert fake_serial_backend._alarm_code is None


# ── unlock_alarm 早退 ──

def test_unlock_alarm_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.unlock_alarm()


# ── grbl settings 预检 / 修复 ──

def test_get_grbl_settings_parses_dump(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._ser.reset_input_buffer = MagicMock()
    fake_serial_backend._ser.flush = MagicMock()
    fake_serial_backend._ser.write = MagicMock()
    fake_serial_backend._ser.readline = MagicMock(
        side_effect=[
            b"$0=10\n",
            b"$4=0\n",
            b"$100=682.670\n",
            b"ok\n",
        ]
    )

    snapshot = fake_serial_backend.get_grbl_settings()

    assert snapshot.settings["$0"] == "10"
    assert snapshot.settings["$4"] == "0"
    assert snapshot.settings["$100"] == "682.670"


def test_validate_grbl_settings_raises_on_mismatch(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._ser.reset_input_buffer = MagicMock()
    fake_serial_backend._ser.flush = MagicMock()
    fake_serial_backend._ser.write = MagicMock()
    fake_serial_backend._ser.readline = MagicMock(
        side_effect=[
            b"$0=130\n",
            b"$4=1\n",
            b"$100=682.670\n",
            b"ok\n",
        ]
    )

    with pytest.raises(GrblConfigMismatchError):
        fake_serial_backend.validate_grbl_settings()


def test_repair_grbl_settings_writes_expected_values(
    fake_serial_backend: GantryBackend,
) -> None:
    import src.hardware.gantry_backend as gb_mod

    fake_serial_backend._send_line_blocking = MagicMock()  # type: ignore[method-assign]
    fake_serial_backend._query_grbl_settings = MagicMock(  # type: ignore[method-assign]
        return_value=dict(gb_mod.GRBL_EXPECTED_SETTINGS)
    )

    result = fake_serial_backend.repair_grbl_settings()

    assert result.repaired is True
    assert fake_serial_backend._send_line_blocking.call_count == len(
        gb_mod.GRBL_EXPECTED_SETTINGS
    )


# ── recover_from_alarm 状态分支 ──

def test_recover_from_alarm_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.recover_from_alarm(idempotency_key="k1")


def test_recover_from_alarm_idle_is_noop(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._poll_status_sync = MagicMock()  # type: ignore[method-assign]
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={"state": MachineState.IDLE}
    )
    result = fake_serial_backend.recover_from_alarm(idempotency_key="noop-key")
    assert result.success is True
    assert result.actions_taken == []
    assert result.entry_state == MachineState.IDLE


@pytest.mark.parametrize("busy_state", [
    MachineState.RUN, MachineState.JOG, MachineState.HOME,
])
def test_recover_from_alarm_raises_when_busy(
    fake_serial_backend: GantryBackend, busy_state: MachineState,
) -> None:
    """Run/Jog/Home 入口状态 → OperationConflictError。"""
    fake_serial_backend._poll_status_sync = MagicMock()  # type: ignore[method-assign]
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={"state": busy_state}
    )
    with pytest.raises(OperationConflictError):
        fake_serial_backend.recover_from_alarm(idempotency_key=f"busy-{busy_state.value}")


# ── move_to 早退（非 dry_run） ──

def test_move_to_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.move_to(Position(x_mm=-10, y_mm=-10, z_mm=-5))


def test_move_to_raises_when_not_homed(
    fake_serial_backend: GantryBackend,
) -> None:
    # is_homed 默认 False
    with pytest.raises(MachineNotHomedError):
        fake_serial_backend.move_to(Position(x_mm=-10, y_mm=-10, z_mm=-5))


def test_move_to_soft_limit_violation_before_connection_check(
    disconnected_backend: GantryBackend,
) -> None:
    """soft_limit 校验先于 _ser 检查（dry-run 时 disconnect 也应能报 soft limit）。"""
    with pytest.raises(SoftLimitExceededError):
        disconnected_backend.move_to(
            Position(x_mm=-999, y_mm=-10, z_mm=-5), dry_run=True
        )


# ── move_to Z 刹车 brake-skip（Issue #025，2026-04-24 从 RelayBackend 上移）──


def _prep_move_to_ready(backend: GantryBackend, current_z: float) -> None:
    """把 backend 搭到 move_to 能走通的状态：已归零 + position.z 设好 +
    mock 掉发令/等 Idle 的串口相关路径。"""
    backend._is_homed = True
    backend._status = backend._status.model_copy(
        update={
            "state": MachineState.IDLE,
            "position": Position(x_mm=-10.0, y_mm=-10.0, z_mm=current_z),
        }
    )
    backend._send_line_blocking = MagicMock()  # type: ignore[method-assign]
    backend._poll_status_sync = MagicMock()  # type: ignore[method-assign]
    backend._wait_idle = MagicMock()  # type: ignore[method-assign]


def test_move_to_skips_brake_when_z_unchanged(
    fake_serial_backend: GantryBackend,
) -> None:
    """Z 位置不变（|Δz| < 0.01mm）→ release/lock brake 都不调，继电器不动。"""
    _prep_move_to_ready(fake_serial_backend, current_z=-5.0)

    fake_serial_backend.move_to(Position(x_mm=-50.0, y_mm=-50.0, z_mm=-5.0))

    fake_serial_backend._release_brake.assert_not_called()  # type: ignore[attr-defined]
    fake_serial_backend._lock_brake.assert_not_called()  # type: ignore[attr-defined]


def test_move_to_refreshes_position_before_brake_skip(
    fake_serial_backend: GantryBackend,
) -> None:
    """A stale cached Z must not cause an unnecessary CH2 cycle."""
    _prep_move_to_ready(fake_serial_backend, current_z=-5.0)

    def refresh(*, timeout_s: float) -> bool:
        del timeout_s
        fake_serial_backend._status = fake_serial_backend._status.model_copy(
            update={
                "position": Position(x_mm=-287.0, y_mm=-245.5, z_mm=-50.0)
            }
        )
        return True

    fake_serial_backend._poll_status_sync = refresh  # type: ignore[method-assign]
    fake_serial_backend.move_to(Position(x_mm=-13.0, y_mm=-40.0, z_mm=-50.0))

    fake_serial_backend._release_brake.assert_not_called()  # type: ignore[attr-defined]
    fake_serial_backend._lock_brake.assert_not_called()  # type: ignore[attr-defined]


def test_wait_idle_reports_idle_position_mismatch(
    fake_serial_backend: GantryBackend,
) -> None:
    """An Idle controller at the wrong position is not an Idle timeout."""
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={
            "state": MachineState.IDLE,
            "position": Position(x_mm=-287.0, y_mm=-245.5, z_mm=-50.0),
        }
    )
    fake_serial_backend._poll_status_sync = (  # type: ignore[method-assign]
        lambda timeout_s: True
    )

    with pytest.raises(HomingTimeoutError) as exc_info:
        fake_serial_backend._wait_idle(
            timeout_s=0.01,
            target=Position(x_mm=-13.0, y_mm=-40.0, z_mm=-50.0),
        )

    assert "Idle but the target was not reached" in exc_info.value.agent_message
    assert "target=(-13.000, -40.000, -50.000)" in exc_info.value.agent_message
    assert "actual=(-287.000, -245.500, -50.000)" in exc_info.value.agent_message


def test_move_to_skips_brake_within_epsilon(
    fake_serial_backend: GantryBackend,
) -> None:
    """|Δz| < 0.01mm 算不变——grbl MPos 噪声容忍。"""
    _prep_move_to_ready(fake_serial_backend, current_z=-5.000)

    fake_serial_backend.move_to(Position(x_mm=-50.0, y_mm=-50.0, z_mm=-5.005))

    fake_serial_backend._release_brake.assert_not_called()  # type: ignore[attr-defined]
    fake_serial_backend._lock_brake.assert_not_called()  # type: ignore[attr-defined]


def test_move_to_invokes_brake_when_z_changes(
    fake_serial_backend: GantryBackend,
) -> None:
    """Z 位置变 → release 1 次 + lock 1 次（运动前释放，finally 锁回）。"""
    _prep_move_to_ready(fake_serial_backend, current_z=-5.0)

    fake_serial_backend.move_to(Position(x_mm=-50.0, y_mm=-50.0, z_mm=-50.0))

    assert fake_serial_backend._release_brake.call_count == 1  # type: ignore[attr-defined]
    assert fake_serial_backend._lock_brake.call_count == 1  # type: ignore[attr-defined]


def test_move_to_ten_z_changes_all_invoke_brake(
    fake_serial_backend: GantryBackend,
) -> None:
    """连续 10 次 Z 变动的 move_to 都要真调 brake——state-memo 不该吞掉任何一次。

    回归防护：这是 2026-04-24 pick-and-place 里跨过 force 路径后的预期行为。
    """
    _prep_move_to_ready(fake_serial_backend, current_z=-5.0)

    zs = [-10.0, -20.0, -30.0, -40.0, -50.0, -40.0, -30.0, -20.0, -10.0, -5.0]
    for z in zs:
        # 每次把 position.z 推进到上一次 target（模拟真实运动）
        fake_serial_backend.move_to(Position(x_mm=-50.0, y_mm=-50.0, z_mm=z))
        fake_serial_backend._status = fake_serial_backend._status.model_copy(
            update={"position": Position(x_mm=-50.0, y_mm=-50.0, z_mm=z)}
        )

    assert fake_serial_backend._release_brake.call_count == 10  # type: ignore[attr-defined]
    assert fake_serial_backend._lock_brake.call_count == 10  # type: ignore[attr-defined]


def test_release_brake_uses_force_path(stub_config: L3Config) -> None:
    """`_release_brake` 调 `_relay._ch_on_force`，不调普通 `ch_on`。

    这是 2026-04-24 修复的核心：memo 和物理失同步时 `ch_on` 会 noop，
    必须走 force 路径保证字节真发。
    """
    b = GantryBackend(config=stub_config, port="/dev/null")
    mock_relay = MagicMock()
    mock_relay.is_connected.return_value = True
    b._relay = mock_relay

    b._release_brake()

    mock_relay._ch_on_force.assert_called_once_with(2)  # Z_BRAKE_CHANNEL
    mock_relay.ch_on.assert_not_called()


def test_lock_brake_uses_force_path_and_sleeps(stub_config: L3Config) -> None:
    """`_lock_brake` 调 `_ch_off_force` + 末尾 sleep（EMI grace period）。"""
    import src.hardware.gantry_backend as gb_mod

    b = GantryBackend(config=stub_config, port="/dev/null")
    mock_relay = MagicMock()
    b._relay = mock_relay

    with patch.object(gb_mod.time, "sleep") as mock_sleep:
        b._lock_brake()

    mock_relay._ch_off_force.assert_called_once_with(2)
    mock_relay.ch_off.assert_not_called()
    # grace sleep 参数 == _BRAKE_LOCK_GRACE_S
    mock_sleep.assert_called_once_with(gb_mod._BRAKE_LOCK_GRACE_S)


# ── start_move_async 预检查 ──

def test_start_move_async_raises_when_disconnected(
    disconnected_backend: GantryBackend,
) -> None:
    with pytest.raises(L3ConnectionError):
        disconnected_backend.start_move_async(Position(x_mm=-10, y_mm=-10, z_mm=-5))


def test_start_move_async_raises_when_not_homed(
    fake_serial_backend: GantryBackend,
) -> None:
    with pytest.raises(MachineNotHomedError):
        fake_serial_backend.start_move_async(Position(x_mm=-10, y_mm=-10, z_mm=-5))


def test_start_move_async_raises_on_soft_limit(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._is_homed = True
    with pytest.raises(SoftLimitExceededError):
        fake_serial_backend.start_move_async(Position(x_mm=-999, y_mm=-10, z_mm=-5))


def test_start_move_async_raises_on_overlap(
    fake_serial_backend: GantryBackend,
) -> None:
    """上一次 move 还没结束 → OperationConflictError。"""
    fake_serial_backend._is_homed = True
    # 模拟 move_thread alive
    alive_thread = MagicMock()
    alive_thread.is_alive.return_value = True
    fake_serial_backend._move_thread = alive_thread
    with pytest.raises(OperationConflictError):
        fake_serial_backend.start_move_async(Position(x_mm=-10, y_mm=-10, z_mm=-5))


# ── is_move_in_progress / consume_last_move_result ──

def test_is_move_in_progress_false_when_no_thread(
    fake_serial_backend: GantryBackend,
) -> None:
    assert fake_serial_backend.is_move_in_progress() is False


def test_is_move_in_progress_false_when_thread_done(
    fake_serial_backend: GantryBackend,
) -> None:
    dead_thread = MagicMock()
    dead_thread.is_alive.return_value = False
    fake_serial_backend._move_thread = dead_thread
    assert fake_serial_backend.is_move_in_progress() is False


def test_consume_last_move_result_returns_and_clears(
    fake_serial_backend: GantryBackend,
) -> None:
    from src.hardware.types import MoveResult
    mock_result = MoveResult(
        success=True,
        final_position=Position(x_mm=-1, y_mm=-2, z_mm=-3),
        duration_ms=100.0,
        event_id="evt-1",
    )
    fake_serial_backend._last_move_result = mock_result
    r, e = fake_serial_backend.consume_last_move_result()
    assert r is mock_result
    assert e is None
    # 第二次取应该为空
    r2, e2 = fake_serial_backend.consume_last_move_result()
    assert r2 is None and e2 is None


# ── _parse_status_line 纯字符串 ──

def test_parse_status_line_idle(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._parse_status_line("<Idle|MPos:-1.234,-5.678,-9.012|Bf:35,255>")
    st = fake_serial_backend._status
    assert st.state == MachineState.IDLE
    assert st.position.x_mm == pytest.approx(-1.234)
    assert st.position.y_mm == pytest.approx(-5.678)
    assert st.position.z_mm == pytest.approx(-9.012)
    assert st.planner_buffer_free == 35
    assert st.rx_buffer_free == 255


def test_parse_status_line_rejects_int32_overflow_position(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._is_homed = True
    fake_serial_backend._status = fake_serial_backend._status.model_copy(
        update={
            "position": Position(x_mm=-150.0, y_mm=-171.0, z_mm=-65.0),
            "is_homed": True,
        }
    )

    fake_serial_backend._parse_status_line(
        "<Idle|MPos:-2147483.647,-2147483.647,2147483.647|Pn:XYZ>"
    )

    status = fake_serial_backend._status
    assert status.position == Position(x_mm=-150.0, y_mm=-171.0, z_mm=-65.0)
    assert status.position_valid is False
    assert status.state == MachineState.UNKNOWN
    assert status.is_homed is False
    assert fake_serial_backend._is_homed is False
    assert status.limit_pins == ["X", "Y", "Z"]
    assert "outside configured envelope" in status.status_error


def test_parse_status_line_jog(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._parse_status_line("<Jog|MPos:-10.0,-20.0,-5.0|FS:2000,0>")
    assert fake_serial_backend._status.state == MachineState.JOG


def test_parse_status_line_alarm(fake_serial_backend: GantryBackend) -> None:
    fake_serial_backend._parse_status_line("<Alarm|MPos:-1.0,-1.0,-1.0>")
    assert fake_serial_backend._status.state == MachineState.ALARM


def test_parse_status_line_unknown_state(fake_serial_backend: GantryBackend) -> None:
    """未知 state 字符串不应 crash —— 回退到 UNKNOWN。"""
    fake_serial_backend._parse_status_line("<WhatEver|MPos:0.0,0.0,0.0>")
    assert fake_serial_backend._status.state == MachineState.UNKNOWN


def test_parse_status_line_ignores_malformed(
    fake_serial_backend: GantryBackend,
) -> None:
    """完全不匹配正则的行不应改变状态。"""
    prev = fake_serial_backend._status
    fake_serial_backend._parse_status_line("not a status line")
    assert fake_serial_backend._status is prev


# ── _drop_serial ──

def test_drop_serial_closes_and_clears_state(
    fake_serial_backend: GantryBackend,
) -> None:
    fake_serial_backend._cline = [10, 20]
    fake_serial_backend._sline = ["$G", "$H"]
    fake_serial_backend._drop_serial()
    assert fake_serial_backend._ser is None
    assert fake_serial_backend._cline == []
    assert fake_serial_backend._sline == []
    assert fake_serial_backend._status.state == MachineState.DISCONNECTED


def test_drop_serial_idempotent_when_already_none(
    disconnected_backend: GantryBackend,
) -> None:
    disconnected_backend._drop_serial()  # 不应抛
    assert disconnected_backend._ser is None
