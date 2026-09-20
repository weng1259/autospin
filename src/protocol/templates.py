"""Parameter-driven protocol factories derived from source fixed templates."""
from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema import ExperimentProtocol


class PerovskiteParameters(BaseModel):
    """Variables and fixed defaults for one perovskite protocol."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sample_id: str = Field(default="sample_001", min_length=1)
    initial_spin_speed_rpm: float = Field(default=1500, gt=0, le=6000)
    initial_spin_time_s: float = Field(default=10, gt=0)
    spin_speed_rpm: float = Field(default=5000, gt=0, le=6000)
    spin_time_s: float = Field(default=20, gt=0)
    precursor_volume_ul: float = Field(default=100, gt=0, le=1000)
    antisolvent_volume_ul: float = Field(default=50, gt=0, le=1000)
    antisolvent_at_s: float = Field(default=20, ge=0)
    annealing_temperature_c: float = Field(default=120, ge=0, le=150)
    annealing_time_s: float = Field(default=480, ge=0)
    precursor_tip_clearance_mm: float = Field(default=7, gt=0)
    antisolvent_tip_clearance_mm: float = Field(default=15, gt=0)

    @model_validator(mode="after")
    def timed_event_within_profile(self) -> "PerovskiteParameters":
        total = self.initial_spin_time_s + self.spin_time_s
        if self.antisolvent_at_s > total:
            raise ValueError(
                "antisolvent_at_s must be within the total spin profile"
            )
        return self


def create_perovskite_protocol(
    *,
    spin_speed: float = 5000,
    spin_time: float = 20,
    antisolvent_volume: float = 50,
    annealing_temperature: float = 120,
    sample_id: str = "sample_001",
    initial_spin_speed: float = 1500,
    initial_spin_time: float = 10,
    precursor_volume: float = 100,
    antisolvent_at: float = 20,
    annealing_time: float = 480,
    precursor_tip_clearance: float = 7,
    antisolvent_tip_clearance: float = 15,
    use_antisolvent: bool = True,
    precursor_tip_slot: int = 1,
    antisolvent_tip_slot: int | None = 2,
) -> ExperimentProtocol:
    """Create a validated protocol without touching runtime or hardware."""
    parameters = PerovskiteParameters(
        sample_id=sample_id,
        initial_spin_speed_rpm=initial_spin_speed,
        initial_spin_time_s=initial_spin_time,
        spin_speed_rpm=spin_speed,
        spin_time_s=spin_time,
        precursor_volume_ul=precursor_volume,
        antisolvent_volume_ul=antisolvent_volume,
        antisolvent_at_s=antisolvent_at,
        annealing_temperature_c=annealing_temperature,
        annealing_time_s=annealing_time,
        precursor_tip_clearance_mm=precursor_tip_clearance,
        antisolvent_tip_clearance_mm=antisolvent_tip_clearance,
    )
    operations: list[dict[str, object]] = [
        {
            "name": "Anneal",
            "temperature": {
                "quantity": parameters.annealing_temperature_c,
                "unit": "C",
            },
            "target": "Hotplate1",
        },
        {
            "name": "MoveSample",
            "from": "storage_tray",
            "to": "spin_center",
        },
        {
            "name": "DispenseLiquid",
            "liquid": "perovskite_precursor",
            "volume": {
                "quantity": parameters.precursor_volume_ul,
                "unit": "uL",
            },
            "target": "spin_center",
        },
        {
            "name": "SpinCoat",
            "steps": [
                {
                    "speed": {
                        "quantity": parameters.initial_spin_speed_rpm,
                        "unit": "rpm",
                    },
                    "duration": {
                        "quantity": parameters.initial_spin_time_s,
                        "unit": "s",
                    },
                },
                {
                    "speed": {
                        "quantity": parameters.spin_speed_rpm,
                        "unit": "rpm",
                    },
                    "duration": {
                        "quantity": parameters.spin_time_s,
                        "unit": "s",
                    },
                },
            ],
            "timed_events": [],
        },
        {"name": "Measure", "method": "manual_note", "target": "film"},
    ]
    if use_antisolvent:
        if antisolvent_tip_slot is None:
            raise ValueError("antisolvent_tip_slot is required when antisolvent is enabled")
        operations[3]["timed_events"] = [
            {
                "at": {
                    "quantity": parameters.antisolvent_at_s,
                    "unit": "s",
                },
                "operation": {
                    "name": "DispenseLiquid",
                    "liquid": "antisolvent",
                    "volume": {
                        "quantity": parameters.antisolvent_volume_ul,
                        "unit": "uL",
                    },
                    "target": "spin_center",
                },
            }
        ]
    return ExperimentProtocol.model_validate(
        {
            "sample_id": parameters.sample_id,
            "description": "Parameter-generated perovskite spin coating.",
            "pipette_tip_clearance_mm": {
                "precursor": parameters.precursor_tip_clearance_mm,
                "antisolvent": parameters.antisolvent_tip_clearance_mm,
            },
            "pipette_tip_slots": {
                "perovskite_precursor": precursor_tip_slot,
                **(
                    {"antisolvent": antisolvent_tip_slot}
                    if use_antisolvent
                    else {}
                ),
            },
            "hotplate_dwell": {
                "quantity": parameters.annealing_time_s,
                "unit": "s",
            },
            "operations": operations,
        }
    )


def create_spin_only_protocol(
    *,
    spin_speed: float = 1500,
    spin_time: float = 5,
    sample_id: str = "spin_test_001",
) -> ExperimentProtocol:
    if not 0 < spin_speed <= 6000:
        raise ValueError("spin_speed must be in (0, 6000]")
    if spin_time <= 0:
        raise ValueError("spin_time must be positive")
    return ExperimentProtocol.model_validate(
        {
            "sample_id": sample_id,
            "description": "Parameter-generated spin-only protocol.",
            "operations": [
                {
                    "name": "SpinCoat",
                    "steps": [
                        {
                            "speed": {"quantity": spin_speed, "unit": "rpm"},
                            "duration": {"quantity": spin_time, "unit": "s"},
                        }
                    ],
                }
            ],
        }
    )


TemplateFactory = Callable[..., ExperimentProtocol]


def _create_perovskite_group_2(**values: object) -> ExperimentProtocol:
    defaults: dict[str, object] = {
        "initial_spin_speed": 700,
        "spin_speed": 4000,
        "precursor_tip_clearance": 5,
    }
    defaults.update(values)
    return create_perovskite_protocol(**defaults)  # type: ignore[arg-type]


TEMPLATES: dict[str, TemplateFactory] = {
    "perovskite_basic": create_perovskite_protocol,
    "perovskite_group_1": create_perovskite_protocol,
    "perovskite_group_2": _create_perovskite_group_2,
    "spin_only_test": create_spin_only_protocol,
}

PEROVSKITE_GROUP_1 = create_perovskite_protocol().model_dump(
    mode="json", by_alias=True
)
PEROVSKITE_GROUP_2 = TEMPLATES["perovskite_group_2"]().model_dump(
    mode="json", by_alias=True
)


def load_template(name: str) -> ExperimentProtocol:
    try:
        factory = TEMPLATES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown template {name!r}; available: "
            f"{', '.join(sorted(TEMPLATES))}"
        ) from exc
    return factory()
