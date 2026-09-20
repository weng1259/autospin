"""Compile validated protocols into semantic experiment actions."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .actions import (
    AnnealAction,
    Aspirate,
    ChangeTip,
    Complete,
    Dispense,
    EjectTip,
    ExperimentAction,
    PickSample,
    PlaceSample,
    SpinCoatAction,
    SpinSegment,
    TimedDispense,
    WaitAction,
)
from .protocol import (
    Anneal,
    DispenseLiquid,
    ExperimentProtocol,
    Measure,
    MoveSample,
    SpinCoat,
    Wait,
    to_celsius,
    to_rpm,
    to_seconds,
    to_ul,
)

_SAMPLE_COORDINATES = {
    "storage_tray": "stations.substrate_rack.pickup",
    "spin_center": "stations.spin_coater.operation",
    "Hotplate1": "stations.hotplate.place",
}
_LIQUID_COORDINATES = {
    "perovskite_precursor": "stations.reagent_rack.precursor_1",
    "antisolvent": "stations.reagent_rack.antisolvent",
}
_DISPENSE_COORDINATES = {
    "spin_center": "stations.spin_coater.pipette_dispense",
}


class ExperimentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    description: str | None = None
    actions: list[ExperimentAction] = Field(min_length=1)


def compile_protocol(
    protocol: ExperimentProtocol, *, round_index: int | None = None
) -> ExperimentPlan:
    """Preserve source operation order while expanding semantic primitives."""
    actions: list[ExperimentAction] = []
    anneal: Anneal | None = None
    completion_note = "experiment complete"
    for operation in protocol.operations:
        if isinstance(operation, Anneal):
            anneal = operation
            actions.append(
                AnnealAction(
                    temperature_c=to_celsius(operation.temperature),
                    duration_s=0,
                    place_sample=False,
                )
            )
        elif isinstance(operation, MoveSample):
            try:
                source = _SAMPLE_COORDINATES[operation.from_position]
                destination = _SAMPLE_COORDINATES[operation.to]
            except KeyError as exc:
                raise ValueError(f"unknown sample location {exc.args[0]!r}") from exc
            if operation.from_position == "storage_tray":
                source = _substrate_coordinate(round_index)
            actions.extend(
                [PickSample(coordinate=source), PlaceSample(coordinate=destination)]
            )
        elif isinstance(operation, DispenseLiquid):
            actions.extend(
                _liquid_actions(
                    operation,
                    protocol.pipette_tip_slots,
                    protocol.pipette_tip_clearance_mm,
                    round_index,
                )
            )
        elif isinstance(operation, SpinCoat):
            # Source behavior prepares timed additions before the spin profile.
            for event in operation.timed_events:
                source = _liquid_source(event.operation.liquid)
                actions.append(
                    ChangeTip(
                        coordinate=_tip_coordinate(
                            event.operation.liquid,
                            protocol.pipette_tip_slots,
                        )
                    )
                )
                actions.append(
                    Aspirate(
                        liquid=event.operation.liquid,
                        volume_ul=to_ul(event.operation.volume),
                        coordinate=source,
                    )
                )
            actions.append(
                SpinCoatAction(
                    segments=[
                        SpinSegment(
                            rpm=to_rpm(step.speed),
                            duration_s=to_seconds(step.duration),
                        )
                        for step in operation.steps
                    ],
                    timed_dispenses=[
                        TimedDispense(
                            at_s=to_seconds(event.at),
                            liquid=event.operation.liquid,
                            volume_ul=to_ul(event.operation.volume),
                            coordinate=_dispense_target(event.operation.target),
                            clearance_mm=_tip_clearance(
                                event.operation.liquid,
                                protocol.pipette_tip_clearance_mm,
                            ),
                        )
                        for event in operation.timed_events
                    ],
                )
            )
            actions.append(EjectTip())
        elif isinstance(operation, Wait):
            actions.append(WaitAction(duration_s=to_seconds(operation.duration)))
        elif isinstance(operation, Measure):
            completion_note = f"{operation.method}:{operation.target}"

    if anneal is not None:
        dwell = (
            to_seconds(protocol.hotplate_dwell)
            if protocol.hotplate_dwell is not None
            else (
                0.0
                if anneal.duration is None
                else to_seconds(anneal.duration)
            )
        )
        actions.extend(
            [
                PickSample(coordinate="stations.spin_coater.operation"),
                PlaceSample(coordinate=_hotplate_coordinate(round_index, "place")),
                WaitAction(duration_s=dwell),
                PickSample(coordinate=_hotplate_coordinate(round_index, "place")),
                PlaceSample(coordinate=_substrate_coordinate(round_index)),
            ]
        )
    actions.append(Complete(note=completion_note))
    return ExperimentPlan(
        sample_id=protocol.sample_id,
        description=protocol.description,
        actions=actions,
    )


def _substrate_coordinate(round_index: int | None) -> str:
    return (
        "stations.substrate_rack.pickup"
        if round_index is None
        else f"stations.substrate_rack.slot_{round_index}"
    )


def _hotplate_coordinate(round_index: int | None, point: str) -> str:
    if round_index is None:
        return f"stations.hotplate.{point}"
    slot = ((round_index - 1) % 9) + 1
    return f"stations.hotplate.slot_{slot}.{point}"


def _liquid_actions(
    operation: DispenseLiquid,
    tip_slots: dict[str, int] | None,
    tip_clearances: dict[str, float] | None,
    round_index: int | None,
) -> list[ExperimentAction]:
    volume = to_ul(operation.volume)
    return [
        ChangeTip(coordinate=_tip_coordinate(operation.liquid, tip_slots)),
        Aspirate(
            liquid=operation.liquid,
            volume_ul=volume,
            coordinate=_liquid_source(operation.liquid, round_index),
        ),
        Dispense(
            liquid=operation.liquid,
            volume_ul=volume,
            coordinate=_dispense_target(operation.target),
            clearance_mm=_tip_clearance(operation.liquid, tip_clearances),
        ),
    ]


def _tip_coordinate(
    liquid: str,
    tip_slots: dict[str, int] | None,
) -> str:
    legacy_defaults = {"perovskite_precursor": 1, "antisolvent": 2}
    slot = (
        legacy_defaults.get(liquid, 1)
        if tip_slots is None
        else tip_slots.get(liquid, 1)
    )
    if not 1 <= slot <= 96:
        raise ValueError(f"pipette tip slot {slot} is outside rack capacity 1-96")
    return f"stations.tip_rack.slot_{slot}"


def _liquid_source(liquid: str, round_index: int | None = None) -> str:
    if liquid == "perovskite_precursor" and round_index is not None:
        source_index = ((round_index - 1) // 4) + 1
        if source_index > 4:
            raise ValueError("precursor source capacity exceeded")
        return f"stations.reagent_rack.precursor_{source_index}"
    try:
        return _LIQUID_COORDINATES[liquid]
    except KeyError as exc:
        raise ValueError(f"unknown liquid coordinate for {liquid!r}") from exc


def _tip_clearance(
    liquid: str, clearances: dict[str, float] | None
) -> float | None:
    if not clearances:
        return None
    key = "precursor" if liquid == "perovskite_precursor" else liquid
    return clearances.get(key)


def _dispense_target(target: str) -> str:
    try:
        return _DISPENSE_COORDINATES[target]
    except KeyError as exc:
        raise ValueError(f"unknown dispense target {target!r}") from exc
