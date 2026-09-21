# Gripper Hardware Source Audit

## Scope And Source Rule

This audit uses only `AutoSpinmotorSystem` as the source of hardware behavior.
No `autospin` Gripper implementation was used as a behavioral reference, and
no implementation files were modified.

The Gripper in the verified source is an IO-controlled actuator. It has:

- Open command
- Close command
- Relay channel mapping
- Software commanded-state tracking

It does not have:

- Position feedback
- Grip detection
- Force feedback
- Object-presence detection
- A dedicated smart-controller stop or reset command

## Audited Sources

Primary behavior sources:

| Source | Role |
| --- | --- |
| `hardware/xyz_stage/l3_backend/hardware/gripper_backend.py` | Semantic open/close behavior and software state |
| `hardware/xyz_stage/l3_backend/hardware/relay_backend.py` | Verified DSTUR-T80 serial frames, timing, retry, and channel state |
| `config/system_config.yaml` | Relay channel assignment, baudrate, Gantry safe Z, and tool offset |
| `test_gantry_gripper_pick.py` | Real-hardware pickup sequence and Gantry interaction |
| `test_full_linkage_sequence.py` | Multi-device transfer sequence and configurable settle timing |
| `example_experiment.py` | Recipe execution, settle timing, safe XYZ movement, and emergency cleanup |
| `examples/one_round_from_process_coordinates.json` | Concrete ordering of Gantry and Gripper operations |
| `CHANGELOG.md` | Raspberry Pi relay failures and operational findings |

Secondary/legacy source:

| Source | Status |
| --- | --- |
| `hardware/relay/relay_manager.py` | Older LCUS-8 style relay manager; useful for frame comparison but not the current Gantry-shared Gripper path |

## 1. Existing Gripper Control Logic

The active Gripper path is:

```text
Experiment / pickup workflow
          |
          v
GripperBackend
  open()
  close()
  get_state()
          |
          v
Shared RelayBackend
          |
          v
DSTUR-T80 CH1
          |
          v
24 V Gripper control input
```

`GripperBackend` is a semantic translation layer. It receives an already
constructed shared `RelayBackend` and defaults to channel 1.

### Open

```text
GripperBackend.open()
    -> RelayBackend.ch_off(CH1)
    -> CH1 output OFF
    -> Gripper opens/releases
```

### Close

```text
GripperBackend.close()
    -> RelayBackend.ch_on(CH1)
    -> CH1 output ON
    -> Gripper closes/grips
```

The backend tracks:

- `UNKNOWN` before the first successful command
- `OPEN` after a successful open command
- `CLOSED` after a successful close command
- Time since the last successful command

The first command from `UNKNOWN` must be transmitted. Subsequent requests for
the already-commanded state are treated as no-ops and do not write the relay.

This state is optimistic software state only. A successful serial write is
assumed to mean the physical relay and Gripper changed state.

## 2. Relay Channel Mapping

`config/system_config.yaml` defines:

| Function | Channel |
| --- | ---: |
| Gripper | CH1 |
| Gantry Z brake | CH2 |
| Vacuum valve | CH3 |
| Spin power | CH4 |
| Auxiliary light | CH5 |

The Gripper and Gantry Z brake share the same DSTUR-T80 relay controller and
serial connection, but use separate channels.

Relevant relay settings:

| Setting | Value |
| --- | --- |
| Controller | DSTUR-T80 / 8-channel USB relay path |
| Gripper channel | `1` |
| Baudrate | `9600` |
| Valid channel range | `1` through `8` |
| Command prefix | `0xA0` |
| ON state byte | `0x01` |
| OFF state byte | `0x00` |

The verified relay frame is:

```text
[0xA0, channel, state, checksum]
checksum = (0xA0 + channel + state) & 0xFF
```

Therefore:

```text
Gripper close: [0xA0, 0x01, 0x01, 0xA2]
Gripper open:  [0xA0, 0x01, 0x00, 0xA1]
```

The shared `RelayBackend` must remain the single owner of the DSTUR serial
connection. A separate Gripper serial client would conflict with Gantry
Z-brake commands and bypass the relay write lock.

## 3. Open/Close Electrical Logic

The source documents the physical wiring as:

- Gripper motor supply `V+` and `V-` remain connected to 24 V.
- The Gripper control input marked `24V` is switched by DSTUR-T80 CH1.
- `CH1 ON` applies the control voltage and commands close.
- `CH1 OFF` removes the control voltage and commands open.
- The Gripper RS485 differential interface is not used by this control path.

