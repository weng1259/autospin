from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import threading

from src.actions import (
    ActionExecutionError,
    ActionExecutor,
    AnnealTimerStart,
    Aspirate,
    ChangeTip,
    PrepareHeater,
    SampleStateUpdate,
    WaitForHeaterStable,
    WaitUntilElapsed,
    WaitUntilSpinElapsed,
)
from src.coordinates import CoordinateRegistry
from src.multi_round import HotplateResourceManager, compile_multi_round
from src.protocol import create_perovskite_protocol
from src.workflows import compile_protocol


def _protocol(index: int, dwell_s: float = 120):
    return create_perovskite_protocol(
        sample_id=f"sample-{index}",
        annealing_time=dwell_s,
        precursor_tip_slot=index * 2 - 1,
        antisolvent_tip_slot=index * 2,
    )


def test_single_round_has_complete_hotplate_place_pick_and_return() -> None:
    actions = compile_protocol(_protocol(1), round_index=1).actions
    kinds = [action.kind for action in actions]
    wait_index = kinds.index("Wait")

    assert kinds[wait_index - 2 : wait_index + 4] == [
        "PickSample",
        "PlaceSample",
        "Wait",
        "PickSample",
        "PlaceSample",
        "Complete",
    ]
    assert actions[wait_index - 1].coordinate == "stations.hotplate.slot_1.place"
    assert actions[wait_index + 1].coordinate == "stations.hotplate.slot_1.place"
    assert actions[wait_index + 2].coordinate == "stations.substrate_rack.slot_1"


def test_multi_round_prepositions_above_hotplate_then_waits_absolute() -> None:
    plan = compile_multi_round([_protocol(1, 10), _protocol(2, 10)])
    waits = [
        (index, action)
        for index, action in enumerate(plan.actions)
        if action.kind == "WaitUntilElapsed"
    ]

    assert len(waits) == 2
    for index, wait in waits:
        previous = plan.actions[index - 1]
        assert previous.kind == "MoveTool"
        assert previous.coordinate.endswith(".safe_above")
        assert wait.phase == "hotplate_dwell_before_return"
    assert not any(action.kind == "Wait" for action in plan.actions)


def test_multi_round_starts_near_target_but_gates_before_spin_pickup() -> None:
    plan = compile_multi_round([_protocol(1, 10)])
    prepare = next(action for action in plan.actions if isinstance(action, PrepareHeater))
    gate_index = next(
        index
        for index, action in enumerate(plan.actions)
        if isinstance(action, WaitForHeaterStable)
    )

    assert prepare.temperature_c == pytest.approx(120.0)
    assert prepare.start_margin_c == pytest.approx(10.0)
    assert plan.actions[gate_index + 1].kind == "PickSample"
    assert plan.actions[gate_index + 1].coordinate == "stations.spin_coater.operation"
    assert plan.actions[gate_index + 2].kind == "PlaceSample"
    assert plan.actions[gate_index + 2].coordinate == "stations.hotplate.slot_1.place"
    assert plan.actions[gate_index - 1].kind == "MoveTool"
    assert plan.actions[gate_index - 1].coordinate == "stations.spin_coater.safe_above"


def test_executor_uses_one_sided_start_threshold_and_strict_placement_gate() -> None:
    heater = MagicMock()
    heater.wait_until_at_least.return_value = SimpleNamespace(
        success=True, pv_c=110.0, timed_out=False
    )
    heater.wait_until_stable.return_value = SimpleNamespace(
        success=True, pv_c=120.0, timed_out=False
    )
    executor = ActionExecutor(
        SimpleNamespace(heater=heater),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
    )

    executor.execute(PrepareHeater(temperature_c=120.0, start_margin_c=10.0))
    executor.execute(WaitForHeaterStable(temperature_c=120.0, tolerance_c=1.0))

    heater.set_sv.assert_called_once()
    assert heater.wait_until_at_least.call_args.kwargs["minimum_c"] == pytest.approx(110.0)
    assert heater.wait_until_at_least.call_args.kwargs["target_c"] == pytest.approx(120.0)
    assert heater.wait_until_stable.call_args.kwargs["target_c"] == pytest.approx(120.0)
    assert heater.wait_until_stable.call_args.kwargs["tolerance_c"] == pytest.approx(1.0)


