# GripperBackend Implementation Plan

## Status

Planning only. No implementation is authorized by this document.

The only hardware-behavior source is `AutoSpinmotorSystem`. Existing `autospin`
Gripper code may be inspected later for compatibility, but it must not define
electrical behavior, timing, state semantics, or safety policy.

## Confirmed Hardware Contract

| Property | Confirmed behavior |
| --- | --- |
| Control type | IO-only |
| Relay controller | DSTUR-T80 |
| Relay channel | CH1 |
| Close command | CH1 ON |
| Open command | CH1 OFF |
| Position feedback | None |
| Force feedback | None |
| Grip detection | None |
| Relay ownership | Shared with Gantry Z brake |
| Minimum relay write interval | `0.3 s` |

The implementation must never claim that an object is gripped or that the jaws
physically reached a requested position. It can report only the last
successfully commanded state.

## 1. Target Architecture

```text
Routine / Pick-Place Workflow / SystemEstop
                    |
                    v
              GripperBackend
        semantic API and software state
                    |
                    v
              GripperAdapter
       CH1 polarity and relay translation
                    |
                    v
      Shared verified RelayBackend instance
           write lock + 0.3 s throttle
                    |
                    v
             DSTUR-T80 CH1
                    |
                    v
          IO-controlled Gripper
```

Gantry and Gripper must receive the same `RelayBackend` object:

```text
                         +-> CH1: Gripper
DeviceRegistry -> RelayBackend
                         +-> CH2: Gantry Z brake
```

No second serial connection may be opened for Gripper.

## 2. Files

### Files To Create

| File | Purpose |
| --- | --- |
| `src/hardware/autospinmotor_adapters/gripper_adapter.py` | Translate semantic open/close requests into shared RelayBackend CH1 operations |
| `tests/test_gripper_controller_adapter.py` | Verify polarity, channel mapping, shared relay delegation, errors, and dry-run behavior |
| `tests/test_gripper_config_wiring.py` | Verify YAML, typed configuration, Registry construction, and shared relay identity |
| `tools/gripper_runtime_smoke.py` | Default no-write smoke test with an explicit hardware execution gate |
| `GRIPPER_IMPLEMENTATION_RESULT.md` | Record implementation and validation after approval |

If the existing `src/hardware/gripper_backend.py` already provides the required
typed facade, it should be adapted with the smallest compatibility changes. A
second competing Backend file must not be created.

### Files Expected To Be Modified

| File | Planned change |
| --- | --- |
| `src/hardware/gripper_backend.py` | Restrict responsibilities to semantic commands, software state, stop semantics, and adapter delegation |
| `src/hardware/autospinmotor_adapters/__init__.py` | Export the Gripper adapter |
| `src/config.py` | Add typed `GripperHardwareConfig` and validation |
| `config/hardware.yaml` | Add Gripper runtime configuration |
| `config/devices.yaml` | Record model/protocol/source/migration status |
| `src/webapp/registry.py` | Construct Gripper from config using the already-created shared RelayBackend |
| `src/system_estop.py` | Add Gripper only after emergency policy is approved; otherwise leave integration explicitly pending |
| Existing Registry/config tests | Add Gripper expectations without redesigning Registry |

### Files To Protect

Do not modify:

- `AutoSpinmotorSystem/`
- `src/hardware/drivers/gantry/grbl_controller.py`
- `src/hardware/gantry_backend.py`
- Verified Relay communication frames
- Relay channel write-lock and minimum interval behavior
- Spin motor, Heater, Pipette, and LinearStage implementations
- Gantry motion command generation and software limits

## 3. GripperBackend API

The API should remain typed and explicit about commanded versus physical state.

### `open()`

Proposed signature:

```python
def open(
    self,
    *,
    idempotency_key: str,
    dry_run: bool = False,
    wait: bool = True,
) -> GripperActionResult | GripperActionPlan:
    ...
```

Behavior:

1. Validate that the Backend is configured and the shared RelayBackend is
   available.
2. Plan or delegate CH1 OFF to `GripperAdapter`.
3. Update commanded state to `OPEN` only after successful relay completion.
4. If `wait=True`, wait for configured open settle time.
5. Return commanded state, relay action, duration, and whether the request was a
   no-op.
6. Never report physical position or successful release detection.

### `close()`

Proposed signature:

