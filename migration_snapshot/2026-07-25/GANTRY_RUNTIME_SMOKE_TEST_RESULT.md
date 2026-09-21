# Gantry Runtime Smoke Test Result

## Scope

Added a configuration-wired Gantry runtime smoke test at:

- `tools/gantry_runtime_smoke.py`
- `tests/test_gantry_runtime_smoke.py`

The smoke test uses:

```text
config/hardware.yaml
        |
        v
DeviceRegistry.from_config()
        |
        v
GantryBackend
```

No changes were made to `src/hardware/gantry_backend.py`,
`src/hardware/drivers/gantry/grbl_controller.py`, or hardware behavior.

## Safety Gate

Default execution is non-hardware dry-run:

```bash
python tools/gantry_runtime_smoke.py
```

It loads the production configuration, creates the Registry, verifies that a
Gantry backend exists, and displays the planned safe target. It does not call
`connect()`, `home()`, `move_to()`, or `disconnect()`.

Real hardware execution requires both an action flag and an exact confirmation:

```bash
python tools/gantry_runtime_smoke.py \
  --execute-hardware \
  --confirm MOVE_GANTRY_TO_X-20_Y-20_Z-10
```

Supplying `--execute-hardware` without the exact confirmation is rejected by
the command-line parser.

## Hardware Sequence

After explicit authorization, the script performs:

1. Load `config/hardware.yaml`.
2. Create `DeviceRegistry` with `DeviceRegistry.from_config()`.
3. Obtain the configured `GantryBackend`.
4. Connect Gantry.
5. Query and print status.
6. Home Gantry with a unique idempotency key.
7. Move to `X=-20`, `Y=-20`, `Z=-10`.
8. Query and print the final position.
9. Disconnect Gantry.

Disconnect is protected by `finally` after a successful connection, including
when status, homing, movement, or position query raises an exception.

## Automated Tests

The new tests verify:

- Default mode creates the configured Registry without any hardware calls.
- Hardware mode follows the required call sequence.
- The exact target is `(-20, -20, -10)`.
- Failure after connection still disconnects Gantry.
- Hardware execution requires the exact confirmation text.

Focused Gantry and configuration regression:

```text
66 passed
```

Full test invocation:

```text
393 passed, 1 skipped, 1 failed
```

The failure was the pre-existing timing boundary assertion in
`test_get_status_returns_fresh_snapshot_during_lock_hold`: measured
`99.999755859375 ms` versus the asserted minimum `100.0 ms`. The same test
passed when run independently.

Full regression excluding that unstable timing-boundary case:

```text
393 passed, 1 skipped, 1 deselected
```

The remaining warning is the existing Starlette `TestClient`/`httpx`
deprecation warning.

## Execution Status

The default dry-run command was executed successfully. No real serial
connection, homing command, or movement was performed during development or
automated testing.

## Before Real Execution

- Confirm `/dev/ttyUSB1` is the intended GRBL controller.
- Clear the full XYZ travel envelope and verify emergency-stop access.
- Confirm the configured soft limits and safe target match the physical setup.
- Expect homing to move all configured axes before the safe-target move.
- Run the command only with an operator present.

