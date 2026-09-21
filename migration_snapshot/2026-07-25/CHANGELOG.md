# Changelog

This file records project-level software, configuration, test, deployment, and
documentation changes. Runtime logs, experiment data, caches, and generated
results are not changelog content.

## 2026-07-25 - Web parity, configuration alignment, and migration snapshot

### Added

- Added runtime configuration API and configuration-driven browser limits.
- Added process-coordinate read/write with Gantry soft-limit validation.
- Added multi-round routine generation with per-round XYZ offsets and
  post-offset soft-limit validation.
- Added Gantry Web controls for dry-run, homing diagnostics, Z brake, and
  immediate halt.
- Added `WEB_SERVER_USAGE.md` and migration snapshot documentation.
- Added Web configuration and multi-round regression tests.

### Updated

- Updated the browser Gantry ranges to use the backend configuration
  (`X/Y -310..-5 mm`, `Z -110..-5 mm`) instead of stale HTML values.
- Updated Spin, Heater, Pipette, and Linear Stage browser ranges to come from
  `config/hardware.yaml`.
- Kept serial ports and hardware topology under configuration ownership
  instead of restoring browser-supplied port overrides from the legacy server.

### Verification

- `python -m pytest tests/test_webapp_configuration.py
  tests/test_webapp_static.py tests/test_webapp_routines.py -q`
- Result: 17 passed, 1 dependency deprecation warning.

## 2026-07-25 - AutoSpinmotorSystem backend migration completion

### Added

- Added verified hardware drivers and adapters under `src/hardware/` for:
  - DBLS400 Spin Motor
  - AI-516 Heater
  - Pipette
  - RS485 Linear Stage
  - Relay
  - GRBL Gantry
  - IO-only Gripper
- Added unified runtime configuration:
  - `config/hardware.yaml`
  - `config/devices.yaml`
  - Typed hardware models in `src/config.py`
- Added `DeviceRegistry.from_config()` construction for migrated hardware.
- Added Gantry runtime smoke test with an explicit two-part hardware gate:

  ```text
  --execute-hardware
  --confirm MOVE_GANTRY_TO_X-20_Y-20_Z-10
  ```

- Added Gripper runtime integration:
  - DSTUR-T80 CH1 ON closes
  - DSTUR-T80 CH1 OFF opens
  - `close_wait_s=1.0`
  - Emergency release through the shared RelayBackend
- Added system emergency-stop integration for Gantry, Gripper, Spin Motor,
  Linear Stage, Pipette, and Heater.
- Added migration, configuration, deployment, runtime, and acceptance reports,
  including:
  - `GANTRY_MIGRATION_COMPLETED.md`
  - `GRIPPER_HARDWARE_SOURCE_AUDIT.md`
  - `GRIPPER_IMPLEMENTATION_PLAN.md`
  - `GRIPPER_IMPLEMENTATION_RESULT.md`
  - `HARDWARE_ACCEPTANCE_TEST_PLAN.md`
  - `HARDWARE_ACCEPTANCE_RESULT.md`
  - `RASPBERRY_PI_SYNC_REPORT.md`
- Added autospin project documentation:
  - `AUTOSPIN_说明文档.md`
  - `EXPERIMENT_RUNBOOK.md`

### Updated

- Updated `GantryBackend` to delegate all GRBL hardware behavior to
  `src/hardware/drivers/gantry/grbl_controller.py`.
- Updated Gantry motion to preserve the verified complete command:

  ```text
  $J=G90 X... Y... Z... F...
  ```

- Updated Gantry configuration with:
  - `/dev/ttyUSB1`
  - `115200 baud`
  - X/Y software limits `-310..-5 mm`
  - Z software limits `-110..-5 mm`
  - `$H`, `$X`, and Ctrl-X command metadata
- Updated DeviceRegistry so Gantry and Gripper share one RelayBackend instance.
- Updated SystemEstop ordering so Gantry motion abort occurs before Gripper
  emergency release.
- Updated Web package imports so the Gantry runtime smoke test can use
  DeviceRegistry without requiring FastAPI.
- Preserved mock mode for all migrated Backend paths.

### Fixed

- Fixed the migrated Gantry XYZ command path that previously generated a
  Z-only command for an XYZ API.
- Fixed Gantry configuration status from deferred to completed.
- Fixed the Gripper emergency policy to use the confirmed release behavior
  through RelayBackend instead of direct serial access or relay `all_off()`.
- Fixed old configuration compatibility by keeping Gantry and Gripper sections
  optional at the top-level schema boundary.
- Fixed runtime smoke import coupling to eager FastAPI imports.

### Hardware Verification

Raspberry Pi real-hardware verification was reported complete for:

- GRBL connection
- `$H` homing
- `$J=G90` absolute XYZ movement
- Coordinated XYZ motion
- Ctrl-X emergency stop
- DeviceRegistry configuration loading
- GantryBackend runtime control

Gripper software integration is complete. Final physical Gripper acceptance
remains governed by `HARDWARE_ACCEPTANCE_TEST_PLAN.md`.

### Software Verification

- Focused Gripper/Relay/Gantry/configuration/Web emergency-stop regression:

  ```text
  172 passed
  ```

- Full regression excluding the known timing-edge test:

  ```text
  399 passed, 1 skipped, 1 deselected
  ```

- Known unstable test:

  ```text
  tests/test_get_status_concurrency.py::
  test_get_status_returns_fresh_snapshot_during_lock_hold
  ```

  It can observe `99.999755859375 ms` against an exact `>=100.0 ms`
  assertion. This is a floating-point/timing boundary and not a hardware
  protocol failure.

### Remaining Risks

- DSTUR-T80 USB relay communication has historically produced Raspberry Pi
  `Errno 5` failures; validate USB power, grounding, EMI suppression, cable,
  hub, stable udev path, and exclusive port ownership.
- Gripper has no position, force, grip, or sample-presence feedback.
- Gripper emergency release can drop a carried sample.
- Process-coordinate editing and multi-round recipe generation are not yet
  integrated into the current autospin Web UI.
- The Web Gantry numeric inputs still expose legacy ranges
  (`X/Y -275..-5`, `Z -90..-5`) while the Backend configuration allows
  (`X/Y -310..-5`, `Z -110..-5`). Backend validation remains authoritative,
  but the UI cannot yet address the complete configured range.
- Hardware acceptance evidence must be completed in
  `HARDWARE_ACCEPTANCE_RESULT.md`.
