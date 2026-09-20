from __future__ import annotations

import inspect
import shutil
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from fastapi.testclient import TestClient

from src import actions, workflows
from src.coordinates import CoordinateRegistry
from src.coordinates import DEFAULT_COORDINATES_PATH
from src.experiment_service import ExperimentService
from src.hardware.types import MoveResult, Position
from src.protocol import ExperimentProtocol, load_template, validate_protocol
from src.webapp import DeviceRegistry, create_app


def test_source_perovskite_protocol_validates_and_compiles_in_order() -> None:
    protocol = load_template("perovskite_basic")
    assert validate_protocol(
        protocol,
        max_rpm=6000,
        max_volume_ul=1000,
        max_temperature_c=150,
    ) == []
    plan = workflows.compile_protocol(protocol)
    kinds = [action.kind for action in plan.actions]
    assert kinds[:6] == [
        "Anneal",
        "PickSample",
        "PlaceSample",
        "ChangeTip",
        "Aspirate",
        "Dispense",
    ]
    assert "SpinCoat" in kinds
    spin_index = kinds.index("SpinCoat")
    assert kinds[spin_index + 1] == "EjectTip"
    assert kinds[-5:] == [
        "PlaceSample",
        "Wait",
        "PickSample",
        "PlaceSample",
        "Complete",
    ]


def test_complete_perovskite_dry_run_uses_no_hardware() -> None:
    registry = DeviceRegistry.from_mocks()
    service = ExperimentService(
        registry,
        CoordinateRegistry.from_yaml(),
        sleep=lambda _: None,
    )
    result = service.execute(load_template("perovskite_basic"), dry_run=True)

    assert result.success is True
    assert result.action_count >= 10
    assert all(record.dry_run for record in result.records)


def test_pipette_coordinate_moves_stage_process_then_safe() -> None:
    linear_stage = MagicMock()
    registry = SimpleNamespace(
        gantry=MagicMock(),
        linear_stage=linear_stage,
        pipette=MagicMock(),
    )
    executor = actions.ActionExecutor(
        registry,
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    executor.execute(
        actions.Aspirate(
            liquid="perovskite_precursor",
            volume_ul=100,
            coordinate="stations.reagent_rack.precursor_1",
        )
    )

    assert [
        call.args[0] for call in linear_stage.move_to.call_args_list
    ] == [82.0, 10.0]


def test_tip_pickup_uses_contact_tolerance_only_at_process_position() -> None:
    linear_stage = MagicMock()
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=MagicMock(),
            linear_stage=linear_stage,
            pipette=MagicMock(),
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    contact_move, safe_after = linear_stage.move_to.call_args_list
    assert [contact_move.args[0], safe_after.args[0]] == [75.0, 10.0]
    assert contact_move.kwargs["position_tolerance_mm"] == 3.0
    assert safe_after.kwargs.get("position_tolerance_mm") is None


def test_tip_pickup_blocks_stage_when_gantry_has_not_reached_z() -> None:
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    gantry.move_to.return_value = MoveResult(
        success=True,
        final_position=Position(x_mm=0.0, y_mm=0.0, z_mm=0.0),
        duration_ms=1.0,
        event_id="test",
    )
    linear_stage = MagicMock()
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=linear_stage,
            pipette=MagicMock(),
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
    )

    with pytest.raises(
        actions.ActionExecutionError,
        match="linear stage movement was blocked",
    ):
        executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    linear_stage.move_to.assert_not_called()


def test_spin_coater_placement_opens_vacuum_immediately_after_release() -> None:
    gripper = MagicMock()
    relay = MagicMock()
    relay.is_connected.return_value = True
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(gantry=gantry, gripper=gripper, relay=relay),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
    )

    executor.execute(
        actions.PlaceSample(coordinate="stations.spin_coater.operation")
    )

    gripper.open.assert_called_once()
    relay.force_set.assert_called_once()
    assert relay.force_set.call_args.args[:2] == (3, True)


def test_hotplate_placement_settles_after_release_before_next_action() -> None:
    sleep = MagicMock()
    gripper = MagicMock()
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(gantry=gantry, gripper=gripper),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
    )

    executor.execute(
        actions.PlaceSample(coordinate="stations.hotplate.slot_1.place")
    )

    gripper.open.assert_called_once()
    sleep.assert_called_once_with(1.0)


