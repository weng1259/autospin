"""Semantic experiment actions; the only workflow-to-hardware boundary."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .config import get_config
from .coordinates import CoordinateRegistry
from .hardware.types import MoveResult, Position


_TIP_MOUNT_SETTLE_S = 0.4
_TIP_MOUNT_RETRACT_CONFIRM_READS = 3
_TIP_EJECT_SETTLE_S = 2.0
_HOTPLATE_RELEASE_SETTLE_S = 1.0


class ActionExecutionError(RuntimeError):
    pass


class ActionRecord(BaseModel):
    timestamp: datetime
    action: str
    device: str
    parameters: dict[str, Any]
    dry_run: bool
    success: bool
    result: Any = None
    error: str | None = None


class ActionLogger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[ActionRecord] = []

    def append(self, record: ActionRecord) -> None:
        with self._lock:
            self._records.append(record.model_copy(deep=True))

    def records(self) -> list[ActionRecord]:
        with self._lock:
            return [record.model_copy(deep=True) for record in self._records]


class ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str


class HomeGantry(ActionBase):
    kind: Literal["HomeGantry"] = "HomeGantry"


class MoveTool(ActionBase):
    kind: Literal["MoveTool"] = "MoveTool"
    coordinate: str
    tool: Literal["gripper", "pipette"] | None = None


class PickSample(ActionBase):
    kind: Literal["PickSample"] = "PickSample"
    coordinate: str = "stations.substrate_rack.pickup"


class PlaceSample(ActionBase):
    kind: Literal["PlaceSample"] = "PlaceSample"
    coordinate: str


class Aspirate(ActionBase):
    kind: Literal["Aspirate"] = "Aspirate"
    liquid: str
    volume_ul: float = Field(gt=0)
    coordinate: str


class Dispense(ActionBase):
    kind: Literal["Dispense"] = "Dispense"
    liquid: str
    volume_ul: float = Field(gt=0)
    coordinate: str
    clearance_mm: float | None = Field(default=None, gt=0)


class ChangeTip(ActionBase):
    kind: Literal["ChangeTip"] = "ChangeTip"
    coordinate: str = "stations.tip_rack.pickup"


class EjectTip(ActionBase):
    """Move to waste, eject the mounted tip, and allow mechanical release."""

    kind: Literal["EjectTip"] = "EjectTip"
    coordinate: str = "stations.waste.tip_eject"


class SpinSegment(BaseModel):
    rpm: float = Field(ge=0)
    duration_s: float = Field(gt=0)


class TimedDispense(BaseModel):
    at_s: float = Field(ge=0)
    liquid: str
    volume_ul: float = Field(gt=0)
    coordinate: str = "stations.spin_coater.pipette_dispense"
    clearance_mm: float | None = Field(default=None, gt=0)


class SpinCoatAction(ActionBase):
    kind: Literal["SpinCoat"] = "SpinCoat"
    segments: list[SpinSegment] = Field(min_length=1)
    timed_dispenses: list[TimedDispense] = Field(default_factory=list)


class AnnealAction(ActionBase):
    kind: Literal["Anneal"] = "Anneal"
    temperature_c: float = Field(ge=0)
    duration_s: float = Field(ge=0)
    coordinate: str = "stations.hotplate.place"
    place_sample: bool = True


class PrepareHeater(ActionBase):
    """Set SV and allow upstream work once PV is near the target from below."""

    kind: Literal["PrepareHeater"] = "PrepareHeater"
    temperature_c: float = Field(ge=0)
    start_margin_c: float = Field(default=10.0, ge=0)


class WaitForHeaterStable(ActionBase):
    """Gate hotplate placement on strict process-temperature stability."""

    kind: Literal["WaitForHeaterStable"] = "WaitForHeaterStable"
    temperature_c: float = Field(ge=0)
    tolerance_c: float = Field(default=1.0, ge=0)


class WaitAction(ActionBase):
    kind: Literal["Wait"] = "Wait"
    duration_s: float = Field(ge=0)


class WaitUntilElapsed(ActionBase):
    """Wait against the batch epoch instead of accumulating relative sleeps."""

    kind: Literal["WaitUntilElapsed"] = "WaitUntilElapsed"
    elapsed_s: float = Field(ge=0)
    round_index: int = Field(ge=1)
    phase: str = "hotplate_dwell_before_return"


class WaitUntilSpinElapsed(ActionBase):
    """Wait against the confirmed motor-start epoch."""

    kind: Literal["WaitUntilSpinElapsed"] = "WaitUntilSpinElapsed"
    elapsed_s: float = Field(ge=0)


class AnnealTimerStart(ActionBase):
    """Anchor one substrate dwell to confirmed hotplate placement."""

    kind: Literal["AnnealTimerStart"] = "AnnealTimerStart"
    round_index: int = Field(ge=1)
    sample_id: str
    hotplate_slot: int = Field(ge=1)
    duration_s: float = Field(ge=0)


class SampleStateUpdate(ActionBase):
    kind: Literal["SampleStateUpdate"] = "SampleStateUpdate"
    round_index: int = Field(ge=1)
    sample_id: str
    hotplate_slot: int = Field(ge=1)
    state: Literal["returned"] = "returned"


class Complete(ActionBase):
    kind: Literal["Complete"] = "Complete"
    note: str = "experiment complete"


ExperimentAction = Annotated[
    HomeGantry
    | MoveTool
    | PickSample
    | PlaceSample
    | Aspirate
    | Dispense
    | ChangeTip
    | EjectTip
    | SpinCoatAction
    | AnnealAction
    | PrepareHeater
    | WaitForHeaterStable
    | WaitAction
    | WaitUntilElapsed
    | WaitUntilSpinElapsed
    | AnnealTimerStart
    | SampleStateUpdate
    | Complete,
    Field(discriminator="kind"),
]


class ActionExecutor:
    """Execute validated actions exclusively through a DeviceRegistry."""

    def __init__(
        self,
        registry: Any,
        coordinates: CoordinateRegistry,
        *,
        dry_run: bool,
        logger: ActionLogger | None = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.registry = registry
        self.coordinates = coordinates
        self.dry_run = dry_run
        self.logger = logger or ActionLogger()
        self._sleep = sleep
        self._clock = clock
        self._started_at = float(clock())
        self._cancelled = cancel_event or threading.Event()
        self._cooperative_cancel = cancel_event is not None
        self._spin_started_at: float | None = None
        self._anneal_deadlines: dict[int, float] = {}
        self._sample_states: dict[int, dict[str, Any]] = {}

    def cancel(self) -> None:
        self._cancelled.set()

    def sample_states(self) -> dict[int, dict[str, Any]]:
        return {key: dict(value) for key, value in self._sample_states.items()}

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ActionExecutionError("experiment execution was cancelled")

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            self._raise_if_cancelled()
            return
        if self._cooperative_cancel:
            if self._cancelled.wait(seconds):
                raise ActionExecutionError("experiment execution was cancelled")
        else:
            self._sleep(seconds)
            self._raise_if_cancelled()

    def execute(self, action: ExperimentAction) -> Any:
        self._raise_if_cancelled()
        device = self._device(action)
        parameters = action.model_dump(mode="json")
        try:
            result = self._dispatch(action)
            self._raise_if_cancelled()
        except Exception as exc:
            self.logger.append(
                ActionRecord(
                    timestamp=datetime.now(timezone.utc),
                    action=action.kind,
                    device=device,
                    parameters=parameters,
                    dry_run=self.dry_run,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        encoded = (
            result.model_dump(mode="json")
            if isinstance(result, BaseModel)
            else result
        )
        self.logger.append(
            ActionRecord(
                timestamp=datetime.now(timezone.utc),
                action=action.kind,
                device=device,
                parameters=parameters,
                dry_run=self.dry_run,
                success=True,
                result=encoded,
            )
        )
        return result

    def _require(self, name: str) -> Any:
        backend = getattr(self.registry, name, None)
        if backend is None:
            raise ActionExecutionError(f"required device {name!r} is unavailable")
        return backend

    def _move(
        self,
        coordinate: str,
        tool: str | None,
        *,
        linear_stage_position_mm: float | None = None,
    ) -> Any:
        # Every semantic XYZ is a taught gantry machine position for the
        # corresponding installed tool.  ``tool`` selects the actuator used by
        # the action; it must not geometrically offset an already taught point.
        resolved = self.coordinates.resolve(
            coordinate,
            tool=None,
            production=not self.dry_run,
        )
        process_stage_position = (
            resolved.linear_stage_position_mm
            if linear_stage_position_mm is None
            else linear_stage_position_mm
        )
        gantry = self._require("gantry")
        current = gantry.get_position()
        if isinstance(current, Position):
            current_position = current
        elif isinstance(current, dict):
            current_position = Position(
                x_mm=float(current.get("X", current.get("x_mm", resolved.xyz[0]))),
                y_mm=float(current.get("Y", current.get("y_mm", resolved.xyz[1]))),
                z_mm=float(current.get("Z", current.get("z_mm", resolved.xyz[2]))),
            )
        else:
            # Dry-run test doubles may not expose a meaningful live position.
            current_position = Position(
                x_mm=resolved.xyz[0],
                y_mm=resolved.xyz[1],
                z_mm=resolved.xyz[2],
            )
        safe_z = self.coordinates.config.global_safe_positions["safe_travel"].xyz[2]
        motion_z = current_position.z_mm
        xy_changes = (
            abs(resolved.xyz[0] - current_position.x_mm) > 1e-6
            or abs(resolved.xyz[1] - current_position.y_mm) > 1e-6
        )
        if xy_changes and current_position.z_mm < safe_z:
            safe_target = Position(
                x_mm=current_position.x_mm,
                y_mm=current_position.y_mm,
                z_mm=safe_z,
            )
            gantry.move_to(
                safe_target,
                feed_mm_min=self._gantry_feed_for(
                    (safe_target.z_mm,),
                    axes=("z",),
                    current_values=(current_position.z_mm,),
                ),
                dry_run=self.dry_run,
            )
            motion_z = safe_z
        xy_origin = Position(
            x_mm=current_position.x_mm,
            y_mm=current_position.y_mm,
            z_mm=motion_z,
        )
        # AutoSpinmotorSystem hardware logs showed that leaving an XY hard
        # limit with one long diagonal jog could retrigger ALARM:1/Pn:XYZ.
        # First move each affected axis a short distance into the workspace,
        # at the boundary feed, then begin the normal XY transfer.
        for axis in ("x", "y"):
            escape_value = self._boundary_escape_value(
                axis,
                current_value=getattr(xy_origin, f"{axis}_mm"),
                target_value=resolved.xyz[0 if axis == "x" else 1],
            )
            if escape_value is None:
                continue
            escape_target = xy_origin.model_copy(
                update={f"{axis}_mm": escape_value}
            )
            gantry.move_to(
                escape_target,
                feed_mm_min=get_config().hardware.gantry.boundary_feed_mm_min,
                dry_run=self.dry_run,
            )
            xy_origin = escape_target
        xy_target = Position(
            x_mm=resolved.xyz[0],
            y_mm=resolved.xyz[1],
            z_mm=motion_z,
        )
        xy_feed = self._gantry_feed_for(
            (xy_target.x_mm, xy_target.y_mm),
            axes=("x", "y"),
            current_values=(xy_origin.x_mm, xy_origin.y_mm),
        )
        gantry.move_to(
            xy_target,
            feed_mm_min=xy_feed,
            dry_run=self.dry_run,
        )
        target = Position(
            x_mm=resolved.xyz[0],
            y_mm=resolved.xyz[1],
            z_mm=resolved.xyz[2],
        )
        z_feed = self._gantry_feed_for(
            (target.z_mm,),
            axes=("z",),
            current_values=(motion_z,),
        )
        result = gantry.move_to(
            target,
            feed_mm_min=z_feed,
            dry_run=self.dry_run,
        )
        if isinstance(result, MoveResult) and any(
            abs(actual - expected) > 1.0
            for actual, expected in (
                (result.final_position.x_mm, target.x_mm),
                (result.final_position.y_mm, target.y_mm),
                (result.final_position.z_mm, target.z_mm),
            )
        ):
            raise ActionExecutionError(
                "gantry did not reach the commanded XYZ position; "
                "linear stage movement was blocked"
            )
        if process_stage_position is not None:
            self._require("linear_stage").move_to(
                process_stage_position,
                idempotency_key=f"stage-process-position-{uuid4()}",
                dry_run=self.dry_run,
                position_tolerance_mm=(
                    resolved.linear_stage_position_tolerance_mm
                ),
            )
        return result

    @staticmethod
    def _boundary_escape_value(
        axis: str,
        *,
        current_value: float,
        target_value: float,
    ) -> float | None:
        gantry = get_config().hardware.gantry
        if gantry is None:
            raise ActionExecutionError("gantry hardware configuration is unavailable")
        limits = gantry.soft_limits
        low = getattr(limits, f"{axis}_min_mm")
        high = getattr(limits, f"{axis}_max_mm")
        escape = gantry.boundary_escape_mm
        low_escape = low + escape
        high_escape = high - escape
        if current_value <= low_escape and target_value > low_escape:
            return low_escape
        if current_value >= high_escape and target_value < high_escape:
            return high_escape
        return None

    @staticmethod
    def _gantry_feed_for(
        values: tuple[float, ...],
        *,
        axes: tuple[str, ...],
        current_values: tuple[float, ...] | None = None,
    ) -> float:
        gantry = get_config().hardware.gantry
        if gantry is None:
            raise ActionExecutionError("gantry hardware configuration is unavailable")
        limits = gantry.soft_limits
        ranges = {
            "x": (limits.x_min_mm, limits.x_max_mm),
            "y": (limits.y_min_mm, limits.y_max_mm),
            "z": (limits.z_min_mm, limits.z_max_mm),
        }
        target_near_boundary = any(
            value - ranges[axis][0] <= gantry.boundary_margin_mm
            or ranges[axis][1] - value <= gantry.boundary_margin_mm
            for axis, value in zip(axes, values)
        )
        current_near_boundary = (
            current_values is not None
            and any(
                value - ranges[axis][0] <= gantry.boundary_margin_mm
                or ranges[axis][1] - value <= gantry.boundary_margin_mm
                for axis, value in zip(axes, current_values)
            )
        )
        if axes == ("z",):
            return min(
                getattr(gantry, "z_feed_mm_min", 1000.0),
                gantry.boundary_feed_mm_min,
            )
        return (
            gantry.boundary_feed_mm_min
            if target_near_boundary or current_near_boundary
            else gantry.feed_rate_default
        )

    def _retract_linear_stage_for(self, coordinate: str) -> None:
        resolved = self.coordinates.resolve(
            coordinate,
            tool=None,
            production=not self.dry_run,
        )
        if resolved.linear_stage_position_mm is None:
            return
        self._require("linear_stage").move_to(
            self._linear_stage_safe_position_mm(),
            idempotency_key=f"stage-retract-{uuid4()}",
            dry_run=self.dry_run,
        )

    def _linear_stage_safe_position_mm(self) -> float:
        try:
            return self.coordinates.config.calibration[
                "linear_stage_safe_position"
            ].value
        except KeyError as exc:
            raise ActionExecutionError(
                "coordinate calibration lacks linear_stage_safe_position"
            ) from exc

    def _dispense_stage_position(self, clearance_mm: float | None) -> float | None:
        if clearance_mm is None:
            return None
        try:
            contact = self.coordinates.config.calibration[
                "linear_stage_glass_contact"
            ].value
        except KeyError as exc:
            raise ActionExecutionError(
                "coordinate calibration lacks linear_stage_glass_contact"
            ) from exc
        target = float(contact) - float(clearance_mm)
        if target < 0:
            raise ActionExecutionError(
                f"dispense clearance {clearance_mm:g} mm produces invalid stage target {target:g} mm"
            )
        return target

    def _dispatch(self, action: ExperimentAction) -> Any:
        key = f"action-{uuid4()}"
        if isinstance(action, HomeGantry):
            return self._require("gantry").home(
                idempotency_key=key, dry_run=self.dry_run
            )
        if isinstance(action, MoveTool):
            return self._move(action.coordinate, action.tool)
        if isinstance(action, PickSample):
            self._move(action.coordinate, "gripper")
            return self._require("gripper").close(
                idempotency_key=key, dry_run=self.dry_run
            )
        if isinstance(action, PlaceSample):
            self._move(action.coordinate, "gripper")
            result = self._require("gripper").open(
                idempotency_key=key, dry_run=self.dry_run
            )
            if (
                not self.dry_run
                and action.coordinate.startswith("stations.hotplate.")
            ):
                # Let the substrate fully separate from the gripper before the
                # following action can lift Z away from the hotplate.
                self._wait(_HOTPLATE_RELEASE_SETTLE_S)
            if action.coordinate == "stations.spin_coater.operation":
                relay = self._require("relay")
                if not self.dry_run and not relay.is_connected():
                    relay.connect()
                relay.force_set(
                    3,
                    True,
                    idempotency_key=f"spin-vacuum-after-placement-{uuid4()}",
                    dry_run=self.dry_run,
                )
            return result
        if isinstance(action, ChangeTip):
            pipette = self._require("pipette")
            if not self.dry_run:
                status = pipette.get_status()
                if status.tip_present is None:
                    raise ActionExecutionError("pipette tip state is unknown before ChangeTip")
                if status.tip_present:
                    self._move("stations.waste.tip_eject", "pipette")
                    pipette.eject_tip(idempotency_key=f"tip-eject-{uuid4()}")
                    # Real-hardware recovery preserved from AutoSpinmotorSystem:
                    # allow the ejector and tip sensor to finish releasing before
                    # any subsequent XYZ departure from the waste station.
                    self._wait(_TIP_EJECT_SETTLE_S)
            self._move(action.coordinate, "pipette")
            tip_present_after_press = True
            try:
                if not self.dry_run:
                    # AutoSpinmotorSystem waits for the mechanical press-fit
                    # and tip sensor to settle before taking the live sample.
                    self._wait(_TIP_MOUNT_SETTLE_S)
                    status = pipette.refresh_status()
                    tip_present_after_press = status.tip_present is True
            finally:
                self._retract_linear_stage_for(action.coordinate)
            if not self.dry_run and not tip_present_after_press:
                # A mechanically compressed tip can assert its sensor only after
                # the mounting stage has released it.  The real input has also
                # shown isolated false samples with a physically mounted tip,
                # so confirm a persistent absence after retraction rather than
                # aborting on one sample.  Any positive sample proves presence;
                # three consecutive negatives still fail safely.
                for _ in range(_TIP_MOUNT_RETRACT_CONFIRM_READS):
                    self._wait(_TIP_MOUNT_SETTLE_S)
                    status = pipette.refresh_status()
                    if status.tip_present is True:
                        break
                else:
                    raise ActionExecutionError(
                        "tip mount did not produce tip_present=true after "
                        f"{_TIP_MOUNT_RETRACT_CONFIRM_READS} retraction checks"
                    )
            return {"tip_change": "planned" if self.dry_run else "verified"}
        if isinstance(action, EjectTip):
            pipette = self._require("pipette")
            self._move(action.coordinate, "pipette")
            result = pipette.eject_tip(
                idempotency_key=f"tip-eject-{uuid4()}",
                dry_run=self.dry_run,
            )
            if not self.dry_run:
                self._wait(_TIP_EJECT_SETTLE_S)
            return result
        if isinstance(action, Aspirate):
            self._move(action.coordinate, "pipette")
            try:
                return self._require("pipette").aspirate(
                    action.volume_ul, idempotency_key=key, dry_run=self.dry_run
                )
            finally:
                self._retract_linear_stage_for(action.coordinate)
        if isinstance(action, Dispense):
            self._move(
                action.coordinate,
                "pipette",
                linear_stage_position_mm=self._dispense_stage_position(
                    action.clearance_mm
                ),
            )
            try:
                return self._require("pipette").dispense(
                    action.volume_ul, idempotency_key=key, dry_run=self.dry_run
                )
            finally:
                self._retract_linear_stage_for(action.coordinate)
        if isinstance(action, SpinCoatAction):
            return self._spin(action)
        if isinstance(action, AnnealAction):
            heater = self._require("heater")
            heater.set_sv(
                action.temperature_c,
                idempotency_key=key,
                dry_run=self.dry_run,
            )
            if action.place_sample:
                self._move(action.coordinate, "gripper")
            if not self.dry_run:
                stability = heater.wait_until_stable(
                    target_c=action.temperature_c,
                    abort_check=self._cancelled.is_set,
                )
                if not stability.success:
                    raise RuntimeError(
                        "heater failed to stabilize: "
                        f"target={stability.target_c:g} C, "
                        f"last_pv={stability.pv_c!r}, timed_out={stability.timed_out}"
                    )
                self._wait(action.duration_s)
            return {"temperature_c": action.temperature_c, "duration_s": action.duration_s}
        if isinstance(action, PrepareHeater):
            heater = self._require("heater")
            heater.set_sv(
                action.temperature_c,
                idempotency_key=key,
                dry_run=self.dry_run,
            )
            if self.dry_run:
                return {
                    "temperature_c": action.temperature_c,
                    "minimum_ready_c": action.temperature_c - action.start_margin_c,
                    "ready": None,
                }
            readiness = heater.wait_until_at_least(
                minimum_c=max(0.0, action.temperature_c - action.start_margin_c),
                target_c=action.temperature_c,
                abort_check=self._cancelled.is_set,
            )
            if not readiness.success:
                raise RuntimeError(
                    "heater failed to reach experiment start threshold: "
                    f"target={action.temperature_c:g} C, "
                    f"minimum={max(0.0, action.temperature_c - action.start_margin_c):g} C, "
                    f"last_pv={readiness.pv_c!r}, timed_out={readiness.timed_out}"
                )
            return readiness
        if isinstance(action, WaitForHeaterStable):
            if self.dry_run:
                return {
                    "temperature_c": action.temperature_c,
                    "tolerance_c": action.tolerance_c,
                    "stable": None,
                }
            stability = self._require("heater").wait_until_stable(
                target_c=action.temperature_c,
                tolerance_c=action.tolerance_c,
                abort_check=self._cancelled.is_set,
            )
            if not stability.success:
                raise RuntimeError(
                    "heater failed to stabilize before hotplate placement: "
                    f"target={stability.target_c:g} C, "
                    f"last_pv={stability.pv_c!r}, timed_out={stability.timed_out}"
                )
            return stability
        if isinstance(action, WaitAction):
            if not self.dry_run:
                self._wait(action.duration_s)
            return {"duration_s": action.duration_s}
        if isinstance(action, WaitUntilElapsed):
            elapsed = max(0.0, float(self._clock()) - self._started_at)
            target_elapsed = self._anneal_deadlines.get(
                action.round_index, action.elapsed_s
            )
            remaining = max(0.0, target_elapsed - elapsed)
            if not self.dry_run and remaining > 0:
                self._wait(remaining)
            return {
                "target_elapsed_s": target_elapsed,
                "observed_elapsed_s": elapsed,
                "waited_s": 0.0 if self.dry_run else remaining,
                "late_by_s": max(0.0, elapsed - target_elapsed),
                "round_index": action.round_index,
            }
        if isinstance(action, WaitUntilSpinElapsed):
            if self._spin_started_at is None:
                raise ActionExecutionError("WaitUntilSpinElapsed used before motor start")
            observed = max(0.0, float(self._clock()) - self._spin_started_at)
            remaining = max(0.0, action.elapsed_s - observed)
            if not self.dry_run and remaining > 0:
                self._wait(remaining)
            return {
                "target_elapsed_s": action.elapsed_s,
                "observed_elapsed_s": observed,
                "late_by_s": max(0.0, observed - action.elapsed_s),
            }
        if isinstance(action, AnnealTimerStart):
            elapsed = max(0.0, float(self._clock()) - self._started_at)
            self._anneal_deadlines[action.round_index] = elapsed + action.duration_s
            self._sample_states[action.round_index] = {
                "sample_id": action.sample_id,
                "state": "annealing",
                "hotplate_slot": action.hotplate_slot,
                "placed_elapsed_s": elapsed,
                "due_elapsed_s": elapsed + action.duration_s,
            }
            return dict(self._sample_states[action.round_index])
        if isinstance(action, SampleStateUpdate):
            state = self._sample_states.setdefault(action.round_index, {})
            state.update(
                sample_id=action.sample_id,
                state=action.state,
                hotplate_slot=action.hotplate_slot,
                returned_elapsed_s=max(
                    0.0, float(self._clock()) - self._started_at
                ),
            )
            return dict(state)
        if isinstance(action, Complete):
            return {"note": action.note}
        raise ActionExecutionError(f"unsupported action {action!r}")

    def _spin(self, action: SpinCoatAction) -> dict[str, Any]:
        spin = self._require("spincoater")
        relay = self._require("relay")
        elapsed = 0.0
        event_index = 0
        events = sorted(action.timed_dispenses, key=lambda item: item.at_s)
        if not self.dry_run and not relay.is_connected():
            relay.connect()
        relay.force_set(
            3,
            True,
            idempotency_key=f"spin-vacuum-on-{uuid4()}",
            dry_run=self.dry_run,
        )
        try:
            # Anchor the complete spin profile before issuing the potentially
            # blocking start call.  The real backend performs stop-state
            # confirmation and the acceleration ramp inside ``start()``; if
            # the epoch were recorded only after it returned, that already
            # elapsed motor time would be omitted and timed dispensing would
            # appear to start near pipette arrival.  Pipette travel therefore
            # consumes the event's remaining delay and never resets this clock.
            first_segment = action.segments[0]
            self._spin_started_at = float(self._clock())
            spin.start(
                first_segment.rpm,
                idempotency_key=f"spin-{uuid4()}",
                dry_run=self.dry_run,
            )
            if events:
                first = events[0]
                self._move(
                    first.coordinate,
                    "pipette",
                    linear_stage_position_mm=self._dispense_stage_position(
                        first.clearance_mm
                    ),
                )
            for segment_index, segment in enumerate(action.segments):
                if segment_index > 0:
                    spin.start(
                        segment.rpm,
                        idempotency_key=f"spin-{uuid4()}",
                        dry_run=self.dry_run,
                    )
                end = elapsed + segment.duration_s
                while event_index < len(events) and events[event_index].at_s <= end:
                    event = events[event_index]
                    self.execute(WaitUntilSpinElapsed(elapsed_s=event.at_s))
                    self._require("pipette").dispense(
                        event.volume_ul,
                        idempotency_key=f"timed-dispense-{uuid4()}",
                        dry_run=self.dry_run,
                    )
                    elapsed = event.at_s
                    event_index += 1
                self.execute(WaitUntilSpinElapsed(elapsed_s=end))
                elapsed = end
        finally:
            try:
                spin.stop(
                    use_brake=True,
                    idempotency_key=f"spin-stop-{uuid4()}",
                    dry_run=self.dry_run,
                )
            finally:
                relay.force_set(
                    3,
                    False,
                    idempotency_key=f"spin-vacuum-off-{uuid4()}",
                    dry_run=self.dry_run,
                )
            if events:
                self._retract_linear_stage_for(events[0].coordinate)
            self._spin_started_at = None
        return {"duration_s": elapsed, "segments": len(action.segments)}

    @staticmethod
    def _device(action: ExperimentAction) -> str:
        if isinstance(action, (HomeGantry, MoveTool, PickSample, PlaceSample)):
            return "gantry"
        if isinstance(action, (Aspirate, Dispense, ChangeTip, EjectTip)):
            return "pipette"
        if isinstance(action, SpinCoatAction):
            return "spincoater"
        if isinstance(action, (AnnealAction, PrepareHeater, WaitForHeaterStable)):
            return "heater"
        return "system"
