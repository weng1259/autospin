from __future__ import annotations

from fastapi.testclient import TestClient
import json
from pathlib import Path
import shutil
import time

from src.coordinates import DEFAULT_COORDINATES_PATH
from src.routine import Routine, RoutineStep
from src.webapp import DeviceRegistry, create_app


TOKEN = "configuration-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path):
    coordinates = tmp_path / "coordinates.yaml"
    if not coordinates.exists():
        shutil.copyfile(DEFAULT_COORDINATES_PATH, coordinates)
    app = create_app(
        DeviceRegistry.from_mocks(),
        token=TOKEN,
        routines_path=tmp_path / "routines",
        coordinates_path=coordinates,
        recipes_path=tmp_path / "recipes",
    )
    return TestClient(app)


def test_runtime_configuration_uses_hardware_limits(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/config/runtime", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["gantry"]["soft_limits"] == {
        "x_min_mm": -310.0,
        "x_max_mm": -5.0,
        "y_min_mm": -310.0,
        "y_max_mm": -5.0,
        "z_min_mm": -110.0,
        "z_max_mm": -5.0,
    }
    assert payload["spincoater"]["max_rpm"] == 6000
    assert payload["spincoater"]["max_rpm_confirmation_required"] is False
    assert "DBLS400" in payload["spincoater"]["capability_source"]
    assert Path(payload["storage"]["recipes_dir"]) == tmp_path / "recipes"


def test_process_coordinates_are_saved_only_inside_soft_limits(tmp_path) -> None:
    with _client(tmp_path) as client:
        current = client.get(
            "/api/config/coordinates", headers=HEADERS
        ).json()
        valid_payload = current.copy()
        valid = client.put(
            "/api/config/coordinates",
            headers=HEADERS,
            json=valid_payload,
        )
        invalid_payload = valid.json()
        invalid_payload["stations"]["spin_coater"]["operation"]["xyz"] = [
            0,
            -20,
            -10,
        ]
        invalid = client.put(
            "/api/config/coordinates",
            headers=HEADERS,
            json=invalid_payload,
        )

    assert valid.status_code == 200
    assert invalid.status_code == 422


def test_legacy_process_coordinate_endpoint_is_retired(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/config/process-coordinates", headers=HEADERS
        )
    assert response.status_code == 410


def test_multi_round_generation_offsets_xyz_positions(tmp_path) -> None:
    routines = tmp_path / "routines"
    routines.mkdir()
    source = Routine(
        name="single",
        steps=[
            RoutineStep(
                seq=0,
                device="gantry",
                action="move_to",
                args=[
                    {
                        "__type__": "Position",
                        "fields": {"x_mm": -20, "y_mm": -20, "z_mm": -10},
                    }
                ],
                label="move",
            )
        ],
    )
    source.save(routines / "single.json")

    with _client(tmp_path) as client:
        response = client.post(
            "/api/routines/generate-multi-round",
            headers=HEADERS,
            json={
                "source_name": "single",
                "output_name": "three-rounds",
                "rounds": 3,
                "offset_x_mm": -10,
                "offset_y_mm": -5,
                "offset_z_mm": 0,
            },
        )

    assert response.status_code == 200
    generated = Routine.load(routines / "three-rounds.json")
    assert len(generated.steps) == 3
    assert [
        step.args[0]["fields"]["x_mm"] for step in generated.steps
    ] == [-20, -30, -40]


def test_multi_round_generation_rejects_out_of_range_round(tmp_path) -> None:
    routines = tmp_path / "routines"
    routines.mkdir()
    Routine(
        name="edge",
        steps=[
            RoutineStep(
                seq=0,
                device="gantry",
                action="move_to",
                args=[
                    {
                        "__type__": "Position",
                        "fields": {"x_mm": -300, "y_mm": -20, "z_mm": -10},
                    }
                ],
            )
        ],
    ).save(routines / "edge.json")

    with _client(tmp_path) as client:
        response = client.post(
            "/api/routines/generate-multi-round",
            headers=HEADERS,
            json={
                "source_name": "edge",
                "output_name": "unsafe",
                "rounds": 2,
                "offset_x_mm": -20,
            },
        )

    assert response.status_code == 422
    assert not (routines / "unsafe.json").exists()


