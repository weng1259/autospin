# Gantry Migration Completed

## Completion Status

The Gantry migration from `AutoSpinmotorSystem` into the `autospin` backend
architecture is complete.

`AutoSpinmotorSystem/hardware/xyz_stage/` remained the source of truth for
hardware behavior. The verified GRBL protocol, motion behavior, coordinate
sign convention, alarm handling, software limits, homing, and emergency-stop
behavior were migrated without redesigning the machine-control logic.

Raspberry Pi real-hardware acceptance has confirmed:

- GRBL serial connection
- `$H` homing
- `$J=G90` absolute XYZ commands
- Coordinated XYZ movement
- Ctrl-X (`0x18`) emergency stop
- `DeviceRegistry` configuration loading
- Runtime control through `GantryBackend`

## 1. Architecture Mapping

| Concern | AutoSpinmotorSystem architecture | autospin architecture |
| --- | --- | --- |
| Experiment-facing control | Direct use of `XYZStage` and orchestration code | `GantryBackend` typed L3 facade |
| Hardware controller | `hardware/xyz_stage/xyz_stage.py` | `src/hardware/drivers/gantry/grbl_controller.py` |
| Serial transport | pyserial inside the XYZ stage implementation | pyserial owned only by `GrblController` |
| Command generation | GRBL/G-code generation in the stage controller | Preserved in `GrblController` |
| Runtime composition | `Maestro` and legacy global configuration | `DeviceRegistry.from_config()` |
| Hardware configuration | Legacy YAML and Python configuration sources | `config/hardware.yaml` plus typed models in `src/config.py` |
| Device metadata | Implicit in legacy project structure | `config/devices.yaml` |
| Software limits | Legacy XYZ stage constants/configuration | Gantry configuration injected into the driver-facing `L3Config` |
| Emergency stop | Direct GRBL soft reset and legacy shutdown paths | Backend delegation plus registration with `SystemEstop` |
| Process coordinates | Legacy process-coordinate files and orchestration | Intentionally separate from the Gantry driver migration |

Final dependency direction:

```text
Routine / System / API
          |
          v
DeviceRegistry
          |
          v
GantryBackend
          |
          v
GrblController
          |
          v
pyserial -> grbl-Mega-5X
```

## 2. Driver Migration

The migrated driver layer is:

```text
src/hardware/drivers/gantry/
    __init__.py
    grbl_controller.py
    README.md
```

`GrblController` owns all hardware-level behavior:

- Serial connection and disconnection
- GRBL command transmission and acknowledgement handling
- Status and machine-position queries
- Absolute coordinated movement
- Homing and alarm unlock
- Feed hold, jog cancellation, and immediate emergency stop
- Alarm parsing and recovery state
- Software soft-limit validation
- Homed-state tracking
- Z-brake sequencing through the configured relay

The required motion command remains:

```text
$J=G90 X<value> Y<value> Z<value> F<feed>
```

All three axes are emitted for `move_to()`. The migration does not accept the
historical Z-only command defect.

Preserved GRBL control commands include:

| Operation | Command |
| --- | --- |
| Home | `$H` |
| Unlock | `$X` |
| Feed hold | `!` |
| Jog cancel | `0x85` |
| Emergency stop / soft reset | Ctrl-X (`0x18`) |

GRBL `ALARM` responses remain structured hardware errors. Emergency stop
clears the software homed state and attempts to return the Z brake to its safe
condition.

## 3. Backend Responsibilities

`GantryBackend` is the experiment-facing facade. It:

- Exposes typed position, status, homing, movement, stop, and recovery APIs.
- Delegates hardware operations to `GrblController`.
- Preserves dry-run planning and idempotency contracts.
- Provides the boundary used by routines, Web APIs, and system emergency stop.
- Accepts configuration and dependency injection for testing.

It does not:

- Import pyserial.
- Write serial bytes.
- Generate GRBL commands.
- Define machine communication behavior.
- Own process coordinates or experimental tool offsets.

This separation keeps verified hardware behavior in the driver while allowing
the framework to compose and observe the device through a stable backend API.

## 4. Configuration Integration

Gantry runtime configuration is loaded through:

```text
config/hardware.yaml + config/devices.yaml
                |
                v
           src/config.py
                |
                v
     DeviceRegistry.from_config()
                |
                v
          GantryBackend
```

The completed production configuration includes:

