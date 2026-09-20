"""Validated experiment protocol models migrated from AutoSpinmotorSystem."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Quantity(BaseModel):
    quantity: float
    unit: str


class MoveSample(BaseModel):
    name: Literal["MoveSample"]
    from_position: str = Field(alias="from")
    to: str
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class DispenseLiquid(BaseModel):
    name: Literal["DispenseLiquid"]
    liquid: str
    volume: Quantity
    target: str = "spin_center"


class SpinStep(BaseModel):
    speed: Quantity
    duration: Quantity


class TimedEvent(BaseModel):
    at: Quantity
    operation: DispenseLiquid


class SpinCoat(BaseModel):
    name: Literal["SpinCoat"]
    steps: list[SpinStep]
    timed_events: list[TimedEvent] = Field(default_factory=list)


class Anneal(BaseModel):
    name: Literal["Anneal"]
    temperature: Quantity
    duration: Quantity | None = None
    target: str = "Hotplate1"


class Wait(BaseModel):
    name: Literal["Wait"]
    duration: Quantity


class Measure(BaseModel):
    name: Literal["Measure"]
    method: str = "manual"
    target: str = "sample"


ProtocolOperation = Annotated[
    MoveSample | DispenseLiquid | SpinCoat | Anneal | Wait | Measure,
    Field(discriminator="name"),
]


class ExperimentProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sample_id: str = Field(min_length=1)
    description: str | None = None
    pipette_tip_clearance_mm: dict[str, float] | None = None
    pipette_tip_slots: dict[str, int] | None = None
    hotplate_dwell: Quantity | None = None
    operations: list[ProtocolOperation] = Field(min_length=1)


def to_seconds(value: Quantity) -> float:
    factor = {"s": 1.0, "sec": 1.0, "min": 60.0}.get(value.unit)
    if factor is None:
        raise ValueError(f"unsupported time unit {value.unit!r}")
    return value.quantity * factor


def to_ul(value: Quantity) -> float:
    factor = {"uL": 1.0, "ul": 1.0, "mL": 1000.0}.get(value.unit)
    if factor is None:
        raise ValueError(f"unsupported volume unit {value.unit!r}")
    return value.quantity * factor


def to_rpm(value: Quantity) -> float:
    if value.unit != "rpm":
        raise ValueError(f"unsupported speed unit {value.unit!r}")
    return value.quantity


def to_celsius(value: Quantity) -> float:
    if value.unit not in {"C", "°C"}:
        raise ValueError(f"unsupported temperature unit {value.unit!r}")
    return value.quantity


def validate_protocol(
    protocol: ExperimentProtocol,
    *,
    max_rpm: float,
    max_volume_ul: float,
    max_temperature_c: float,
) -> list[str]:
    errors: list[str] = []
    for index, operation in enumerate(protocol.operations, 1):
        prefix = f"operation {index} ({operation.name})"
        if isinstance(operation, DispenseLiquid):
            volume = to_ul(operation.volume)
            if not 0 < volume <= max_volume_ul:
                errors.append(f"{prefix}: volume outside 0-{max_volume_ul} uL")
        elif isinstance(operation, SpinCoat):
            total = 0.0
            for step in operation.steps:
                rpm, duration = to_rpm(step.speed), to_seconds(step.duration)
                if not 0 <= rpm <= max_rpm:
                    errors.append(f"{prefix}: speed outside 0-{max_rpm} rpm")
                if duration <= 0:
                    errors.append(f"{prefix}: duration must be positive")
                total += duration
            for event in operation.timed_events:
                when = to_seconds(event.at)
                if not 0 <= when <= total:
                    errors.append(f"{prefix}: timed event outside spin duration")
        elif isinstance(operation, Anneal):
            temperature = to_celsius(operation.temperature)
            if not 0 <= temperature <= max_temperature_c:
                errors.append(
                    f"{prefix}: temperature outside 0-{max_temperature_c} C"
                )
        elif isinstance(operation, Wait) and to_seconds(operation.duration) < 0:
            errors.append(f"{prefix}: duration must not be negative")
    return errors
