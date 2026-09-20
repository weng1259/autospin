"""Composed startup and execution safety validation."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .actions import (
    AnnealAction,
    PrepareHeater,
    WaitForHeaterStable,
    Aspirate,
    ChangeTip,
    Dispense,
    EjectTip,
    ExperimentAction,
    MoveTool,
    PickSample,
    PlaceSample,
    SpinCoatAction,
)
from .config import get_config
from .coordinates import CoordinateRegistry, load_coordinates
from .protocol import ExperimentProtocol, validate_protocol
from .workflows import ExperimentPlan


class SafetyIssue(BaseModel):
    code: str
    message: str


class SafetyReport(BaseModel):
    ready: bool
    issues: list[SafetyIssue] = Field(default_factory=list)


def startup_validation(registry: Any, coordinates: CoordinateRegistry) -> SafetyReport:
    issues: list[SafetyIssue] = []
    try:
        config = get_config()
        if config.hardware is None:
            issues.append(SafetyIssue(code="CONFIG", message="hardware config missing"))
    except Exception as exc:
        issues.append(SafetyIssue(code="CONFIG", message=str(exc)))
    try:
        load_coordinates()
    except Exception as exc:
        issues.append(SafetyIssue(code="COORDINATES", message=str(exc)))
    for name in (
        "gantry",
        "gripper",
        "heater",
        "spincoater",
        "pipette",
        "linear_stage",
        "relay",
    ):
        if getattr(registry, name, None) is None:
            issues.append(
                SafetyIssue(code="DEVICE_MISSING", message=f"{name} unavailable")
            )
    return SafetyReport(ready=not issues, issues=issues)


def validate_execution(
    protocol: ExperimentProtocol,
    plan: ExperimentPlan,
    registry: Any,
    coordinates: CoordinateRegistry,
    *,
    dry_run: bool,
) -> SafetyReport:
    config = get_config()
    hardware = config.hardware
    issues: list[SafetyIssue] = []
    if hardware is None:
        return SafetyReport(
            ready=False,
            issues=[SafetyIssue(code="CONFIG", message="hardware config missing")],
        )
    required_devices = _required_devices(plan.actions)
    for message in validate_protocol(
        protocol,
        max_rpm=hardware.spincoater.max_rpm,
        max_volume_ul=hardware.pipette.max_volume_ul,
        max_temperature_c=hardware.heater.sv_max_c,
    ):
        issues.append(SafetyIssue(code="PARAMETER", message=message))
    for action in plan.actions:
        for coordinate, tool in _coordinates(action):
            try:
                # Process coordinates are already tool-specific taught machine
                # positions, so safety checks the stored XYZ directly.
                resolved = coordinates.resolve(
                    coordinate,
                    tool=None,
                    production=not dry_run,
                )
                if resolved.linear_stage_position_mm is not None:
                    required_devices.add("linear_stage")
            except Exception as exc:
                issues.append(
                    SafetyIssue(
                        code="COORDINATE",
                        message=f"{action.kind}: {exc}",
                    )
                )
    if "gripper" in required_devices:
        required_devices.add("relay")
    for device in required_devices:
        backend = getattr(registry, device, None)
        if backend is None:
            issues.append(
                SafetyIssue(
                    code="DEVICE_MISSING", message=f"{device} unavailable"
                )
            )
            continue
        if dry_run or getattr(registry, "mock", False):
            continue
        if device == "gripper":
            # The gripper has no independent connection; it is actuated by
            # the relay backend, whose readiness is checked separately.
            continue
        if device in {"gantry", "relay"}:
            checker = getattr(backend, "is_connected", None)
            connected = bool(checker()) if callable(checker) else False
        else:
            connected = bool(getattr(backend, "_connected", False))
        if not connected:
            issues.append(
                SafetyIssue(
                    code="DEVICE_NOT_CONNECTED",
                    message=f"{device} is not connected",
                )
            )
    if not dry_run and not getattr(registry, "mock", False):
        gantry = getattr(registry, "gantry", None)
        if gantry is not None and gantry.is_connected() and not gantry.is_homed():
            issues.append(
                SafetyIssue(
                    code="MACHINE_NOT_HOMED",
                    message="gantry is connected but has not been homed",
                )
            )
    for item in registry.serial_diagnostics():
        if item.get("locked"):
            issues.append(
                SafetyIssue(
                    code="RESOURCE_BUSY",
                    message=f"{item['resource']} busy: {item['current_operation']}",
                )
            )
    return SafetyReport(ready=not issues, issues=issues)


def validate_action_plan(
    actions: list[ExperimentAction],
    registry: Any,
    coordinates: CoordinateRegistry,
    *,
    dry_run: bool,
) -> SafetyReport:
    """Preflight the exact final action list after scheduler insertion."""
    sentinel = ExperimentProtocol.model_validate(
        {
            "sample_id": "scheduler-preflight",
            "operations": [
                {
                    "name": "Wait",
                    "duration": {"quantity": 0, "unit": "s"},
                }
            ],
        }
    )
    return validate_execution(
        sentinel,
        ExperimentPlan(sample_id=sentinel.sample_id, actions=actions),
        registry,
        coordinates,
        dry_run=dry_run,
    )


def _coordinates(action: ExperimentAction) -> list[tuple[str, str | None]]:
    if isinstance(action, MoveTool):
        return [(action.coordinate, action.tool)]
    if isinstance(action, (PickSample, PlaceSample)):
        return [(action.coordinate, "gripper")]
    if isinstance(action, (Aspirate, Dispense, ChangeTip, EjectTip)):
        return [(action.coordinate, "pipette")]
    if isinstance(action, AnnealAction) and action.place_sample:
        return [(action.coordinate, "gripper")]
    if isinstance(action, SpinCoatAction):
        return [
            (event.coordinate, "pipette") for event in action.timed_dispenses
        ]
    return []


def _required_devices(actions: list[ExperimentAction]) -> set[str]:
    result: set[str] = set()
    for action in actions:
        if _coordinates(action):
            result.add("gantry")
        if isinstance(action, (PickSample, PlaceSample)):
            result.add("gripper")
        if (
            isinstance(action, PlaceSample)
            and action.coordinate == "stations.spin_coater.operation"
        ):
            result.add("relay")
        if isinstance(action, (Aspirate, Dispense, ChangeTip, EjectTip)):
            result.add("pipette")
        if isinstance(action, SpinCoatAction):
            result.update({"spincoater", "pipette", "relay"})
        if isinstance(action, (AnnealAction, PrepareHeater, WaitForHeaterStable)):
            result.add("heater")
    return result