def test_tip_pickup_waits_for_legacy_mechanical_settle_before_detection() -> None:
    sleep = MagicMock()
    pipette = MagicMock()
    pipette.get_status.return_value = SimpleNamespace(tip_present=False)
    pipette.refresh_status.return_value = SimpleNamespace(tip_present=True)
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=MagicMock(),
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
    )

    result = executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    sleep.assert_called_once_with(0.4)
    pipette.refresh_status.assert_called_once_with()
    assert result == {"tip_change": "verified"}


def test_tip_eject_settles_before_departing_for_next_tip() -> None:
    sleep = MagicMock()
    pipette = MagicMock()
    pipette.get_status.return_value = SimpleNamespace(tip_present=True)
    pipette.refresh_status.return_value = SimpleNamespace(tip_present=True)
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=MagicMock(),
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
    )

    executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    pipette.eject_tip.assert_called_once()
    assert sleep.call_args_list == [call(2.0), call(0.4)]


def test_tip_pickup_rechecks_after_retract_when_press_fit_read_is_false() -> None:
    sleep = MagicMock()
    pipette = MagicMock()
    pipette.get_status.return_value = SimpleNamespace(tip_present=False)
    pipette.refresh_status.side_effect = [
        SimpleNamespace(tip_present=False),
        SimpleNamespace(tip_present=True),
    ]
    linear_stage = MagicMock()
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=linear_stage,
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
    )

    result = executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    assert sleep.call_args_list == [call(0.4), call(0.4)]
    assert pipette.refresh_status.call_count == 2
    assert linear_stage.move_to.call_args_list[-1].args[0] == 10.0
    assert result == {"tip_change": "verified"}


def test_tip_pickup_tolerates_transient_false_after_retraction() -> None:
    sleep = MagicMock()
    pipette = MagicMock()
    pipette.get_status.return_value = SimpleNamespace(tip_present=False)
    pipette.refresh_status.side_effect = [
        SimpleNamespace(tip_present=False),  # compressed at mount position
        SimpleNamespace(tip_present=False),  # first retracted sample glitches
        SimpleNamespace(tip_present=True),
    ]
    linear_stage = MagicMock()
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=linear_stage,
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
    )

    result = executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    assert sleep.call_args_list == [call(0.4), call(0.4), call(0.4)]
    assert pipette.refresh_status.call_count == 3
    assert result == {"tip_change": "verified"}


def test_tip_pickup_rejects_three_absent_reads_after_retraction() -> None:
    pipette = MagicMock()
    pipette.get_status.return_value = SimpleNamespace(tip_present=False)
    pipette.refresh_status.return_value = SimpleNamespace(tip_present=False)
    gantry = MagicMock()
    gantry.get_position.return_value = Position(x_mm=0.0, y_mm=0.0, z_mm=0.0)
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=MagicMock(),
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=lambda _: None,
    )

    with pytest.raises(
        actions.ActionExecutionError,
        match="after 3 retraction checks",
    ):
        executor.execute(actions.ChangeTip(coordinate="stations.tip_rack.pickup"))

    assert pipette.refresh_status.call_count == 4