def test_hotplate_return_never_splits_liquid_preparation_chain() -> None:
    plan = compile_multi_round([_protocol(i, 1) for i in range(1, 5)])
    kinds = [action.kind for action in plan.actions]
    for index, kind in enumerate(kinds):
        if kind != "WaitUntilElapsed":
            continue
        previous_change = max(
            (i for i in range(index) if kinds[i] == "ChangeTip"), default=-1
        )
        next_dispense = next(
            (i for i in range(index, len(kinds)) if kinds[i] == "Dispense"), None
        )
        assert not (
            previous_change >= 0
            and next_dispense is not None
            and all(
                item in {"ChangeTip", "Aspirate", "Dispense", "MoveTool", "WaitUntilElapsed"}
                for item in kinds[previous_change : next_dispense + 1]
            )
        )
    for index, kind in enumerate(kinds):
        if kind == "SpinCoat":
            previous_aspirate = max(
                i for i in range(index) if kinds[i] == "Aspirate"
            )
            assert "WaitUntilElapsed" not in kinds[previous_aspirate:index]


def test_hotplate_slot_is_freed_before_tenth_round_reuses_it() -> None:
    plan = compile_multi_round([_protocol(i, 10_000) for i in range(1, 11)])
    round_one_wait = next(
        index
        for index, action in enumerate(plan.actions)
        if action.kind == "WaitUntilElapsed" and action.round_index == 1
    )
    round_ten_pick = next(
        index
        for index, action in enumerate(plan.actions)
        if action.kind == "PickSample"
        and action.coordinate == "stations.substrate_rack.slot_10"
    )
    assert round_one_wait < round_ten_pick
    assert plan.rounds[0].hotplate_slot == plan.rounds[9].hotplate_slot == 1


def test_generated_substrate_and_hotplate_slots_are_resolvable() -> None:
    coordinates = CoordinateRegistry.from_yaml()
    substrate = coordinates.resolve("stations.substrate_rack.slot_16", production=False)
    hotplate = coordinates.resolve("stations.hotplate.slot_9.safe_above", production=False)

    assert substrate.xyz == pytest.approx((-286.99, -245.49, -73.6))
    assert hotplate.xyz == pytest.approx((-245.0, -45.0, -30.0))


def test_sixteen_rounds_use_assigned_tip_pairs_and_four_precursor_sources() -> None:
    coordinates = CoordinateRegistry.from_yaml()
    plans = [compile_protocol(_protocol(index), round_index=index) for index in range(1, 17)]

    tip_coordinates = [
        action.coordinate
        for plan in plans
        for action in plan.actions
        if isinstance(action, ChangeTip)
    ]
    precursor_coordinates = [
        action.coordinate
        for plan in plans
        for action in plan.actions
        if isinstance(action, Aspirate) and action.liquid == "perovskite_precursor"
    ]

    assert tip_coordinates == [
        item
        for index in range(1, 17)
        for item in (
            f"stations.tip_rack.slot_{index * 2 - 1}",
            f"stations.tip_rack.slot_{index * 2}",
        )
    ]
    assert precursor_coordinates == [
        f"stations.reagent_rack.precursor_{((index - 1) // 4) + 1}"
        for index in range(1, 17)
    ]
    for name in tip_coordinates + precursor_coordinates:
        coordinates.resolve(name, production=False)


def test_no_antisolvent_rounds_use_one_fresh_tip_each() -> None:
    plans = [
        compile_protocol(
            create_perovskite_protocol(
                sample_id=f"no-antisolvent-{index}",
                use_antisolvent=False,
                precursor_tip_slot=index,
                antisolvent_tip_slot=None,
            ),
            round_index=index,
        )
        for index in range(1, 3)
    ]

    assert [
        action.coordinate
        for plan in plans
        for action in plan.actions
        if isinstance(action, ChangeTip)
    ] == ["stations.tip_rack.slot_1", "stations.tip_rack.slot_2"]