```python
def close(
    self,
    *,
    idempotency_key: str,
    dry_run: bool = False,
    wait: bool = True,
) -> GripperActionResult | GripperActionPlan:
    ...
```

Behavior:

1. Plan or delegate CH1 ON to `GripperAdapter`.
2. Update commanded state to `CLOSED` only after successful relay completion.
3. If `wait=True`, wait for configured close settle time.
4. Return command completion only, not grip confirmation.

### `stop()`

The hardware has no dedicated stop/reset command. The API must not invent one.

Proposed behavior:

```python
def stop(self) -> GripperStopResult:
    ...
```

`stop()` should:

- Block or cancel subsequent workflow-level Gripper operations where the
  framework supports cancellation.
- Preserve the current relay output.
- Perform no CH1 write by default.
- Return the current commanded state and `physical_state_known=False`.
- Be idempotent.

`stop()` must not silently call `open()`. Emergency release/hold is a separate
policy decision.

If the current synchronous architecture has no cancellable in-flight operation,
`stop()` should be documented as a semantic hold/no-write operation. The relay
write itself is short and cannot be safely recalled after transmission.

### `get_state()`

Proposed signature:

```python
def get_state(self) -> GripperState:
    ...
```

Required fields:

```text
commanded_state: unknown | open | closed
physical_state_known: false
grip_detected: unavailable
relay_channel: 1
last_command_ms_ago: float | null
settling: bool
```

Optional diagnostic fields:

```text
relay_connected
last_error
active_logic
```

These fields must not imply physical feedback.

## 4. Adapter Design

`GripperAdapter` is the only polarity translation layer.

Proposed constructor:

```python
GripperAdapter(
    relay: RelayBackend,
    *,
    channel: int,
    active_logic: str,
)
```

Required mapping for confirmed hardware:

```text
active_logic = "on_closes"
open  -> relay.ch_off(channel=1)
close -> relay.ch_on(channel=1)
```

Adapter responsibilities:

- Validate relay channel `1..8`.
- Translate open/close into relay operations.
- Preserve relay exceptions without reporting false success.
- Pass through dry-run and idempotency metadata.
- Expose no serial-port ownership.
- Expose no Gantry or coordinate behavior.

Adapter non-responsibilities:

- Mechanical settle waiting
- Z or XY movement
- Emergency policy selection
- Grip detection
- Force or position control
- Relay frame generation
- Relay retry or reconnect logic

The verified RelayBackend remains responsible for:

- DSTUR frame generation
- Serial connection
- Shared write lock
- Minimum `0.3 s` inter-command interval
- Retry/reconnect behavior
- Optimistic relay channel state

## 5. Configuration Design

Add a `gripper` section under the unified hardware configuration:

```yaml
hardware:
  gripper:
    backend: "verified_adapter"
    source: "AutoSpinmotorSystem/hardware/xyz_stage/l3_backend/hardware/gripper_backend.py"

    relay_channel: 1
    active_logic: "on_closes"

    open_settle_s: 1.0
    close_settle_s: null
    timeout_s: 2.0

    emergency_behavior: "pending_human_decision"
```

The exact YAML nesting must match the existing `config/hardware.yaml`
structure; the example above describes fields, not authorization to restructure
the file.

### Required Configuration Fields

| Field | Type | Validation |
| --- | --- | --- |
| `backend` | Literal | `verified_adapter` |
| `relay_channel` | Integer | `1..8`; production value `1` |
| `active_logic` | Literal | `on_closes` for confirmed wiring |
| `open_settle_s` | Float | `>= 0` |
| `close_settle_s` | Float or pending | `>= 0`; unresolved before implementation |
| `timeout_s` | Float | `> 0` |
| `emergency_behavior` | Literal | Must remain pending until approved |

### Settle-Time Interpretation

The `0.3 s` relay minimum interval is owned by RelayBackend and is not the same
as Gripper mechanical settle time.

```text
Relay throttle: prevents relay writes too close together
Gripper settle: waits for jaws/sample mechanics before Gantry movement
```

Neither layer should subtract one delay from the other. A relay command may
wait for the throttle and then require the full configured Gripper settle time.

### Timeout Interpretation

The Gripper timeout should bound Backend-level command completion, including:

- Waiting for the shared RelayBackend write lock
- Relay retry/reconnect duration
- Optional settle wait if the public API defines timeout as end-to-end

The implementation plan must choose and document whether settle time counts
inside `timeout_s`. Recommended: treat timeout as end-to-end and reject
configuration where timeout is shorter than the selected settle time plus
expected relay retry allowance.

