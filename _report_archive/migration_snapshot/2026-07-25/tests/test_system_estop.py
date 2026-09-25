"""SystemEstop（W2）：顺序、故障隔离、缺席跳过、信号接线。全部用 fake。"""
from __future__ import annotations

import signal
from typing import Any, cast

import pytest

import src.system_estop as estop_module
from src.hardware.errors import L3Error
from src.hardware.gantry_backend import GantryBackend
from src.hardware.gripper_backend import GripperBackend
from src.hardware.heater_backend import HeaterBackend
from src.hardware.linearstage_backend import LinearStageBackend
from src.hardware.pipette_backend import PipetteBackend
from src.hardware.spincoater_backend import SpincoaterBackend
from src.system_estop import EstopReport, SystemEstop


class CallLog:
    def __init__(self) -> None:
        self.calls: list[str] = []


class FakeGantry:
    def __init__(self, log: CallLog, *, fail: bool = False) -> None:
        self._log = log
        self._fail = fail

    def abort_motion_immediate(self) -> None:
        self._log.calls.append("gantry.abort")
        if self._fail:
            raise L3Error(
                human_message="龙门串口失联",
                agent_message="gantry serial lost",
            )


class FakeSpincoater:
    def __init__(self, log: CallLog) -> None:
        self._log = log
        self.kwargs: dict[str, Any] = {}

    def stop(self, *, use_brake: bool = True) -> None:
        self.kwargs = {"use_brake": use_brake}
        self._log.calls.append("spincoater.stop")


class FakeGripper:
    def __init__(self, log: CallLog) -> None:
        self._log = log

    def emergency_release(self) -> None:
        self._log.calls.append("gripper.release")


class FakeLinearStage:
    def __init__(self, log: CallLog) -> None:
        self._log = log

    def stop(self) -> None:
        self._log.calls.append("linear_stage.stop")


class FakePipette:
    def __init__(self, log: CallLog) -> None:
        self._log = log

    def stop(self) -> None:
        self._log.calls.append("pipette.stop")


class FakeHeater:
    def __init__(self, log: CallLog) -> None:
        self._log = log
        self.sv_writes: list[float] = []

    def set_sv(self, sv_c: float) -> None:
        self.sv_writes.append(sv_c)
        self._log.calls.append("heater.set_sv")


def _full_estop(log: CallLog, *, gantry_fail: bool = False) -> tuple[
    SystemEstop, FakeSpincoater, FakeHeater
]:
    spin = FakeSpincoater(log)
    heater = FakeHeater(log)
    estop = SystemEstop(
        gantry=cast(GantryBackend, FakeGantry(log, fail=gantry_fail)),
        gripper=cast(GripperBackend, FakeGripper(log)),
        spincoater=cast(SpincoaterBackend, spin),
        linear_stage=cast(LinearStageBackend, FakeLinearStage(log)),
        pipette=cast(PipetteBackend, FakePipette(log)),
        heater=cast(HeaterBackend, heater),
    )
    return estop, spin, heater


def test_halt_all_runs_motion_first_heater_last_in_fixed_order() -> None:
    log = CallLog()
    estop, spin, heater = _full_estop(log)

    report = estop.halt_all()

    assert log.calls == [
        "gantry.abort",
        "gripper.release",
        "spincoater.stop",
        "linear_stage.stop",
        "pipette.stop",
        "heater.set_sv",
    ]
    assert spin.kwargs == {"use_brake": True}
    assert heater.sv_writes == [0.0]
    assert isinstance(report, EstopReport)
    assert report.ok is True
    assert [s.device for s in report.steps] == [
        "gantry", "gripper", "spincoater", "linear_stage", "pipette", "heater",
    ]
    assert all(s.ok and not s.skipped for s in report.steps)


def test_one_failing_device_does_not_block_the_rest() -> None:
    log = CallLog()
    estop, _spin, heater = _full_estop(log, gantry_fail=True)

    report = estop.halt_all()

    # 龙门抛错后，其余四台照停。
    assert log.calls == [
        "gantry.abort",
        "gripper.release",
        "spincoater.stop",
        "linear_stage.stop",
        "pipette.stop",
        "heater.set_sv",
    ]
    assert heater.sv_writes == [0.0]
    assert report.ok is False
    gantry_step = report.steps[0]
    assert gantry_step.ok is False
    assert gantry_step.error is not None and "龙门串口失联" in gantry_step.error
    assert all(s.ok for s in report.steps[1:])


def test_missing_devices_are_reported_as_skipped() -> None:
    log = CallLog()
    estop = SystemEstop(pipette=cast(PipetteBackend, FakePipette(log)))

    report = estop.halt_all(stop_heater=False)

    assert log.calls == ["pipette.stop"]
    assert report.ok is True
    by_device = {s.device: s for s in report.steps}
    assert by_device["pipette"].skipped is False
    for device in ("gantry", "gripper", "spincoater", "linear_stage", "heater"):
        assert by_device[device].skipped is True


def test_install_signal_handlers_routes_sigint_to_halt_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = CallLog()
    estop, _spin, _heater = _full_estop(log)
    installed: dict[int, Any] = {}
    raised: list[int] = []

    def fake_signal(signum: int, handler: Any) -> None:
        installed[signum] = handler

    monkeypatch.setattr(estop_module.signal, "signal", fake_signal)
    monkeypatch.setattr(
        estop_module.signal, "raise_signal", lambda s: raised.append(s)
    )
    estop.install_signal_handlers()

    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    installed[signal.SIGINT](signal.SIGINT, None)

    # handler = 急停 → 恢复默认 handler → 重投递信号
    assert log.calls[-1] == "heater.set_sv"
    assert installed[signal.SIGINT] is signal.SIG_DFL
    assert raised == [signal.SIGINT]