def test_experiment_builder_defaults_lists_templates(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/api/experiment-builder/defaults",
            headers=HEADERS,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["template"] == "perovskite_basic"
    assert payload["default_repeats_per_group"] == 4
    assert payload["default_group"]["repeats"] == 4


def test_experiment_builder_preview_validates_without_writing(
    tmp_path,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "preview",
                "output_name": "preview_recipe.json",
                "save": False,
                "groups": [
                    {
                        "name": "A",
                        "repeats": 2,
                        "precursor_volume_ul": 100,
                        "antisolvent_volume_ul": 50,
                        "annealing_temperature_c": 120,
                        "stage_1_speed_rpm": 1500,
                        "stage_1_time_s": 10,
                        "stage_2_speed_rpm": 5000,
                        "stage_2_time_s": 20,
                        "antisolvent_delay_stage_2_s": 10,
                        "tip_height_mm": 10,
                        "annealing_time_s": 480,
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["saved"] is False
    assert payload["summary"]["operation_count"] > 0
    assert '"recipe_name":"preview_recipe.json","dry_run":true' in (
        payload["run_commands"]["mock"]
    )
    assert not (tmp_path / "recipes" / "preview_recipe.json").exists()


def test_experiment_builder_save_writes_single_recipe_file(
    tmp_path,
) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "saved",
                "output_name": "saved_recipe.json",
                "save": True,
                "groups": [
                    {
                        "name": "A",
                        "repeats": 1,
                        "precursor_volume_ul": 100,
                        "antisolvent_volume_ul": 50,
                        "annealing_temperature_c": 120,
                        "stage_1_speed_rpm": 1500,
                        "stage_1_time_s": 10,
                        "stage_2_speed_rpm": 5000,
                        "stage_2_time_s": 20,
                        "antisolvent_delay_stage_2_s": 5,
                        "tip_height_mm": 10,
                        "annealing_time_s": 480,
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    recipe_path = tmp_path / "recipes" / "saved_recipe.json"
    assert recipe_path.is_file()
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert len(recipe["protocols"]) == 1
    assert recipe["parameter_groups"][0]["repeats"] == 1
    spin = next(
        operation
        for operation in recipe["operations"]
        if operation["operation"] == "SpinCoat"
    )
    assert spin["params"]["timed_events"][0]["at"]["quantity"] == 15


def test_saved_multi_round_recipe_can_be_submitted_for_dry_run(
    tmp_path,
) -> None:
    examples = tmp_path / "recipes"
    examples.mkdir(parents=True)

    with _client(tmp_path) as client:
        generated = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "two_round_run",
                "output_name": "two_round_run.json",
                "save": True,
                "groups": [{"name": "A", "repeats": 2}],
            },
        )
        assert generated.status_code == 200
        assert generated.json()["rounds"] == 2
        assert generated.json()["recipe_name"] == "two_round_run.json"

        recipe_path = examples / "two_round_run.json"
        legacy_recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
        legacy_recipe.pop("protocols")
        recipe_path.write_text(
            json.dumps(legacy_recipe, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        accepted = client.post(
            "/api/experiments/multi-round/execute",
            headers=HEADERS,
            json={"recipe_name": "two_round_run.json", "dry_run": True},
        )
        assert accepted.status_code == 202
        assert accepted.json()["rounds"] == 2
        operation_id = accepted.json()["operation_id"]
        for _ in range(200):
            operation = client.get(
                f"/api/operations/{operation_id}",
                headers=HEADERS,
            )
            if operation.status_code == 200 and operation.json()["status"] != "running":
                break
            time.sleep(0.01)

    assert operation.json()["status"] == "succeeded"
    assert operation.json()["result"]["rounds_completed"] == 2


def test_builder_without_antisolvent_consumes_one_tip_per_round(
    tmp_path,
) -> None:
    examples = tmp_path / "recipes"
    examples.mkdir(parents=True)

    with _client(tmp_path) as client:
        generated = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "no_antisolvent",
                "output_name": "no_antisolvent.json",
                "save": True,
                "groups": [
                    {
                        "name": "A",
                        "repeats": 2,
                        "use_antisolvent": False,
                    }
                ],
            },
        )

    assert generated.status_code == 200
    recipe = json.loads(
        (examples / "no_antisolvent.json").read_text(encoding="utf-8")
    )
    assert [
        protocol["pipette_tip_slots"]["perovskite_precursor"]
        for protocol in recipe["protocols"]
    ] == [1, 2]
    assert all(
        next(
            operation
            for operation in protocol["operations"]
            if operation["name"] == "SpinCoat"
        )["timed_events"] == []
        for protocol in recipe["protocols"]
    )

def test_experiment_builder_supports_distinct_four_repeat_groups(
    tmp_path,
) -> None:
    common = {
        "repeats": 4,
        "precursor_volume_ul": 100,
        "antisolvent_volume_ul": 50,
        "annealing_temperature_c": 120,
        "stage_1_speed_rpm": 1500,
        "stage_1_time_s": 10,
        "stage_2_time_s": 20,
        "antisolvent_delay_stage_2_s": 5,
        "tip_height_mm": 10,
        "annealing_time_s": 480,
    }

    with _client(tmp_path) as client:
        response = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "two_groups",
                "output_name": "two_groups.json",
                "groups": [
                    {**common, "name": "A", "stage_2_speed_rpm": 3000},
                    {**common, "name": "B", "stage_2_speed_rpm": 5000},
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["rounds"] == 8
    assert payload["groups"][0]["round_start"] == 1
    assert payload["groups"][0]["round_end"] == 4
    assert payload["groups"][1]["round_start"] == 5
    assert payload["groups"][1]["round_end"] == 8
    assert payload["groups"][0]["antisolvent_timing_ratio"] == 0.25


def test_experiment_builder_rejects_antisolvent_after_stage_two(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/experiment-builder/recipe",
            headers=HEADERS,
            json={
                "experiment_name": "invalid_timing",
                "output_name": "invalid.json",
                "groups": [
                    {
                        "name": "A",
                        "stage_2_time_s": 20,
                        "antisolvent_delay_stage_2_s": 21,
                    }
                ],
            },
        )

    assert response.status_code == 422