## 6. DeviceRegistry Wiring

`DeviceRegistry.from_config()` should:

1. Create exactly one `RelayBackend`.
2. Pass that RelayBackend to Gantry for CH2 Z-brake behavior.
3. Create `GripperAdapter` with the same RelayBackend and configured CH1.
4. Create `GripperBackend` with the adapter and timing configuration.
5. Attach the Gripper to the Registry.
6. Add it to `SystemEstop` only after emergency behavior is approved.

Required identity test:

```python
assert registry.gantry.driver.relay is registry.relay
assert registry.gripper.adapter.relay is registry.relay
```

The exact attribute access may differ, but the shared-object invariant must be
testable without opening serial ports.

Registry construction must not connect the relay or move hardware.

## 7. Safety Design

### Emergency Stop

Two policies are physically possible:

| Policy | CH1 behavior | Benefit | Risk |
| --- | --- | --- | --- |
| Hold | No write; preserve current output | Avoid dropping a carried sample | May retain clamp force or leave jaws closed |
| Release | CH1 OFF; open Gripper | Removes close command and may aid access | May drop a sample onto equipment |

No universal default is justified by the current source.

Recommended implementation gate:

- Keep `emergency_behavior: pending_human_decision`.
- Do not register an automatic Gripper action in `SystemEstop` until approved.
- Gantry emergency stop must remain operational regardless of Gripper policy.
- If a policy is later selected, log the requested action and whether the relay
  write succeeded.
- Relay communication failure during emergency handling must remain visible.

### Gantry Z Interaction

The Backend must not move Gantry itself. A workflow coordinator must enforce:

- Gripper open before homing where the fixture assessment confirms this is safe.
- Close completion and settle before lifting Z.
- Safe Z reached before XY transfer.
- Placement Z reached before opening.
- Release settle and Z lift before the next XY transfer.
- No concurrent CH1 command while a safety-critical CH2 Z-brake transition is
  pending.

### Relay Interaction

- Use one shared RelayBackend instance.
- Do not bypass the relay write lock.
- Do not duplicate the `0.3 s` relay throttle in the adapter.
- Update Gripper commanded state only after a successful relay result.
- Treat timeout/retry exhaustion as an unknown physical outcome.
- After relay reconnect or process restart, preserve Gripper state as `UNKNOWN`
  until a deliberate command is issued.

## 8. Runtime Pick Sequence

The workflow-level pick operation must be:

```text
1. Verify Gantry is connected and homed.
2. Verify Gripper state/policy and shared RelayBackend availability.
3. Move Gantry at safe Z to pickup XY.
4. Move Z down to the validated pickup coordinate.
5. Command Gripper close through CH1 ON.
6. Wait for configured close settle time.
7. Move Z up to safe Z.
8. Verify Gantry reports safe Z.
9. Perform XY transfer.
```

Expanded failure handling:

| Failure point | Required response |
| --- | --- |
| Gantry approach fails | Do not command Gripper |
| Gripper close relay write fails | Do not lift or transfer automatically; physical state is unknown |
| Close settle is interrupted | Stop workflow; apply approved emergency policy |
| Z lift fails | Stop XY transfer; preserve/report Gripper commanded state |
| Safe Z not confirmed | Reject XY movement |
| Relay reconnect occurs | Record it and treat physical Gripper outcome conservatively |

Placement should mirror the safety order:

```text
safe-Z XY move -> Z down -> open -> release settle -> Z up -> next XY move
```

## 9. Test Strategy

All automated tests must use mocks and open no serial ports.

### Adapter Unit Tests

1. `open()` maps to CH1 OFF.
2. `close()` maps to CH1 ON.
3. Configured channel is forwarded exactly.
4. Dry-run performs no relay write.
5. Relay errors propagate as structured failures.
6. No serial object is created by the adapter.
7. Unsupported active logic is rejected.

### Backend Unit Tests

1. Initial state is `UNKNOWN`.
2. First `open()` from `UNKNOWN` writes CH1 OFF.
3. First `close()` from `UNKNOWN` writes CH1 ON.
4. State changes only after successful relay completion.
5. Repeated same-state command is a no-op where source-compatible.
6. `get_state()` always reports physical state unknown.
7. `stop()` issues no relay write.
8. Settle wait uses configured open/close values.
9. Timeout behavior is deterministic under a blocked shared relay.
10. Concurrent commands cannot reorder state updates.

