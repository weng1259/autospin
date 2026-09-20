"""Deadline-aware multi-round planning for shared gantry and hotplate resources."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from .actions import (
    AnnealAction,
    Aspirate,
    ChangeTip,
    Complete,
    Dispense,
    EjectTip,
    ExperimentAction,
    MoveTool,
    PickSample,
    PlaceSample,
    PrepareHeater,
    SpinCoatAction,
    WaitAction,
    WaitUntilElapsed,
    WaitUntilSpinElapsed,
    WaitForHeaterStable,
    AnnealTimerStart,
    SampleStateUpdate,
)
from .protocol import ExperimentProtocol
from .workflows import compile_protocol

GANTRY_MOVE_ESTIMATE_S = 18.0
GRIPPER_ESTIMATE_S = 2.0
PIPETTE_ACTION_ESTIMATE_S = 6.0
TIP_CHANGE_ESTIMATE_S = 23.0
HEATER_COMMAND_ESTIMATE_S = 1.0
MULTI_ROUND_START_TOLERANCE_C = 10.0
ANNEAL_PLACEMENT_TOLERANCE_C = 1.0
SPIN_COMMAND_OVERHEAD_S = 2.0
HOTPLATE_TRAVEL_LEAD_S = GANTRY_MOVE_ESTIMATE_S
HOTPLATE_CAPACITY = 9
MAX_ROUNDS = 16


class ScheduledRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_index: int
    sample_id: str
    hotplate_slot: int
    due_s: float


class MultiRoundPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[ExperimentAction] = Field(default_factory=list)
    rounds: list[ScheduledRound] = Field(default_factory=list)
    estimated_duration_s: float = Field(ge=0)


@dataclass
class _PendingReturn:
    round_index: int
    sample_id: str
    hotplate_slot: int
    due_s: float
    return_actions: list[ExperimentAction]


class HotplateResourceManager:
    """Deterministic planner-side ownership for the physical 3x3 hotplate."""

    def __init__(self, capacity: int = HOTPLATE_CAPACITY) -> None:
        self.capacity = capacity
        self._owners: dict[int, tuple[int, str]] = {}

    def preferred_slot(self, round_index: int) -> int:
        return ((round_index - 1) % self.capacity) + 1

    def owner(self, slot: int) -> tuple[int, str] | None:
        return self._owners.get(slot)

    def place(self, slot: int, round_index: int, sample_id: str) -> None:
        if slot in self._owners:
            raise ValueError(f"hotplate slot {slot} is still occupied")
        self._owners[slot] = (round_index, sample_id)

    def retrieve(self, slot: int, round_index: int) -> None:
        owner = self._owners.get(slot)
        if owner is None or owner[0] != round_index:
            raise ValueError(f"hotplate slot {slot} ownership mismatch")
        del self._owners[slot]


def compile_multi_round(protocols: list[ExperimentProtocol]) -> MultiRoundPlan:
    """Interleave annealing returns at semantic safe boundaries.

    Each semantic action is atomic.  Liquid preparation chains are grouped so
    a hotplate return can never be inserted between aspirating precursor and
    dispensing it.  Absolute waits prevent earlier timing error accumulating.
    """
    if not protocols:
        raise ValueError("multi-round execution requires at least one protocol")
    if len(protocols) > MAX_ROUNDS:
        raise ValueError(f"multi-round execution supports at most {MAX_ROUNDS} rounds")

    actions: list[ExperimentAction] = []
    rounds: list[ScheduledRound] = []
    pending: list[_PendingReturn] = []
    elapsed_s = 0.0
    resources = HotplateResourceManager()

    for round_index, protocol in enumerate(protocols, start=1):
        slot = resources.preferred_slot(round_index)
        occupying = next((item for item in pending if item.hotplate_slot == slot), None)
        if occupying is not None:
            elapsed_s = _append_return(
                actions, pending, occupying, elapsed_s, resources
            )

        plan = compile_protocol(protocol, round_index=round_index)
        before_dwell, dwell, after_dwell = _split_anneal_lifecycle(plan.actions)
        before_dwell = _add_two_stage_heater_gates(before_dwell)
        for chunk in _safe_chunks(before_dwell):
            projected_end = elapsed_s + sum(_estimate(action) for action in chunk)
            while pending and pending[0].due_s <= projected_end + HOTPLATE_TRAVEL_LEAD_S:
                elapsed_s = _append_return(
                    actions, pending, pending[0], elapsed_s, resources
                )
                projected_end = elapsed_s + sum(_estimate(action) for action in chunk)
            actions.extend(chunk)
            elapsed_s = projected_end

        due_s = elapsed_s + dwell.duration_s
        resources.place(slot, round_index, protocol.sample_id)
        actions.append(
            AnnealTimerStart(
                round_index=round_index,
                sample_id=protocol.sample_id,
                hotplate_slot=slot,
                duration_s=dwell.duration_s,
            )
        )
        event = _PendingReturn(
            round_index=round_index,
            sample_id=protocol.sample_id,
            hotplate_slot=slot,
            due_s=due_s,
            return_actions=after_dwell,
        )
        pending.append(event)
        pending.sort(key=lambda item: (item.due_s, item.round_index))
        rounds.append(
            ScheduledRound(
                round_index=round_index,
                sample_id=protocol.sample_id,
                hotplate_slot=slot,
                due_s=due_s,
            )
        )

    while pending:
        elapsed_s = _append_return(
            actions, pending, pending[0], elapsed_s, resources
        )

    return MultiRoundPlan(
        actions=actions,
        rounds=rounds,
        estimated_duration_s=elapsed_s,
    )


def _split_anneal_lifecycle(
    actions: list[ExperimentAction],
) -> tuple[list[ExperimentAction], WaitAction, list[ExperimentAction]]:
    indexes = [
        index for index, action in enumerate(actions) if isinstance(action, WaitAction)
    ]
    if not indexes:
        raise ValueError("round plan has no annealing dwell")
    index = indexes[-1]
    before = actions[:index]
    after = actions[index + 1 :]
    if not before or not isinstance(before[-1], PlaceSample):
        raise ValueError("annealing dwell must follow hotplate placement")
    if len(after) < 3 or not isinstance(after[0], PickSample):
        raise ValueError("annealing dwell lacks hotplate pickup/return lifecycle")
    return before, actions[index], after


def _safe_chunks(actions: list[ExperimentAction]) -> list[list[ExperimentAction]]:
    chunks: list[list[ExperimentAction]] = []
    index = 0
    while index < len(actions):
        action = actions[index]
        if isinstance(action, WaitForHeaterStable):
            chunk = [action]
            index += 1
            # Keep the final temperature gate adjacent to the complete
            # spin-coater -> hotplate transfer. A due return must run before
            # this block, never after temperature validation.
            while index < len(actions) and len(chunk) < 3 and isinstance(
                actions[index], (PickSample, PlaceSample)
            ):
                chunk.append(actions[index])
                index += 1
            chunks.append(chunk)
            continue
        if isinstance(action, ChangeTip):
            chunk = [action]
            index += 1
            while index < len(actions) and isinstance(
                actions[index], (Aspirate, Dispense)
            ):
                chunk.append(actions[index])
                index += 1
            # Timed antisolvent is prepared immediately before SpinCoat.  Keep
            # the loaded pipette and its spin/drop window as one protected
            # block; a due hotplate return is inserted before ChangeTip.
            if index < len(actions) and isinstance(actions[index], SpinCoatAction):
                chunk.append(actions[index])
                index += 1
                if index < len(actions) and isinstance(actions[index], EjectTip):
                    chunk.append(actions[index])
                    index += 1
            chunks.append(chunk)
            continue
        chunks.append([action])
        index += 1
    return chunks


def _add_two_stage_heater_gates(
    actions: list[ExperimentAction],
) -> list[ExperimentAction]:
    """Allow batch work near SV, but require strict stability before placement."""
    prepared = list(actions)
    anneal_index = next(
        (index for index, action in enumerate(prepared) if isinstance(action, AnnealAction)),
        None,
    )
    if anneal_index is None:
        raise ValueError("round plan has no heater setpoint action")
    anneal = prepared[anneal_index]
    assert isinstance(anneal, AnnealAction)
    prepared[anneal_index] = PrepareHeater(
        temperature_c=anneal.temperature_c,
        start_margin_c=MULTI_ROUND_START_TOLERANCE_C,
    )

    spin_pick_index = next(
        (
            index
            for index in range(len(prepared) - 1, -1, -1)
            if isinstance(prepared[index], PickSample)
            and prepared[index].coordinate == "stations.spin_coater.operation"
        ),
        None,
    )
    if spin_pick_index is None:
        raise ValueError("round plan has no spin-coater pickup before annealing")
    prepared.insert(
        spin_pick_index,
        WaitForHeaterStable(
            temperature_c=anneal.temperature_c,
            tolerance_c=ANNEAL_PLACEMENT_TOLERANCE_C,
        ),
    )
    prepared.insert(
        spin_pick_index,
        MoveTool(coordinate="stations.spin_coater.safe_above", tool="gripper"),
    )
    return prepared


def _append_return(
    actions: list[ExperimentAction],
    pending: list[_PendingReturn],
    event: _PendingReturn,
    elapsed_s: float,
    resources: HotplateResourceManager,
) -> float:
    pending.remove(event)
    safe_above = f"stations.hotplate.slot_{event.hotplate_slot}.safe_above"
    actions.append(MoveTool(coordinate=safe_above, tool="gripper"))
    elapsed_s += _estimate(actions[-1])
    actions.append(
        WaitUntilElapsed(elapsed_s=event.due_s, round_index=event.round_index)
    )
    elapsed_s = max(elapsed_s, event.due_s)
    actions.extend(event.return_actions)
    insert_at = len(actions) - 1 if actions and isinstance(actions[-1], Complete) else len(actions)
    actions.insert(
        insert_at,
        SampleStateUpdate(
            round_index=event.round_index,
            sample_id=event.sample_id,
            hotplate_slot=event.hotplate_slot,
        ),
    )
    resources.retrieve(event.hotplate_slot, event.round_index)
    elapsed_s += sum(_estimate(action) for action in event.return_actions)
    return elapsed_s


def _estimate(action: ExperimentAction) -> float:
    if isinstance(action, MoveTool):
        return GANTRY_MOVE_ESTIMATE_S
    if isinstance(action, (PickSample, PlaceSample)):
        return GANTRY_MOVE_ESTIMATE_S + GRIPPER_ESTIMATE_S
    if isinstance(action, ChangeTip):
        return TIP_CHANGE_ESTIMATE_S
    if isinstance(action, (Aspirate, Dispense, EjectTip)):
        return GANTRY_MOVE_ESTIMATE_S + PIPETTE_ACTION_ESTIMATE_S
    if isinstance(action, SpinCoatAction):
        return sum(segment.duration_s for segment in action.segments) + SPIN_COMMAND_OVERHEAD_S
    if isinstance(action, (AnnealAction, PrepareHeater)):
        return HEATER_COMMAND_ESTIMATE_S
    if isinstance(action, WaitForHeaterStable):
        return 0.0
    if isinstance(action, WaitAction):
        return action.duration_s
    if isinstance(
        action,
        (
            WaitUntilElapsed,
            WaitUntilSpinElapsed,
            WaitForHeaterStable,
            AnnealTimerStart,
            SampleStateUpdate,
            Complete,
        ),
    ):
        return 0.0
    return 1.0
