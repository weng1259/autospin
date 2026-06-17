"""Typed protocol models for template-driven experiment execution.

This module is the formal contract between a human-readable experiment
template and the machine execution layer. The LLM integration can be added
later by generating this same JSON shape; the validator and compiler do not
need to know whether the JSON came from a fixed template or a model.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Quantity(BaseModel):
    """Physical quantity with an explicit unit."""

    quantity: float
    unit: str


class MoveSampleOperation(BaseModel):
    """Move a sample between named platform positions."""

    name: Literal["MoveSample"]
    from_position: str = Field(alias="from")
    to: str

    # The external JSON field is "from", but Python cannot use it as an
    # attribute name. Pydantic maps it to from_position while preserving the
    # JSON form used by experiment templates.
    model_config = ConfigDict(populate_by_name=True)


class DispenseLiquidOperation(BaseModel):
    """Dispense a registered liquid to a named target position."""

    name: Literal["DispenseLiquid"]
    liquid: str
    volume: Quantity
    target: str = "spin_center"


class SpinStep(BaseModel):
    """One constant-speed segment in a spin-coating profile."""

    speed: Quantity
    duration: Quantity


class TimedEvent(BaseModel):
    """Operation scheduled relative to the start of a SpinCoat operation."""

    at: Quantity
    operation: DispenseLiquidOperation


class SpinCoatOperation(BaseModel):
    """Spin-coating profile with optional timed liquid additions."""

    name: Literal["SpinCoat"]
    steps: list[SpinStep]
    timed_events: list[TimedEvent] = Field(default_factory=list)


class AnnealOperation(BaseModel):
    """Thermal annealing step on a named hotplate."""

    name: Literal["Anneal"]
    temperature: Quantity
    duration: Quantity
    target: str = "Hotplate1"


class WaitOperation(BaseModel):
    """Passive wait step, useful for templates and future scheduling."""

    name: Literal["Wait"]
    duration: Quantity


class MeasureOperation(BaseModel):
    """Placeholder measurement step for manually entered observations."""

    name: Literal["Measure"]
    method: str = "manual"
    target: str = "sample"


# The operation union is discriminated by "name", so Pydantic can parse each
# list item directly into the correct operation model.
Operation = Annotated[
    Union[
        MoveSampleOperation,
        DispenseLiquidOperation,
        SpinCoatOperation,
        AnnealOperation,
        WaitOperation,
        MeasureOperation,
    ],
    Field(discriminator="name"),
]


class ExperimentProtocol(BaseModel):
    """Top-level experiment protocol passed into validation and compilation."""

    sample_id: str
    description: str | None = None
    operations: list[Operation]
