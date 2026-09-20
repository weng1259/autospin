from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.coordinate_preflight import PreflightError, RoutePreflight
from src.coordinates import (
    CoordinateConfirmationRequiredError,
    CoordinateError,
    CoordinateRegistry,
    UnresolvedCoordinateError,
    load_coordinates,
)


PRODUCTION_PATH = Path(__file__).resolve().parents[1] / "config" / "coordinates.yaml"


def _raw() -> dict[str, Any]:
    value = yaml.safe_load(PRODUCTION_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "coordinates.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_production_coordinates_load_with_verified_limits() -> None:
    config = load_coordinates()
    limits = config.coordinate_system.verified_limits

    assert config.coordinate_system.units == "mm"
    assert config.coordinate_system.frame == "grbl_machine_negative"
    assert (
        limits.x_min_mm,
        limits.x_max_mm,
        limits.y_min_mm,
        limits.y_max_mm,
        limits.z_min_mm,
        limits.z_max_mm,
    ) == (-310.0, -5.0, -310.0, -5.0, -110.0, -5.0)
    assert len(config.all_points()) == 19


def test_empty_coordinate_map_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["stations"] = {}
    with pytest.raises(ValueError, match="must not be empty"):
        load_coordinates(_write(tmp_path, raw))


def test_zero_placeholder_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["stations"]["spin_coater"]["operation"]["xyz"] = [0, 0, 0]
    with pytest.raises(ValueError, match="zero XYZ"):
        load_coordinates(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("axis", "value"),
    [(0, -311.0), (1, -4.0), (2, -111.0), (2, -4.0)],
)
def test_out_of_range_xyz_rejected(
    tmp_path: Path, axis: int, value: float
) -> None:
    raw = _raw()
    raw["stations"]["substrate_rack"]["pickup"]["xyz"][axis] = value
    with pytest.raises(ValueError, match="outside verified"):
        load_coordinates(_write(tmp_path, raw))


def test_missing_unit_rejected(tmp_path: Path) -> None:
    raw = _raw()
    del raw["stations"]["spin_coater"]["operation"]["unit"]
    with pytest.raises(ValueError, match="unit"):
        load_coordinates(_write(tmp_path, raw))


def test_unknown_frame_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["coordinate_system"]["frame"] = "unknown_frame"
    with pytest.raises(ValueError, match="frame"):
        load_coordinates(_write(tmp_path, raw))


def test_unresolved_coordinate_rejected_in_production(tmp_path: Path) -> None:
    raw = _raw()
    raw["unresolved_coordinates"]["legacy_positive_spin_center"] = {
        "candidates": [[50.0, 50.0, 5.0], [-101.0, -8.5, -30.0]],
        "unit": "mm",
        "frames": ["legacy_lab_positive", "grbl_machine_negative"],
        "source": "test legacy configuration",
        "reason": "Different frames",
    }
    registry = CoordinateRegistry.from_yaml(_write(tmp_path, raw))
    with pytest.raises(UnresolvedCoordinateError, match="Different frames"):
        registry.resolve("legacy_positive_spin_center")


def test_confirmation_required_coordinate_rejected() -> None:
    registry = CoordinateRegistry.from_yaml()
    with pytest.raises(CoordinateConfirmationRequiredError):
        registry.resolve("stations.reagent_rack.precursor_2")


def test_confirmed_tool_offset_is_applied(tmp_path: Path) -> None:
    raw = _raw()
    raw["tool_offsets"]["pipette"] = {
        "tool": "pipette",
        "xyz": [71.5, 35.0, 20.5],
        "unit": "mm",
        "frame": "gantry_carriage_delta",
        "source": "test calibration",
        "verification": "runtime_validated",
        "confidence": "high",
        "requires_hardware_confirmation": False,
        "executable": True,
    }
    registry = CoordinateRegistry.from_yaml(_write(tmp_path, raw))

    result = registry.resolve("stations.spin_coater.operation", tool="pipette")

    assert result.xyz == (-84.5, -75.0, -104.5)
    assert result.tool == "pipette"


def test_offset_induced_out_of_range_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["tool_offsets"]["pipette"] = {
        "tool": "pipette",
        "xyz": [500.0, 0.0, 0.0],
        "unit": "mm",
        "frame": "gantry_carriage_delta",
        "source": "test calibration",
        "verification": "runtime_validated",
        "confidence": "high",
        "requires_hardware_confirmation": False,
        "executable": True,
    }
    registry = CoordinateRegistry.from_yaml(_write(tmp_path, raw))

    with pytest.raises(CoordinateError, match="outside verified"):
        registry.resolve("stations.spin_coater.operation", tool="pipette")


def test_station_approach_operation_retract_validation() -> None:
    plan = RoutePreflight(
        CoordinateRegistry.from_yaml()
    ).validate_station_cycle("spin_coater")

    assert plan.validated is True
    assert [point.xyz[2] for point in plan.points] == [-30.0, -84.0, -30.0]


def test_confirmed_rack_offsets_match_calibrated_grid_steps() -> None:
    config = CoordinateRegistry.from_yaml().config
    tip = config.stations["tip_rack"]
    reagent = config.stations["reagent_rack"]

    assert tip.pickup is not None
    tip_points = tip.points()
    assert "pickup_2" in tip_points
    assert tuple(
        tip_points["pickup_2"].xyz[index] - tip.pickup.xyz[index]
        for index in range(3)
    ) == (9.0, 0.0, 0.0)

    registry = CoordinateRegistry.from_yaml()
    assert registry.resolve(
        "stations.tip_rack.slot_2", production=False
    ).xyz == (-65.5, -92.0, -80.0)
    assert registry.resolve(
        "stations.tip_rack.slot_9", production=False
    ).xyz == (-74.5, -101.0, -80.0)

    reagent_xyz = {
        name: point.xyz
        for name, point in reagent.points().items()
    }
    assert reagent_xyz == {
        "precursor_1": (-186.0, -15.0, -80.0),
        "precursor_2": (-226.0, -15.0, -80.0),
        "precursor_3": (-226.0, -55.0, -80.0),
        "precursor_4": (-266.0, -15.0, -80.0),
        "precursor_5": (-266.0, -55.0, -80.0),
        "antisolvent": (-186.0, -55.0, -80.0),
    }


def test_changing_only_grid_origins_translates_all_derived_points(
    tmp_path: Path,
) -> None:
    raw = _raw()
    raw["stations"]["tip_rack"]["pickup"]["xyz"] = [-70.0, -90.0, -81.0]
    raw["stations"]["substrate_rack"]["pickup"]["xyz"] = [
        -140.0,
        -170.0,
        -75.0,
    ]
    raw["stations"]["reagent_rack"]["precursor_1"]["xyz"] = [
        -180.0,
        -10.0,
        -79.0,
    ]
    raw["stations"]["spin_coater"]["operation"]["xyz"] = [
        -15.0,
        -42.0,
        -85.0,
    ]
    raw["stations"]["hotplate"]["place"]["xyz"] = [-290.0, -8.0, -100.0]

    registry = CoordinateRegistry.from_yaml(_write(tmp_path, raw))

    assert registry.resolve("stations.tip_rack.slot_9").xyz == (-70.0, -99.0, -81.0)
    assert registry.resolve("stations.substrate_rack.slot_16").xyz == (
        -278.99,
        -242.99,
        -74.1,
    )
    assert registry.resolve(
        "stations.reagent_rack.antisolvent"
    ).xyz == (-180.0, -50.0, -79.0)
    assert registry.resolve("stations.spin_coater.safe_above").xyz == (
        -15.0,
        -42.0,
        -30.0,
    )
    assert registry.resolve("stations.hotplate.slot_9.place").xyz == (
        -240.0,
        -48.0,
        -100.0,
    )
    assert registry.resolve("stations.hotplate.slot_9.safe_above").xyz == (
        -240.0,
        -48.0,
        -30.0,
    )


def test_safe_z_before_xy_transfer_is_planned() -> None:
    preflight = RoutePreflight(CoordinateRegistry.from_yaml())
    plan = preflight.plan_transfer(
        "stations.substrate_rack.pickup",
        "stations.spin_coater.operation",
    )

    assert plan.points[1].xyz[2] == -30.0
    assert plan.points[2].xyz[2] == -30.0
    assert plan.points[1].xyz[:2] != plan.points[2].xyz[:2]


def test_unsafe_xy_at_low_z_rejected() -> None:
    preflight = RoutePreflight(CoordinateRegistry.from_yaml())
    with pytest.raises(PreflightError, match="unsafe XY transfer"):
        preflight.validate_sequence(
            [
                "stations.substrate_rack.pickup",
                "stations.spin_coater.operation",
            ]
        )


def test_legacy_alias_resolution_warns() -> None:
    registry = CoordinateRegistry.from_yaml()
    with pytest.warns(DeprecationWarning, match="deprecated"):
        result = registry.resolve("first_glass_pick")

    assert result.semantic_name == "stations.substrate_rack.pickup"
    assert result.alias_used == "first_glass_pick"


def test_ambiguous_legacy_name_is_not_aliased() -> None:
    registry = CoordinateRegistry.from_yaml()
    assert "legacy_positive_spin_center" not in registry.config.aliases
    with pytest.raises(CoordinateError, match="unknown semantic coordinate"):
        registry.resolve("legacy_positive_spin_center")


def test_duplicate_station_name_rejected(tmp_path: Path) -> None:
    text = PRODUCTION_PATH.read_text(encoding="utf-8")
    marker = "  hotplate:\n"
    duplicated = text.replace(marker, "  spin_coater:\n" + marker, 1)
    path = tmp_path / "coordinates.yaml"
    path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(CoordinateError, match="duplicate YAML key"):
        load_coordinates(path)


def test_loading_and_preflight_do_not_access_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("coordinate preflight attempted serial access")

    monkeypatch.setattr("serial.Serial", forbidden)
    registry = CoordinateRegistry.from_yaml()
    plan = RoutePreflight(registry).plan_transfer(
        "stations.tip_rack.pickup",
        "stations.waste.tip_eject",
    )

    assert plan.validated is True