def test_absolute_wait_uses_batch_epoch_and_reports_lateness() -> None:
    clock_values = iter([100.0, 107.0])
    sleep = MagicMock()
    executor = ActionExecutor(
        SimpleNamespace(),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
        clock=lambda: next(clock_values),
    )

    result = executor.execute(WaitUntilElapsed(elapsed_s=5, round_index=1))

    sleep.assert_not_called()
    assert result["late_by_s"] == pytest.approx(2.0)


def test_annealing_overlap_uses_actual_placement_time_as_deadline() -> None:
    clock_values = iter([100.0, 104.0, 109.0])
    sleep = MagicMock()
    executor = ActionExecutor(
        SimpleNamespace(),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
        clock=lambda: next(clock_values),
    )

    executor.execute(
        AnnealTimerStart(
            round_index=1,
            sample_id="sample-1",
            hotplate_slot=1,
            duration_s=10,
        )
    )
    result = executor.execute(WaitUntilElapsed(elapsed_s=2, round_index=1))

    sleep.assert_called_once_with(5.0)
    assert result["target_elapsed_s"] == pytest.approx(14.0)
    assert executor.sample_states()[1]["state"] == "annealing"


def test_sample_return_updates_runtime_state() -> None:
    clock_values = iter([0.0, 1.0, 12.0])
    executor = ActionExecutor(
        SimpleNamespace(),
        CoordinateRegistry.from_yaml(),
        dry_run=True,
        clock=lambda: next(clock_values),
    )
    executor.execute(
        AnnealTimerStart(
            round_index=2,
            sample_id="sample-2",
            hotplate_slot=2,
            duration_s=10,
        )
    )
    executor.execute(
        SampleStateUpdate(
            round_index=2,
            sample_id="sample-2",
            hotplate_slot=2,
        )
    )

    assert executor.sample_states()[2] == {
        "sample_id": "sample-2",
        "state": "returned",
        "hotplate_slot": 2,
        "placed_elapsed_s": 1.0,
        "due_elapsed_s": 11.0,
        "returned_elapsed_s": 12.0,
    }


def test_hotplate_resource_manager_reuses_only_released_slot() -> None:
    manager = HotplateResourceManager(capacity=1)
    manager.place(1, 1, "sample-1")
    with pytest.raises(ValueError, match="still occupied"):
        manager.place(1, 2, "sample-2")
    manager.retrieve(1, 1)
    manager.place(1, 2, "sample-2")

    assert manager.owner(1) == (2, "sample-2")


def test_spin_elapsed_wait_requires_motor_epoch_and_uses_absolute_deadline() -> None:
    clock_values = iter([20.0, 22.5])
    sleep = MagicMock()
    executor = ActionExecutor(
        SimpleNamespace(),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        sleep=sleep,
        clock=lambda: next(clock_values),
    )
    with pytest.raises(ActionExecutionError, match="before motor start"):
        executor.execute(WaitUntilSpinElapsed(elapsed_s=3))

    executor._spin_started_at = 20.0
    result = executor.execute(WaitUntilSpinElapsed(elapsed_s=5))

    sleep.assert_called_once_with(2.5)
    assert result["observed_elapsed_s"] == pytest.approx(2.5)


def test_cancelled_executor_does_not_start_next_critical_action() -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    pipette = MagicMock()
    executor = ActionExecutor(
        SimpleNamespace(pipette=pipette),
        CoordinateRegistry.from_yaml(),
        dry_run=False,
        cancel_event=cancel_event,
    )

    with pytest.raises(ActionExecutionError, match="cancelled"):
        executor.execute(
            Aspirate(
                liquid="perovskite_precursor",
                volume_ul=10,
                coordinate="stations.reagent_rack.precursor_1",
            )
        )
    pipette.aspirate.assert_not_called()
