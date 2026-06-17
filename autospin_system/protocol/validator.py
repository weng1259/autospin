"""Validation layer for structured experiment protocols.

This is the safety gate between a template or future LLM output and real
hardware execution. It checks semantic assumptions that Pydantic cannot know,
such as available liquids, valid platform positions, hardware limits, and
timed-event consistency.
"""

from dataclasses import dataclass, field

try:
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ImportError:
    from config.hardware_config import CONFIG

from .inventory import load_reagent_inventory
from .schema import (
    AnnealOperation,
    DispenseLiquidOperation,
    ExperimentProtocol,
    MeasureOperation,
    MoveSampleOperation,
    SpinCoatOperation,
    WaitOperation,
)
from .units import to_celsius, to_rpm, to_seconds, to_ul


@dataclass
class ValidationResult:
    """Human-readable validation result used by CLI and future UI layers."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _position_names() -> set[str]:
    """Collect position-like names from config plus logical platform aliases."""

    names = set(CONFIG.get("geometry", {}).get("lab_coordinates", {}).keys())
    names.update(CONFIG.get("system", {}).get("default_worker_capacity", {}).keys())
    names.update({"Hotplate1", "Tray1", "Tray2", "spincoater", "storage_tray"})
    return {name.lower() for name in names}


def validate_protocol(protocol: ExperimentProtocol) -> ValidationResult:
    """Check whether a protocol is safe enough to compile and execute."""

    errors: list[str] = []
    warnings: list[str] = []
    inventory = load_reagent_inventory()
    liquids = inventory.get("liquids", {})
    positions = _position_names()
    max_volume_ul = CONFIG.get("devices", {}).get("pipette", {}).get("max_volume_ul", 1000)
    max_rpm = CONFIG.get("devices", {}).get("spin_motor", {}).get("max_rpm", 6000)

    for index, operation in enumerate(protocol.operations, start=1):
        prefix = f"operation {index} ({operation.name})"

        # Each branch performs semantic checks for one operation type. These
        # checks run before compilation so invalid tasks never reach Maestro
        # or any hardware controller.
        if isinstance(operation, MoveSampleOperation):
            for value in (operation.from_position, operation.to):
                if value.lower() not in positions:
                    errors.append(f"{prefix}: unknown position '{value}'")

        elif isinstance(operation, DispenseLiquidOperation):
            _validate_dispense(operation, prefix, liquids, positions, max_volume_ul, errors)

        elif isinstance(operation, SpinCoatOperation):
            spin_duration = 0.0
            if not operation.steps:
                errors.append(f"{prefix}: SpinCoat requires at least one step")
            for step_i, step in enumerate(operation.steps, start=1):
                try:
                    speed = to_rpm(step.speed.quantity, step.speed.unit)
                    if speed < 0 or speed > max_rpm:
                        errors.append(
                            f"{prefix} step {step_i}: speed {speed:g} rpm exceeds 0-{max_rpm} rpm"
                        )
                except ValueError as exc:
                    errors.append(f"{prefix} step {step_i}: {exc}")
                try:
                    duration = to_seconds(step.duration.quantity, step.duration.unit)
                    spin_duration += duration
                    if duration <= 0:
                        errors.append(f"{prefix} step {step_i}: duration must be positive")
                except ValueError as exc:
                    errors.append(f"{prefix} step {step_i}: {exc}")

            # Timed additions are specified relative to the SpinCoat start.
            # They must fall within the total profile duration.
            for event_i, event in enumerate(operation.timed_events, start=1):
                try:
                    event_time = to_seconds(event.at.quantity, event.at.unit)
                    if event_time < 0 or event_time > spin_duration:
                        errors.append(
                            f"{prefix} timed event {event_i}: time {event_time:g}s is outside "
                            f"spin duration {spin_duration:g}s"
                        )
                except ValueError as exc:
                    errors.append(f"{prefix} timed event {event_i}: {exc}")
                _validate_dispense(
                    event.operation,
                    f"{prefix} timed event {event_i}",
                    liquids,
                    positions,
                    max_volume_ul,
                    errors,
                )

        elif isinstance(operation, AnnealOperation):
            try:
                temp_c = to_celsius(operation.temperature.quantity, operation.temperature.unit)
                if temp_c < 20 or temp_c > 250:
                    warnings.append(f"{prefix}: anneal temperature {temp_c:g} C is outside 20-250 C")
            except ValueError as exc:
                errors.append(f"{prefix}: {exc}")
            try:
                duration = to_seconds(operation.duration.quantity, operation.duration.unit)
                if duration <= 0:
                    errors.append(f"{prefix}: duration must be positive")
            except ValueError as exc:
                errors.append(f"{prefix}: {exc}")
            if operation.target.lower() not in positions and not operation.target.lower().startswith("hotplate"):
                errors.append(f"{prefix}: unknown hotplate target '{operation.target}'")

        elif isinstance(operation, WaitOperation):
            try:
                duration = to_seconds(operation.duration.quantity, operation.duration.unit)
                if duration < 0:
                    errors.append(f"{prefix}: duration must not be negative")
            except ValueError as exc:
                errors.append(f"{prefix}: {exc}")

        elif isinstance(operation, MeasureOperation):
            warnings.append(f"{prefix}: Measure is recorded as a placeholder action")

    return ValidationResult(valid=not errors, errors=errors, warnings=warnings)


def _validate_dispense(
    operation: DispenseLiquidOperation,
    prefix: str,
    liquids: dict,
    positions: set[str],
    max_volume_ul: float,
    errors: list[str],
) -> None:
    """Validate liquid name, volume range, and target position for dispensing."""

    if operation.liquid not in liquids:
        errors.append(f"{prefix}: unknown liquid '{operation.liquid}'")
    try:
        volume_ul = to_ul(operation.volume.quantity, operation.volume.unit)
        if volume_ul <= 0 or volume_ul > max_volume_ul:
            errors.append(
                f"{prefix}: volume {volume_ul:g} uL exceeds pipette range 0-{max_volume_ul} uL"
            )
    except ValueError as exc:
        errors.append(f"{prefix}: {exc}")
    if operation.target.lower() not in positions:
        errors.append(f"{prefix}: unknown target '{operation.target}'")
