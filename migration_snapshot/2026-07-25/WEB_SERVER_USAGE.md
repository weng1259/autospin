# AutoSpin Web Control Usage

## Purpose

`autospin` provides the maintained Web control surface that replaces the
experimental `AutoSpinmotorSystem/web_control/server.py` page. The Web layer
uses `DeviceRegistry`, backend APIs, adapters, and verified controllers. It
does not open serial ports or implement hardware protocols itself.

## Start Safely

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Start without hardware:

```bash
python tools/run_webserver.py --mock
```

Start with `config/hardware.yaml`:

```bash
python tools/run_webserver.py --host 127.0.0.1 --port 8800
```

Open `http://127.0.0.1:8800`, then enter the Bearer token printed by the
server. Always use `--mock` first after deployment or configuration changes.

Binding to another address exposes the control API to the network. Use a
trusted isolated network and an external TLS/authentication proxy when remote
access is required.

## Web Functions

The first screen is the operating console:

- Gantry: connect, disconnect, home, absolute XYZ move, axis jog, dry-run
  validation, homing diagnostics, alarm recovery, Z brake, settings check,
  immediate halt, and global emergency stop.
- Heater: connect, disconnect, set SV, and read PV.
- Spin coater: connect, disconnect, start at RPM, stop/brake, and read fault.
- Pipette: connect, disconnect, home, aspirate, dispense, and eject tip.
- Linear stage: connect, disconnect, home, absolute move, and immediate stop.
- Relay: connect, disconnect, and CH3-CH8 operation. CH1 and CH2 remain
  protected because Gripper and Z brake own them.
- Gripper: close and open through `RelayBackend`.
- Routine: record successful manual actions, save, list, replay, abort, and
  delete.
- Multi-round builder: repeat a saved routine for 1-100 rounds and apply a
  per-round XYZ offset to Gantry `move_to` steps.
- Process coordinates: read and save named XYZ points as JSON.

## Configuration-Driven Limits

The browser reads `/api/config/runtime`. Input bounds are not separately
maintained in HTML:

- Gantry XYZ comes from `config/hardware.yaml: gantry.soft_limits`.
- Spin RPM comes from `spincoater.max_rpm`.
- Heater SV comes from `heater.sv_max_c`.
- Pipette volume comes from `pipette.max_volume_ul`.
- Linear-stage range comes from its configured minimum and maximum/travel.

The current Gantry browser range is X/Y `-310..-5 mm` and Z
`-110..-5 mm`. The backend validates the same values again before motion.

## Multi-Round Procedure

1. Use manual controls and the Routine panel to record one complete round.
2. Stop recording and confirm the source routine appears in the list.
3. Enter source routine name, output routine name, and round count.
4. Enter per-round XYZ offsets. Use zero offsets to repeat the same positions.
5. Click **生成多轮程序**.
6. Inspect the generated routine file in `runtime/routines/`.
7. Home the Gantry and replay only after reviewing every generated position.

Generation does not move hardware. Every generated Gantry position is checked
against configured soft limits. Any out-of-range round rejects the whole
generation request.

## Process Coordinates

Coordinates are stored in `config/process_coordinates.yaml`. The Web editor
uses this JSON shape:

```json
{
  "points": {
    "spin_center": {
      "x_mm": -20.0,
      "y_mm": -20.0,
      "z_mm": -10.0
    }
  }
}
```

Click **读取坐标** before editing, then **校验并保存**. Every point must be
inside the configured Gantry soft limits. Process coordinates remain separate
from machine limits and software safety limits.

## Legacy Server Mapping

| Legacy function | Maintained Web equivalent |
| --- | --- |
| status | SSE device status and operation status |
| connect/disconnect | Per-device connect/disconnect |
| home | Gantry Home |
| move_abs | Gantry absolute XYZ form |
| move_rel/manual_jog | Six-axis jog controls |
| dry_run | Gantry **仅校验** |
| halt | Gantry **立即停止** |
| recover | Gantry alarm recovery |
| homing_diagnostics | Gantry homing diagnostics |
| linear-stage actions | Linear-stage card |
| z_brake | Gantry Z brake controls |
| pipette actions | Pipette card |
| spin actions | Spin-coater card |
| gripper | Gripper card |
| record_position | Named process-coordinate editor |

Legacy browser-supplied serial ports are intentionally replaced by
`config/hardware.yaml`. This prevents an unaudited Web request from changing
the active hardware topology.

## Emergency and Recovery

The red global emergency-stop control bypasses the normal operation queue.
After any emergency stop:

1. Remove the physical hazard and inspect every axis/device.
2. Confirm the Gantry state and limit switches.
3. Reconnect devices as required.
4. Run alarm recovery and home the Gantry.
5. Re-test with mock/dry-run before resuming an experiment.

The Gripper emergency policy is release: CH1 OFF/open. There is no grip,
position, or force feedback, so the operator must verify the sample state.

## Files

- Server entry: `tools/run_webserver.py`
- Application factory: `src/webapp/app.py`
- Web UI: `src/webapp/static/`
- Runtime/config routes: `src/webapp/routes_configuration.py`
- Hardware configuration: `config/hardware.yaml`
- Process coordinates: `config/process_coordinates.yaml`
- Generated routines: `runtime/routines/`

