"""Slice 4 自动化验收：4 个错误场景 + 建议按钮映射 + 中文字段。

PM 绕过 UI 直接看 L3Error 数据模型是不是按 ADR-004 §原则 2 设计的。
不动硬件（场景 2/3/4 纯逻辑；场景 1 只 connect 不归零不移动）。

运行:
    tools/spikes/.venv/bin/python tools/ui/verify_slice4_errors.py

预期输出:每个场景 ✅ PASS + 一行字段摘要；全通过末尾 "Slice 4 数据模型 ✅"。
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.hardware.errors import (  # noqa: E402
    ConnectionError as L3ConnectionError,
    L3Error,
    MachineNotHomedError,
    OperationConflictError,
    SoftLimitExceededError,
)
from src.hardware.gantry_backend import GantryBackend  # noqa: E402
from src.hardware.types import Position  # noqa: E402
from tools.ui.error_actions import get_action  # noqa: E402

PORT = "/dev/cu.wchusbserial110"
GOOD = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
DIM = "\033[2m"
RST = "\033[0m"


def _check(desc: str, cond: bool) -> bool:
    print(f"  {GOOD if cond else BAD} {desc}")
    return cond


def _dump_error(e: L3Error) -> None:
    print(
        f"    {DIM}code={e.error_code} severity={e.severity} recoverable={e.recoverable}{RST}"
    )
    print(f"    {DIM}human = {e.human_message!r}{RST}")
    print(f"    {DIM}zh    = {e.suggested_action_zh!r}{RST}")
    print(f"    {DIM}en    = {e.suggested_action!r}{RST}")


def scenario_1_not_homed(backend: GantryBackend) -> bool:
    """没归零就 move → MachineNotHomedError（warning + 🏠 立即归零按钮）"""
    print("\n── 场景 1：没归零就 move → MachineNotHomedError ──")
    backend._is_homed = False  # 强制未归零状态（绕过真归零）
    ok = True
    try:
        backend.start_move_async(Position(x_mm=-50.0, y_mm=-50.0, z_mm=-5.0))
        ok &= _check("抛出异常", False)
    except MachineNotHomedError as e:
        ok &= _check("MachineNotHomedError 抛出", True)
        _dump_error(e)
        ok &= _check("error_code == L3.MACHINE_NOT_HOMED", e.error_code == "L3.MACHINE_NOT_HOMED")
        ok &= _check("severity == warning", e.severity == "warning")
        ok &= _check("recoverable == True", e.recoverable is True)
        ok &= _check("human_message 非空中文", "归零" in e.human_message)
        ok &= _check("suggested_action_zh 非空中文", "归零" in e.suggested_action_zh)
        spec = get_action(e.error_code)
        ok &= _check("error_actions 注册了按钮", spec is not None)
        if spec is not None:
            ok &= _check(f"按钮 label = {spec.label!r}", "归零" in spec.label)
    except Exception as e:
        ok &= _check(f"抛出 MachineNotHomedError（实际 {type(e).__name__}）", False)
        traceback.print_exc()
    return ok


def scenario_2_soft_limit(backend: GantryBackend) -> bool:
    """输入超限 X=-500 → SoftLimitExceededError（warning + 无按钮）"""
    print("\n── 场景 2：坐标超限 (X=-500) → SoftLimitExceededError ──")
    backend._is_homed = True  # 让 is_homed 检查通过，暴露 soft-limit 检查
    ok = True
    try:
        # Position 的 pydantic 校验 在 types.py 里**没**加 ge/le（故意保留 grbl 原始范围，
        # 软限位由 backend.SoftLimits.assert_contains 负责）
        target = Position(x_mm=-500.0, y_mm=0.0, z_mm=0.0)
        backend.start_move_async(target)
        ok &= _check("抛出异常", False)
    except SoftLimitExceededError as e:
        ok &= _check("SoftLimitExceededError 抛出", True)
        _dump_error(e)
        ok &= _check("error_code == L3.SOFT_LIMIT_EXCEEDED", e.error_code == "L3.SOFT_LIMIT_EXCEEDED")
        ok &= _check("severity == warning", e.severity == "warning")
        ok &= _check("recoverable == False (用户改输入即可)", e.recoverable is False)
        ok &= _check("suggested_action_zh 提到 '工作空间'", "工作空间" in e.suggested_action_zh)
        spec = get_action(e.error_code)
        ok &= _check("error_actions **不**注册按钮（按 plan 设计）", spec is None)
    except Exception as e:
        ok &= _check(f"抛出 SoftLimitExceededError（实际 {type(e).__name__}）", False)
        traceback.print_exc()
    return ok


def scenario_3_connection() -> bool:
    """bogus 串口 → ConnectionError（alarm + 🔌 重新连接按钮）"""
    print("\n── 场景 3：串口连接失败 → ConnectionError ──")
    b = GantryBackend(port="/dev/cu.does_not_exist_xyz")
    ok = True
    try:
        b.connect()
        ok &= _check("抛出异常", False)
    except L3ConnectionError as e:
        ok &= _check("ConnectionError 抛出", True)
        _dump_error(e)
        ok &= _check("error_code == L3.CONNECTION", e.error_code == "L3.CONNECTION")
        ok &= _check("severity == alarm", e.severity == "alarm")
        ok &= _check("recoverable == True", e.recoverable is True)
        ok &= _check("suggested_action_zh 提到 'USB'", "USB" in e.suggested_action_zh)
        spec = get_action(e.error_code)
        ok &= _check("error_actions 注册了按钮", spec is not None)
        if spec is not None:
            ok &= _check(f"按钮 label = {spec.label!r}", "连接" in spec.label or "重连" in spec.label)
    except Exception as e:
        ok &= _check(f"抛出 ConnectionError（实际 {type(e).__name__}）", False)
        traceback.print_exc()
    return ok


def scenario_4_operation_conflict(backend: GantryBackend) -> bool:
    """运动中再下指令 → OperationConflictError（warning + 🛑 立即停 按钮）。
    用假线程模拟"上一次 move 还在跑"，不动硬件。"""
    print("\n── 场景 4：运行中再下指令 → OperationConflictError ──")
    backend._is_homed = True
    import threading

    def _sleep():
        time.sleep(1.5)

    fake_move = threading.Thread(target=_sleep, name="FakeMoveWorker", daemon=True)
    fake_move.start()
    backend._move_thread = fake_move  # 让 is_move_in_progress() == True

    ok = True
    try:
        try:
            backend.start_move_async(Position(x_mm=-50.0, y_mm=-50.0, z_mm=-5.0))
            ok &= _check("抛出异常", False)
        except OperationConflictError as e:
            ok &= _check("OperationConflictError 抛出", True)
            _dump_error(e)
            ok &= _check("error_code == L3.OPERATION_CONFLICT", e.error_code == "L3.OPERATION_CONFLICT")
            ok &= _check("severity == warning", e.severity == "warning")
            ok &= _check("suggested_action_zh 提到 '🛑 停' 或 '正忙'", "🛑" in e.suggested_action_zh or "正忙" in e.suggested_action_zh)
            spec = get_action(e.error_code)
            ok &= _check("error_actions 注册了按钮", spec is not None)
            if spec is not None:
                ok &= _check(f"按钮 label = {spec.label!r}", "停" in spec.label)
        except Exception as e:
            ok &= _check(f"抛出 OperationConflictError（实际 {type(e).__name__}）", False)
            traceback.print_exc()
    finally:
        fake_move.join()
        backend._move_thread = None
    return ok


def main() -> int:
    print("Slice 4 错误数据模型自动验收（不动硬件）\n")

    # 连接到真 Arduino 以拿到一个活 backend；后续用 _is_homed 绕过归零
    print("── 连接 backend (DTR reset Arduino) ──")
    backend = GantryBackend(port=PORT)
    try:
        backend.connect()
        print(f"  {GOOD} connect() OK")
    except L3Error as e:
        print(f"  {BAD} connect 失败：{e.human_message}")
        print("  场景 1/2/4 跳过，只跑场景 3（不需要硬件）")
        return 1 if not scenario_3_connection() else 0

    try:
        results = [
            ("场景 1", scenario_1_not_homed(backend)),
            ("场景 2", scenario_2_soft_limit(backend)),
            ("场景 3", scenario_3_connection()),
            ("场景 4", scenario_4_operation_conflict(backend)),
        ]
    finally:
        backend.close()

    print("\n── 总结 ──")
    all_ok = True
    for name, ok in results:
        print(f"  {GOOD if ok else BAD} {name}")
        all_ok &= ok
    print()
    if all_ok:
        print(f"{GOOD} Slice 4 数据模型 ✅（severity + 中文 + action 映射全部对齐）")
        return 0
    else:
        print(f"{BAD} Slice 4 存在不一致，见上方 ✗ 项")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
