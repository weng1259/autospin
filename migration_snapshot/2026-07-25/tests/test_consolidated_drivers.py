"""Tests for autospin-owned verified hardware driver imports and configuration."""

from __future__ import annotations

from pathlib import Path

from src.config import load_config
from src.hardware.autospinmotor_adapters import (
    HeaterControllerAdapter,
    LinearStageControllerAdapter,
    PipetteControllerAdapter,
    SpinMotorControllerAdapter,
)
from src.hardware.drivers.heater import HeatingStageController
from src.hardware.drivers.linear_stage import EmmLinearStage
from src.hardware.drivers.pipette import PipetteController
from src.hardware.drivers.spin_motor import MotorController


def test_default_config_loads_split_hardware_yaml() -> None:
    config = load_config()

    assert config.hardware is not None
    assert config.hardware.spincoater.slave_id == 2
    assert config.hardware.heater.slave_id == 3
    assert config.hardware.pipette.slave_id == 1
    assert config.hardware.linear_stage.address == 4


def test_consolidated_drivers_do_not_import_legacy_package() -> None:
    driver_root = Path(__file__).parents[1] / "src" / "hardware" / "drivers"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in driver_root.rglob("*.py")
    )

    assert "AutoSpinmotorSystem.config" not in source
    assert "autospin_system" not in source


def test_spin_factory_injects_verified_parameters() -> None:
    adapter = SpinMotorControllerAdapter.from_verified_controller(
        port="/dev/test",
        mock=True,
        slave_id=2,
        baudrate=9600,
        timeout_s=2.0,
        max_rpm=3000,
        pole_pairs=4,
    )

    controller = adapter._controller
    assert isinstance(controller, MotorController)
    assert controller.comm.slave_id == 2
    assert controller.comm.baudrate == 9600
    assert controller.comm.timeout == 2.0
    assert controller.pole_pairs == 4
    assert controller.max_speed_rpm == 3000
    assert controller._get_control_word(run=True) == 0x0409


def test_heater_factory_injects_ai516_parameters() -> None:
    adapter = HeaterControllerAdapter.from_verified_controller(
        port="/dev/test",
        mock=True,
        slave_id=3,
        baudrate=9600,
        timeout_s=3.0,
        pv_register=74,
        sv_register=0,
        srun_register=27,
        run_on_sv_write=True,
        scale=10.0,
    )

    controller = adapter._controller
    assert isinstance(controller, HeatingStageController)
    assert controller.slave_id == 3
    assert controller.baudrate == 9600
    assert controller.pv_addr == 74
    assert controller.sv_addr == 0
    assert controller.srun_addr == 27
    assert controller.scale == 10.0


def test_pipette_factory_injects_verified_parameters() -> None:
    adapter = PipetteControllerAdapter.from_verified_controller(
        port="/dev/test",
        mock=True,
        slave_id=1,
        baudrate=115200,
        timeout_s=2.0,
        max_volume_ul=1000,
    )

    controller = adapter._controller
    assert isinstance(controller, PipetteController)
    assert controller.comm.slave_id == 1
    assert controller.comm.baudrate == 115200
    assert controller.comm.timeout == 2.0
    assert controller.max_volume == 1000


def test_linear_stage_factory_uses_consolidated_driver() -> None:
    adapter = LinearStageControllerAdapter.from_verified_controller(
        port="/dev/test",
        mock=True,
        address=4,
        baudrate=115200,
        min_position_mm=0.0,
        max_position_mm=100.0,
    )

    controller = adapter._controller
    assert isinstance(controller, EmmLinearStage)
    assert controller.address == 4
    assert controller.baudrate == 115200
    assert controller.min_position_mm == 0.0
    assert controller.max_position_mm == 100.0
