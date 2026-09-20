"""Experiment preview and execution routes using the semantic action service."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..coordinates import CoordinateRegistry
from ..config import get_config
from ..experiment_service import ExperimentService
from ..experiment import ExperimentGenerator, perovskite_parameter_space
from ..multi_round import compile_multi_round
from ..production_safety import (
    startup_validation,
    validate_action_plan,
    validate_execution,
)
from ..workflows import compile_protocol
from ..protocol import (
    ExperimentProtocol,
    create_perovskite_protocol,
    load_template,
)
from ..recipe_storage import resolve_recipe_file, resolve_recipes_path
from .gate import OperationExecutionError, OperationGate
from .registry import DeviceRegistry


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str | None = None
    protocol: ExperimentProtocol | None = None
    dry_run: bool = True

    def resolved(self) -> ExperimentProtocol:
        if (self.template is None) == (self.protocol is None):
            raise ValueError("provide exactly one of template or protocol")
        return self.protocol if self.protocol is not None else load_template(self.template or "")


class ParameterBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[dict]


class MultiRoundExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recipe_name: str = Field(min_length=1, max_length=160)
    dry_run: bool = True


class MultiRoundExecutionError(OperationExecutionError):
    error_code = "L3.MULTI_ROUND_EXECUTION_FAILED"
    severity = "alarm"
    recoverable = True
    suggested_action = "Check the failed round and device connection, then retry."
    suggested_action_zh = "检查失败轮次及设备连接状态，恢复后重新运行。"


def _load_multi_round_protocols(
    recipe_name: str,
    *,
    recipes_path: str | Path | None = None,
) -> list[ExperimentProtocol]:
    path = resolve_recipe_file(resolve_recipes_path(recipes_path), recipe_name)
    if not path.is_file():
        raise ValueError(f"saved multi-round recipe not found: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_protocols = payload.get("protocols") if isinstance(payload, dict) else None
    if isinstance(raw_protocols, list) and raw_protocols:
        return [
            ExperimentProtocol.model_validate(item)
            for item in raw_protocols
        ]
    groups = payload.get("parameter_groups") if isinstance(payload, dict) else None
    experiment_name = payload.get("experiment_name") if isinstance(payload, dict) else None
    if not isinstance(groups, list) or not groups or not isinstance(experiment_name, str):
        raise ValueError(
            "saved recipe has neither executable protocols nor parameter_groups; "
            "regenerate it with the current experiment builder"
        )
    rebuilt: list[ExperimentProtocol] = []
    round_index = 0
    next_tip_slot = 1
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("saved recipe contains an invalid parameter group")
        repeats = int(group.get("repeats", 0))
        if repeats < 1:
            raise ValueError("saved recipe parameter-group repeats must be positive")
        for _ in range(repeats):
            round_index += 1
            use_antisolvent = bool(group.get("use_antisolvent", True))
            precursor_tip_slot = next_tip_slot
            next_tip_slot += 1
            antisolvent_tip_slot: int | None = None
            if use_antisolvent:
                antisolvent_tip_slot = next_tip_slot
                next_tip_slot += 1
            if next_tip_slot > 97:
                raise ValueError(
                    "saved recipe requires more than the 96 available tips"
                )
            rebuilt.append(
                create_perovskite_protocol(
                    sample_id=f"{experiment_name}_{round_index:03d}",
                    precursor_volume=float(group["precursor_volume_ul"]),
                    antisolvent_volume=float(group["antisolvent_volume_ul"]),
                    annealing_temperature=float(group["annealing_temperature_c"]),
                    initial_spin_speed=float(group["stage_1_speed_rpm"]),
                    initial_spin_time=float(group["stage_1_time_s"]),
                    spin_speed=float(group["stage_2_speed_rpm"]),
                    spin_time=float(group["stage_2_time_s"]),
                    antisolvent_at=(
                        float(group["stage_1_time_s"])
                        + float(group["antisolvent_delay_stage_2_s"])
                    ),
                    precursor_tip_clearance=float(group["tip_height_mm"]),
                    antisolvent_tip_clearance=float(group["tip_height_mm"]),
                    annealing_time=float(group["annealing_time_s"]),
                    use_antisolvent=use_antisolvent,
                    precursor_tip_slot=precursor_tip_slot,
                    antisolvent_tip_slot=antisolvent_tip_slot,
                )
            )
    return rebuilt


def register_experiment_routes(
    app: FastAPI,
    registry: DeviceRegistry,
    gate: OperationGate,
    coordinates: CoordinateRegistry,
    *,
    recipes_path: str | Path | None = None,
) -> None:
    service = ExperimentService(registry, coordinates)
    recipes_dir = resolve_recipes_path(recipes_path)
    app.state.experiment_abort_event = threading.Event()

    @app.get("/api/experiment-templates")
    def experiment_templates() -> dict:
        space = perovskite_parameter_space()
        return {
            "templates": ["perovskite_basic", "spin_only_test"],
            "perovskite_basic": {
                "fixed": [
                    item.model_dump(mode="json") for item in space.fixed
                ],
                "variables": space.optimizer_schema(),
            },
        }

    @app.post("/api/experiment-templates/perovskite_basic/batch-preview")
    def batch_preview(request: ParameterBatchRequest) -> dict:
        try:
            generated = ExperimentGenerator(
                perovskite_parameter_space()
            ).generate_batch(request.candidates)
            previews = []
            for item in generated:
                plan, safety = service.prepare(item.protocol, dry_run=True)
                previews.append(
                    {
                        "experiment_id": item.experiment_id,
                        "parameters": item.parameters,
                        "protocol": item.protocol.model_dump(
                            mode="json", by_alias=True
                        ),
                        "action_count": len(plan.actions),
                        "safety": safety.model_dump(mode="json"),
                    }
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"count": len(previews), "experiments": previews}

    @app.get("/api/safety/startup")
    def startup_safety() -> dict:
        report = startup_validation(registry, coordinates).model_dump(mode="json")
        spin = get_config().hardware.spincoater  # validated during app startup
        report["hardware_limits"] = {"spincoater_max_rpm": spin.max_rpm}
        report["configuration_source"] = {
            "hardware": "config/hardware.yaml",
            "coordinates": "config/coordinates.yaml",
            "spincoater_capability": spin.capability_source,
        }
        report["runtime_validation_required"] = any(
            point.verification == "pending_runtime_validation"
            for point in coordinates.config.all_points().values()
        ) or any(
            offset.verification == "pending_runtime_validation"
            for offset in coordinates.config.tool_offsets.values()
        )
        return report

    @app.get("/api/runtime/serial-resources")
    def serial_resources() -> list[dict[str, object]]:
        return registry.serial_diagnostics()

    @app.get("/api/coordinates")
    def coordinate_status() -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "xyz": point.xyz,
                "source": point.source,
                "status": point.status,
                "verification": point.verification,
                "confidence": point.confidence,
                "verified": (
                    point.executable
                    and not point.requires_hardware_confirmation
                    and not point.placeholder
                    and point.verification == "runtime_validated"
                ),
                "executable": point.executable,
                "runtime_validation_required": (
                    point.verification == "pending_runtime_validation"
                ),
            }
            for name, point in sorted(coordinates.config.all_points().items())
        ]

    @app.get("/api/tool-offsets")
    def tool_offset_status() -> list[dict[str, object]]:
        return [
            {
                "tool": name,
                "xyz": offset.xyz,
                "source": offset.source,
                "status": offset.status,
                "verification": offset.verification,
                "executable": offset.executable,
                "runtime_validation_required": (
                    offset.verification == "pending_runtime_validation"
                ),
            }
            for name, offset in sorted(
                coordinates.config.tool_offsets.items()
            )
        ]

    @app.post("/api/experiments/preview")
    def preview(request: ExperimentRequest) -> dict:
        try:
            protocol = request.resolved()
            plan, safety = service.prepare(protocol, dry_run=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "protocol": protocol.model_dump(mode="json", by_alias=True),
            "plan": plan.model_dump(mode="json"),
            "safety": safety.model_dump(mode="json"),
        }

    @app.post("/api/experiments/execute", status_code=202)
    def execute(request: ExperimentRequest) -> dict[str, object]:
        try:
            protocol = request.resolved()
            _, safety = service.prepare(protocol, dry_run=request.dry_run)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not safety.ready:
            raise HTTPException(
                status_code=422,
                detail=safety.model_dump(mode="json"),
            )
        operation = gate.try_start(
            "experiment", "dry-run" if request.dry_run else "execute"
        )
        operation_abort_event = threading.Event()
        app.state.experiment_abort_event = operation_abort_event
        gate.run(
            operation,
            lambda _: service.execute(
                protocol,
                dry_run=request.dry_run,
                cancel_event=operation_abort_event,
            ),
        )
        return {"operation_id": operation.id, "accepted": True}

    @app.post("/api/experiments/multi-round/execute", status_code=202)
    def execute_multi_round(
        request: MultiRoundExperimentRequest,
    ) -> dict[str, object]:
        try:
            protocols = _load_multi_round_protocols(
                request.recipe_name,
                recipes_path=recipes_dir,
            )
            batch_plan = compile_multi_round(protocols)
            connected_devices: list[str] = []
            homed_devices: list[str] = []
            if not request.dry_run:
                connected_devices = registry.connect_experiment_hardware()
                homed_devices = registry.home_experiment_hardware()
            for round_index, protocol in enumerate(protocols, start=1):
                safety = validate_execution(
                    protocol,
                    compile_protocol(protocol, round_index=round_index),
                    registry,
                    coordinates,
                    dry_run=request.dry_run,
                )
                if not safety.ready:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "round": round_index,
                            "safety": safety.model_dump(mode="json"),
                        },
                    )
            final_safety = validate_action_plan(
                batch_plan.actions,
                registry,
                coordinates,
                dry_run=request.dry_run,
            )
            if not final_safety.ready:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "phase": "final-scheduled-plan",
                        "safety": final_safety.model_dump(mode="json"),
                    },
                )
        except HTTPException:
            raise
        except (OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        def run_all(_: object) -> dict[str, object]:
            result = service.execute_plan(
                batch_plan,
                dry_run=request.dry_run,
                sample_id=request.recipe_name,
                cancel_event=operation_abort_event,
            )
            if not result.success:
                detail = result.error or "unknown execution error"
                raise MultiRoundExecutionError(
                    f"多轮实验失败：{detail}",
                    f"multi-round experiment failed: {detail}",
                )
            return {
                "success": True,
                "rounds_completed": len(protocols),
                "rounds_total": len(protocols),
                "dry_run": request.dry_run,
                "recipe_name": request.recipe_name,
                "scheduled_actions": len(batch_plan.actions),
                "estimated_duration_s": batch_plan.estimated_duration_s,
            }

        operation = gate.try_start(
            "experiment",
            "multi-round-dry-run" if request.dry_run else "multi-round-execute",
        )
        operation_abort_event = threading.Event()
        app.state.experiment_abort_event = operation_abort_event
        gate.run(operation, run_all)
        return {
            "operation_id": operation.id,
            "accepted": True,
            "rounds": len(protocols),
            "recipe_name": request.recipe_name,
            "dry_run": request.dry_run,
            "auto_connected_devices": connected_devices,
            "auto_homed_devices": homed_devices,
        }
