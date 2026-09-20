"""Runtime configuration, process coordinates, and routine generation routes."""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import L3Config, get_config
from ..coordinates import CoordinatesConfig, load_coordinates
from ..hardware.types import Position
from ..recipe_storage import resolve_recipes_path
from ..routine import Routine, RoutineStep


class MultiRoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_name: str = Field(min_length=1, max_length=120)
    output_name: str = Field(min_length=1, max_length=120)
    rounds: int = Field(ge=1, le=100)
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    offset_z_mm: float = 0.0


class ExperimentParameterGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(default="参数组 1", min_length=1, max_length=80)
    repeats: int = Field(default=4, ge=1, le=100)
    precursor_volume_ul: float = Field(default=100, gt=0)
    use_antisolvent: bool = True
    antisolvent_volume_ul: float = Field(default=50, gt=0)
    annealing_temperature_c: float = Field(default=120, ge=0)
    stage_1_speed_rpm: float = Field(default=1500, gt=0)
    stage_1_time_s: float = Field(default=10, gt=0)
    stage_2_speed_rpm: float = Field(default=5000, gt=0)
    stage_2_time_s: float = Field(default=20, gt=0)
    antisolvent_delay_stage_2_s: float = Field(default=10, ge=0)
    tip_height_mm: float = Field(default=10, gt=0)
    annealing_time_s: float = Field(default=480, ge=0)

    @model_validator(mode="after")
    def antisolvent_within_stage_2(self) -> "ExperimentParameterGroupRequest":
        if (
            self.use_antisolvent
            and self.antisolvent_delay_stage_2_s > self.stage_2_time_s
        ):
            raise ValueError(
                "antisolvent_delay_stage_2_s must not exceed stage_2_time_s"
            )
        return self


class ExperimentRecipeBuilderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    experiment_name: str = Field(
        default="perovskite_experiment",
        min_length=1,
        max_length=120,
    )
    output_name: str = Field(
        default="perovskite_experiment.json",
        min_length=1,
        max_length=160,
    )
    save: bool = False
    groups: list[ExperimentParameterGroupRequest] = Field(
        default_factory=lambda: [ExperimentParameterGroupRequest()],
        min_length=1,
        max_length=25,
    )

    @model_validator(mode="after")
    def total_rounds_within_limit(self) -> "ExperimentRecipeBuilderRequest":
        if sum(group.repeats for group in self.groups) > 100:
            raise ValueError("total repeats across groups must not exceed 100")
        return self


def _safe_name(name: str) -> str:
    cleaned = "".join(
        char if (char.isalnum() or char in "-_.") else "_"
        for char in name
    ).strip("_")
    return cleaned or "routine"


def _safe_recipe_filename(name: str) -> str:
    cleaned = _safe_name(Path(name).name)
    if not cleaned.endswith(".json"):
        cleaned = f"{cleaned}.json"
    return cleaned


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _recipe_builder_defaults() -> dict[str, Any]:
    return {
        "template": "perovskite_basic",
        "template_label": "钙钛矿两阶段旋涂",
        "default_experiment_name": "perovskite_experiment",
        "default_output": "perovskite_experiment.json",
        "default_repeats_per_group": 4,
        "max_rounds": 100,
        "max_groups": 25,
        "default_group": {
            "name": "参数组 1",
            "repeats": 4,
            "precursor_volume_ul": 100,
            "use_antisolvent": True,
            "antisolvent_volume_ul": 50,
            "annealing_temperature_c": 120,
            "stage_1_speed_rpm": 1500,
            "stage_1_time_s": 10,
            "stage_2_speed_rpm": 5000,
            "stage_2_time_s": 20,
            "antisolvent_delay_stage_2_s": 10,
            "tip_height_mm": 10,
            "annealing_time_s": 480,
        },
    }


def _load_recipe_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("recipe JSON root must be an object")
    return payload


