import pytest

from AutoSpinmotorSystem.protocol import (
    compile_protocol,
    normalize_llm_json,
    protocol_from_llm_json,
    runner_recipe_from_llm_json,
    simulate_protocol,
    validate_protocol,
)


def _fa_cs_llm_recipe():
    return {
        "experiment_name": "FA-Cs PVSK film preparation",
        "operations": [
            {
                "operation": "DispenseLiquid",
                "params": {
                    "source": "FA-Cs precursor",
                    "target": "substrate_center",
                    "volume_uL": 80,
                },
            },
            {
                "operation": "SpinCoat",
                "params": {
                    "steps": [
                        {"rpm": 1000, "time_s": 10},
                        {"rpm": 4000, "time_s": 35},
                    ],
                    "antisolvent": {
                        "enabled": True,
                        "source": "chlorobenzene",
                        "volume_uL": 250,
                        "drop_at_remaining_time_s": 10,
                    },
                },
            },
            {
                "operation": "Anneal",
                "params": {"temperature_C": 100, "time_min": 30},
            },
        ],
    }


def test_llm_recipe_normalizes_to_typed_protocol_and_validates():
    protocol = protocol_from_llm_json(_fa_cs_llm_recipe())

    assert protocol.sample_id == "sample_001"
    assert protocol.description == "FA-Cs PVSK film preparation"
    assert validate_protocol(protocol).valid

    tasks = compile_protocol(protocol)
    assert tasks[0]["details"] == {
        "liquid": "perovskite_precursor",
        "volume_ul": 80,
        "target": "spin_center",
    }
    assert tasks[1]["details"]["profile"] == [
        {"speed": 1000, "duration": 10},
        {"speed": 4000, "duration": 35},
    ]
    assert tasks[1]["details"]["timed_events"] == [
        {
            "at_s": 35,
            "operation": {
                "liquid": "antisolvent",
                "volume_ul": 250,
                "target": "spin_center",
            },
        }
    ]
    assert tasks[2]["details"]["target_temp"] == 100
    assert tasks[2]["details"]["duration"] == 1800


def test_llm_recipe_exports_runner_json_with_optional_antisolvent():
    recipe = runner_recipe_from_llm_json(_fa_cs_llm_recipe())

    assert recipe["experiment_name"] == "FA-Cs PVSK film preparation"
    assert recipe["operations"][1] == {
        "operation": "SpinCoat",
        "params": {
            "direction": "forward",
            "steps": [
                {"rpm": 1000, "time_s": 10},
                {"rpm": 4000, "time_s": 35},
            ],
            "antisolvent": {
                "enabled": True,
                "source": "antisolvent",
                "volume_uL": 250,
                "drop_at_remaining_time_s": 10,
            },
        },
    }


def test_disabled_antisolvent_generates_no_timed_event():
    raw = _fa_cs_llm_recipe()
    raw["operations"][1]["params"]["antisolvent"] = {"enabled": False}

    normalized = normalize_llm_json(raw)
    assert normalized["operations"][1]["timed_events"] == []

    recipe = runner_recipe_from_llm_json(raw)
    assert recipe["operations"][1]["params"]["antisolvent"] is False


def test_enabled_antisolvent_requires_source_and_volume():
    raw = _fa_cs_llm_recipe()
    raw["operations"][1]["params"]["antisolvent"] = {"enabled": True}

    with pytest.raises(ValueError, match="enabled antisolvent requires source and volume_uL"):
        protocol_from_llm_json(raw)


def test_llm_protocol_simulation_uses_experiment_language():
    lines = simulate_protocol(protocol_from_llm_json(_fa_cs_llm_recipe()))

    assert any("Dispense 80 uL perovskite_precursor" in line for line in lines)
    assert any("at 35 s: dispense 250 uL antisolvent" in line for line in lines)
