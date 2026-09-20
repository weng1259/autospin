# AutoSpin Experimental Behavior Migration Implementation Report

Date: 2026-08-05

## Outcome

The experimentally critical scheduling and annealing behaviors identified in
the migration audit have been restored inside the new action-based
architecture. Protocols are still compiled into semantic actions, executed by
`ActionExecutor`, routed through `DeviceRegistry`, and ultimately delegated to
backend/adapter APIs. No legacy controller file was copied into the new
project.

The implementation now supports a single interleaved multi-sample plan,
absolute batch and spin timing, placement-anchored annealing deadlines,
hotplate-slot ownership and reuse, protected liquid/spin scheduling blocks,
cooperative cancellation, and final-plan safety validation.

## Files changed

### Scheduling and action execution

- `src/actions.py`
  - Added `WaitUntilSpinElapsed`, `AnnealTimerStart`, and
    `SampleStateUpdate` actions.
  - Added absolute monotonic batch, spin, and per-sample annealing clocks.
  - Added cancellation checks and interruptible waits.
  - Added timed-dispense pre-positioning and spin-relative deadline waits.
  - Added tip ejection/mount feedback checks and dispense-clearance handling.
- `src/multi_round.py`
  - Added deadline-aware multi-round interleaving and dynamic return-action
    insertion at semantic safe boundaries.
  - Added `HotplateResourceManager`, slot ownership, release, and reuse.
  - Added safe-above-hotplate waiting before sample retrieval.
  - Kept precursor and antisolvent/spin critical sequences indivisible.
- `src/workflows.py`
  - Compiles complete glass-holder/spin-coater/hotplate/return lifecycles.
  - Applies saved pipette clearances and deterministic precursor source
    allocation across rounds.
- `src/coordinates.py`
  - Resolves generated substrate-rack slots 1-16 and hotplate slots 1-9,
    including hotplate safe-above points.
- `src/experiment_service.py`
  - Executes a compiled multi-round action plan through one executor and
    returns final sample states.

### Hardware and safety integration

- `src/hardware/heater_backend.py`
  - Added bounded temperature-stability polling with consecutive in-tolerance
    samples, timeout result, and cancellation checks.
- `src/hardware/pipette_backend.py`
  - Added live status refresh used to verify tip replacement.
- `src/hardware/autospinmotor_adapters/pipette_adapter.py`
  - Exposes live legacy-controller status through the adapter boundary.
- `src/production_safety.py`
  - Added final scheduled-plan validation so dynamically inserted actions and
    coordinates are checked before execution.
- `src/webapp/routes_experiments.py`
  - Runs multi-round recipes as one gated action plan.
  - Gives each accepted experiment an independent cancellation event.
- `src/webapp/estop.py`
  - Signals the active experiment to stop before issuing system hardware halt
    commands and releasing the operation gate.
- `src/webapp/mock_devices.py`
  - Added mock live tip feedback needed by the same execution path used in
    production.

### Tests and documentation

- `tests/test_multi_round.py`
  - Added/expanded scheduling, overlap, return-state, slot-reuse, spin-clock,
    and cancellation protection tests.
- `CHANGELOG.md`
  - Records the migration and verification result.
- `MIGRATION_IMPLEMENTATION_REPORT.md`
  - This report.

## Design decisions

### One monotonic execution epoch

`ActionExecutor` creates one monotonic epoch for the accepted batch.
`WaitUntilElapsed` targets that epoch instead of accumulating relative sleeps.
This prevents normal motion-command lateness from shortening or extending later
annealing waits unpredictably.

### Annealing starts after confirmed placement action

The planner inserts `AnnealTimerStart` immediately after the hotplate
`PlaceSample` action. At runtime it records the actual elapsed time and derives
the retrieval deadline from that point. Consequently, delays before placement
do not consume the required annealing dwell.

### Pre-position before an annealing deadline

When a sample is due, the scheduler inserts a gripper move to the slot's
`safe_above` coordinate, then `WaitUntilElapsed`, then pickup and return. This
restores the old behavior in which the shared gantry waits above the glass
instead of remaining too far away to retrieve it on time.

### Semantic safe insertion points

Dynamic hotplate-return actions may be inserted only between protected action
chunks. The following sequences cannot be split:

- tip change, precursor aspiration, and precursor dispense;
- tip change, antisolvent aspiration, spin start/profile, and timed dispense;
- a running spin segment and its `WaitUntilSpinElapsed` deadline.

This preserves the action model while preventing the scheduler from inserting
gantry work into experimentally critical liquid and spin windows.

### Explicit hotplate ownership

The planner owns each of nine physical hotplate slots from placement until the
corresponding return actions are appended. A slot cannot be allocated while
occupied and is reusable only after retrieval. Runtime sample state records
`annealing` and `returned` transitions for operation results.

### Safety at the final action boundary

Safety validation is performed again on the complete interleaved action plan,
not only on each input protocol. This ensures generated slot coordinates and
inserted moves are subject to the same coordinate, device, and parameter
checks as ordinary actions.

## Tests added or expanded

- Multi-sample scheduling produces absolute retrieval waits and no sequential
  per-round annealing sleeps.
- Annealing overlap anchors the deadline to actual placement time.
- Retrieval returns a sample to its original rack slot and records `returned`.
- A hotplate slot rejects concurrent ownership and can be reused after release.
- Liquid preparation and timed spin blocks cannot be split by inserted returns.
- `WaitUntilSpinElapsed` requires a confirmed motor-start epoch and waits only
  for the remaining absolute interval.
- A cancelled executor does not begin the next critical aspiration action.

Verification command:

```text
python -m pytest -q
```

Result:

```text
486 passed, 1 skipped, 1 warning
```

The warning is the existing Starlette `TestClient` deprecation warning.

## Remaining limitations

- The full suite uses mocks and unit-level driver tests. A dry run followed by
  a supervised hardware acceptance run is still required before experimental
  use, especially for hotplate slot and safe-above coordinates.
- Motion-duration estimates select when to pre-position, but actual retrieval
  correctness is enforced by runtime absolute waits rather than a predictive
  live travel-time model.
- Cancellation is cooperative around waits and action boundaries. Python
  cannot safely preempt a blocking vendor/serial call; emergency stop still
  commands the hardware halt path for that case.
- Hotplate/sample ownership is held in the active plan/executor result and is
  not persisted across process restart. Crash recovery and restart/resume need
  an operator reconciliation workflow before being automated.
- Temperature tolerance, consecutive stable samples, poll interval, and
  stabilization timeout currently use backend defaults rather than
  per-protocol parameters.
- Precursor source selection is deterministic (rounds 1-8 use source 1 and
  rounds 9-16 use source 2), but remaining liquid volume is not measured or
  tracked.
- Tip replacement verification depends on the pipette controller's
  `tip_present` feedback. It detects negative/unknown feedback but does not add
  an independent optical or force sensor.
- There is no mid-action resume after emergency stop. The failed batch remains
  failed and requires hardware re-home/state verification before a new run.

