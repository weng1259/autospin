"""Runtime configuration, process coordinates, and routine generation routes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..config import L3Config, get_config
from ..hardware.types import Position
from ..routine import Routine, RoutineStep


class ProcessPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_mm: float
    y_mm: float
    z_mm: float


class ProcessCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: dict[str, ProcessPoint] = Field(default_factory=dict)


class MultiRoundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_name: str = Field(min_length=1, max_length=120)
    output_name: str = Field(min_length=1, max_length=120)
    rounds: int = Field(ge=1, le=100)
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    offset_z_mm: float = 0.0


def _safe_name(name: str) -> str:
    cleaned = "".join(
        char if (char.isalnum() or char in "-_.") else "_"
        for char in name
    ).strip("_")
    return cleaned or "routine"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
        "spincoater": {"max_rpm": hardware.spincoater.max_rpm},
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


def _validate_points(value: ProcessCoordinates, config: L3Config) -> None:
    limits = (
        config.hardware.gantry.soft_limits
        if config.hardware is not None and config.hardware.gantry is not None
        else config.soft_limits
    )
    for name, point in value.points.items():
        try:
            limits.assert_contains(
                Position(x_mm=point.x_mm, y_mm=point.y_mm, z_mm=point.z_mm)
            )
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"process point {name!r} is outside gantry soft limits: {exc}",
            ) from exc


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
) -> None:
    routines_dir = Path(routines_path)
    coordinates_file = Path(coordinates_path)

    @app.get("/api/config/runtime")
    def runtime_configuration() -> dict[str, Any]:
        return _runtime_payload(get_config())

    @app.get("/api/config/process-coordinates", response_model=ProcessCoordinates)
    def get_process_coordinates() -> ProcessCoordinates:
        if not coordinates_file.is_file():
            return ProcessCoordinates()
        raw = yaml.safe_load(coordinates_file.read_text(encoding="utf-8")) or {}
        return ProcessCoordinates.model_validate(raw)

    @app.put("/api/config/process-coordinates", response_model=ProcessCoordinates)
    def put_process_coordinates(
        request: ProcessCoordinates,
    ) -> ProcessCoordinates:
        _validate_points(request, get_config())
        _atomic_write(
            coordinates_file,
            yaml.safe_dump(
                request.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=True,
            ),
        )
        return request

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
