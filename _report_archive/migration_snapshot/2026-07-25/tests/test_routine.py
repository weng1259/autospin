"""示教-重放（src/routine.py）单测。

不依赖真硬件：用假 backend 验证录制（白名单/dry_run 跳过/成功才记）、
pydantic 参数 JSON 往返、回放（顺序/还原参数/中止/步错误/安全闸）、文件持久化。
"""

from __future__ import annotations

import pytest

from src.hardware.types import Position
from src.routine import (
    DEFAULT_RECORDABLE,
    PlayerOptions,
    RecordingProxy,
    Routine,
    RoutinePlayer,
    RoutineRecorder,
    list_routines,
    load_routine,
    save_routine,
)


# ── 假 backend ──────────────────────────────────────────────────────────────


class FakeGantry:
    def __init__(self, homed: bool = True) -> None:
        self._homed = homed
        self.calls: list[tuple] = []

    def is_homed(self) -> bool:
        return self._homed

    def get_status(self):
        self.calls.append(("get_status", (), {}))
        return "status"

    def move_to(self, target, *, feed_mm_min=None, dry_run=False, wait_for_idle=True):
        self.calls.append(("move_to", (target,), {"feed_mm_min": feed_mm_min, "dry_run": dry_run}))
        return "moveresult"

    def jog(self, axis, distance_mm, feed_mm_min=100.0):
        self.calls.append(("jog", (axis, distance_mm), {"feed_mm_min": feed_mm_min}))
        return Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)

    def home(self):
        self.calls.append(("home", (), {}))
        return "home"


class FakeGripper:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def open(self):
        self.calls.append(("open", (), {}))

    def close(self):
        self.calls.append(("close", (), {}))


class FakeRelay:
    """像真 RelayBackend 一样要求 idempotency_key（keyword-only, 必填）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ch_on(self, channel, *, idempotency_key, dry_run=False):
        self.calls.append(("ch_on", channel, idempotency_key))

    def ch_off(self, channel, *, idempotency_key, dry_run=False):
        self.calls.append(("ch_off", channel, idempotency_key))


class Boom:
    """一调指定方法就抛错的假 backend。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def open(self):
        self.calls.append(("open", (), {}))
        raise RuntimeError("夹爪卡住")


# ── 录制 ────────────────────────────────────────────────────────────────────


def test_proxy_records_whitelisted_actions():
    rec = RoutineRecorder()
    g = FakeGantry()
    proxy = RecordingProxy(g, rec, "gantry", DEFAULT_RECORDABLE["gantry"])

    rec.arm("test")
    proxy.move_to(Position(x_mm=-100.0, y_mm=-50.0, z_mm=-20.0), feed_mm_min=800.0)
    proxy.jog("X", -1.0, feed_mm_min=100.0)

    assert rec.step_count == 2
    s0, s1 = rec.steps
    assert (s0.device, s0.action) == ("gantry", "move_to")
    # Position 被编码成带标签 dict
    assert s0.args[0]["__type__"] == "Position"
    assert s0.args[0]["fields"]["x_mm"] == -100.0
    assert s0.kwargs["feed_mm_min"] == 800.0
    assert (s1.device, s1.action) == ("gantry", "jog")
    assert s1.args == ["X", -1.0]
    # 真方法确实被调用了
    assert [c[0] for c in g.calls] == ["move_to", "jog"]


def test_dry_run_not_recorded():
    rec = RoutineRecorder()
    proxy = RecordingProxy(FakeGantry(), rec, "gantry")
    rec.arm()
    proxy.move_to(Position(x_mm=-1.0, y_mm=-1.0, z_mm=-1.0), dry_run=True)
    assert rec.step_count == 0


def test_disarmed_not_recorded():
    rec = RoutineRecorder()
    proxy = RecordingProxy(FakeGantry(), rec, "gantry")
    proxy.jog("X", -1.0)            # 未 arm
    assert rec.step_count == 0


def test_non_whitelisted_not_recorded():
    rec = RoutineRecorder()
    proxy = RecordingProxy(FakeGantry(), rec, "gantry")
    rec.arm()
    proxy.get_status()             # 查询不在白名单
    assert rec.step_count == 0


def test_failed_action_not_recorded():
    rec = RoutineRecorder()
    proxy = RecordingProxy(Boom(), rec, "gripper", {"open"})
    rec.arm()
    with pytest.raises(RuntimeError):
        proxy.open()
    assert rec.step_count == 0     # 抛错的动作不记


def test_record_strips_idempotency_key():
    rec = RoutineRecorder()
    rec.arm()
    rec.record("relay", "ch_on", kwargs={"channel": 3, "idempotency_key": "abc123"})
    s = rec.steps[0]
    assert "idempotency_key" not in s.kwargs   # 临时值不入程序
    assert s.kwargs["channel"] == 3


def test_player_injects_fresh_idempotency_key():
    rec = RoutineRecorder()
    rec.arm()
    rec.record("relay", "ch_on", kwargs={"channel": 3})
    routine = rec.to_routine()

    relay = FakeRelay()
    player = RoutinePlayer({"relay": relay}, clock=lambda: 0.0, sleep=lambda s: None)
    result = player.run(routine, PlayerOptions(step_delay_s=0.0, require_homed=False))

    assert result.success
    assert relay.calls[0][0] == "ch_on"
    assert relay.calls[0][1] == 3
    assert relay.calls[0][2]                   # idempotency_key 被注入（非空）


def test_add_wait_step():
    rec = RoutineRecorder()
    rec.arm()
    rec.add_wait(5.0)
    s = rec.steps[0]
    assert (s.device, s.action) == ("control", "wait")
    assert s.kwargs["seconds"] == 5.0
    assert "等待" in s.label


