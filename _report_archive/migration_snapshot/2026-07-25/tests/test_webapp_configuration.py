from __future__ import annotations

from fastapi.testclient import TestClient

from src.routine import Routine, RoutineStep
from src.webapp import DeviceRegistry, create_app


TOKEN = "configuration-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path):
    app = create_app(
        DeviceRegistry.from_mocks(),
        token=TOKEN,
        routines_path=tmp_path / "routines",
        coordinates_path=tmp_path / "process_coordinates.yaml",
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
    assert payload["spincoater"]["max_rpm"] == 3000


def test_process_coordinates_are_saved_only_inside_soft_limits(tmp_path) -> None:
    with _client(tmp_path) as client:
        valid = client.put(
            "/api/config/process-coordinates",
            headers=HEADERS,
            json={"points": {"spin": {"x_mm": -20, "y_mm": -20, "z_mm": -10}}},
        )
        invalid = client.put(
            "/api/config/process-coordinates",
            headers=HEADERS,
            json={"points": {"bad": {"x_mm": 0, "y_mm": -20, "z_mm": -10}}},
        )

    assert valid.status_code == 200
    assert invalid.status_code == 422


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
