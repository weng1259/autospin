# AutoSpin stable serial names (udev)

The Raspberry Pi maps three USB serial devices to stable names consumed by
`config/hardware.yaml`. Logical backend selection is in `config/devices.yaml`.

| Physical USB port | Chip | Device | Stable name |
|---|---|---|---|
| 1-1 | CH340 `1a86:7523` | grbl-Mega-5X motion controller | `/dev/autospin_xyz` |
| 1-2 | CH340 `1a86:7523` | shared USB-RS485 adapter | `/dev/autospin_rs485` |
| 3-2 | STM32 `0483:5740` | DSTUR-T80 USB relay | `/dev/autospin_relay` |

The two CH340 devices have the same vendor/product identity and no unique
serial number, so their rules use the physical USB path. Do not exchange the
1-1 and 1-2 connectors without updating and revalidating the udev rules.

## Install or refresh

```bash
deploy/udev/install.sh
```

Inspect the resulting links before starting the application:

```bash
ls -l /dev/autospin_xyz /dev/autospin_rs485 /dev/autospin_relay
udevadm info -a -n /dev/autospin_xyz
udevadm info -a -n /dev/autospin_rs485
udevadm info -a -n /dev/autospin_relay
```

## Maintained verification commands

Offline configuration and serial-resource regression tests:

```bash
.venv/bin/python -m pytest tests/test_config_wiring.py tests/test_serial_resources.py -q
```

Read-only full-registry wiring check (opens configured hardware ports but sends
no actuator command):

```bash
.venv/bin/python tools/panel_wiring_smoke.py --confirm-read-only-hardware
```

Gantry identification should return a GRBL banner/status from
`/dev/autospin_xyz`. Heater PV, spincoater status, and pipette status share
`/dev/autospin_rs485`; the resource diagnostics printed by the smoke tool must
show current `src` ownership without a second serial controller.

For a guarded low-speed spin bring-up, first complete the hardware acceptance
checklist, then use the maintained registry-backed tool. Without `--apply` it
does not start the motor:

```bash
.venv/bin/python tools/spinmotor_bringup.py --rpm 150
```

Real spin requires both `--apply` and the exact confirmation phrase printed by
`--help`. Never use a real-spin command as a udev identity test.
