"""Hardware-driver tests with all GRBL communication kept in memory."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config import L3Config, MotionConfig, SoftLimits
from src.hardware.gantry_backend import GantryBackend
from src.hardware.drivers.gantry import GrblController
from src.hardware.errors import AlarmStateError, SoftLimitExceededError
from src.hardware.types import HomePlan, MachineState, MachineStatus, Position


@pytest.fixture
def controller() -> GrblController:
    config = L3Config(
        soft_limits=SoftLimits(
            x_min_mm=-310.0,
            x_max_mm=-5.0,
            y_min_mm=-310.0,
            y_max_mm=-5.0,
            z_min_mm=-110.0,
            z_max_mm=-5.0,
        ),
        motion=MotionConfig(
            default_feed_mm_min=2000.0,
            max_feed_mm_min=8000.0,
            move_timeout_s=60.0,
            status_poll_interval_ms=200,
        ),
    )
    relay = MagicMock()
    relay.is_connected.return_value = True
    driver = GrblController(
        port="/dev/gantry",
        baud=115200,
        relay=relay,
        config=config,
    )
    driver._ser = MagicMock()
    driver._ser.is_open = True
    driver._is_homed = True
    driver._status = MachineStatus(
        state=MachineState.IDLE,
        position=Position(x_mm=-5.0, y_mm=-5.0, z_mm=-30.0),
    )
    driver._send_line_blocking = MagicMock()
    driver._poll_status_sync = MagicMock(return_value=True)
    driver._wait_idle = MagicMock()
    driver._release_brake = MagicMock()
    driver._lock_brake = MagicMock()
    return driver


def test_driver_generates_complete_absolute_xyz_jog(
    controller: GrblController,
) -> None:
    controller.move_to(
        -153.25,
        -237.5,
        -74.125,
        2500.0,
        wait_for_idle=False,
    )

    controller._send_line_blocking.assert_called_once()
    assert controller._send_line_blocking.call_args.args[0] == (
        "$J=G90 X-153.250 Y-237.500 Z-74.125 F2500\n"
    )


def test_driver_home_api_supports_hardware_level_call_without_key(
    controller: GrblController,
) -> None:
    plan = controller.home(dry_run=True)

    assert isinstance(plan, HomePlan)
    controller._send_line_blocking.assert_not_called()


@pytest.mark.parametrize(
    "target",
    [
        (-310.02, -100.0, -50.0),
        (-4.98, -100.0, -50.0),
        (-100.0, -310.02, -50.0),
        (-100.0, -4.98, -50.0),
        (-100.0, -100.0, -110.02),
        (-100.0, -100.0, -4.98),
    ],
)
def test_driver_rejects_out_of_range_target_before_serial_write(
    controller: GrblController,
    target: tuple[float, float, float],
) -> None:
    with pytest.raises(SoftLimitExceededError):
        controller.move_to(*target, 1000.0)

    controller._send_line_blocking.assert_not_called()
    controller._release_brake.assert_not_called()


def test_driver_accepts_confirmed_soft_limit_boundaries(
    controller: GrblController,
) -> None:
    controller.move_to(-310.0, -310.0, -110.0, 1000.0, wait_for_idle=False)
    controller.move_to(-5.0, -5.0, -5.0, 1000.0, wait_for_idle=False)

    commands = [
        call.args[0] for call in controller._send_line_blocking.call_args_list
    ]
    assert commands == [
        "$J=G90 X-310.000 Y-310.000 Z-110.000 F1000\n",
        "$J=G90 X-5.000 Y-5.000 Z-5.000 F1000\n",
    ]


def test_driver_maps_grbl_alarm_and_updates_cached_state(
    controller: GrblController,
) -> None:
    error = controller._make_alarm_error(
        "ALARM:1",
        "$J=G90 X-100.000 Y-100.000 Z-50.000 F1000",
    )

    assert isinstance(error, AlarmStateError)
    assert controller._alarm_code == 1
    assert controller._status.state == MachineState.ALARM
    assert controller._status.alarm_code == 1


def test_driver_unlock_sends_dollar_x(controller: GrblController) -> None:
    controller.unlock()

    controller._send_line_blocking.assert_called_once_with(
        "$X\n",
        timeout_s=3.0,
        timeout_msg="$X 命令 3s 内未返回 ok",
    )


def test_driver_emergency_stop_sends_ctrl_x_and_clears_homing(
    controller: GrblController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.hardware.drivers.gantry.grbl_controller.time.sleep",
        lambda _: None,
    )

    controller.emergency_stop()

    controller._ser.write.assert_called_once_with(b"\x18")
    assert controller._is_homed is False
    controller._lock_brake.assert_called_once_with()


def test_driver_disconnect_closes_serial_and_relay(
    controller: GrblController,
) -> None:
    relay = controller._relay

    controller.disconnect()

    assert controller._ser is None
    relay.close.assert_called_once_with()


def test_backend_delegates_motion_to_injected_driver() -> None:
    driver = MagicMock(spec=GrblController)
    expected = MagicMock()
    driver.move_to.return_value = expected
    backend = GantryBackend(driver=driver)
    target = Position(x_mm=-100.0, y_mm=-120.0, z_mm=-50.0)

    result = backend.move_to(target, feed_mm_min=1500.0, wait_for_idle=False)

    assert result is expected
    driver.move_to.assert_called_once_with(
        target,
        feed_mm_min=1500.0,
        wait_for_idle=False,
        timeout_s=None,
        dry_run=False,
    )
