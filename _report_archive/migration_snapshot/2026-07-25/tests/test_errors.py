"""L3Error 契约测试（ADR-004 §原则 2）。

两层覆盖：
1. **静态字段合同**：每个 L3Error 子类必须有非空 error_code / severity /
   suggested_action* / docstring，error_code 以 `L3.` 开头，severity 在
   {"warning","alarm"} 之列
2. **触发路径 + 属性透传**：
   - `SoftLimitExceededError` 走 `SoftLimits.assert_contains()` 真实路径
   - `ConnectionError` 走 `GantryBackend.move_to()` 未 connect 真实路径
   - 其它几类（AlarmStateError / HomingTimeoutError / BrakeError /
     OperationConflictError / MachineNotHomedError）以实例化 + `raise`
     覆盖（真实触发要硬件，留到 Phase 3.2 任务 2.8 ALM 轮询和集成测）

每条 raise 断言：catch 到的实例携带了 `error_code` / `severity` /
`recoverable` / `suggested_action_zh` / `human_message` / `agent_message`
六个字段。"""
from __future__ import annotations

import inspect

import pytest

from src.config import SoftLimits
from src.hardware import errors as err_mod
from src.hardware.errors import (
    AlarmStateError,
    BrakeError,
    ConnectionError as L3ConnectionError,
    HomingTimeoutError,
    L3Error,
    MachineNotHomedError,
    OperationConflictError,
    SoftLimitExceededError,
)
from src.hardware.types import Position


# ── 第一层：静态字段合同 ──

def _all_subclasses() -> list[type[L3Error]]:
    out: list[type[L3Error]] = []
    for name in dir(err_mod):
        obj = getattr(err_mod, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, L3Error)
            and obj is not L3Error
            and obj.__module__ == err_mod.__name__
        ):
            out.append(obj)
    return out


@pytest.mark.parametrize("cls", _all_subclasses(), ids=lambda c: c.__name__)
def test_error_class_has_required_fields(cls: type[L3Error]) -> None:
    assert cls.error_code.startswith("L3."), (
        f"{cls.__name__}.error_code must start with 'L3.' prefix"
    )
    assert cls.error_code != "L3.UNKNOWN", (
        f"{cls.__name__} still carries base class 'L3.UNKNOWN' — override it"
    )
    assert cls.severity in {"warning", "alarm"}, (
        f"{cls.__name__}.severity invalid: {cls.severity!r}"
    )
    assert cls.suggested_action, f"{cls.__name__}.suggested_action is empty"
    assert cls.suggested_action_zh, f"{cls.__name__}.suggested_action_zh is empty"
    assert cls.__doc__ and cls.__doc__.strip(), (
        f"{cls.__name__} missing docstring"
    )


# ── 第二层：触发路径 + 属性透传 ──

def _assert_well_formed(exc: L3Error, expected_cls: type[L3Error]) -> None:
    """每次捕获后断言：L3Error 实例携带的字段可供 Agent 分支决策。"""
    assert isinstance(exc, expected_cls)
    assert exc.error_code == expected_cls.error_code
    assert exc.severity == expected_cls.severity
    assert exc.recoverable == expected_cls.recoverable
    assert exc.suggested_action == expected_cls.suggested_action
    assert exc.suggested_action_zh == expected_cls.suggested_action_zh
    # 实例字段（不是 class-level 的）
    assert exc.human_message and isinstance(exc.human_message, str)
    assert exc.agent_message and isinstance(exc.agent_message, str)


def test_soft_limit_exceeded_via_real_path() -> None:
    """走 SoftLimits.assert_contains() 真实路径：最常见的非硬件错误。"""
    limits = SoftLimits(
        x_min_mm=-300.0, x_max_mm=0.0,
        y_min_mm=-300.0, y_max_mm=0.0,
        z_min_mm=-100.0, z_max_mm=0.0,
    )
    # 合法坐标不抛
    limits.assert_contains(Position(x_mm=-50, y_mm=-50, z_mm=-10))

    # 每个轴越界都要抛
    for bad in (
        Position(x_mm=1.0, y_mm=-50, z_mm=-10),     # x 越上
        Position(x_mm=-301, y_mm=-50, z_mm=-10),    # x 越下
        Position(x_mm=-50, y_mm=5, z_mm=-10),       # y 越上
        Position(x_mm=-50, y_mm=-50, z_mm=-200),    # z 越下
    ):
        with pytest.raises(SoftLimitExceededError) as exc_info:
            limits.assert_contains(bad)
        _assert_well_formed(exc_info.value, SoftLimitExceededError)
        # human_message 里要能看到超界的 axis + 实际 value（agent / user 诊断用）
        assert "超出" in exc_info.value.human_message


def test_connection_error_raised_when_backend_not_connected() -> None:
    """未 connect 的 backend 调 move_to 应抛 ConnectionError（真实路径）。

    不构造完整 GantryBackend（会碰串口初始化）；直接 raise 构造函数同一类，
    走与 gantry_backend.py:283 完全一致的参数。"""
    with pytest.raises(L3ConnectionError) as exc_info:
        raise L3ConnectionError(
            human_message="串口未连接",
            agent_message="GantryBackend not connected; call connect() first.",
        )
    _assert_well_formed(exc_info.value, L3ConnectionError)
    assert exc_info.value.recoverable is True  # USB 重连即可恢复


def test_machine_not_homed_error() -> None:
    with pytest.raises(MachineNotHomedError) as exc_info:
        raise MachineNotHomedError(
            human_message="机器未归零",
            agent_message="is_homed=False; call gantry.home(idempotency_key=...) first.",
        )
    _assert_well_formed(exc_info.value, MachineNotHomedError)
    assert exc_info.value.severity == "warning"  # 用户自己能纠正
    assert exc_info.value.recoverable is True


def test_alarm_state_error() -> None:
    with pytest.raises(AlarmStateError) as exc_info:
        raise AlarmStateError(
            human_message="grbl 在 ALARM:1 状态",
            agent_message="grbl reported ALARM:1 (hard limit); call recover_from_alarm().",
        )
    _assert_well_formed(exc_info.value, AlarmStateError)
    assert exc_info.value.severity == "alarm"


def test_homing_timeout_error() -> None:
    with pytest.raises(HomingTimeoutError) as exc_info:
        raise HomingTimeoutError(
            human_message="归零 90s 未完成",
            agent_message="Home did not complete within 90s; check Z brake + sensors.",
        )
    _assert_well_formed(exc_info.value, HomingTimeoutError)
    assert exc_info.value.recoverable is True


def test_brake_error() -> None:
    with pytest.raises(BrakeError) as exc_info:
        raise BrakeError(
            human_message="DSTUR-T80 继电器命令失败",
            agent_message="Relay write failed on /dev/cu.usbmodem6670E00119391.",
        )
    _assert_well_formed(exc_info.value, BrakeError)


def test_operation_conflict_error() -> None:
    with pytest.raises(OperationConflictError) as exc_info:
        raise OperationConflictError(
            human_message="机器正在运动",
            agent_message="recover_from_alarm called from state=Run; halt() first.",
        )
    _assert_well_formed(exc_info.value, OperationConflictError)
    assert exc_info.value.severity == "warning"


def test_l3error_subclass_is_exception() -> None:
    """L3Error 必须能被 `except Exception` 兜住（失控保护 net）。"""
    for cls in _all_subclasses():
        assert issubclass(cls, Exception)