### Configuration Tests

1. Production `hardware.yaml` loads the Gripper section.
2. CH1 and `on_closes` are validated.
3. Invalid channel and negative timing values are rejected.
4. Device metadata records the AutoSpinmotorSystem source.
5. Older configuration remains compatible if required by current policy.

### Registry Tests

1. Registry creates Gripper when config is present.
2. Gantry and Gripper share the exact same RelayBackend.
3. Registry construction performs no serial connection or relay write.
4. Gripper remains outside automatic system emergency action while policy is
   pending.

### Workflow Tests

Verify ordered calls:

```text
safe XY -> Z down -> close -> settle -> Z up -> XY transfer
```

Reject:

- XY transfer before safe Z
- Z lift after failed close
- Transfer after unknown close result
- Open/close overlap with unresolved relay failure

### Hardware Validation

After approval and software tests:

1. Confirm stable Raspberry Pi relay device path.
2. Run default smoke mode and verify no relay write.
3. Confirm work area and jaw envelope are clear.
4. Execute explicit open and visually confirm CH1 OFF/open.
5. Execute explicit close and visually confirm CH1 ON/close.
6. Measure open and close travel times.
7. Validate the selected settle values with a representative sample.
8. Validate close-wait-lift at low Gantry speed.
9. Validate placement-open-wait-lift.
10. Test relay reconnect behavior without a suspended sample.
11. Test the approved emergency policy with a sacrificial load and protected
    landing area.

## 10. Implementation Order

1. Resolve emergency behavior and close settle time.
2. Finalize typed Gripper configuration.
3. Add adapter tests.
4. Implement `GripperAdapter`.
5. Reconcile existing `GripperBackend` facade against this plan.
6. Add Backend state, stop, settle, and timeout tests.
7. Wire config loading.
8. Wire DeviceRegistry with the shared RelayBackend.
9. Add workflow-order tests.
10. Run focused Relay, Gantry, config, Registry, and Gripper tests.
11. Run full `pytest`.
12. Add gated runtime smoke test.
13. Perform Raspberry Pi visual/electrical validation.
14. Generate `GRIPPER_IMPLEMENTATION_RESULT.md`.

## 11. Rollback Strategy

- Keep Gripper configuration optional until hardware acceptance is complete.
- Registry must leave `gripper=None` when the section is absent or migration is
  disabled.
- Do not change the already verified Gantry or Relay hardware implementations.
- If Gripper validation fails, remove/disable only Registry Gripper
  construction; Gantry and CH2 Z-brake remain available.
- Never roll back by creating a second relay serial owner.
- Preserve the AutoSpinmotorSystem source unchanged for comparison.

## 12. Unresolved Human Decisions

### Blocking Decision 1: Emergency Release Or Hold

Choose one:

```text
hold:
  no CH1 write; preserve the last commanded relay state

release:
  CH1 OFF; open the Gripper
```

Recommended interim behavior: hold/no-write, with explicit state reporting,
because unconditional release can drop a suspended sample. This recommendation
must be validated against the real actuator's power-loss and mechanical safety
behavior.

### Blocking Decision 2: Close Wait Time

Existing source values:

```text
0.5 s: full linkage default
1.0 s: pickup smoke and experiment runner
```

Recommended initial value: `1.0 s`, followed by measurement on Raspberry Pi
hardware. The final value must account for the slowest expected jaw travel,
sample compliance, relay activation, and acceptable safety margin.

### Additional Decisions

- Whether open and close use separate settle values.
- Whether settle time is included in `timeout_s`.
- Whether open-before-home is mandatory for every fixture.
- Whether startup from `UNKNOWN` requires operator confirmation.
- Stable Raspberry Pi relay device alias.
- Acceptance criteria for the known DSTUR-T80 USB `Errno 5` risk.

## Exit Criteria

Implementation may be considered complete only when:

- CH1 polarity is tested on real hardware.
- One shared RelayBackend serves CH1 Gripper and CH2 Z brake.
- `open()`, `close()`, `stop()`, and `get_state()` match this hardware contract.
- No API claims physical feedback.
- Close settle time is approved and validated.
- Emergency behavior is explicitly approved.
- Pick and placement ordering tests pass.
- Default smoke mode cannot write hardware.
- Raspberry Pi relay reliability is acceptable for supervised operation.

