"""Typed fixed/variable parameter spaces independent of sampling strategy."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FixedParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fixed"] = "fixed"
    name: str = Field(min_length=1)
    value: Any


class VariableParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["variable"] = "variable"
    name: str = Field(min_length=1)
    value_type: Literal["float", "integer", "categorical"] = "float"
    minimum: float | None = None
    maximum: float | None = None
    choices: list[Any] | None = None

    @model_validator(mode="after")
    def validate_domain(self) -> "VariableParameter":
        if self.value_type == "categorical":
            if not self.choices:
                raise ValueError("categorical parameter requires choices")
            if self.minimum is not None or self.maximum is not None:
                raise ValueError("categorical parameter cannot define bounds")
        else:
            if self.minimum is None or self.maximum is None:
                raise ValueError("numeric parameter requires minimum and maximum")
            if self.minimum >= self.maximum:
                raise ValueError("minimum must be less than maximum")
            if self.choices is not None:
                raise ValueError("numeric parameter cannot define choices")
        return self

    def validate_value(self, value: Any) -> Any:
        if self.value_type == "categorical":
            if value not in (self.choices or []):
                raise ValueError(
                    f"{self.name} must be one of {self.choices!r}"
                )
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.name} must be numeric")
        numeric = float(value)
        assert self.minimum is not None and self.maximum is not None
        if not self.minimum <= numeric <= self.maximum:
            raise ValueError(
                f"{self.name}={numeric} outside "
                f"[{self.minimum}, {self.maximum}]"
            )
        if self.value_type == "integer":
            if not numeric.is_integer():
                raise ValueError(f"{self.name} must be an integer")
            return int(numeric)
        return numeric


class ParameterSpace(BaseModel):
    """Validation contract consumed by LHS/BO/active-learning clients."""

    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=1)
    fixed: list[FixedParameter] = Field(default_factory=list)
    variables: list[VariableParameter] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_names(self) -> "ParameterSpace":
        names = [item.name for item in [*self.fixed, *self.variables]]
        if len(names) != len(set(names)):
            raise ValueError("parameter names must be unique")
        return self

    def validate_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        variable_map = {item.name: item for item in self.variables}
        unknown = set(candidate) - set(variable_map)
        missing = set(variable_map) - set(candidate)
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing parameters: {sorted(missing)}")
        values = {item.name: item.value for item in self.fixed}
        values.update(
            {
                name: variable_map[name].validate_value(value)
                for name, value in candidate.items()
            }
        )
        return values

    def optimizer_schema(self) -> dict[str, dict[str, Any]]:
        """Framework-neutral domain metadata for external optimizers."""
        return {
            item.name: {
                "type": item.value_type,
                "minimum": item.minimum,
                "maximum": item.maximum,
                "choices": item.choices,
            }
            for item in self.variables
        }


def perovskite_parameter_space() -> ParameterSpace:
    return ParameterSpace(
        template="perovskite_basic",
        fixed=[
            FixedParameter(name="initial_spin_speed", value=1500),
            FixedParameter(name="initial_spin_time", value=10),
            FixedParameter(name="precursor_volume", value=100),
            FixedParameter(name="antisolvent_at", value=20),
            FixedParameter(name="annealing_time", value=480),
        ],
        variables=[
            VariableParameter(
                name="spin_speed", minimum=1, maximum=6000
            ),
            VariableParameter(
                name="spin_time", minimum=10, maximum=600
            ),
            VariableParameter(
                name="antisolvent", minimum=0.1, maximum=1000
            ),
            VariableParameter(
                name="annealing_temperature", minimum=0, maximum=150
            ),
        ],
    )