The expected polarity is therefore active-high close:

| Logical action | CH1 | Electrical output | Physical intent |
| --- | --- | --- | --- |
| Open | OFF | 24 V control removed | Release/open |
| Close | ON | 24 V control applied | Grip/close |

`test_full_linkage_sequence.py` includes an `--invert-gripper` diagnostic
option. This is evidence that wiring polarity has needed field verification,
not a second canonical behavior. Migration must use the configured and
physically verified polarity rather than expose inversion as an unnoticed
runtime choice.

There is no electrical status readback in the relay protocol. The system
cannot prove that the CH1 contact changed or that the jaws reached their target.

## 4. Timing Requirements

Timing exists at two different layers and must not be conflated.

### Relay transport timing

The verified `RelayBackend` enforces:

| Timing | Value | Purpose |
| --- | ---: | --- |
| Minimum interval between relay writes | `0.3 s` | Allow relay action and reduce accumulated EMI peaks |
| Delay after opening/reopening serial port | `0.5 s` | Allow USB relay connection to settle |
| Reconnect attempts after write failure | `3` | Recover from stale USB serial handles |
| Reconnect backoff | Multiples of `0.5 s` | Avoid immediate repeated writes during USB recovery |

The write lock serializes CH1 Gripper and CH2 Z-brake commands. This is
important because a Gripper command and a Z movement can otherwise compete for
the same relay serial interface.

### Mechanical/process settle timing

`GripperBackend.open()` and `close()` return after the relay command succeeds.
They do not wait for jaw travel or verify mechanical completion.

Observed workflow delays:

| Source | Delay |
| --- | ---: |
| `example_experiment.py` | `1.0 s` after both open and close |
| `test_gantry_gripper_pick.py` | Default `1.0 s` after close before lifting |
| `test_full_linkage_sequence.py` | Default `0.5 s` after close before lifting |

There is no single reconciled, hardware-validated Gripper travel time in the
source. Until measured on the actual Gripper, migration planning should treat
`1.0 s` as the conservative existing workflow value and make it configuration,
not a hidden constant in the hardware adapter.

An open-settle delay is present in the recipe runner but not consistently in
the standalone linkage scripts.

## 5. Safety Behavior

### Preserved safety properties

- Channel values outside `1..8` are rejected.
- Relay writes are serialized with a lock.
- Repeated commands for the same software state are no-ops.
- The initial `UNKNOWN` state never suppresses the first physical command.
- Serial write failures raise `RelayCommunicationError`; they are not reported
  as successful Gripper movement.
- Relay write failure triggers close/reopen/retry behavior.
- Dry-run reports the intended state and channel without writing hardware.
- Interactive real-hardware tests require operator confirmations.

### Known safety limitations

- Command success means only that a frame was written successfully.
- There is no jaw position, grip, force, current, or sample-presence feedback.
- Relay state after process restart is not physically queried.
- The default software relay state is OFF, but physical startup state is not
  independently verified.
- Loss of 24 V or USB communication can leave the actual Gripper state unknown.
- The source has no dedicated Gripper stop/reset operation.

### Emergency behavior

`example_experiment.py` and `test_gantry_gripper_pick.py` use this interrupt
pattern:

1. Emergency-stop the Gantry.
2. Attempt to open the Gripper.
3. Close hardware connections.

Opening is treated as the cleanup state in these sources. This is not
universally safe: if the Gripper is carrying a sample above equipment, opening
can drop the sample. The final migration must not label unconditional open as
safe without a human decision based on load, location, and power-loss behavior.

The older `RelayManager.emergency_stop()` turns all relay channels off. Under
the canonical polarity, that also opens the Gripper, locks the Z brake if CH2
OFF is its safe state, and disables other relay-controlled outputs. This broad
all-off behavior must not be copied blindly into a semantic Gripper emergency
API.

## 6. Interaction With Gantry Z Movement

The verified source couples Gripper handling to a strict Gantry sequence.

### Before homing

The interactive pickup test opens the Gripper before Gantry homing:

```text
open Gripper
    -> home Gantry
```

This reduces the chance of an extended or closed jaw colliding with a fixture
during the large homing movement, but assumes an open jaw is mechanically the
safer envelope.

### Pickup

The pickup sequence is:

```text
move above/at pickup coordinate
    -> lower Z to pickup height
    -> close Gripper
    -> wait 0.5-1.0 s
    -> raise Z to safe height
    -> perform XY transfer
```