# ── 序列化往返 ──────────────────────────────────────────────────────────────


def test_recorder_editing():
    rec = RoutineRecorder()
    rec.arm()
    rec.record("gripper", "close")
    rec.record("gripper", "open")
    rec.add_wait(2.0)
    assert rec.step_count == 3
    rec.remove_last()
    assert rec.step_count == 2
    rec.remove_at(0)
    assert rec.step_count == 1
    assert rec.steps[0].seq == 0 and rec.steps[0].action == "open"   # 重编号

    r2 = RoutineRecorder()
    r2.set_steps(rec.steps)
    assert r2.step_count == 1 and r2.is_armed


def test_routine_json_roundtrip_preserves_position():
    rec = RoutineRecorder()
    proxy = RecordingProxy(FakeGantry(), rec, "gantry")
    rec.arm("往返")
    proxy.move_to(Position(x_mm=-100.0, y_mm=-50.0, z_mm=-20.0), feed_mm_min=800.0)
    routine = rec.to_routine()

    text = routine.to_json()
    restored = Routine.from_json(text)
    assert restored.name == "往返"
    assert restored.steps[0].args[0]["fields"]["y_mm"] == -50.0


# ── 回放 ────────────────────────────────────────────────────────────────────


def _build_routine():
    rec = RoutineRecorder()
    g = RecordingProxy(FakeGantry(), rec, "gantry")
    gr = RecordingProxy(FakeGripper(), rec, "gripper")
    rec.arm("演示")
    g.move_to(Position(x_mm=-100.0, y_mm=-50.0, z_mm=-20.0), feed_mm_min=800.0)
    gr.close()
    g.jog("Y", -2.0, feed_mm_min=100.0)
    gr.open()
    return rec.to_routine()


def test_player_replays_in_order_with_decoded_args():
    routine = _build_routine()
    gantry, gripper = FakeGantry(homed=True), FakeGripper()
    player = RoutinePlayer({"gantry": gantry, "gripper": gripper},
                           clock=lambda: 0.0, sleep=lambda s: None)

    result = player.run(routine, PlayerOptions(step_delay_s=0.0))

    assert result.success
    assert result.steps_completed == 4
    assert [c[0] for c in gantry.calls] == ["move_to", "jog"]
    assert [c[0] for c in gripper.calls] == ["close", "open"]
    # Position 被还原成真实例（不是 dict）传给真 backend
    target = gantry.calls[0][1][0]
    assert isinstance(target, Position) and target.x_mm == -100.0


def test_player_aborts():
    routine = _build_routine()
    gantry, gripper = FakeGantry(homed=True), FakeGripper()
    player = RoutinePlayer({"gantry": gantry, "gripper": gripper},
                           clock=lambda: 0.0, sleep=lambda s: None)

    calls = {"n": 0}

    def abort_after_two():
        calls["n"] += 1
        return calls["n"] > 2      # 第 3 步前中止

    result = player.run(routine, PlayerOptions(step_delay_s=0.0), abort_check=abort_after_two)
    assert not result.success and result.aborted
    assert result.steps_completed == 2


def test_player_requires_homed_for_motion():
    routine = _build_routine()                 # 含 move_to/jog → has_motion
    player = RoutinePlayer({"gantry": FakeGantry(homed=False)},
                           clock=lambda: 0.0, sleep=lambda s: None)
    result = player.run(routine, PlayerOptions(require_homed=True))
    assert not result.success
    assert "归零" in result.error
    assert result.steps_completed == 0


def test_player_step_error_stops_and_reports():
    rec = RoutineRecorder()
    gr = RecordingProxy(FakeGripper(), rec, "gripper")
    rec.arm()
    gr.open()
    gr.close()
    routine = rec.to_routine()

    # 回放时 gripper 在 open 后 close 抛错
    boom = Boom()           # open 会抛错
    player = RoutinePlayer({"gripper": boom}, clock=lambda: 0.0, sleep=lambda s: None)
    result = player.run(routine, PlayerOptions(step_delay_s=0.0, require_homed=False))
    assert not result.success
    assert result.steps_completed == 0          # 第 1 步 open 即失败
    assert "失败" in result.error
    assert result.step_results[-1].ok is False


def test_player_wait_step_sleeps():
    rec = RoutineRecorder()
    rec.arm()
    rec.add_wait(3.0)
    routine = rec.to_routine()

    slept: list[float] = []
    player = RoutinePlayer({}, clock=lambda: 0.0, sleep=lambda s: slept.append(s))
    result = player.run(routine, PlayerOptions(step_delay_s=0.0, require_homed=False))
    assert result.success
    assert slept == [3.0]


def test_player_unknown_device_errors():
    rec = RoutineRecorder()
    gr = RecordingProxy(FakeGripper(), rec, "gripper")
    rec.arm()
    gr.open()
    routine = rec.to_routine()
    # 回放时没提供 gripper backend
    player = RoutinePlayer({}, clock=lambda: 0.0, sleep=lambda s: None)
    result = player.run(routine, PlayerOptions(step_delay_s=0.0, require_homed=False))
    assert not result.success
    assert "未连接" in result.error


# ── 文件持久化 ──────────────────────────────────────────────────────────────


def test_save_list_load_roundtrip(tmp_path):
    routine = _build_routine()
    routine.name = "搬运演示"
    path = save_routine(routine, base=tmp_path)
    assert path.exists()
    assert path in list_routines(base=tmp_path)

    loaded = load_routine("搬运演示", base=tmp_path)
    assert loaded.name == "搬运演示"
    assert len(loaded.steps) == 4
