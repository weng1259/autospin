"""Adapters for LLM-friendly experiment JSON.

The public LLM contract is intentionally experimental-language oriented:
operations use ``operation`` plus ``params`` and avoid gantry coordinates,
sample tray layouts, or template patch indexes. This module normalizes that
shape into the typed protocol models used by validation and compilation.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .schema import ExperimentProtocol
from .units import to_celsius, to_rpm, to_seconds, to_ul


TARGET_ALIASES = {
    "substrate_center": "spin_center",
    "sample_center": "spin_center",
    "spin_center": "spin_center",
}

LIQUID_ALIASES = {
    "fa-cs precursor": "perovskite_precursor",
    "fa-cs perovskite precursor": "perovskite_precursor",
    "perovskite precursor": "perovskite_precursor",
    "perovskite_precursor": "perovskite_precursor",
    "chlorobenzene": "antisolvent",
    "cb": "antisolvent",
    "antisolvent": "antisolvent",
}


def protocol_from_llm_json(raw: dict[str, Any]) -> ExperimentProtocol:
    """Parse LLM-friendly JSON into a typed :class:`ExperimentProtocol`.

    Accepted input shape::

        {
          "experiment_name": "FA-Cs PVSK film preparation",
          "operations": [
            {
              "operation": "DispenseLiquid",
              "params": {
                "source": "FA-Cs precursor",
                "target": "substrate_center",
                "volume_uL": 80
              }
            }
          ]
        }
    """

    return ExperimentProtocol.model_validate(normalize_llm_json(raw))


def normalize_llm_json(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert LLM-friendly operation JSON into the internal protocol shape."""

    if not isinstance(raw, dict):
        raise TypeError("LLM protocol must be a JSON object")

    operations = raw.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("LLM protocol must contain a non-empty operations list")

    protocol: dict[str, Any] = {
        "sample_id": str(raw.get("sample_id") or "sample_001"),
        "description": raw.get("description") or raw.get("experiment_name"),
        "operations": [],
    }

    for index, item in enumerate(operations, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"operation {index} must be an object")
        operation = item.get("operation") or item.get("name")
        params = item.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"operation {index} params must be an object")

        if operation == "DispenseLiquid":
            protocol["operations"].append(_normalize_dispense(params, index))
        elif operation == "SpinCoat":
            protocol["operations"].append(_normalize_spincoat(params, index))
        elif operation == "Anneal":
            protocol["operations"].append(_normalize_anneal(params, index))
        elif operation == "Wait":
            protocol["operations"].append(_normalize_wait(params, index))
        else:
            raise ValueError(f"operation {index}: unsupported operation '{operation}'")

    return protocol


def runner_recipe_from_protocol(
    protocol: ExperimentProtocol,
    *,
    experiment_name: str | None = None,
) -> dict[str, Any]:
    """Export a typed protocol to ``example_experiment.py`` recipe JSON."""

    recipe = {
        "experiment_name": experiment_name or protocol.description or protocol.sample_id,
        "operations": [],
    }
    for operation in protocol.operations:
        name = operation.name
        if name == "DispenseLiquid":
            recipe["operations"].append(
                {
                    "operation": "DispenseLiquid",
                    "params": _dispense_to_runner_params(operation),
                }
            )
        elif name == "SpinCoat":
            params = {
                "direction": "forward",
                "steps": [
                    {
                        "rpm": to_rpm(step.speed.quantity, step.speed.unit),
                        "time_s": to_seconds(step.duration.quantity, step.duration.unit),
                    }
                    for step in operation.steps
                ],
            }
            if operation.timed_events:
                if len(operation.timed_events) > 1:
                    raise ValueError("runner SpinCoat export supports at most one antisolvent event")
                total_duration = sum(step["time_s"] for step in params["steps"])
                event = operation.timed_events[0]
                event_at = to_seconds(event.at.quantity, event.at.unit)
                antisolvent = _dispense_to_runner_params(event.operation)
                params["antisolvent"] = {
                    "enabled": True,
                    "source": antisolvent["source"],
                    "volume_uL": antisolvent["volume_uL"],
                    "drop_at_remaining_time_s": max(0.0, total_duration - event_at),
                }
            else:
                params["antisolvent"] = False
            recipe["operations"].append({"operation": "SpinCoat", "params": params})
        elif name == "Anneal":
            recipe["operations"].append(
                {
                    "operation": "Anneal",
                    "params": {
                        "temperature_C": to_celsius(
                            operation.temperature.quantity,
                            operation.temperature.unit,
                        ),
                        "time_s": to_seconds(operation.duration.quantity, operation.duration.unit),
                        "hotplate": operation.target,
                    },
                }
            )
        elif name == "Wait":
            recipe["operations"].append(
                {
                    "operation": "Wait",
                    "params": {"time_s": to_seconds(operation.duration.quantity, operation.duration.unit)},
                }
            )
        else:
            raise ValueError(f"runner export does not support operation '{name}'")
    return recipe


