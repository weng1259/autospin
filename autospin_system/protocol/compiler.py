"""Compile validated experiment protocols into Maestro task dictionaries.

The schema layer uses readable operation names such as "SpinCoat"; Maestro
currently consumes lower-level task names such as "spincoat". This compiler is
the only place that should know how to translate between those two worlds.
"""

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


def compile_protocol(protocol: ExperimentProtocol) -> list[dict]:
    """Convert a validated ExperimentProtocol into executable Maestro tasks."""

    tasks: list[dict] = []
    for operation in protocol.operations:
        if isinstance(operation, MoveSampleOperation):
            tasks.append(
                {
                    "name": "move_sample",
                    "sample": protocol.sample_id,
                    "details": {
                        "from": operation.from_position,
                        "to": operation.to,
                    },
                }
            )
        elif isinstance(operation, DispenseLiquidOperation):
            tasks.append(
                {
                    "name": "dispense_liquid",
                    "sample": protocol.sample_id,
                    "details": _compile_dispense(operation),
                }
            )
        elif isinstance(operation, SpinCoatOperation):
            # Timed events remain nested inside the spincoat task because they
            # must be scheduled relative to the motor profile, not as separate
            # top-level tasks.
            tasks.append(
                {
                    "name": "spincoat",
                    "sample": protocol.sample_id,
                    "details": {
                        "profile": [
                            {
                                "speed": to_rpm(step.speed.quantity, step.speed.unit),
                                "duration": to_seconds(step.duration.quantity, step.duration.unit),
                            }
                            for step in operation.steps
                        ],
                        "timed_events": [
                            {
                                "at_s": to_seconds(event.at.quantity, event.at.unit),
                                "operation": _compile_dispense(event.operation),
                            }
                            for event in operation.timed_events
                        ],
                    },
                }
            )
        elif isinstance(operation, AnnealOperation):
            tasks.append(
                {
                    "name": "anneal",
                    "sample": protocol.sample_id,
                    "details": {
                        "target_temp": to_celsius(
                            operation.temperature.quantity,
                            operation.temperature.unit,
                        ),
                        "duration": to_seconds(operation.duration.quantity, operation.duration.unit),
                        "hotplate": operation.target,
                    },
                }
            )
        elif isinstance(operation, WaitOperation):
            tasks.append(
                {
                    "name": "wait",
                    "sample": protocol.sample_id,
                    "details": {
                        "duration": to_seconds(operation.duration.quantity, operation.duration.unit)
                    },
                }
            )
        elif isinstance(operation, MeasureOperation):
            tasks.append(
                {
                    "name": "measure",
                    "sample": protocol.sample_id,
                    "details": {"method": operation.method, "target": operation.target},
                }
            )
    return tasks


def _compile_dispense(operation: DispenseLiquidOperation) -> dict:
    """Normalize a dispense operation into the worker's expected detail shape."""

    return {
        "liquid": operation.liquid,
        "volume_ul": to_ul(operation.volume.quantity, operation.volume.unit),
        "target": operation.target,
    }