def _summarize_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    operations = recipe.get("operations", [])
    if not isinstance(operations, list):
        operations = []
    counts: dict[str, int] = {}
    gantry_targets = 0
    pipette_ops = 0
    spin_ops = 0
    heater_ops = 0
    for item in operations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("operation", "Unknown"))
        counts[name] = counts.get(name, 0) + 1
        if name in {"MoveGantry", "MoveGantrySafe"}:
            gantry_targets += 1
        if name.startswith("Pipette"):
            pipette_ops += 1
        if name.startswith("Spin"):
            spin_ops += 1
        if "Hotplate" in name or name == "AnnealWait":
            heater_ops += 1
    preview_steps = [
        {
            "index": index,
            "operation": item.get("operation", "Unknown"),
            "params": item.get("params", {}),
        }
        for index, item in enumerate(operations[:80])
        if isinstance(item, dict)
    ]
    return {
        "rounds": recipe.get("rounds"),
        "operations_per_round": recipe.get("operations_per_round"),
        "hotplate_dwell_s": recipe.get("hotplate_dwell_s"),
        "operation_count": len(operations),
        "gantry_targets": gantry_targets,
        "pipette_operations": pipette_ops,
        "spin_operations": spin_ops,
        "heater_operations": heater_ops,
        "operation_counts": counts,
        "preview_steps": preview_steps,
    }