def test_spin_controls_vacuum_relay_channel_three() -> None:
    relay = MagicMock()
    spincoater = MagicMock()
    executor = actions.ActionExecutor(
        SimpleNamespace(relay=relay, spincoater=spincoater),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    executor.execute(
        actions.SpinCoatAction(
            segments=[actions.SpinSegment(rpm=1000, duration_s=1)]
        )
    )

    relay.force_set.assert_any_call(
        3,
        True,
        idempotency_key=relay.force_set.call_args_list[0].kwargs[
            "idempotency_key"
        ],
        dry_run=True,
    )
    spincoater.start.assert_called_once()
    spincoater.stop.assert_called_once()
    assert relay.force_set.call_args_list[-1].args[:2] == (3, False)


def test_timed_antisolvent_starts_spin_before_pipette_positioning() -> None:
    order: list[str] = []
    relay = MagicMock()
    spincoater = MagicMock()
    pipette = MagicMock()
    spincoater.start.side_effect = lambda *args, **kwargs: order.append("spin_start")
    pipette.dispense.side_effect = lambda *args, **kwargs: order.append("dispense")
    executor = actions.ActionExecutor(
        SimpleNamespace(
            relay=relay,
            spincoater=spincoater,
            pipette=pipette,
            linear_stage=MagicMock(),
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )
    executor._move = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *args, **kwargs: order.append("pipette_move")
    )

    executor.execute(
        actions.SpinCoatAction(
            segments=[actions.SpinSegment(rpm=1500, duration_s=10)],
            timed_dispenses=[
                actions.TimedDispense(
                    at_s=5,
                    liquid="antisolvent",
                    volume_ul=50,
                )
            ],
        )
    )

    assert order == ["spin_start", "pipette_move", "dispense"]


def test_spin_clock_starts_before_blocking_motor_start_and_pipette_travel() -> None:
    class ManualClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = ManualClock()
    waits: list[float] = []
    relay = MagicMock()
    relay.is_connected.return_value = True
    spincoater = MagicMock()
    pipette = MagicMock()

    def blocking_start(*args, **kwargs) -> None:
        del args, kwargs
        clock.now += 4.0

    def pipette_travel(*args, **kwargs) -> None:
        del args, kwargs
        clock.now += 3.0

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.now += seconds

    spincoater.start.side_effect = blocking_start
    executor = actions.ActionExecutor(
        SimpleNamespace(
            relay=relay,
            spincoater=spincoater,
            pipette=pipette,
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
        clock=clock,
    )
    executor._move = MagicMock(side_effect=pipette_travel)  # type: ignore[method-assign]
    executor._retract_linear_stage_for = MagicMock()  # type: ignore[method-assign]

    executor.execute(
        actions.SpinCoatAction(
            segments=[actions.SpinSegment(rpm=1500, duration_s=12)],
            timed_dispenses=[
                actions.TimedDispense(
                    at_s=10,
                    liquid="antisolvent",
                    volume_ul=50,
                )
            ],
        )
    )

    # Four seconds in start() plus three seconds of pipette travel have already
    # consumed seven seconds of the ten-second antisolvent deadline.
    assert waits == pytest.approx([3.0, 2.0])
    pipette.dispense.assert_called_once()


def test_spin_failure_still_stops_motor_and_closes_vacuum() -> None:
    relay = MagicMock()
    spincoater = MagicMock()
    spincoater.start.side_effect = RuntimeError("simulated spin failure")
    executor = actions.ActionExecutor(
        SimpleNamespace(relay=relay, spincoater=spincoater),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    try:
        executor._spin(
            actions.SpinCoatAction(
                segments=[actions.SpinSegment(rpm=1000, duration_s=1)]
            )
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("simulated spin failure was not propagated")

    spincoater.stop.assert_called_once()
    assert relay.force_set.call_args_list[-1].args[:2] == (3, False)


def test_automation_moves_xy_before_z_and_slows_near_boundary() -> None:
    gantry = MagicMock()
    gantry.get_position.return_value = Position(
        x_mm=-100.0,
        y_mm=-100.0,
        z_mm=-30.0,
    )
    executor = actions.ActionExecutor(
        SimpleNamespace(
            gantry=gantry,
            linear_stage=MagicMock(),
            pipette=MagicMock(),
        ),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    executor.execute(
        actions.Aspirate(
            liquid="perovskite_precursor",
            volume_ul=100,
            coordinate="stations.reagent_rack.precursor_1",
        )
    )

    first, second = gantry.move_to.call_args_list
    assert first.args[0] == Position(x_mm=-186.0, y_mm=-15.0, z_mm=-30.0)
    assert first.kwargs["feed_mm_min"] == 1000
    assert second.args[0] == Position(x_mm=-186.0, y_mm=-15.0, z_mm=-80.0)
    assert second.kwargs["feed_mm_min"] == 1000


def test_automation_lifts_z_and_leaves_boundary_at_reduced_feed() -> None:
    gantry = MagicMock()
    gantry.get_position.return_value = Position(
        x_mm=-295.0,
        y_mm=-5.0,
        z_mm=-99.5,
    )
    executor = actions.ActionExecutor(
        SimpleNamespace(gantry=gantry, gripper=MagicMock()),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
    )

    executor.execute(
        actions.PickSample(coordinate="stations.substrate_rack.pickup")
    )

    lift, boundary_escape, xy_move, z_move = gantry.move_to.call_args_list
    assert lift.args[0] == Position(x_mm=-295.0, y_mm=-5.0, z_mm=-30.0)
    assert lift.kwargs["feed_mm_min"] == 1000
    assert boundary_escape.args[0] == Position(
        x_mm=-295.0, y_mm=-15.0, z_mm=-30.0
    )
    assert boundary_escape.kwargs["feed_mm_min"] == 1000
    assert xy_move.args[0] == Position(x_mm=-148.0, y_mm=-172.5, z_mm=-30.0)
    assert xy_move.kwargs["feed_mm_min"] == 1000
    assert z_move.args[0] == Position(x_mm=-148.0, y_mm=-172.5, z_mm=-74.5)
    assert z_move.kwargs["feed_mm_min"] == 1000


def test_production_execution_accepts_explicitly_executable_taught_coordinates() -> None:
    service = ExperimentService(
        DeviceRegistry.from_mocks(),
        CoordinateRegistry.from_yaml(),
    )
    _, safety = service.prepare(load_template("perovskite_basic"), dry_run=False)
    assert safety.ready is True
    assert not any(issue.code == "COORDINATE" for issue in safety.issues)


def test_hardware_capability_accepts_5000_and_rejects_above_6000() -> None:
    service = ExperimentService(
        DeviceRegistry.from_mocks(),
        CoordinateRegistry.from_yaml(),
    )
    _, source_safety = service.prepare(
        load_template("perovskite_basic"), dry_run=False
    )
    assert not any(issue.code == "PARAMETER" for issue in source_safety.issues)

    too_fast = ExperimentProtocol.model_validate(
        {
            "sample_id": "too-fast",
            "operations": [
                {
                    "name": "SpinCoat",
                    "steps": [
                        {
                            "speed": {"quantity": 6000.1, "unit": "rpm"},
                            "duration": {"quantity": 1, "unit": "s"},
                        }
                    ],
                }
            ],
        }
    )
    _, rejected = service.prepare(too_fast, dry_run=False)
    assert any(issue.code == "PARAMETER" for issue in rejected.issues)


def test_action_failure_triggers_emergency_stop() -> None:
    registry = DeviceRegistry.from_mocks()
    registry.estop = MagicMock()
    protocol = ExperimentProtocol.model_validate(
        {
            "sample_id": "failure",
            "operations": [
                {
                    "name": "Wait",
                    "duration": {"quantity": 1, "unit": "s"},
                }
            ],
        }
    )
    service = ExperimentService(
        registry,
        CoordinateRegistry.from_yaml(),
        sleep=MagicMock(side_effect=RuntimeError("simulated failure")),
    )
    result = service.execute(protocol, dry_run=False)

    assert result.success is False
    registry.estop.halt_all.assert_called_once_with()


def test_workflow_and_experiment_web_route_cannot_import_hardware_drivers() -> None:
    assert "hardware.drivers" not in inspect.getsource(workflows)
    assert "serial" not in inspect.getsource(actions)
    from src.webapp import routes_experiments

    route_source = inspect.getsource(routes_experiments)
    assert "from ..hardware" not in route_source
    assert "DeviceRegistry" in route_source


def test_web_experiment_preview_and_dry_run(tmp_path) -> None:
    coordinates_path = tmp_path / "coordinates.yaml"
    shutil.copyfile(DEFAULT_COORDINATES_PATH, coordinates_path)
    app = create_app(
        DeviceRegistry.from_mocks(),
        token="test",
        routines_path=tmp_path / "routines",
        coordinates_path=coordinates_path,
    )
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        preview = client.post(
            "/api/experiments/preview",
            headers=headers,
            json={"template": "perovskite_basic", "dry_run": True},
        )
        assert preview.status_code == 200
        assert preview.json()["safety"]["ready"] is True

        safety = client.get("/api/safety/startup", headers=headers)
        assert safety.status_code == 200
        assert safety.json()["hardware_limits"]["spincoater_max_rpm"] == 6000
        assert safety.json()["runtime_validation_required"] is True

        offsets = client.get("/api/tool-offsets", headers=headers)
        assert offsets.status_code == 200
        assert offsets.json() == []

        accepted = client.post(
            "/api/experiments/execute",
            headers=headers,
            json={"template": "perovskite_basic", "dry_run": True},
        )
        assert accepted.status_code == 202
        operation_id = accepted.json()["operation_id"]
        for _ in range(100):
            operation = client.get(
                f"/api/operations/{operation_id}", headers=headers
            )
            if operation.status_code == 200 and operation.json()["status"] != "running":
                break
            time.sleep(0.01)
        assert operation.json()["status"] == "succeeded"
        assert operation.json()["result"]["success"] is True
