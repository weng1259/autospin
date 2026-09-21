"""Slice 5 自动化验收：`recover_from_alarm` 的入口状态分支 + action 注册表。

不动硬件：mock 掉 `_ser` / `_brake` / 内部 `soft_reset` / `unlock_alarm` /
`home`，只跑 `recover_from_alarm` 的控制流。验证：

  场景 I  (Idle)        → 立即返回，actions_taken=[]，天然幂等 no-op
  场景 R  (Run)         → OperationConflictError（让 PM 先 halt）
  场景 D  (Disconnected)→ ConnectionError（Slice 4 重连路径接管）
  场景 A  (Alarm)       → actions = [soft_reset, unlock_alarm, home]
  场景 H  (Hold)        → actions = [soft_reset, unlock_alarm, home]
                           （soft-reset 后 `$22=1` 进 ALARM:11 再 $X）
  注册表                → error_actions["L3.ALARM_STATE"] 指向 _action_recover
                           且 label 含「清除」+「恢复」

对应 phase-3.1-plan §Slice 5 验收清单的 **#2 语义层**（"点按钮自动
unlock → home → Idle"）以及 **#3 事件层**（"历史表格完整记录整个过程"）。
真撞 alarm 的验收清单 #1 / #4 依然靠 PM 手动在浏览器跑一遍（做不了 mock）。

运行:
    tools/spikes/.venv/bin/python tools/ui/verify_slice5_recovery.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.hardware.errors import (  # noqa: E402
    ConnectionError as L3ConnectionError,
    L3Error,
    OperationConflictError,
)
from src.hardware.gantry_backend import GantryBackend  # noqa: E402
from src.hardware.types import (  # noqa: E402
    HomeResult,
    MachineState,
    Position,
    RecoveryResult,
)
from tools.ui.error_actions import ACTIONS, _action_recover  # noqa: E402

GOOD = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
DIM = "\033[2m"
RST = "\033[0m"


def _check(desc: str, cond: bool) -> bool:
    print(f"  {GOOD if cond else BAD} {desc}")
    return cond


def _make_fake_backend(initial_state: MachineState) -> GantryBackend:
    """一个没接任何真硬件的 backend：_ser / _brake 都是 MagicMock，_status
    预置成 `initial_state`。poller 没启动。"""
    b = GantryBackend()
    b._ser = MagicMock()
    b._ser.is_open = True
    b._brake = MagicMock()
    b._status = b._status.model_copy(update={"state": initial_state})
    return b


def scenario_idle() -> bool:
    """Idle 入口 → no-op；actions_taken=[]；final state = Idle"""
    print("\n── 场景 I：Idle 入口 → 天然幂等 no-op ──")
    b = _make_fake_backend(MachineState.IDLE)
    b._poll_status_sync = lambda timeout_s: None  # 不真发 `?`
    ok = True
    try:
        result = b.recover_from_alarm(idempotency_key="idle-noop")
        ok &= _check("返回 RecoveryResult", isinstance(result, RecoveryResult))
        ok &= _check("success == True", result.success is True)
        ok &= _check("entry_state == Idle", result.entry_state == MachineState.IDLE)
        ok &= _check("actions_taken == []（无动作）", result.actions_taken == [])
        ok &= _check(
            "final_status.state == Idle",
            result.final_status.state == MachineState.IDLE,
        )
        ok &= _check("event_id 非空（@observable 注入）", bool(result.event_id))
    except Exception as e:
        ok &= _check(f"未抛异常（实际 {type(e).__name__}: {e}）", False)
        traceback.print_exc()
    return ok


def scenario_run() -> bool:
    """Run 入口 → OperationConflictError"""
    print("\n── 场景 R：Run 入口 → OperationConflictError（先 halt）──")
    b = _make_fake_backend(MachineState.RUN)
    b._poll_status_sync = lambda timeout_s: None
    ok = True
    try:
        b.recover_from_alarm(idempotency_key="run-conflict")
        ok &= _check("抛出 OperationConflictError", False)
    except OperationConflictError as e:
        ok &= _check("OperationConflictError 抛出", True)
        ok &= _check(
            "error_code == L3.OPERATION_CONFLICT",
            e.error_code == "L3.OPERATION_CONFLICT",
        )
        ok &= _check("human_message 提到「停」", "停" in e.human_message)
    except Exception as e:
        ok &= _check(
            f"抛 OperationConflictError（实际 {type(e).__name__}）", False
        )
        traceback.print_exc()
    return ok


def scenario_disconnected() -> bool:
    """未连接 → ConnectionError（Slice 4 重连路径接管）"""
    print("\n── 场景 D：未连接 → ConnectionError ──")
    b = GantryBackend()  # 不 mock _ser，保持 None
    ok = True
    try:
        b.recover_from_alarm(idempotency_key="dc-test")
        ok &= _check("抛出 ConnectionError", False)
    except L3ConnectionError as e:
        ok &= _check("ConnectionError 抛出", True)
        ok &= _check("error_code == L3.CONNECTION", e.error_code == "L3.CONNECTION")
    except Exception as e:
        ok &= _check(f"抛 ConnectionError（实际 {type(e).__name__}）", False)
        traceback.print_exc()
    return ok


def _run_alarm_or_hold(entry: MachineState) -> bool:
    """Alarm / Hold 入口的共享验证逻辑：actions_taken=[soft_reset, unlock_alarm,
    home]，soft_reset 后 `$22=1` 让 grbl 进 ALARM:11 所以 $X 也会被调。"""
    b = _make_fake_backend(entry)

    call_log: list[str] = []

    def mock_soft_reset() -> None:
        call_log.append("soft_reset")
        # grbl `$22=1` 情形：soft-reset 后进 alarm:11（Homing required）
        b._status = b._status.model_copy(
            update={"state": MachineState.ALARM, "alarm_code": 11}
        )
        b._is_homed = False

    def mock_unlock_alarm():
        call_log.append("unlock_alarm")
        b._status = b._status.model_copy(update={"state": MachineState.IDLE})
        b._alarm_code = None
        return b.get_status()

    def mock_home(*, idempotency_key: str):
        call_log.append("home")
        b._is_homed = True
        b._status = b._status.model_copy(update={"state": MachineState.IDLE})
        return HomeResult(
            success=True,
            position_after_pulloff=Position(x_mm=-5.0, y_mm=-5.0, z_mm=-5.0),
            duration_ms=30000.0,
            event_id="mock-home-event",
        )

    b.soft_reset = mock_soft_reset  # type: ignore[method-assign]
    b.unlock_alarm = mock_unlock_alarm  # type: ignore[method-assign]
    b.home = mock_home  # type: ignore[method-assign]
    b._poll_status_sync = lambda timeout_s: None  # 不发 `?`

    ok = True
    try:
        result = b.recover_from_alarm(idempotency_key=f"{entry.value}-path")
        ok &= _check(
            f"entry_state == {entry.value}", result.entry_state == entry
        )
        ok &= _check("success == True", result.success is True)
        ok &= _check(
            "actions_taken == [soft_reset, unlock_alarm, home]",
            result.actions_taken == ["soft_reset", "unlock_alarm", "home"],
        )
        ok &= _check(
            "final_status.state == Idle（恢复后）",
            result.final_status.state == MachineState.IDLE,
        )
        ok &= _check(
            "final_status.is_homed == True",
            result.final_status.is_homed is True,
        )
        ok &= _check("duration_ms > 0", result.duration_ms > 0)
        ok &= _check("event_id 非空", bool(result.event_id))
        ok &= _check(
            "call_log 顺序 = [soft_reset, unlock_alarm, home]",
            call_log == ["soft_reset", "unlock_alarm", "home"],
        )
    except Exception as e:
        ok &= _check(f"未抛异常（实际 {type(e).__name__}: {e}）", False)
        traceback.print_exc()
    return ok


def scenario_alarm() -> bool:
    print("\n── 场景 A：Alarm 入口 → soft_reset → $X → $H ──")
    return _run_alarm_or_hold(MachineState.ALARM)


def scenario_hold() -> bool:
    print("\n── 场景 H：Hold 入口 → soft_reset → $X（alarm:11） → $H ──")
    return _run_alarm_or_hold(MachineState.HOLD)


def scenario_skip_rehome() -> bool:
    """skip_rehome=True → 不调 home，actions_taken 只剩 soft_reset + unlock_alarm"""
    print("\n── 场景 S：skip_rehome=True → home 被跳过 ──")
    b = _make_fake_backend(MachineState.ALARM)

    call_log: list[str] = []

    def mock_soft_reset() -> None:
        call_log.append("soft_reset")
        b._status = b._status.model_copy(
            update={"state": MachineState.ALARM, "alarm_code": 11}
        )

    def mock_unlock_alarm():
        call_log.append("unlock_alarm")
        b._status = b._status.model_copy(update={"state": MachineState.IDLE})
        return b.get_status()

    def mock_home(*, idempotency_key: str):
        call_log.append("home")  # 不应被调用
        return HomeResult(
            success=True,
            position_after_pulloff=Position(x_mm=0, y_mm=0, z_mm=0),
            duration_ms=0, event_id="X",
        )

    b.soft_reset = mock_soft_reset  # type: ignore[method-assign]
    b.unlock_alarm = mock_unlock_alarm  # type: ignore[method-assign]
    b.home = mock_home  # type: ignore[method-assign]
    b._poll_status_sync = lambda timeout_s: None

    ok = True
    try:
        result = b.recover_from_alarm(
            idempotency_key="skip-rehome", skip_rehome=True
        )
        ok &= _check(
            "actions_taken == [soft_reset, unlock_alarm]（无 home）",
            result.actions_taken == ["soft_reset", "unlock_alarm"],
        )
        ok &= _check("home 未被调用", "home" not in call_log)
    except Exception as e:
        ok &= _check(f"未抛异常（实际 {type(e).__name__}: {e}）", False)
        traceback.print_exc()
    return ok


def scenario_action_registry() -> bool:
    """error_actions 注册表：L3.ALARM_STATE → _action_recover + 中文 label"""
    print("\n── 场景 X：error_actions 注册表对齐 ──")
    ok = True
    spec = ACTIONS.get("L3.ALARM_STATE")
    ok &= _check("L3.ALARM_STATE 注册了 ActionSpec", spec is not None)
    if spec is not None:
        ok &= _check(
            f"handler == _action_recover（实际 {spec.handler.__name__}）",
            spec.handler is _action_recover,
        )
        ok &= _check(
            f"label 含「清除」与「恢复」（实际 {spec.label!r}）",
            "清除" in spec.label and "恢复" in spec.label,
        )
        ok &= _check(
            "help 提到「soft-reset」或「$H」或「归零」",
            any(kw in spec.help for kw in ("soft-reset", "$H", "归零")),
        )
    return ok


def scenario_idempotency_5min() -> bool:
    """@observable 的 TTL 改到 5 min，确认不是 24h。"""
    print("\n── 场景 T：observable 幂等 TTL == 5 min ──")
    from src.observable import _IDEM_TTL_S
    ok = _check(
        f"_IDEM_TTL_S == 300（5 min），实际 {_IDEM_TTL_S}",
        _IDEM_TTL_S == 5 * 60,
    )
    return ok


def main() -> int:
    print("Slice 5 恢复流程自动验收（不动硬件）\n")

    results = [
        ("场景 I  Idle no-op",               scenario_idle()),
        ("场景 R  Run conflict",             scenario_run()),
        ("场景 D  Disconnected",             scenario_disconnected()),
        ("场景 A  Alarm 全路径",             scenario_alarm()),
        ("场景 H  Hold 全路径",              scenario_hold()),
        ("场景 S  skip_rehome",              scenario_skip_rehome()),
        ("场景 X  注册表对齐",               scenario_action_registry()),
        ("场景 T  idem TTL = 5 min",         scenario_idempotency_5min()),
    ]

    print("\n── 总结 ──")
    all_ok = True
    for name, ok in results:
        print(f"  {GOOD if ok else BAD} {name}")
        all_ok &= ok
    print()
    if all_ok:
        print(
            f"{GOOD} Slice 5 数据模型 ✅"
            f"（分支矩阵 + 注册表 + TTL 全部对齐）"
        )
        print(
            f"  {DIM}真撞 alarm 验收项 #1/#4 仍需 PM 在浏览器手动跑一遍（做不了 mock）{RST}"
        )
        return 0
    else:
        print(f"{BAD} Slice 5 存在不一致，见上方 ✗ 项")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
