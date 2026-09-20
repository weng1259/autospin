# Gantry Driver Migration Result

## Outcome

The verified Gantry hardware behavior has been separated from the autospin L3
backend and placed in:

```text
src/hardware/drivers/gantry/
    __init__.py
    grbl_controller.py
    README.md
```

`AutoSpinmotorSystem/hardware/xyz_stage/` remained the only hardware-behavior
source. The motion protocol and safety behavior were moved, not redesigned.

## Architecture

```text
Routine / Web / System
          |
          v
GantryBackend
  typed L3 facade
  no serial or G-code generation
          |
          v
GrblController
  GRBL communication and state
  command generation
  homing and alarm recovery
  software limits
  emergency stop
          |
          v
pyserial -> grbl-Mega-5X
```

The Backend owns a `GrblController` instance and explicitly delegates all
public operations. A narrow private-attribute compatibility bridge remains for
existing diagnostics and tests; hardware operations still execute only in the
driver.

## Modified files

| File | Change |
|---|---|
| `src/hardware/drivers/gantry/grbl_controller.py` | Contains the verified GRBL hardware implementation as `GrblController` |
| `src/hardware/drivers/gantry/__init__.py` | Exports `GrblController` |
| `src/hardware/drivers/gantry/README.md` | Documents the driver/backend boundary |
| `src/hardware/gantry_backend.py` | Replaced direct hardware implementation with a driver-backed L3 facade |
| `tests/test_gantry_grbl_controller.py` | Adds direct driver communication, command, limit, alarm, homing, stop, and delegation tests |
| `GANTRY_DRIVER_MIGRATION_RESULT.md` | Records this migration and its verification |

The Spin Motor, Heater, Pipette, Relay, and configuration implementation files
were not modified.

## Driver API

`GrblController` exposes:

- `connect()`
- `disconnect()`
- `home()`
- `unlock()`
- `move_to(x, y, z, feed)`
- `get_position()`
- `stop()`
- `emergency_stop()`

It also retains the source-compatible typed APIs needed by the existing
Backend, including status queries, dry-run planning, GRBL settings validation,
alarm recovery, manual recovery, and asynchronous movement.

## Preserved hardware behavior

### Serial communication

- pyserial remains owned by the GRBL driver.
- Baud rate remains constructor/configuration controlled, with the established
  default of `115200`.
- Existing command acknowledgement, RX-buffer accounting, status polling,
  timeout, disconnect, and stale-status handling remain in the driver.

### Absolute XYZ motion

The driver generates:

```text
$J=G90 X<value> Y<value> Z<value> F<feed>
```

Verified test example:

```text
$J=G90 X-153.250 Y-237.500 Z-74.125 F2500
```

Z-only command generation is not accepted.

### Homing and unlock

- Homing still uses `$H`.
- Alarm unlock still uses `$X`.
- The hardware-level `home()` can generate its own idempotency key, while
  `GantryBackend.home()` preserves the L3 caller-provided idempotency contract.

### Stop and emergency stop

- `stop()` retains feedhold `!` followed by jog cancel `0x85`.
- `emergency_stop()` retains immediate GRBL Ctrl-X (`0x18`), clears the
  software homed state, and attempts to lock the Z brake.

### Alarm handling

- GRBL ALARM responses remain structured as `AlarmStateError`.
- Alarm code and cached machine state are updated before the exception is
  returned or raised.
- Existing ALARM:8 homing diagnostics and recovery behavior remain in the
  driver.

### Software limits

The driver validates the complete target before brake or serial activity:

| Axis | Minimum | Maximum |
|---|---:|---:|
| X | -310.0 | -5.0 |
| Y | -310.0 | -5.0 |
| Z | -110.0 | -5.0 |

Machine/GRBL limits, software safety limits, and process coordinates remain
separate concepts. This migration did not introduce process-coordinate
handling into the driver.

## Tests added

Direct driver tests cover:

1. Complete `$J=G90 X Y Z F` command generation.
2. All six out-of-range boundaries rejected before serial write.
3. Confirmed minimum and maximum targets accepted.
4. GRBL alarm parsing and cached ALARM state.
5. `$X` unlock command.
6. Ctrl-X emergency stop and homed-state clearing.
7. Driver disconnect behavior.
8. Hardware-level `home()` dry-run without an external idempotency key.
9. GantryBackend delegation to an injected driver.

All GRBL communication in these tests uses mocks; no physical serial port is
opened.

## Test results

Focused Gantry, idempotency, schema, and concurrency regression:

```text
73 passed
```

Expanded Gantry/API regression during implementation:

```text
90 passed, 1 warning
```

Full suite with one known timing-edge test deselected:

```text
387 passed, 1 skipped, 1 deselected, 1 warning
```

The deselected test was:

```text
tests/test_get_status_concurrency.py::
test_get_status_returns_fresh_snapshot_during_lock_hold
```

It passed when run alone. In two full-suite runs it observed
`99.999755859375 ms` against an exact `>=100.0 ms` assertion. This is an
existing floating-point/timing boundary, not a Gantry protocol failure. Neither
production code nor the test threshold was changed for it.

The warning is the existing FastAPI/Starlette `httpx` deprecation warning.

## Compatibility

- Existing L3 method signatures remain available on `GantryBackend`.
- Existing API schema output remains unchanged.
- Existing 24-hour idempotency metadata for `home()` and
  `recover_from_alarm()` remains visible on the Backend facade.
- Existing diagnostics that inspect the Gantry object's private state continue
  to reach the driver during the transition.
- `GantryBackend` contains no pyserial import, serial write, or GRBL command
  construction.

## Remaining risks

1. No Raspberry Pi or physical GRBL controller was connected during this
   migration.
2. Raspberry Pi validation must confirm `/dev/gantry`, permissions, baud rate,
   and exclusive serial ownership.
3. Low-feed tests must verify X-only, Y-only, Z-only, XY, and complete XYZ
   movement and compare final machine position on every axis.
4. `$H`, `$X`, jog cancel, Ctrl-X emergency stop, hard-limit alarms, and Z
   brake sequencing require real-hardware confirmation.
5. Live GRBL settings must be checked against the confirmed machine setup
   before motion.
6. The private compatibility bridge should remain only until diagnostics and
   older tests are converted to driver-level fixtures.
7. Gantry hardware validation must not implicitly enable or migrate Gripper
   behavior.

## Next validation gate

Before experiment use:

1. Deploy only the updated Gantry source and tests through the protected
   Raspberry Pi synchronization procedure.
2. Run import and mock tests on the Pi.
3. Confirm emergency-stop access with motors mechanically safe.
4. Home once with the work envelope clear.
5. Perform short low-feed movements away from all boundaries.
6. Verify reported final positions and alarm propagation.

The driver migration is software-complete; real-hardware acceptance remains
pending.
