"""Slice 5 自动化 PM 验收代理脚本。

直接调 GantryBackend（不走 Streamlit UI），跑 PM 清单里软件可自动化的场景。

**注意：grbl 启动行为观察（2026-04-22）**：grbl-Mega-5X commit a5596ef 即使
`$22=1`，在**已归零过的机器**上也会 boot 回 Idle（保留 is_homed + MPos），
不进 Alarm:11。所以"冷启 alarm"这个场景在我们的机器上**软件无法模拟**。
只能靠 PM #4 真撞硬限位或清 EEPROM 后重新 boot。

本脚本跑的场景：
  #A  真 alarm（soft-limit 违规，G0 过 +边界）→ recover_from_alarm 恢复
  #B  整体端到端剧本（plan §整体）：查状态 → 归零 → 去 A → 超限 → 去 B →
       触发 alarm → recover → 去 C → 查历史
  #C  历史表事件链条（runlog 末尾若干条）

PM 仍需补测：
  #4  真撞硬限位（推机构触发 ES61 → grbl 进 alarm:1）
  #5  UI 双击防抖（浏览器）
  #7  Idle 隐藏按钮（UI 条件渲染）

前置：**必须先 kill Streamlit**（避免抢串口）。

运行:
    tools/spikes/.venv/bin/python tools/ui/slice5_automated_acceptance.py
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.hardware.errors import (  # noqa: E402
    AlarmStateError,
    ConnectionError as L3ConnectionError,
    L3Error,
    SoftLimitExceededError,
)
from src.hardware.gantry_backend import GantryBackend  # noqa: E402
from src.hardware.types import MachineState, Position  # noqa: E402
from src.runlog import RUNLOG  # noqa: E402

GOOD = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
INFO = "\033[36m→\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RST = "\033[0m"


def _check(desc: str, cond: bool) -> bool:
    print(f"  {GOOD if cond else BAD} {desc}")
    return cond


def _info(msg: str) -> None:
    print(f"  {INFO} {msg}")


def _section(title: str) -> None:
    print(f"\n{BOLD}── {title} ──{RST}")


def _poll_until(backend: GantryBackend, pred, timeout_s: float, desc: str) -> bool:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            s = backend.get_status()
            last = s.state
            if pred(s):
                _info(f"{desc} 达成（state={last.value}）")
                return True
        except L3Error:
            pass
        time.sleep(0.1)
    _info(f"{desc} 超时，last state={last}")
    return False


def trigger_real_alarm(backend: GantryBackend) -> None:
    """发 raw `G0 X5 Y5 Z5`（过 +X/Y/Z 边界，max=0）触发真 soft-limit alarm。

    绕过 Python 层 `SoftLimits.assert_contains`，让 grbl 自己 reject 进 Alarm:2。
    `_send_line_blocking` 会捕获 `ALARM:` 响应抛 `AlarmStateError`。
    """
    try:
        backend._send_line_blocking(
            "G0 X5 Y5 Z5\n",
            timeout_s=3.0,
            timeout_msg="raw G0 过界测试命令无响应",
        )
        _info("⚠️ 命令意外没被 reject —— $20 可能没开启软限位")
    except AlarmStateError as e:
        _info(f"grbl 报 {e.agent_message.split(';')[0].split('returned ')[-1]}")


def scenario_a_real_alarm(backend: GantryBackend) -> bool:
    """#A 真 alarm：soft-limit 违规触发 grbl Alarm:2 → recover → Idle + homed"""
    _section("场景 A：真 alarm (soft-limit) → recover_from_alarm")
    ok = True

    # 前置：必须 homed（否则 soft-limits 未激活）
    if not backend.is_homed():
        _info("预归零（soft-limits 需要 homed 才激活）")
        backend.home(idempotency_key=f"prehome-{uuid.uuid4().hex[:8]}")
    ok &= _check("前置：is_homed == True", backend.is_homed())

    _info("发 raw G0 X5 Y5 Z5 触发 soft-limit alarm...")
    trigger_real_alarm(backend)

    time.sleep(0.5)
    status = backend.get_status()
    ok &= _check(
        f"grbl 进 Alarm（state={status.state.value} alarm_code={status.alarm_code}）",
        status.state == MachineState.ALARM,
    )
    ok &= _check(
        f"alarm_code 非空（实际 {status.alarm_code}）",
        status.alarm_code is not None,
    )

    _info("调 recover_from_alarm(key=scenario-A)...")
    t0 = time.time()
    result = backend.recover_from_alarm(idempotency_key=f"scenario-A-{uuid.uuid4().hex[:8]}")
    dt = time.time() - t0
    _info(f"完成用时 {dt:.1f}s")

    ok &= _check(
        f"entry_state == alarm（实际 {result.entry_state.value}）",
        result.entry_state == MachineState.ALARM,
    )
    ok &= _check(
        f"actions_taken == [soft_reset, unlock_alarm, home]（实际 {result.actions_taken}）",
        result.actions_taken == ["soft_reset", "unlock_alarm", "home"],
    )
    ok &= _check(
        f"最终 state == idle（实际 {result.final_status.state.value}）",
        result.final_status.state == MachineState.IDLE,
    )
    ok &= _check("最终 is_homed == True", result.final_status.is_homed is True)
    ok &= _check(f"event_id 非空（{result.event_id[:8]}...）", bool(result.event_id))

    # 验证恢复后能继续用：移到合法位置
    _info("恢复后做一次 move_to 验证机器可用...")
    dest = Position(x_mm=-20.0, y_mm=-20.0, z_mm=-5.0)
    r = backend.move_to(dest, feed_mm_min=2000.0)
    ok &= _check(
        f"恢复后 move_to 成功（最终 X={r.final_position.x_mm:.2f} Y={r.final_position.y_mm:.2f} Z={r.final_position.z_mm:.2f}）",
        r.success and abs(r.final_position.x_mm - dest.x_mm) < 0.5,
    )
    return ok


