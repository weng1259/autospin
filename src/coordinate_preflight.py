"""Static semantic motion preflight; never generates G-code or accesses hardware."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .coordinates import CoordinateError, CoordinateRegistry, ResolvedCoordinate


class PreflightError(CoordinateError):
    pass


class MotionPlan(BaseModel):
    names: list[str]
    points: list[ResolvedCoordinate]
    safe_travel_z_mm: float
    validated: bool = True
    notes: list[str] = Field(default_factory=list)


class RoutePreflight:
    def __init__(self, registry: CoordinateRegistry) -> None:
        self.registry = registry
        self.safe_z = registry.resolve(
            "global_safe_positions.safe_travel"
        ).xyz[2]

    def validate_named_position(
        self, name: str, *, tool: str | None = None
    ) -> MotionPlan:
        point = self.registry.resolve(name, tool=tool)
        return MotionPlan(
            names=[name],
            points=[point],
            safe_travel_z_mm=self.safe_z,
        )

    def validate_sequence(
        self,
        names: list[str],
        *,
        tool: str | None = None,
    ) -> MotionPlan:
        if not names:
            raise PreflightError("motion sequence must not be empty")
        points = [self.registry.resolve(name, tool=tool) for name in names]
        for previous, current in zip(points, points[1:]):
            xy_changes = previous.xyz[:2] != current.xyz[:2]
            if xy_changes and (
                previous.xyz[2] < self.safe_z or current.xyz[2] < self.safe_z
            ):
                raise PreflightError(
                    "unsafe XY transfer below safe travel Z: "
                    f"{previous.semantic_name} -> {current.semantic_name}"
                )
        return MotionPlan(
            names=list(names),
            points=points,
            safe_travel_z_mm=self.safe_z,
            notes=["Static semantic plan only; no GRBL commands generated."],
        )

    def validate_station_cycle(
        self,
        station_name: str,
        *,
        operation_name: str = "operation",
        tool: str | None = None,
    ) -> MotionPlan:
        prefix = f"stations.{station_name}"
        names = [
            f"{prefix}.safe_above",
            f"{prefix}.{operation_name}",
            f"{prefix}.retract",
        ]
        plan = self.validate_sequence(names, tool=tool)
        above, operation, retract = plan.points
        if not (
            above.xyz[:2] == operation.xyz[:2] == retract.xyz[:2]
            and above.xyz[2] >= self.safe_z
            and retract.xyz[2] >= self.safe_z
            and operation.xyz[2] < above.xyz[2]
        ):
            raise PreflightError(
                f"station {station_name!r} does not satisfy "
                "approach -> operation -> vertical retract"
            )
        return plan

    def plan_transfer(
        self,
        source_name: str,
        destination_name: str,
        *,
        tool: str | None = None,
    ) -> MotionPlan:
        source = self.registry.resolve(source_name, tool=tool)
        destination = self.registry.resolve(destination_name, tool=tool)
        safe_source = ResolvedCoordinate(
            semantic_name=f"{source.semantic_name}::safe_retract",
            xyz=(source.xyz[0], source.xyz[1], self.safe_z),
            source="derived from confirmed global safe travel Z",
            confidence=source.confidence,
            tool=source.tool,
        )
        safe_destination = ResolvedCoordinate(
            semantic_name=f"{destination.semantic_name}::safe_approach",
            xyz=(destination.xyz[0], destination.xyz[1], self.safe_z),
            source="derived from confirmed global safe travel Z",
            confidence=destination.confidence,
            tool=destination.tool,
        )
        points = [source, safe_source, safe_destination, destination, safe_destination]
        # Validate the only XY-changing segment explicitly.
        if safe_source.xyz[2] < self.safe_z or safe_destination.xyz[2] < self.safe_z:
            raise PreflightError("derived XY transfer is below safe travel Z")
        return MotionPlan(
            names=[p.semantic_name for p in points],
            points=points,
            safe_travel_z_mm=self.safe_z,
            notes=[
                "raise/retract vertically",
                "transfer XY at safe Z",
                "descend vertically",
                "retract before next XY transfer",
            ],
        )
