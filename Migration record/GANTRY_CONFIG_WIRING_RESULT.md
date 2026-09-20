# Gantry Configuration Wiring Result

## Scope

This phase wires the already verified Gantry implementation into the unified
runtime configuration path:

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

No Gantry motion, serial communication, command generation, or hardware driver
logic was changed. Gripper migration was not started.

## Changed Files

| File | Change |
| --- | --- |
| `config/devices.yaml` | Marked Gantry migration completed and recorded its model, protocol, and verified source. |
| `config/hardware.yaml` | Added the Gantry port, baudrate, timeout, homing flag, soft limits, default feed, and GRBL command metadata. |
| `src/config.py` | Added typed Gantry hardware and command configuration models plus device metadata loading. Gantry remains optional for compatibility with older configuration files. |
| `src/webapp/registry.py` | Added conditional Gantry construction from runtime configuration and included the instance in `SystemEstop`. |
| `tests/test_config_wiring.py` | Added Gantry YAML, metadata, schema, Registry, soft-limit, feed-rate, shared-relay, and emergency-stop wiring tests. |

## Configuration Mapping

The production Gantry configuration resolves to:

| Setting | Value |
| --- | --- |
| Backend | `verified_driver` |
| Driver | `grbl_controller` |
| Serial port | `/dev/ttyUSB1` |
| Baudrate | `115200` |
| Timeout | `2.0 s` |
| Homing enabled | `true` |
| Default feed | `300 mm/min` |
| X soft limits | `-310.0` to `-5.0 mm` |
| Y soft limits | `-310.0` to `-5.0 mm` |
| Z soft limits | `-110.0` to `-5.0 mm` |
| Home command metadata | `$H` |
| Unlock command metadata | `$X` |
| Emergency-stop metadata | Ctrl-X (`0x18`) |

`DeviceRegistry.from_config()` injects the configured serial port and baudrate
into `GantryBackend`. It copies the Gantry soft limits and default feed into the
backend's `L3Config`, shares the configured `RelayBackend` for the Z brake, and
registers Gantry with `SystemEstop`.

The command strings, timeout, and homing flag are validated and retained as
runtime configuration metadata. They are not injected into or used to alter the
verified driver implementation.

## Compatibility

`hardware.gantry` is optional at the top-level schema boundary. Existing custom
or test configurations that predate Gantry wiring still load and leave
`registry.gantry` unset. The production `config/hardware.yaml` includes the full
Gantry section and therefore creates the backend.

## Tests

Focused regression:

```text
python -m pytest -q tests/test_config_wiring.py tests/test_webapp_skeleton.py
  tests/test_gantry_grbl_controller.py tests/test_gantry_backend_unit.py
  tests/test_system_estop.py tests/test_webapp_mock_registry.py

80 passed, 1 warning
```

Full regression:

```text
python -m pytest -q

390 passed, 1 skipped, 1 warning
```

The warning is the existing Starlette `TestClient`/`httpx` deprecation warning.
No test failed.

## Protected Implementation

The following protected implementation files were not modified during this
configuration-wiring phase:

- `src/hardware/drivers/gantry/grbl_controller.py`
- `src/hardware/gantry_backend.py`
- Existing hardware communication and motion-control implementations

## Remaining Risks

- `/dev/ttyUSB1` must remain the correct Raspberry Pi device path; a stable udev
  alias is preferable if USB enumeration can change.
- The configured command metadata must stay aligned with the verified driver's
  fixed GRBL behavior.
- Loading configuration and constructing the Registry do not connect to or move
  real hardware. Deployment should still perform a no-motion import/config
  smoke test before hardware operation.
- The single skipped full-suite test should be reviewed separately if it covers
  a deployment-specific dependency or hardware environment.