def scenario_b_end_to_end(backend: GantryBackend) -> bool:
    """#B plan §整体验收剧本：查状态 → 归零 → 去 A → 超限 → 去 B → 软撞 alarm →
    恢复 → 去 C → 查历史"""
    _section("场景 B：整体端到端剧本（plan §整体验收）")
    ok = True

    # Step 1: 查状态
    s = backend.get_status()
    ok &= _check(
        f"Step 1: 查状态（state={s.state.value} homed={s.is_homed}）",
        s.state == MachineState.IDLE and s.is_homed,
    )

    # Step 2: 归零 —— 已归过，跳过（场景 A 末尾状态已 homed + Idle）
    _info(f"Step 2: 已归零，跳过（场景 A 的 post-recover 留下干净 Idle）")

    # Step 3: 去 A 点
    A = Position(x_mm=-50.0, y_mm=-50.0, z_mm=-5.0)
    _info(f"Step 3: move_to A={A.x_mm},{A.y_mm},{A.z_mm}")
    r = backend.move_to(A, feed_mm_min=2000.0)
    ok &= _check(
        f"Step 3: 到达 A（用时 {r.duration_ms/1000:.1f}s）",
        r.success and abs(r.final_position.x_mm - A.x_mm) < 0.5,
    )

    # Step 4: 超限报错（Python 层拦住）
    _info("Step 4: 故意超限 X=-500 → 期望 SoftLimitExceededError（Python 预检）")
    try:
        backend.move_to(Position(x_mm=-500.0, y_mm=-50.0, z_mm=-5.0))
        ok &= _check("Step 4: 抛 SoftLimitExceededError", False)
    except SoftLimitExceededError as e:
        ok &= _check(
            f"Step 4: SoftLimitExceededError 正确抛出（{e.error_code}）",
            e.error_code == "L3.SOFT_LIMIT_EXCEEDED",
        )
    except Exception as e:
        ok &= _check(
            f"Step 4: 抛 SoftLimitExceededError（实际 {type(e).__name__}）", False
        )

    # Step 5: 去 B 点（证明 Step 4 的错误没破坏状态）
    B = Position(x_mm=-100.0, y_mm=-100.0, z_mm=-10.0)
    _info(f"Step 5: move_to B={B.x_mm},{B.y_mm},{B.z_mm}")
    r = backend.move_to(B, feed_mm_min=2000.0)
    ok &= _check(
        f"Step 5: 到达 B（用时 {r.duration_ms/1000:.1f}s）",
        r.success and abs(r.final_position.x_mm - B.x_mm) < 0.5,
    )

    # Step 6: 触发真 alarm（soft-limit 过界）
    _info("Step 6: raw G0 X5 触发 soft-limit alarm")
    trigger_real_alarm(backend)
    time.sleep(0.5)
    ok &= _check(
        "Step 6: grbl 进 Alarm",
        backend.get_status().state == MachineState.ALARM,
    )

    # Step 7: 恢复
    _info("Step 7: recover_from_alarm")
    result = backend.recover_from_alarm(idempotency_key=f"e2e-{uuid.uuid4().hex[:8]}")
    ok &= _check(
        f"Step 7: 恢复完成（entry={result.entry_state.value} → {result.final_status.state.value}，用时 {result.duration_ms/1000:.1f}s）",
        result.final_status.state == MachineState.IDLE and result.final_status.is_homed,
    )

    # Step 8: 去 C 点（证明恢复后机器可用）
    C = Position(x_mm=-80.0, y_mm=-80.0, z_mm=-5.0)
    _info(f"Step 8: move_to C={C.x_mm},{C.y_mm},{C.z_mm}（证明恢复后可用）")
    r = backend.move_to(C, feed_mm_min=2000.0)
    ok &= _check(
        f"Step 8: 到达 C（用时 {r.duration_ms/1000:.1f}s）",
        r.success and abs(r.final_position.x_mm - C.x_mm) < 0.5,
    )
    return ok


