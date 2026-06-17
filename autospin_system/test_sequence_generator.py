import json
from pathlib import Path

import pytest

from AutoSpinmotorSystem.protocol.sequence_generator import (
    ReplacementIndexes,
    generate_multi_round_recipe,
    get_liquid_position,
    get_sample_position,
)


def _single_round_recipe():
    return {
        "experiment_name": "single round",
        "operations": [
            {"operation": "ReadStatus", "params": {"device": "all"}},
            {"operation": "HomeGantry", "params": {}},
            {"operation": "GripperOpen", "params": {}},
            {"operation": "MoveGantry", "params": {"x": -11.0, "y": -147.0, "z": -145.0}},
            {"operation": "GripperClose", "params": {}},
            {"operation": "MoveGantry", "params": {"x": -30.0, "y": -125.0, "z": -50.0}},
            {"operation": "PipetteAspirate", "params": {"volume_uL": 0}},
            {"operation": "MoveGantry", "params": {"x": -11.0, "y": -147.0, "z": -145.0}},
            {"operation": "GripperOpen", "params": {}},
            {"operation": "MoveGantry", "params": {"x": -34.0, "y": -364.0, "z": -156.0}},
            {"operation": "GripperClose", "params": {}},
            {"operation": "MoveGantry", "params": {"x": -34.0, "y": -364.0, "z": -50.0}},
            {"operation": "MoveGantry", "params": {"x": -400.0, "y": -5.0, "z": -50.0}},
            {"operation": "SpinCoat", "params": {"direction": "forward", "steps": [{"rpm": 1000, "time_s": 5}]}},
            {"operation": "MoveGantry", "params": {"x": -430.0, "y": -320.0, "z": -175.0}},
            {"operation": "GripperOpen", "params": {}},
            {"operation": "MoveGantry", "params": {"x": -430.0, "y": -320.0, "z": -5.0}},
            {"operation": "HomeGantry", "params": {}},
            {"operation": "ReadStatus", "params": {"device": "all"}},
        ],
    }


def _full_cycle_recipe():
    path = Path(__file__).resolve().parent / "examples" / "one_round_full_spin_hotplate_cycle.json"
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def test_sample_positions():
    assert get_sample_position(1) == {"x": -34.0, "y": -364.0, "z_pick": -156.0, "z_lift": -50.0}
    assert get_sample_position(18) == {"x": -183.0, "y": -436.99, "z_pick": -156.0, "z_lift": -50.0}
    with pytest.raises(ValueError):
        get_sample_position(19)


def test_liquid_positions():
    assert get_liquid_position(1) == get_liquid_position(2)
    assert get_liquid_position(1) == {"x": -11.0, "y": -147.0, "z": -145.0}
    assert get_liquid_position(13) == {"x": -49.4, "y": -147.0, "z": -145.0}


def test_generate_multi_round_recipe_patches_expected_steps():
    recipe = generate_multi_round_recipe(_full_cycle_recipe(), rounds=18)
    operations = recipe["operations"]
    ops_per_round = recipe["operations_per_round"]

    assert recipe["rounds"] == 18
    assert ops_per_round == 42
    assert len(operations) == 18 * 42

    round_18 = operations[17 * ops_per_round : 18 * ops_per_round]
    assert round_18[10]["operation"] == "MoveGantrySafe"
    assert round_18[10]["params"] == {"x": -49.4, "y": -108.6, "z": -145.0, "safe_z": -50.0}
    assert round_18[18]["params"] == {"x": -49.4, "y": -108.6, "z": -145.0, "safe_z": -50.0}
    assert round_18[4]["params"] == {"x": -183.0, "y": -436.99, "z": -156.0, "safe_z": -50.0}
    assert round_18[38]["params"] == {"x": -183.0, "y": -436.99, "z": -156.0, "safe_z": -50.0}


def test_generate_multi_round_recipe_keeps_old_move_gantry_compatible():
    indexes = ReplacementIndexes(
        liquid_pick=3,
        antisolvent_pick=7,
        sample_pick=9,
        sample_return=11,
    )
    recipe = generate_multi_round_recipe(_single_round_recipe(), rounds=18, indexes=indexes)
    operations = recipe["operations"]
    ops_per_round = recipe["operations_per_round"]

    round_18 = operations[17 * ops_per_round : 18 * ops_per_round]
    assert round_18[3]["params"] == {"x": -49.4, "y": -108.6, "z": -145.0}
    assert round_18[7]["params"] == {"x": -49.4, "y": -108.6, "z": -145.0}
    assert round_18[9]["params"] == {"x": -183.0, "y": -436.99, "z": -156.0}
    assert round_18[11]["params"] == {"x": -183.0, "y": -436.99, "z": -156.0}
