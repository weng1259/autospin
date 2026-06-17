"""Generate low-level multi-round operation/params recipes.

This module intentionally does not use the higher-level ExperimentProtocol
schema. It targets the existing low-level JSON format consumed by
example_experiment.py, where each item has "operation" and "params" fields.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


DEFAULT_SAMPLE_ROWS = [
    [0, 1, 2, 3, 4],
    [0, 1, 3, 4],
    [0, 1, 3, 4],
    [0, 1, 2, 3, 4],
]

SAMPLE_BASE_X = -34.0
SAMPLE_BASE_Y = -364.0
SAMPLE_DX = 37.25
SAMPLE_DY = 24.33
SAMPLE_Z_PICK = -156.0
SAMPLE_Z_LIFT = -50.0

LIQUID_BASE_X = -11.0
LIQUID_BASE_Y = -147.0
LIQUID_DX_GROUP = 38.4
LIQUID_DY_INNER = 19.2
LIQUID_Z = -145.0

LIQUID_PICK_STEP_INDEX = 10
ANTISOLVENT_PICK_STEP_INDEX = 18
SAMPLE_PICK_STEP_INDEX = 4
SAMPLE_RETURN_STEP_INDEX = 38


@dataclass(frozen=True)
class SequenceLayout:
    """Coordinate layout for multi-round low-level sequence generation."""

    sample_rows: list[list[int]]
    sample_base_x: float
    sample_base_y: float
    sample_dx: float
    sample_dy: float
    sample_z_pick: float
    sample_z_lift: float
    liquid_base_x: float
    liquid_base_y: float
    liquid_dx_group: float
    liquid_dy_inner: float
    liquid_z: float


@dataclass(frozen=True)
class ReplacementIndexes:
    """Operation indexes patched in each single-round operation template."""

    liquid_pick: int
    antisolvent_pick: int
    sample_pick: int
    sample_return: int


DEFAULT_LAYOUT = SequenceLayout(
    sample_rows=DEFAULT_SAMPLE_ROWS,
    sample_base_x=SAMPLE_BASE_X,
    sample_base_y=SAMPLE_BASE_Y,
    sample_dx=SAMPLE_DX,
    sample_dy=SAMPLE_DY,
    sample_z_pick=SAMPLE_Z_PICK,
    sample_z_lift=SAMPLE_Z_LIFT,
    liquid_base_x=LIQUID_BASE_X,
    liquid_base_y=LIQUID_BASE_Y,
    liquid_dx_group=LIQUID_DX_GROUP,
    liquid_dy_inner=LIQUID_DY_INNER,
    liquid_z=LIQUID_Z,
)

DEFAULT_REPLACEMENT_INDEXES = ReplacementIndexes(
    liquid_pick=LIQUID_PICK_STEP_INDEX,
    antisolvent_pick=ANTISOLVENT_PICK_STEP_INDEX,
    sample_pick=SAMPLE_PICK_STEP_INDEX,
    sample_return=SAMPLE_RETURN_STEP_INDEX,
)


def get_sample_position(
    round_no: int,
    layout: SequenceLayout = DEFAULT_LAYOUT,
) -> dict[str, float]:
    """Return the sample pick and lift coordinates for a 1-based round."""

    if round_no < 1:
        raise ValueError("round_no must be >= 1")

    count = 0
    for row_index, cols in enumerate(layout.sample_rows):
        for col in cols:
            count += 1
            if count == round_no:
                return {
                    "x": round(layout.sample_base_x - layout.sample_dx * col, 2),
                    "y": round(layout.sample_base_y - layout.sample_dy * row_index, 2),
                    "z_pick": layout.sample_z_pick,
                    "z_lift": layout.sample_z_lift,
                }

    raise ValueError(f"round_no {round_no} exceeds available sample positions ({count})")


def get_liquid_position(
    round_no: int,
    layout: SequenceLayout = DEFAULT_LAYOUT,
) -> dict[str, float]:
    """Return the liquid/cap coordinate shared by every two sample rounds."""

    if round_no < 1:
        raise ValueError("round_no must be >= 1")

    liquid_index = (round_no - 1) // 2
    liquid_group = liquid_index // 6
    liquid_inner = liquid_index % 6

    return {
        "x": round(layout.liquid_base_x - layout.liquid_dx_group * liquid_group, 2),
        "y": round(layout.liquid_base_y + layout.liquid_dy_inner * liquid_inner, 2),
        "z": layout.liquid_z,
    }


def make_round(
    round_no: int,
    single_round_ops: list[dict[str, Any]],
    layout: SequenceLayout = DEFAULT_LAYOUT,
    indexes: ReplacementIndexes = DEFAULT_REPLACEMENT_INDEXES,
) -> list[dict[str, Any]]:
    """Copy one round of operations and patch round-specific coordinates."""

    _validate_single_round_ops(single_round_ops, indexes)
    ops = copy.deepcopy(single_round_ops)
    sample = get_sample_position(round_no, layout)
    liquid = get_liquid_position(round_no, layout)

    liquid_params = {"x": liquid["x"], "y": liquid["y"], "z": liquid["z"]}
    sample_pick_params = {"x": sample["x"], "y": sample["y"], "z": sample["z_pick"]}
    sample_return_params = {"x": sample["x"], "y": sample["y"], "z": sample["z_pick"]}

    _replace_xyz_params(ops[indexes.liquid_pick], liquid_params)
    _replace_xyz_params(ops[indexes.antisolvent_pick], liquid_params)
    _replace_xyz_params(ops[indexes.sample_pick], sample_pick_params)
    _replace_xyz_params(ops[indexes.sample_return], sample_return_params)
    return ops


def generate_multi_round_recipe(
    single_round_recipe: dict[str, Any],
    rounds: int = 18,
    experiment_name: str | None = None,
    layout: SequenceLayout = DEFAULT_LAYOUT,
    indexes: ReplacementIndexes = DEFAULT_REPLACEMENT_INDEXES,
) -> dict[str, Any]:
    """Generate a complete low-level recipe from a single-round recipe."""

    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    capacity = _sample_capacity(layout)
    if rounds > capacity:
        raise ValueError(f"rounds {rounds} exceeds available sample positions ({capacity})")

    single_round_ops = single_round_recipe.get("operations")
    if not isinstance(single_round_ops, list) or not single_round_ops:
        raise ValueError("single_round_recipe must contain a non-empty operations list")

    all_operations: list[dict[str, Any]] = []
    for round_no in range(1, rounds + 1):
        all_operations.extend(make_round(round_no, single_round_ops, layout, indexes))

    base_name = experiment_name or single_round_recipe.get("experiment_name", "multi-round sequence")
    return {
        "experiment_name": f"{base_name} ({rounds} rounds)",
        "rounds": rounds,
        "operations_per_round": len(single_round_ops),
        "operations": all_operations,
    }


def _sample_capacity(layout: SequenceLayout = DEFAULT_LAYOUT) -> int:
    return sum(len(cols) for cols in layout.sample_rows)


def _validate_single_round_ops(
    single_round_ops: list[dict[str, Any]],
    indexes: ReplacementIndexes = DEFAULT_REPLACEMENT_INDEXES,
) -> None:
    required_indexes = [
        indexes.liquid_pick,
        indexes.antisolvent_pick,
        indexes.sample_pick,
        indexes.sample_return,
    ]
    if len(single_round_ops) <= max(required_indexes):
        raise ValueError(
            "single_round_ops is too short for coordinate replacement indexes "
            f"{required_indexes}"
        )

    for index in required_indexes:
        operation = single_round_ops[index].get("operation")
        if operation not in ("MoveGantry", "MoveGantrySafe"):
            raise ValueError(
                f"operation at index {index} must be MoveGantry or MoveGantrySafe, got {operation!r}"
            )


def _replace_xyz_params(operation: dict[str, Any], xyz: dict[str, float]) -> None:
    params = copy.deepcopy(operation.get("params", {}))
    params.update(xyz)
    operation["params"] = params
