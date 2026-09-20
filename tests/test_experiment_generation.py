from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from src.coordinates import DEFAULT_COORDINATES_PATH, CoordinateRegistry
from src.experiment import (
    ExperimentGenerator,
    perovskite_parameter_space,
)
from src.experiment_service import ExperimentService
from src.protocol import (
    ExperimentProtocol,
    SpinCoat,
    create_perovskite_protocol,
    load_template,
)
from src.webapp import DeviceRegistry, create_app
from src.workflows import compile_protocol


def _candidate(speed: float, antisolvent: float) -> dict[str, float]:
    return {
        "spin_speed": speed,
        "spin_time": 20,
        "antisolvent": antisolvent,
        "annealing_temperature": 120,
    }


def test_parameter_factory_preserves_source_defaults() -> None:
    protocol = create_perovskite_protocol()
    source_default = load_template("perovskite_basic")
    assert protocol.operations == source_default.operations
    spin = next(
        operation
        for operation in protocol.operations
        if isinstance(operation, SpinCoat)
    )
    assert spin.steps[1].speed.quantity == 5000
    assert spin.timed_events[0].operation.volume.quantity == 50


def test_optional_antisolvent_removes_second_tip_and_timed_addition() -> None:
    protocol = create_perovskite_protocol(
        use_antisolvent=False,
        precursor_tip_slot=2,
        antisolvent_tip_slot=None,
    )
    plan = compile_protocol(protocol)

    tip_changes = [action for action in plan.actions if action.kind == "ChangeTip"]
    assert [action.coordinate for action in tip_changes] == [
        "stations.tip_rack.slot_2"
    ]
    assert not any(
        action.kind == "Aspirate" and action.liquid == "antisolvent"
        for action in plan.actions
    )
    spin = next(action for action in plan.actions if action.kind == "SpinCoat")
    assert spin.timed_dispenses == []


def test_parameter_factory_validates_inputs() -> None:
    with pytest.raises(ValueError):
        create_perovskite_protocol(spin_speed=6000.1)
    with pytest.raises(ValueError, match="within the total spin profile"):
        create_perovskite_protocol(
            initial_spin_time=1,
            spin_time=1,
            antisolvent_at=20,
        )


def test_batch_generation_matches_requested_parameters() -> None:
    generator = ExperimentGenerator(perovskite_parameter_space())
    generated = generator.generate_batch(
        [_candidate(3000, 100), _candidate(5000, 200)]
    )
    assert len(generated) == 2
    assert generated[0].experiment_id != generated[1].experiment_id
    assert generated[0].parameters["spin_speed"] == 3000
    assert generated[1].parameters["antisolvent"] == 200
    assert all(isinstance(item.protocol, ExperimentProtocol) for item in generated)


def test_external_candidate_provider_interface() -> None:
    class FakeLhsProvider:
        def candidates(self, space, count):
            assert "spin_speed" in space.optimizer_schema()
            return [_candidate(2000 + index * 100, 50) for index in range(count)]

    generated = ExperimentGenerator(
        perovskite_parameter_space()
    ).generate_from_provider(FakeLhsProvider(), 3)
    assert len(generated) == 3


def test_generated_protocol_compiles_and_dry_runs() -> None:
    protocol = ExperimentGenerator(perovskite_parameter_space()).generate(
        _candidate(4500, 75)
    ).protocol
    assert compile_protocol(protocol).actions
    result = ExperimentService(
        DeviceRegistry.from_mocks(),
        CoordinateRegistry.from_yaml(),
        sleep=lambda _: None,
    ).execute(protocol, dry_run=True)
    assert result.success is True


def test_web_template_batch_preview(tmp_path) -> None:
    coordinates = tmp_path / "coordinates.yaml"
    shutil.copyfile(DEFAULT_COORDINATES_PATH, coordinates)
    app = create_app(
        DeviceRegistry.from_mocks(),
        token="test",
        coordinates_path=coordinates,
        routines_path=tmp_path / "routines",
    )
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        templates = client.get("/api/experiment-templates", headers=headers)
        assert templates.status_code == 200
        assert "spin_speed" in templates.json()["perovskite_basic"]["variables"]
        preview = client.post(
            "/api/experiment-templates/perovskite_basic/batch-preview",
            headers=headers,
            json={
                "candidates": [
                    _candidate(3000, 100),
                    _candidate(5000, 200),
                ]
            },
        )
    assert preview.status_code == 200
    assert preview.json()["count"] == 2
    assert all(
        item["safety"]["ready"] for item in preview.json()["experiments"]
    )