| Setting | Value |
| --- | --- |
| Model | `grbl-Mega-5X` |
| Protocol | `GRBL` |
| Backend | `verified_driver` |
| Driver | `grbl_controller` |
| Serial port | `/dev/ttyUSB1` |
| Baudrate | `115200` |
| Timeout | `2.0 s` |
| Homing enabled | `true` |
| Default feed | `300 mm/min` |
| X software limits | `-310.0` to `-5.0 mm` |
| Y software limits | `-310.0` to `-5.0 mm` |
| Z software limits | `-110.0` to `-5.0 mm` |
| Migration status | `completed` |

`DeviceRegistry.from_config()` creates the Gantry backend without opening the
serial port. It injects the configured port, baudrate, software limits, and
default feed rate, shares the relay dependency used for the Z brake, and
registers Gantry with `SystemEstop`.

Older configuration files without a Gantry section remain loadable and leave
`registry.gantry` unset.

## 5. Runtime Smoke Test

The runtime entry point is:

```text
tools/gantry_runtime_smoke.py
```

Default invocation is non-moving:

```bash
python tools/gantry_runtime_smoke.py
```

It loads the production configuration and constructs the Registry, but does
not connect to a serial port or issue hardware commands.

Real execution requires both explicit arguments:

```bash
python tools/gantry_runtime_smoke.py \
  --execute-hardware \
  --confirm MOVE_GANTRY_TO_X-20_Y-20_Z-10
```

The authorized sequence is:

1. Load configuration and create the Registry.
2. Connect Gantry.
3. Query initial status.
4. Home with `$H`.
5. Move to `X=-20`, `Y=-20`, `Z=-10`.
6. Query final position.
7. Disconnect in a `finally` block.

The smoke test no longer requires FastAPI. A regression test runs it in a fresh
Python process where all `fastapi` imports are deliberately blocked.

Software regression results from the final smoke-import phase:

```text
Focused smoke/Web/config tests: 24 passed
Full suite excluding one known timing-edge test:
394 passed, 1 skipped, 1 deselected
```

The Raspberry Pi hardware run additionally verified configuration loading,
backend construction, connection, homing, coordinated XYZ movement, final
runtime control, and emergency stop.

## 6. Safety Validation

The completed migration preserves the following safety boundaries:

- Complete XYZ targets are checked against software limits before motion.
- Out-of-range commands are rejected before serial movement commands.
- Machine/GRBL limits, software safety limits, and process coordinates remain
  separate concepts.
- Homing uses the verified `$H` behavior.
- Alarm state and alarm codes are propagated instead of reported as success.
- Immediate emergency stop sends Ctrl-X (`0x18`).
- Emergency stop invalidates the homed state.
- Gantry participates in the framework-level `SystemEstop`.
- Runtime smoke movement requires an explicit flag and exact confirmation.
- Runtime cleanup disconnects after success or failure.

Real-hardware validation on Raspberry Pi has closed the earlier acceptance
risks for GRBL communication, homing, absolute coordinated XYZ motion, and
Ctrl-X emergency stop.

## 7. Remaining Limitations

1. GRBL-reported position is controller state, not independent encoder or
   external metrology feedback. Mechanical slip cannot be detected solely from
   the reported coordinates.
2. `/dev/ttyUSB1` may change after USB re-enumeration. A stable udev alias should
   be used for long-term deployment.
3. Process coordinates, station coordinates, tool offsets, and collision-aware
   transfer planning remain outside the Gantry driver and require their own
   controlled integration.
4. Software limits reduce command risk but do not replace GRBL hard limits,
   physical limit switches, a clear work envelope, or operator supervision.
5. Homing and the smoke-test target must be revalidated after mechanical,
   firmware, coordinate, tool, or fixture changes.
6. Z-brake safety depends on correct relay configuration and physical wiring;
   relay commanded state is not independent electrical feedback.
7. A private compatibility bridge remains for older diagnostics and tests. It
   should be removed only after those consumers use driver-level fixtures.
8. The test suite contains a known floating-point timing-edge assertion around
   `100 ms`; it is not a motion-protocol or hardware acceptance failure.
9. Gripper behavior and process-coordinate registry migration are not part of
   this completed Gantry scope.

## Final Acceptance

The Gantry migration is complete at all required layers:

- Verified hardware behavior is owned by the migrated GRBL driver.
- Framework-facing behavior is exposed through `GantryBackend`.
- Runtime parameters are supplied by the unified configuration system.
- `DeviceRegistry` constructs and wires the backend.
- Default smoke execution is hardware-safe.
- Raspberry Pi real-hardware operation has been accepted.

The Gantry is ready for use as an autospin backend device, subject to the
remaining operational limitations and the normal pre-run safety checklist.