def scenario_c_history() -> bool:
    """#C 查 runlog 最近若干条，验证事件链条完整"""
    _section("场景 C：历史表事件链条（runlog 末尾 N 条）")
    events = RUNLOG.query_recent(limit=40)
    print(f"  {DIM}最近 40 条事件时间轴（降序）：{RST}")
    print(f"  {DIM}{'-' * 100}{RST}")
    print(f"  {'#':<3} {'time (UTC)':<20} {'method':<36} {'phase':<10} {'dur(s)':<8}  extra")
    print(f"  {DIM}{'-' * 100}{RST}")
    for i, e in enumerate(events[:40]):
        ts = e["timestamp"][:19]
        method = e["method"]
        phase = e["phase"]
        dur = f"{e['duration_ms']/1000:.2f}" if e["duration_ms"] else "-"
        extra = ""
        if e["error_code"]:
            extra = f" ERR={e['error_code']}"
        elif phase == "completed" and e.get("result"):
            r = e["result"]
            if isinstance(r, dict) and "entry_state" in r:
                extra = f" entry={r['entry_state']} actions={r.get('actions_taken')}"
        print(f"  {i:<3} {ts:<20} {method:<36} {phase:<10} {dur:<8}{extra}")
    print(f"  {DIM}{'-' * 100}{RST}")

    # 断言：最近应该有若干关键事件
    recent_methods = [e["method"] for e in events[:25]]
    ok = True
    ok &= _check(
        "最近 25 条里有 GantryBackend.recover_from_alarm",
        "GantryBackend.recover_from_alarm" in recent_methods,
    )
    ok &= _check(
        "最近 25 条里有 GantryBackend.soft_reset",
        "GantryBackend.soft_reset" in recent_methods,
    )
    ok &= _check(
        "最近 25 条里有 GantryBackend.unlock_alarm",
        "GantryBackend.unlock_alarm" in recent_methods,
    )
    ok &= _check(
        "最近 25 条里有 GantryBackend.home",
        "GantryBackend.home" in recent_methods,
    )
    ok &= _check(
        "最近 25 条里有 GantryBackend.move_to",
        "GantryBackend.move_to" in recent_methods,
    )
    return ok


def main() -> int:
    print(f"{BOLD}Slice 5 自动化 PM 验收代理{RST}")
    print(f"{DIM}（PM 在 grbl $22=1 但已 homed 的机器上，冷启 alarm 不会出现；{RST}")
    print(f"{DIM} 本脚本用 soft-limit 违规触发真 alarm。PM #4/#5/#7 UI 验收仍需手测）{RST}")

    _section("连接 backend")
    backend = GantryBackend(port="/dev/cu.wchusbserial110")
    try:
        backend.connect()
        print(f"  {GOOD} connect() 成功")
        initial = backend.get_status()
        print(f"  {INFO} 初始状态：state={initial.state.value} homed={initial.is_homed} pos=({initial.position.x_mm:.2f},{initial.position.y_mm:.2f},{initial.position.z_mm:.2f})")
    except L3Error as e:
        print(f"  {BAD} connect 失败：{e.human_message}")
        return 1

    try:
        results = []
        results.append(("场景 A  真 alarm + recover",      scenario_a_real_alarm(backend)))
        results.append(("场景 B  整体端到端剧本",           scenario_b_end_to_end(backend)))
        results.append(("场景 C  历史事件链条",              scenario_c_history()))

        print(f"\n{BOLD}── 总结 ──{RST}")
        all_ok = True
        for name, ok in results:
            print(f"  {GOOD if ok else BAD} {name}")
            all_ok &= ok
        print()

        if all_ok:
            print(f"{GOOD} {BOLD}自动化验收全部通过 ✅{RST}")
            print(f"  {DIM}PM 仍需在浏览器补测：#4（真撞硬限位）/ #5（UI 双击防抖）/ #7（Idle 隐藏按钮）{RST}")
            return 0
        else:
            print(f"{BAD} 存在失败项，见上方 ✗")
            return 1
    finally:
        print(f"\n{INFO} 关闭 backend...")
        backend.close()
        print(f"{GOOD} 完成。PM 可以重开 Streamlit 跑剩余 UI 级别验收。")


if __name__ == "__main__":
    raise SystemExit(main())