def runner_recipe_from_llm_json(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize and export LLM-friendly JSON to executable runner JSON."""

    protocol = protocol_from_llm_json(raw)
    return runner_recipe_from_protocol(protocol, experiment_name=raw.get("experiment_name"))


def _normalize_dispense(params: dict[str, Any], index: int) -> dict[str, Any]:
    volume = _required(params, "volume_uL", index)
    source = params.get("source") or params.get("liquid")
    if source is None:
        raise ValueError(f"operation {index}: DispenseLiquid requires source")
    return {
        "name": "DispenseLiquid",
        "liquid": _normalize_liquid(str(source)),
        "volume": {"quantity": float(volume), "unit": "uL"},
        "target": _normalize_target(str(params.get("target", "spin_center"))),
    }


def _normalize_spincoat(params: dict[str, Any], index: int) -> dict[str, Any]:
    steps = params.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"operation {index}: SpinCoat requires non-empty steps")

    normalized_steps = []
    total_duration_s = 0.0
    for step_index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"operation {index} step {step_index}: step must be an object")
        rpm = _required(step, "rpm", index)
        time_s = _required(step, "time_s", index)
        duration_s = float(time_s)
        total_duration_s += duration_s
        normalized_steps.append(
            {
                "speed": {"quantity": float(rpm), "unit": "rpm"},
                "duration": {"quantity": duration_s, "unit": "s"},
            }
        )

    return {
        "name": "SpinCoat",
        "steps": normalized_steps,
        "timed_events": _normalize_antisolvent(
            params.get("antisolvent"),
            total_duration_s,
            index,
        ),
    }


def _normalize_anneal(params: dict[str, Any], index: int) -> dict[str, Any]:
    temperature = _required(params, "temperature_C", index)
    if "time_s" in params:
        duration = {"quantity": float(params["time_s"]), "unit": "s"}
    elif "time_min" in params:
        duration = {"quantity": float(params["time_min"]), "unit": "min"}
    else:
        raise ValueError(f"operation {index}: Anneal requires time_s or time_min")
    return {
        "name": "Anneal",
        "temperature": {"quantity": float(temperature), "unit": "C"},
        "duration": duration,
        "target": str(params.get("hotplate", "Hotplate1")),
    }


def _normalize_wait(params: dict[str, Any], index: int) -> dict[str, Any]:
    time_s = _required(params, "time_s", index)
    return {"name": "Wait", "duration": {"quantity": float(time_s), "unit": "s"}}


def _normalize_antisolvent(
    antisolvent: Any,
    total_duration_s: float,
    index: int,
) -> list[dict[str, Any]]:
    if antisolvent in (None, False):
        return []
    if antisolvent is True:
        raise ValueError(f"operation {index}: antisolvent=true requires source and volume_uL")
    if not isinstance(antisolvent, dict):
        raise ValueError(f"operation {index}: antisolvent must be false, null, or an object")
    if antisolvent.get("enabled", True) is False:
        return []

    source = antisolvent.get("source")
    volume = antisolvent.get("volume_uL")
    if source is None or volume is None:
        raise ValueError(f"operation {index}: enabled antisolvent requires source and volume_uL")

    remaining = float(antisolvent.get("drop_at_remaining_time_s", 0))
    at_s = max(0.0, total_duration_s - remaining)
    target = _normalize_target(str(antisolvent.get("target", "spin_center")))
    return [
        {
            "at": {"quantity": at_s, "unit": "s"},
            "operation": {
                "name": "DispenseLiquid",
                "liquid": _normalize_liquid(str(source)),
                "volume": {"quantity": float(volume), "unit": "uL"},
                "target": target,
            },
        }
    ]


def _dispense_to_runner_params(operation: Any) -> dict[str, Any]:
    return {
        "source": operation.liquid,
        "target": operation.target,
        "volume_uL": to_ul(operation.volume.quantity, operation.volume.unit),
    }


def _normalize_liquid(value: str) -> str:
    key = value.strip().lower().replace("_", " ")
    return LIQUID_ALIASES.get(key, value.strip())


def _normalize_target(value: str) -> str:
    return TARGET_ALIASES.get(value.strip().lower(), value.strip())


def _required(params: dict[str, Any], key: str, index: int) -> Any:
    if key not in params:
        raise ValueError(f"operation {index}: missing required field '{key}'")
    return deepcopy(params[key])
