# Gripper Implementation Result

## Outcome

The IO-only Gripper from `AutoSpinmotorSystem` is integrated into the autospin
runtime architecture.

Confirmed behavior:

```text
DSTUR-T80 CH1 ON  = close
DSTUR-T80 CH1 OFF = open
```

The implementation does not add position, force, object, or gripping feedback.
It reports only the last successfully commanded software state.

## Architecture

```text
Routine / API / SystemEstop
             |
             v
       GripperBackend
   semantic open/close/state
             |
             v
  Shared existing RelayBackend
 write lock + 0.3 s interval
             |
             v
       DSTUR-T80 CH1
```

`GripperBackend` does not own a serial port and does not generate relay frames.
Gantry and Gripper receive the same `RelayBackend` instance from
`DeviceRegistry.from_config()`.

## API

The completed Backend exposes:

| API | Behavior |
| --- | --- |
| `open()` | Requests CH1 OFF through RelayBackend and records commanded state OPEN |
| `close()` | Requests CH1 ON, records CLOSED, then waits the configured stabilization time |
| `stop()` | Stops at the semantic boundary without changing relay output; returns current software state |
| `get_state()` | Returns commanded state and command age with `position_known=False` |
| `emergency_release()` | Forces the semantic release path through RelayBackend using CH1 OFF |

The initial state remains `UNKNOWN`. State is updated only after the RelayBackend
operation succeeds.

## Emergency Behavior

The approved emergency policy is release.

`SystemEstop` now executes:

```text
1. Gantry immediate abort
2. Gripper emergency release through RelayBackend
3. Spincoater stop with brake
4. LinearStage stop
5. Pipette stop
6. Heater SV to zero, when enabled
```

Gripper release does not write serial bytes directly and does not call relay
`all_off()`. It requests only the configured Gripper channel through the shared
RelayBackend, preserving Gantry Z-brake ownership and relay serialization.

Failure of Gripper emergency release is recorded as an independent estop step
and does not prevent later emergency actions.

## Configuration

Added to `config/hardware.yaml`:

```yaml
gripper:
  backend: relay
  channel: gripper
  close_wait_s: 1.0
  emergency_release: true
```

The symbolic channel resolves through the existing Relay configuration:

```yaml
relay:
  settle_s: 0.3
  channel_map:
    gripper: 1
    z_brake: 2
```

`GripperHardwareConfig` validates:

- Backend is `relay`.
- Channel name is non-empty.
- Close wait is non-negative.
- Emergency release is explicitly enabled or disabled.

The Gripper section remains optional at the top-level schema boundary for
backward compatibility. Production configuration includes it.

`config/devices.yaml` records the AutoSpinmotorSystem source and marks Gripper
migration completed.

## DeviceRegistry Integration

`DeviceRegistry.from_config()` now:

1. Creates the existing RelayBackend once.
2. Resolves `gripper` through `relay.channel_map`.
3. Creates GripperBackend with channel 1 and `close_wait_s=1.0`.
4. Passes the same RelayBackend to Gantry and Gripper.
5. Registers Gripper with `SystemEstop`.
6. Performs no serial connection during Registry construction.

Missing symbolic channel configuration raises a clear runtime configuration
error instead of silently selecting another channel.

Mock Registry behavior was also updated so software emergency-stop tests release
the mock Gripper.

## Changed Files

| File | Change |
| --- | --- |
| `src/hardware/gripper_backend.py` | Added close stabilization, stop, and emergency release while preserving IO-only state semantics |
| `src/config.py` | Added typed Gripper hardware configuration |
| `config/hardware.yaml` | Added production Gripper runtime settings |
| `config/devices.yaml` | Added Gripper identity, source, protocol, and completed status |
| `src/webapp/registry.py` | Added shared-Relay Gripper construction and estop wiring |
| `src/webapp/mock_devices.py` | Added mock stop and emergency release behavior |
| `src/system_estop.py` | Added approved Gripper release step after Gantry abort |
| `tests/test_gripper_backend.py` | Added stop, emergency release, and close-wait tests |
| `tests/test_config_wiring.py` | Added schema, metadata, production YAML, Registry, and shared Relay assertions |
| `tests/test_system_estop.py` | Added Gripper release ordering and reporting |
| `tests/test_webapp_mock_registry.py` | Updated Web estop acceptance for Gripper release |

## Tests

Focused Gripper, Relay, Gantry, configuration, Registry, and Web emergency-stop
regression:

```text
172 passed, 1 warning
```

Full test invocation:

```text
399 passed, 1 skipped, 1 failed
```

The sole failure is the pre-existing Gantry timing-edge assertion:

```text
99.999755859375 ms >= 100.0 ms
```

Full regression excluding that known unstable timing-boundary test:

```text
399 passed, 1 skipped, 1 deselected, 1 warning
```

The warning is the existing Starlette `TestClient`/`httpx` deprecation warning.

## Protected Implementation

This phase did not modify:

- `src/hardware/gantry_backend.py`
- `src/hardware/drivers/gantry/grbl_controller.py`
- Relay frame format
- Relay serial communication and reconnect logic
- Relay write lock or `0.3 s` minimum interval
- Spin motor, Heater, Pipette, or LinearStage hardware logic
- `AutoSpinmotorSystem`

## Remaining Risks

1. Relay command success is not physical jaw feedback.
2. Emergency release can drop a carried sample. The approved policy must be
   reflected in operating procedures and protected landing zones.
3. The `1.0 s` close wait is configuration-based and still requires physical
   validation with representative samples.
4. Raspberry Pi DSTUR-T80 USB stability and previously observed `Errno 5`
   failures remain hardware acceptance concerns.
5. Relay state is optimistic after restart or USB reconnection.
6. Pick/place Gantry ordering remains a workflow responsibility:
   close, wait, raise Z, then transfer XY.
7. No real Gripper hardware was operated during this software implementation.