The close command must complete and the configured mechanical settle time must
elapse before lifting Z.

### Placement

The placement sequence is:

```text
move XY while at safe Z
    -> lower Z to placement height
    -> open Gripper
    -> wait for release if configured
    -> raise Z before the next XY move
```

The recipe runner's `MoveGantrySafe` behavior separately enforces:

```text
raise Z to safe Z
    -> move XY
    -> lower Z to target
```

This movement policy belongs to workflow/coordinate orchestration, not to the
relay-level Gripper adapter.

### Shared relay ordering

Gantry Z motion may toggle the Z brake on CH2 while Gripper uses CH1. Both
commands pass through one `RelayBackend`, which provides:

- One serial connection
- One write lock
- A minimum `0.3 s` command interval
- Shared reconnect behavior

The migration must inject the same relay instance into Gantry and Gripper.
Creating separate instances for the same USB relay would remove serialization
and can cause port ownership conflicts.

## Raspberry Pi Evidence And Risks

`CHANGELOG.md` records that a real recipe progressed through safe-Z and XY
movement, then failed while writing `GripperClose` to DSTUR-T80 CH1 with:

```text
SerialException('write failed: [Errno 5] Input/output error')
```

The same relay device has also shown failures on CH2 Z-brake operations. The
source added reconnect retries and reduced unnecessary relay writes, but this
does not eliminate physical USB, power, grounding, or EMI risk.

Before migration acceptance, hardware validation must inspect:

- Raspberry Pi `dmesg`
- Stable `/dev/serial/by-id/` or udev alias
- USB cable and hub quality
- Relay power stability
- Common grounding
- Flyback/surge suppression
- Motor and relay EMI
- Exclusive serial-port ownership

## Required Migration Contract

The future adapter/backend must preserve this behavior:

```text
GripperBackend
      |
      v
GripperAdapter
      |
      v
Shared verified Relay controller
      |
      v
DSTUR-T80 CH1
```

Required semantic operations:

- `open()`: CH1 OFF
- `close()`: CH1 ON
- `get_state()`: commanded software state only
- Optional dry-run plan

Required state semantics:

- Initial state is `UNKNOWN`.
- First open or close always writes.
- State updates only after successful relay completion.
- Report that physical position is unknown.

No force, grip detection, position feedback, or smart Gripper capability should
be designed.

## Files To Migrate Or Reference

Behavior that must be adapted, not directly copied as a folder:

| Source | Migration treatment |
| --- | --- |
| `hardware/xyz_stage/l3_backend/hardware/gripper_backend.py` | Preserve semantic mapping and state behavior through an adapter |
| `hardware/xyz_stage/l3_backend/hardware/relay_backend.py` | Reuse the already migrated verified relay path; do not create a second serial owner |
| `config/system_config.yaml` relay channel mapping | Map CH1 and polarity into unified configuration |
| Pickup and experiment workflows | Preserve settle-before-lift and safe-Z transfer ordering at orchestration level |

Files that should remain legacy references:

- `hardware/relay/relay_manager.py`
- Interactive test scripts as direct production APIs
- Legacy Maestro/Worker composition
- Generated multi-round recipe files

These sources contain useful evidence but should not be copied into the new
hardware package.

## Blocking Human Decisions

1. Confirm on the deployed wiring that `CH1 ON=close` and `CH1 OFF=open`.
2. Choose and validate one mechanical settle time for open and close; current
   source values differ between `0.5 s` and `1.0 s`.
3. Decide emergency load policy: hold the current state, open, or use a
   location-aware response. Unconditional open can drop a carried sample.
4. Confirm whether Gripper-open-before-home is safe for every installed tool
   and fixture.
5. Confirm the stable Raspberry Pi relay device path and resolve recorded
   DSTUR-T80 `Errno 5` failures before autonomous use.
6. Confirm the Z-brake and Gripper command ordering when both channels need to
   switch within one transfer step.
7. Decide whether process restart must force a physical open command or require
   operator reconciliation from `UNKNOWN`.

## Audit Conclusion

The verified source defines a simple relay-driven Gripper:

```text
CH1 OFF = open
CH1 ON  = close
```

Its software logic is suitable for an adapter migration, but its safety depends
on workflow ordering, shared-relay serialization, configured settle time, and
operator awareness that no physical feedback exists.

Gripper implementation should not start until relay polarity, settle timing,
emergency load policy, and the Raspberry Pi relay reliability issue are
explicitly resolved.

