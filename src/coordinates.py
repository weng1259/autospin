"""Authoritative coordinate loading and semantic lookup.

This module is deliberately pure: it imports no Web framework, serial driver,
DeviceRegistry, or hardware backend.
"""
from __future__ import annotations

import math
import warnings
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COORDINATES_PATH = _REPO_ROOT / "config" / "coordinates.yaml"
MACHINE_FRAME = "grbl_machine_negative"
OFFSET_FRAME = "gantry_carriage_delta"
ProvenanceStatus = Literal[
    "inherited_from_source",
    "physically_verified",
    "modified_after_verification",
]
RuntimeVerification = Literal[
    "pending_runtime_validation",
    "runtime_validated",
    "runtime_validation_failed",
]


class CoordinateError(ValueError):
    """Base error for coordinate configuration and lookup."""


class CoordinateNotExecutableError(CoordinateError):
    """A coordinate exists as evidence but is not an executable target."""


class CoordinateConfirmationRequiredError(CoordinateError):
    """A coordinate or offset still requires physical confirmation."""


class UnresolvedCoordinateError(CoordinateError):
    """A requested name is explicitly unresolved or ambiguous."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise CoordinateError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class Limits(BaseModel):
    x_min_mm: float
    x_max_mm: float
    y_min_mm: float
    y_max_mm: float
    z_min_mm: float
    z_max_mm: float

    @model_validator(mode="after")
    def validate_ranges(self) -> "Limits":
        for axis in ("x", "y", "z"):
            if getattr(self, f"{axis}_min_mm") >= getattr(self, f"{axis}_max_mm"):
                raise ValueError(f"invalid {axis} limit range")
        return self

    def assert_xyz(self, xyz: tuple[float, float, float], name: str) -> None:
        for axis, value, low, high in (
            ("X", xyz[0], self.x_min_mm, self.x_max_mm),
            ("Y", xyz[1], self.y_min_mm, self.y_max_mm),
            ("Z", xyz[2], self.z_min_mm, self.z_max_mm),
        ):
            if not low <= value <= high:
                raise CoordinateError(
                    f"{name}: {axis}={value} mm outside verified [{low}, {high}]"
                )


class CoordinateSystem(BaseModel):
    version: int = Field(..., ge=1)
    units: Literal["mm"]
    frame: Literal["grbl_machine_negative"]
    axis_conventions: dict[str, str]
    homing_reference: str = Field(..., min_length=1)
    verified_limits: Limits
    calibration_date: str | None
    calibration_source: str = Field(..., min_length=1)
    requires_hardware_confirmation: bool


class Point(BaseModel):
    xyz: tuple[float, float, float]
    linear_stage_position_mm: float | None = None
    linear_stage_position_tolerance_mm: float | None = None
    unit: Literal["mm"]
    frame: Literal["grbl_machine_negative"]
    source: str = Field(..., min_length=1)
    status: ProvenanceStatus = "inherited_from_source"
    verification: RuntimeVerification = "pending_runtime_validation"
    confidence: Literal["high", "medium", "low"]
    requires_hardware_confirmation: bool
    executable: bool
    placeholder: bool = False
    notes: str = ""

    @field_validator("xyz")
    @classmethod
    def finite_xyz(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if len(value) != 3 or not all(math.isfinite(v) for v in value):
            raise ValueError("xyz must contain three finite numeric values")
        return value

    @field_validator("linear_stage_position_mm")
    @classmethod
    def finite_linear_stage_position(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("linear_stage_position_mm must be finite")
        return value

    @field_validator("linear_stage_position_tolerance_mm")
    @classmethod
    def valid_linear_stage_tolerance(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(
                "linear_stage_position_tolerance_mm must be finite and positive"
            )
        return value

    @model_validator(mode="after")
    def reject_executable_placeholder(self) -> "Point":
        if self.executable and self.placeholder:
            raise ValueError("executable coordinates cannot be placeholders")
        if self.executable and self.xyz == (0.0, 0.0, 0.0):
            raise ValueError("zero XYZ is an unsafe production placeholder")
        if self.executable and self.requires_hardware_confirmation:
            raise ValueError(
                "coordinates requiring hardware confirmation cannot be executable"
            )
        if self.executable and not self.requires_hardware_confirmation:
            self.verification = "runtime_validated"
        return self


class GridPoint(BaseModel):
    """A named point derived from a station grid origin."""

    row: int = Field(..., ge=0)
    column: int = Field(..., ge=0)
    z_mm: float | None = None
    source: str | None = None
    status: ProvenanceStatus | None = None
    verification: RuntimeVerification | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    requires_hardware_confirmation: bool | None = None
    executable: bool | None = None
    notes: str | None = None


class StationGrid(BaseModel):
    """Regular row-major grid based on one explicit station point."""

    origin_point: str = Field(..., min_length=1)
    rows: int = Field(..., ge=1)
    columns: int = Field(..., ge=1)
    step_xyz_mm: tuple[float, float, float]
    index_order: Literal["row_major"] = "row_major"
    named_points: dict[str, GridPoint] = Field(default_factory=dict)

    @field_validator("step_xyz_mm")
    @classmethod
    def finite_step(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if len(value) != 3 or not all(math.isfinite(v) for v in value):
            raise ValueError("grid step_xyz_mm must contain three finite values")
        return value

    @model_validator(mode="after")
    def validate_named_points(self) -> "StationGrid":
        for name, point in self.named_points.items():
            if point.row >= self.rows or point.column >= self.columns:
                raise ValueError(
                    f"grid point {name!r} ({point.row}, {point.column}) "
                    f"outside {self.rows}x{self.columns} grid"
                )
        return self


class Station(BaseModel):
    requires_safe_approach: bool
    grid: StationGrid | None = None
    safe_above: Point | None = None
    approach: Point | None = None
    operation: Point | None = None
    retract: Point | None = None
    pickup: Point | None = None
    place: Point | None = None
    dispense: Point | None = None
    pipette_dispense: Point | None = None
    tip_eject: Point | None = None
    pickup_2: Point | None = None
    precursor_1: Point | None = None
    precursor_2: Point | None = None
    precursor_3: Point | None = None
    precursor_4: Point | None = None
    precursor_5: Point | None = None
    antisolvent: Point | None = None

    @model_validator(mode="after")
    def validate_safe_approach(self) -> "Station":
        points = self.points()
        process_point = points.get("operation") or points.get("place")
        if self.requires_safe_approach:
            safe_above = points.get("safe_above")
            retract = points.get("retract")
            if safe_above is None or retract is None or process_point is None:
                raise ValueError(
                    "station requiring safe approach needs safe_above, "
                    "operation/place, and retract"
                )
            if (
                safe_above.xyz[:2] != process_point.xyz[:2]
                or retract.xyz[:2] != process_point.xyz[:2]
            ):
                raise ValueError("station approach/operation/retract XY must match")
            if safe_above.xyz[2] < process_point.xyz[2]:
                raise ValueError("safe_above must be vertically above the process point")
        return self

    def points(self) -> dict[str, Point]:
        result = {
            name: value
            for name in type(self).model_fields
            if name not in {"requires_safe_approach", "grid"}
            and isinstance((value := getattr(self, name)), Point)
        }
        if self.grid is None:
            return result
        try:
            origin = result[self.grid.origin_point]
        except KeyError as exc:
            raise ValueError(
                f"grid origin point {self.grid.origin_point!r} is missing"
            ) from exc
        for name, grid_point in self.grid.named_points.items():
            if name in result:
                raise ValueError(f"grid point {name!r} duplicates an explicit point")
            result[name] = self.point_at(
                grid_point.row,
                grid_point.column,
                name=name,
                overrides=grid_point,
            )
        return result

    def point_at(
        self,
        row: int,
        column: int,
        *,
        name: str,
        origin_point: str | None = None,
        overrides: GridPoint | None = None,
    ) -> Point:
        if self.grid is None:
            raise CoordinateError("station has no grid definition")
        if not (0 <= row < self.grid.rows and 0 <= column < self.grid.columns):
            raise CoordinateError(
                f"{name}: grid position ({row}, {column}) outside "
                f"{self.grid.rows}x{self.grid.columns} grid"
            )
        base_name = origin_point or self.grid.origin_point
        base = getattr(self, base_name, None)
        if not isinstance(base, Point) and base_name in self.grid.named_points:
            named = self.grid.named_points[base_name]
            base = self.point_at(
                named.row,
                named.column,
                name=base_name,
                overrides=named,
            )
        if not isinstance(base, Point):
            raise CoordinateError(f"grid origin point {base_name!r} is unavailable")
        dx, dy, dz = self.grid.step_xyz_mm
        update: dict[str, Any] = {
            "xyz": (
                base.xyz[0] + dx * column,
                base.xyz[1] + dy * row,
                base.xyz[2] + dz * row,
            ),
            "source": f"{base.source}; generated {name} from {base_name}",
        }
        if overrides is not None:
            if overrides.z_mm is not None:
                update["xyz"] = (update["xyz"][0], update["xyz"][1], overrides.z_mm)
            for field in (
                "source",
                "status",
                "verification",
                "confidence",
                "requires_hardware_confirmation",
                "executable",
                "notes",
            ):
                value = getattr(overrides, field)
                if value is not None:
                    update[field] = value
        return Point.model_validate(base.model_copy(update=update).model_dump())


class ToolOffset(BaseModel):
    tool: str = Field(..., min_length=1)
    xyz: tuple[float, float, float]
    unit: Literal["mm"]
    frame: Literal["gantry_carriage_delta"]
    source: str = Field(..., min_length=1)
    status: ProvenanceStatus = "inherited_from_source"
    verification: RuntimeVerification = "pending_runtime_validation"
    confidence: Literal["high", "medium", "low"]
    requires_hardware_confirmation: bool
    executable: bool
    tip_length_mm: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def normalize_legacy_confirmation(self) -> "ToolOffset":
        # Preserve the Phase 2 boolean contract for physically confirmed
        # deployment overrides while exposing the richer Phase 6 provenance.
        if self.executable and not self.requires_hardware_confirmation:
            self.verification = "runtime_validated"
        return self

    @field_validator("xyz")
    @classmethod
    def finite_xyz(
        cls, value: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        if len(value) != 3 or not all(math.isfinite(v) for v in value):
            raise ValueError("tool offset must contain three finite values")
        return value


class CalibrationValue(BaseModel):
    value: float
    unit: Literal["mm"]
    source: str = Field(..., min_length=1)
    confidence: Literal["high", "medium", "low"]
    requires_hardware_confirmation: bool
    notes: str = ""


class Alias(BaseModel):
    target: str = Field(..., min_length=1)
    deprecated: bool
    confidence: Literal["high", "medium", "low"]
    source: str = Field(..., min_length=1)


class UnresolvedEntry(BaseModel):
    candidates: list[Any]
    unit: str
    frames: list[str]
    source: str
    reason: str


class CoordinatesConfig(BaseModel):
    coordinate_system: CoordinateSystem
    global_safe_positions: dict[str, Point]
    stations: dict[str, Station]
    tool_offsets: dict[str, ToolOffset]
    calibration: dict[str, CalibrationValue]
    aliases: dict[str, Alias]
    unresolved_coordinates: dict[str, UnresolvedEntry]

    @model_validator(mode="after")
    def validate_all_points(self) -> "CoordinatesConfig":
        if not self.global_safe_positions or not self.stations:
            raise ValueError("production coordinate maps must not be empty")
        limits = self.coordinate_system.verified_limits
        for name, point in self.all_points().items():
            if name == "global_safe_positions.home_pull_off" and not point.executable:
                continue
            limits.assert_xyz(point.xyz, name)
        for name, offset in self.tool_offsets.items():
            if offset.tool != name:
                raise ValueError(
                    f"tool offset {name!r} must declare associated tool {name!r}"
                )
        known = set(self.all_points())
        for alias, entry in self.aliases.items():
            if alias in known:
                raise ValueError(f"alias duplicates semantic coordinate {alias!r}")
            if entry.target not in known:
                raise ValueError(f"alias {alias!r} targets missing {entry.target!r}")
        return self

    def all_points(self) -> dict[str, Point]:
        result = {
            f"global_safe_positions.{name}": point
            for name, point in self.global_safe_positions.items()
        }
        for station_name, station in self.stations.items():
            for point_name, point in station.points().items():
                result[f"stations.{station_name}.{point_name}"] = point
        return result


class ResolvedCoordinate(BaseModel):
    semantic_name: str
    xyz: tuple[float, float, float]
    unit: Literal["mm"] = "mm"
    frame: Literal["grbl_machine_negative"] = MACHINE_FRAME
    source: str
    confidence: Literal["high", "medium", "low"]
    linear_stage_position_mm: float | None = None
    linear_stage_position_tolerance_mm: float | None = None
    alias_used: str | None = None
    tool: str | None = None


def load_coordinates(path: str | Path = DEFAULT_COORDINATES_PATH) -> CoordinatesConfig:
    """Load and fully validate coordinate truth without hardware side effects."""
    p = Path(path)
    raw = yaml.load(p.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(raw, dict):
        raise CoordinateError("coordinate configuration root must be a mapping")
    return CoordinatesConfig.model_validate(raw)


class CoordinateRegistry:
    def __init__(self, config: CoordinatesConfig) -> None:
        self.config = config
        self._points = config.all_points()

    @classmethod
    def from_yaml(
        cls, path: str | Path = DEFAULT_COORDINATES_PATH
    ) -> "CoordinateRegistry":
        return cls(load_coordinates(path))

    def resolve(
        self,
        name: str,
        *,
        tool: str | None = None,
        production: bool = True,
    ) -> ResolvedCoordinate:
        alias_used: str | None = None
        if name in self.config.unresolved_coordinates:
            raise UnresolvedCoordinateError(
                f"{name!r} is unresolved: "
                f"{self.config.unresolved_coordinates[name].reason}"
            )
        if name in self.config.aliases:
            alias = self.config.aliases[name]
            if alias.deprecated:
                warnings.warn(
                    f"coordinate alias {name!r} is deprecated; use {alias.target!r}",
                    DeprecationWarning,
                    stacklevel=2,
                )
            alias_used, name = name, alias.target
        try:
            point = self._points[name]
        except KeyError as exc:
            match = re.fullmatch(r"stations\.tip_rack\.slot_(\d+)", name)
            substrate_match = re.fullmatch(
                r"stations\.substrate_rack\.slot_(\d+)", name
            )
            hotplate_match = re.fullmatch(
                r"stations\.hotplate\.slot_(\d+)\.(place|safe_above)", name
            )
            if match is not None:
                point = self._generated_tip_slot(int(match.group(1)), exc)
            elif substrate_match is not None:
                point = self._generated_substrate_slot(
                    int(substrate_match.group(1)), exc
                )
            elif hotplate_match is not None:
                point = self._generated_hotplate_slot(
                    int(hotplate_match.group(1)), hotplate_match.group(2), exc
                )
            else:
                raise CoordinateError(
                    f"unknown semantic coordinate {name!r}"
                ) from exc
        if production and point.requires_hardware_confirmation:
            raise CoordinateConfirmationRequiredError(
                f"{name!r} requires hardware confirmation"
            )
        if production and not point.executable:
            raise CoordinateNotExecutableError(f"{name!r} is not executable")
        xyz = point.xyz
        if tool is not None:
            try:
                offset = self.config.tool_offsets[tool]
            except KeyError as exc:
                raise CoordinateError(f"unknown tool offset {tool!r}") from exc
            if production and (
                not offset.executable or offset.requires_hardware_confirmation
            ):
                raise CoordinateConfirmationRequiredError(
                    f"tool offset {tool!r} is not confirmed for production"
                )
            if production and offset.verification != "runtime_validated":
                raise CoordinateConfirmationRequiredError(
                    f"tool offset {tool!r} requires runtime validation "
                    f"(status={offset.status}, "
                    f"verification={offset.verification})"
                )
            xyz = tuple(point.xyz[i] - offset.xyz[i] for i in range(3))
            self.config.coordinate_system.verified_limits.assert_xyz(
                xyz, f"{name} with {tool} offset"
            )
        return ResolvedCoordinate(
            semantic_name=name,
            xyz=xyz,
            source=point.source,
            confidence=point.confidence,
            linear_stage_position_mm=point.linear_stage_position_mm,
            linear_stage_position_tolerance_mm=(
                point.linear_stage_position_tolerance_mm
            ),
            alias_used=alias_used,
            tool=tool,
        )

    def _generated_tip_slot(self, slot: int, cause: Exception) -> Point:
        return self._generated_grid_slot("tip_rack", slot, cause)

    def _generated_substrate_slot(self, slot: int, cause: Exception) -> Point:
        return self._generated_grid_slot("substrate_rack", slot, cause)

    def _generated_hotplate_slot(
        self, slot: int, point_name: str, cause: Exception
    ) -> Point:
        return self._generated_grid_slot(
            "hotplate", slot, cause, origin_point=point_name
        )

    def _generated_grid_slot(
        self,
        station_name: str,
        slot: int,
        cause: Exception,
        *,
        origin_point: str | None = None,
    ) -> Point:
        station = self.config.stations.get(station_name)
        if station is None or station.grid is None:
            raise CoordinateError(
                f"{station_name} grid definition is unavailable"
            ) from cause
        capacity = station.grid.rows * station.grid.columns
        if not 1 <= slot <= capacity:
            raise CoordinateError(
                f"{station_name} slot {slot} is outside 1-{capacity}"
            )
        zero_based = slot - 1
        row, column = divmod(zero_based, station.grid.columns)
        point = station.point_at(
            row,
            column,
            name=f"slot_{slot}",
            origin_point=origin_point,
        )
        self.config.coordinate_system.verified_limits.assert_xyz(
            point.xyz, f"{station_name} slot {slot}"
        )
        return point