def _build_experiment_recipe(
    request: ExperimentRecipeBuilderRequest,
    *,
    recipes_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from ..protocol import create_perovskite_protocol, validate_protocol

    config = get_config()
    if config.hardware is None:
        raise HTTPException(status_code=503, detail="hardware limits unavailable")

    try:
        operations: list[dict[str, Any]] = []
        protocols: list[dict[str, Any]] = []
        group_summaries: list[dict[str, Any]] = []
        round_index = 0
        operations_per_round: int | None = None
        next_tip_slot = 1
        for group_index, group in enumerate(request.groups, start=1):
            ratio = (
                group.antisolvent_delay_stage_2_s / group.stage_2_time_s
                if group.use_antisolvent
                else None
            )
            group_summaries.append(
                {
                    **group.model_dump(mode="json"),
                    "index": group_index,
                    "antisolvent_timing_ratio": ratio,
                    "round_start": round_index + 1,
                    "round_end": round_index + group.repeats,
                }
            )
            for repetition in range(1, group.repeats + 1):
                round_index += 1
                precursor_tip_slot = next_tip_slot
                next_tip_slot += 1
                antisolvent_tip_slot: int | None = None
                if group.use_antisolvent:
                    antisolvent_tip_slot = next_tip_slot
                    next_tip_slot += 1
                if next_tip_slot > 97:
                    raise ValueError(
                        "selected rounds require more than the 96 available tips"
                    )
                protocol = create_perovskite_protocol(
                    sample_id=f"{_safe_name(request.experiment_name)}_{round_index:03d}",
                    precursor_volume=group.precursor_volume_ul,
                    antisolvent_volume=group.antisolvent_volume_ul,
                    annealing_temperature=group.annealing_temperature_c,
                    initial_spin_speed=group.stage_1_speed_rpm,
                    initial_spin_time=group.stage_1_time_s,
                    spin_speed=group.stage_2_speed_rpm,
                    spin_time=group.stage_2_time_s,
                    antisolvent_at=(
                        group.stage_1_time_s
                        + group.antisolvent_delay_stage_2_s
                    ),
                    precursor_tip_clearance=group.tip_height_mm,
                    antisolvent_tip_clearance=group.tip_height_mm,
                    annealing_time=group.annealing_time_s,
                    use_antisolvent=group.use_antisolvent,
                    precursor_tip_slot=precursor_tip_slot,
                    antisolvent_tip_slot=antisolvent_tip_slot,
                )
                errors = validate_protocol(
                    protocol,
                    max_rpm=config.hardware.spincoater.max_rpm,
                    max_volume_ul=config.hardware.pipette.max_volume_ul,
                    max_temperature_c=config.hardware.heater.sv_max_c,
                )
                if errors:
                    raise ValueError(
                        f"{group.name}, repeat {repetition}: {'; '.join(errors)}"
                    )
                if operations_per_round is None:
                    operations_per_round = len(protocol.operations)
                protocols.append(
                    protocol.model_dump(mode="json", by_alias=True)
                )
                for operation in protocol.operations:
                    operations.append(
                        {
                            "operation": operation.name,
                            "params": operation.model_dump(
                                mode="json", by_alias=True, exclude={"name"}
                            ),
                            "round": round_index,
                            "group": group_index,
                            "group_name": group.name,
                            "repeat": repetition,
                        }
                    )
        generated = {
            "schema_version": 1,
            "template": "perovskite_basic",
            "experiment_name": request.experiment_name,
            "rounds": round_index,
            "operations_per_round": operations_per_round or 0,
            "hotplate_dwell_s": None,
            "parameter_groups": group_summaries,
            "protocols": protocols,
            "operations": operations,
        }
        human_check = {
            "required": True,
            "items": [
                "Confirm all production coordinates and tool offsets.",
                "Confirm reagents, tips, substrates, and waste capacity.",
            ],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    output_path = recipes_dir / _safe_recipe_filename(
        request.output_name
    )
    payload = {
        "saved": False,
        "output": str(output_path),
        "recipe_name": output_path.name,
        "template": "perovskite_basic",
        "rounds": round_index,
        "groups": group_summaries,
        "summary": _summarize_recipe(generated),
        "human_check": human_check,
        "run_commands": {
            "mock": (
                "POST /api/experiments/multi-round/execute "
                f'{{"recipe_name":"{output_path.name}","dry_run":true}}'
            ),
            "real": (
                "POST /api/experiments/multi-round/execute "
                f'{{"recipe_name":"{output_path.name}","dry_run":false}}'
            ),
        },
    }
    if request.save:
        text = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(output_path, text)
        payload["saved"] = True
    return generated, payload


def _runtime_payload(config: L3Config) -> dict[str, Any]:
    hardware = config.hardware
    if hardware is None:
        raise HTTPException(status_code=503, detail="hardware config unavailable")
    gantry = hardware.gantry
    return {
        "mock": hardware.mock,
        "gantry": None if gantry is None else {
            "soft_limits": gantry.soft_limits.model_dump(),
            "feed_rate_default": gantry.feed_rate_default,
            "max_feed_mm_min": config.motion.max_feed_mm_min,
            "homing_enabled": gantry.homing_enabled,
        },
        "spincoater": {
            "max_rpm": hardware.spincoater.max_rpm,
            "max_rpm_confirmation_required": (
                hardware.spincoater.max_rpm_confirmation_required
            ),
            "capability_source": hardware.spincoater.capability_source,
        },
        "heater": {"sv_max_c": hardware.heater.sv_max_c},
        "pipette": {"max_volume_ul": hardware.pipette.max_volume_ul},
        "linear_stage": {
            "min_position_mm": hardware.linear_stage.min_position_mm,
            "max_position_mm": (
                hardware.linear_stage.max_position_mm
                if hardware.linear_stage.max_position_mm is not None
                else hardware.linear_stage.travel_mm
            ),
        },
        "relay": {"channel_map": hardware.relay.channel_map},
        "gripper": (
            None if hardware.gripper is None
            else hardware.gripper.model_dump()
        ),
    }


def _offset_step(
    step: RoutineStep,
    *,
    round_index: int,
    request: MultiRoundRequest,
    config: L3Config,
) -> RoutineStep:
    copied = step.model_copy(deep=True)
    copied.seq = 0
    copied.label = f"R{round_index + 1}: {step.label}"
    if copied.device != "gantry" or copied.action != "move_to":
        return copied

    dx = request.offset_x_mm * round_index
    dy = request.offset_y_mm * round_index
    dz = request.offset_z_mm * round_index
    for value in copied.args:
        if not (
            isinstance(value, dict)
            and value.get("__type__") == "Position"
            and isinstance(value.get("fields"), dict)
        ):
            continue
        fields = value["fields"]
        fields["x_mm"] = float(fields["x_mm"]) + dx
        fields["y_mm"] = float(fields["y_mm"]) + dy
        fields["z_mm"] = float(fields["z_mm"]) + dz
        limits = (
            config.hardware.gantry.soft_limits
            if config.hardware is not None and config.hardware.gantry is not None
            else config.soft_limits
        )
        limits.assert_contains(Position(**fields))
    return copied


def register_configuration_routes(
    app: FastAPI,
    *,
    routines_path: str | Path,
    coordinates_path: str | Path,
    recipes_path: str | Path | None = None,
) -> None:
    routines_dir = Path(routines_path)
    coordinates_file = Path(coordinates_path)
    recipes_dir = resolve_recipes_path(recipes_path)

    @app.get("/api/config/runtime")
    def runtime_configuration() -> dict[str, Any]:
        payload = _runtime_payload(get_config())
        payload["storage"] = {"recipes_dir": str(recipes_dir)}
        return payload

    @app.get("/api/config/coordinates", response_model=CoordinatesConfig)
    def get_coordinates() -> CoordinatesConfig:
        return load_coordinates(coordinates_file)

    @app.put("/api/config/coordinates", response_model=CoordinatesConfig)
    def put_coordinates(request: CoordinatesConfig) -> CoordinatesConfig:
        _atomic_write(
            coordinates_file,
            yaml.safe_dump(
                request.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
        )
        return request

    @app.get("/api/config/process-coordinates", include_in_schema=False)
    @app.put("/api/config/process-coordinates", include_in_schema=False)
    def retired_process_coordinates() -> None:
        raise HTTPException(
            status_code=410,
            detail=(
                "process_coordinates.yaml is retired; use "
                "/api/config/coordinates backed by config/coordinates.yaml"
            ),
        )

    @app.post("/api/routines/generate-multi-round")
    def generate_multi_round(request: MultiRoundRequest) -> dict[str, Any]:
        source = routines_dir / f"{_safe_name(request.source_name)}.json"
        if not source.is_file():
            raise HTTPException(status_code=404, detail="source routine not found")
        routine = Routine.load(source)
        steps: list[RoutineStep] = []
        config = get_config()
        try:
            for round_index in range(request.rounds):
                for step in routine.steps:
                    steps.append(
                        _offset_step(
                            step,
                            round_index=round_index,
                            request=request,
                            config=config,
                        )
                    )
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"generated motion exceeds configured soft limits: {exc}",
            ) from exc
        for seq, step in enumerate(steps):
            step.seq = seq
        generated = Routine(
            name=request.output_name,
            description=(
                f"Generated from {routine.name}: {request.rounds} rounds; "
                f"per-round offset=({request.offset_x_mm}, "
                f"{request.offset_y_mm}, {request.offset_z_mm}) mm."
            ),
            steps=steps,
        )
        destination = routines_dir / f"{_safe_name(request.output_name)}.json"
        _atomic_write(destination, generated.to_json())
        return {
            "name": generated.name,
            "rounds": request.rounds,
            "step_count": len(generated.steps),
            "has_motion": generated.has_motion,
        }

    @app.get("/api/experiment-builder/defaults")
    def experiment_builder_defaults() -> dict[str, Any]:
        return _recipe_builder_defaults()

    @app.post("/api/experiment-builder/recipe")
    def experiment_builder_recipe(
        request: ExperimentRecipeBuilderRequest,
    ) -> dict[str, Any]:
        _, payload = _build_experiment_recipe(
            request,
            recipes_dir=recipes_dir,
        )
        return payload
