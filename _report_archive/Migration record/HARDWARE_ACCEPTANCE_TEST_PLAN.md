# Hardware Acceptance Test Plan

## Purpose

Validate the migrated autospin platform on Raspberry Pi real hardware without
changing software or hardware behavior.

Modules in scope:

1. Spin Motor
2. Heater
3. Pipette
4. Linear Stage
5. Relay
6. Gantry
7. Gripper

This plan is procedural only. It does not authorize unattended operation.

## Acceptance Rules

Each module receives one of these outcomes:

| Outcome | Meaning |
| --- | --- |
| PASS | Every expected result was observed and no safety or communication fault occurred |
| PASS WITH LIMITATION | Core behavior passed, but a documented non-blocking limitation remains |
| FAIL | Any failure condition occurred or a recovery step could not restore a known safe state |
| NOT RUN | Preconditions were not satisfied |

Stop the full acceptance run after any unsafe motion, unexpected heating,
uncontrolled liquid action, dropped sample, repeated serial error, or failed
emergency action. Do not continue to another module merely because software
still responds.

## Test Order

Run in this order:

```text
Preflight
  -> Relay read/connect
  -> Gripper open/close
  -> Gantry
  -> Relay CH1/CH2 coexistence
  -> Linear Stage
  -> Pipette
  -> Heater
  -> Spin Motor
  -> Shared communication stability
  -> System emergency-stop rehearsal
```

The order places low-energy IO checks first and high-energy rotation last.

## Common Preparation

### Personnel And Area

- One operator remains at the machine.
- A second person observes the first emergency-stop validation where possible.
- Physical emergency-stop access is unobstructed.
- Remove loose tools, cables, substrates, liquid containers, and clothing from
  all motion and rotation envelopes.
- Provide a protected landing area for Gripper emergency release.
- Keep the spin chuck unloaded for initial rotation tests.
- Use water only for initial Pipette tests.
- Keep Heater surfaces clear and use a contact-safe temperature reference.

### Raspberry Pi

From the deployed repository:

```bash
cd /home/pi/autospin
python --version
python -m pytest -q
python tools/gantry_runtime_smoke.py
```

The Gantry command above must print dry-run/no-serial behavior. It must not move
hardware.

Verify configuration:

```bash
python - <<'PY'
from src.config import load_config

cfg = load_config()
print(cfg.hardware.model_dump_json(indent=2))
PY
```

Confirm, before proceeding:

- `hardware.mock` is `false`.
- Shared RS485 path is `/dev/rs485_bus`.
- Gantry path is `/dev/ttyUSB1`.
- Relay path resolves to the intended DSTUR-T80.
- Spin Motor slave ID is `2`, Heater slave ID is `3`.
- Pipette slave ID is `1`, Linear Stage address is `4`.
- Gantry baudrate is `115200`.
- Relay baudrate is `9600`.
- Gripper maps to CH1 and Z brake maps to CH2.
- Gripper close wait is `1.0 s`.

### Port Ownership

Before each session:

```bash
ls -l /dev/rs485_bus /dev/ttyUSB1
ls -l /dev/serial/by-id/
lsof /dev/rs485_bus /dev/ttyUSB1 2>/dev/null || true
```

No unrelated process may own a test port. Stop Web servers, dashboards, stale
Python processes, and serial consoles before testing.

### Logging

Create a run directory outside source control:

```bash
RUN_ID="$(date +%Y%m%d_%H%M%S)"
mkdir -p "logs/hardware_acceptance/$RUN_ID"
echo "$RUN_ID"
```

Run each command through `tee`, for example:

```bash
python tools/spincoater_smoke.py --port /dev/rs485_bus \
  2>&1 | tee "logs/hardware_acceptance/$RUN_ID/spin_read.log"
```

Record:

- Operator and observer
- Date/time
- Hardware serial numbers
- Git commit
- Configuration checksum
- Commands executed
- Observed physical behavior
- Final PASS/FAIL
- Recovery actions
- Relevant `dmesg` excerpts

Capture configuration identity:

```bash
git rev-parse HEAD
sha256sum config/hardware.yaml config/devices.yaml
dmesg -T | tail -n 100
```

## 1. Spin Motor Acceptance

### Hardware Preparation

- Verify DBLS400 power, protective earth, motor phases, and RS485 A/B wiring.
- Confirm the chuck is empty, balanced, and mechanically secure.
- Fit the physical guard.
- Confirm slave ID `2`, `9600 baud`, and `/dev/rs485_bus`.
- Keep the Heater, Pipette, and Linear Stage inactive during the first test.

### Safety Checks

- No sample or liquid on the chuck.
- No loose item can enter the rotating envelope.
- Operator can remove drive power immediately.
- Initial target is limited to `500 rpm`.
- Confirm configured maximum is `3000 rpm`; do not test maximum in the first
  acceptance cycle.

### Test Commands

Read/connect only:

```bash
python tools/spincoater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 2
```

Dry-run a 500 rpm request:

```bash
python tools/spincoater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 2 \
  --rpm 500
```

Real low-speed run:

```bash
python tools/spincoater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 2 \
  --rpm 500 \
  --hold-s 3 \
  --apply
```

Repeat once at an approved intermediate speed, for example `1000 rpm`, only
after the 500 rpm test passes.

### Expected Results

- Connection and status read succeed.
- Dry-run sends no start command.
- Motor starts in the configured direction.
- Actual RPM rises toward the requested value and remains plausible.
- Speed feedback uses the preserved DBLS400 compensation behavior.
- The script stops with brake after the hold interval.
- Actual RPM returns near zero.
- No fault register, abnormal vibration, smell, or overheating is observed.

### Failure Conditions

- No Modbus response or response from the wrong slave.
- Motor starts during dry-run.
- Motor rotates in the wrong direction.
- Actual RPM is absent, implausible, unstable, or does not fall after stop.
- DBLS400 fault, repeated CRC error, bus collision, overcurrent, or vibration.
- Stop/brake fails.

### Recovery Procedure

1. Use the physical stop or remove motor drive power if rotation continues.
2. Do not reconnect until the chuck is stationary.
3. Close the smoke process.
4. Inspect DBLS400 fault indication and wiring.
5. Capture `dmesg` and the test log.
6. Verify exclusive `/dev/rs485_bus` ownership.
7. Clear the drive fault only under the DBLS400 procedure.
8. Restart acceptance at read-only status, then 500 rpm.

## 2. Heater Acceptance

### Hardware Preparation

- Confirm AI-516 power, sensor wiring, SSR wiring, and protective earth.
- Confirm AI-516 is in standard Modbus mode, slave ID `3`, `9600 baud`.
- Place an independent temperature probe on the controlled surface.
- Remove flammable materials and temperature-sensitive fixtures.

### Safety Checks

- Confirm the sensor reads plausible ambient temperature before writing SV.
- Confirm the operator can disconnect heater/SSR power.
- Use a low initial setpoint such as `40 C`.
- Never leave the heater unattended.
- Define an acceptance cutoff, for example `SV + 10 C`; crossing it is an
  immediate failure.

### Test Commands

Read PV:

```bash
python tools/heater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 3
```

Dry-run SV:

```bash
python tools/heater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 3 \
  --set-sv 40
```

Apply low SV:

```bash
python tools/heater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 3 \
  --set-sv 40 \
  --apply
```

After observing controlled heating, return SV to zero:

```bash
python tools/heater_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 3 \
  --set-sv 0 \
  --apply
```

### Expected Results

- PV is plausible and agrees reasonably with the independent probe.
- Dry-run does not change the AI-516 display/setpoint.
- Applied SV is accepted.
- SSR cycles according to demand; temperature rises without runaway.
- PV trends toward SV and does not exceed the defined cutoff.
- Setting SV to zero ends heating demand according to configured controller
  behavior.

### Failure Conditions

- PV is open-circuit, fixed, implausible, or inconsistent with the reference.
- Dry-run writes SV.
- Wrong register/slave responds.
- SSR remains continuously energized after SV is zero.
- Temperature rises uncontrollably or exceeds cutoff.
- PID/SV values change unexpectedly.
- Repeated Modbus/CRC/timeout errors.

### Recovery Procedure

1. Remove SSR/heater power on runaway or stuck output.
2. Set SV to zero only if communication remains trustworthy.
3. Allow the surface to cool behind a guarded area.
4. Inspect sensor, SSR polarity, AI-516 mode, and PID configuration.
5. Capture display values, independent probe value, logs, and `dmesg`.
6. Restart with read-only PV; do not repeat heating until the cause is resolved.

## 3. Pipette Acceptance

### Hardware Preparation

- Confirm Pipette RS485 wiring, slave ID `1`, and `115200 baud`.
- Install a compatible tip.
- Prepare water, a waste vessel, absorbent material, and spill containment.
- Position the Pipette where homing cannot collide with Gantry or fixtures.

### Safety Checks

- Use water only.
- Confirm tip seating and sufficient liquid depth.
- Keep aspiration volume well below `1000 uL`; start at `100 uL`.
- Keep hands clear of moving plunger and tip-eject mechanisms.
- Verify the eject path is above the waste container.

### Test Commands

Connect/status:

```bash
python tools/pipette_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 1
```

Dry-run home and liquid actions:

```bash
python tools/pipette_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 1 \
  --home \
  --aspirate-ul 100 \
  --dispense-ul 100
```

Real home:

```bash
python tools/pipette_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 1 \
  --home \
  --apply
```

Real water transfer after homing:

```bash
python tools/pipette_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 1 \
  --aspirate-ul 100 \
  --dispense-ul 100 \
  --apply
```

Run tip eject separately only with the waste path confirmed:

```bash
python tools/pipette_smoke.py \
  --port /dev/rs485_bus \
  --unit-id 1 \
  --eject-tip \
  --apply
```

### Expected Results

- Status identifies the correct device.
- Dry-run causes no plunger, liquid, or tip movement.
- Home completes and reports homed.
- Approximately `100 uL` is aspirated without air ingestion.
- Dispense returns liquid to the target vessel without uncontrolled dripping.
- Tip eject releases into the waste container.
- Final state is idle and communication remains available.

### Failure Conditions

- Wrong slave response, timeout, or incompatible Modbus call.
- Homing does not finish or reports success while still moving.
- Action occurs in dry-run.
- Tip missing/not detected where the controller supports that source behavior.
- Air aspiration, leakage, stall, incorrect volume, failed dispense, or failed
  tip eject.
- Immediate-stop or recovery cannot halt an abnormal action.

### Recovery Procedure

1. Stop software commands and use the Pipette physical stop/power isolation if
   motion continues.
2. Contain liquid and remove the sample from the work area.
3. Do not issue another aspirate/dispense until homing state is known.
4. Reconnect read-only, then re-home.
5. Replace the tip and inspect seals, liquid path, and mechanics.
6. Capture status, error code, logs, and bus diagnostics.

## 4. Linear Stage Acceptance

### Hardware Preparation

- Confirm ZDT Emm address `4`, `115200 baud`, and RS485 wiring.
- Clear the complete single-axis travel.
- Confirm home direction, end sensor/sensorless behavior, and `0..100 mm`
  software range.
- Mark a reference position for independent travel observation.

### Safety Checks

- Remove mounted tools or use a sacrificial fixture for the first run.
- Confirm travel direction before a long move.
- Keep the target at least 10 mm away from either boundary.
- Keep physical stop access available.

### Test Commands

Connect/status:

```bash
python tools/linearstage_smoke.py \
  --port /dev/rs485_bus \
  --address 4
```

Dry-run home and target:

```bash
python tools/linearstage_smoke.py \
  --port /dev/rs485_bus \
  --address 4 \
  --home \
  --move-to-mm 10
```

Real home and move:

```bash
python tools/linearstage_smoke.py \
  --port /dev/rs485_bus \
  --address 4 \
  --home \
  --move-to-mm 10 \
  --apply
```

Immediate-stop acceptance must be performed as a separate supervised action,
using the smoke tool's `--stop --apply` option only while motion is known to be
safe to interrupt.

### Expected Results

- Correct address responds.
- Dry-run causes no motion.
- Homing completes in the configured direction and timeout.
- Absolute move reaches approximately `10 mm`.
- Reported position is within configured tolerance.
- Stop ends motion and leaves a recoverable state.

### Failure Conditions

- Wrong direction, collision, missed home, timeout, or boundary overrun.
- Position differs beyond tolerance.
- Driver reports success while physical motion did not occur.
- Stop does not halt motion.
- Repeated frame/checksum or RS485 ownership errors.

### Recovery Procedure

1. Remove stage power if stop fails.
2. Inspect for mechanical binding before moving again.
3. Do not trust commanded position after interrupted or failed motion.
4. Reconnect and re-home with the path clear.
5. Revalidate at `10 mm` before any process coordinate.

## 5. Relay Acceptance

### Hardware Preparation

- Confirm the configured DSTUR-T80 serial device and `9600 baud`.
- Verify CH1 wiring reaches only the Gripper control input.
- Verify CH2 wiring reaches only the Gantry Z brake.
- Confirm flyback/surge suppression, grounding, relay power, and USB stability.
- Ensure only one RelayBackend instance owns the serial port.

### Safety Checks

- Support the Gantry Z axis mechanically before first CH2 release.
- Keep the Gripper empty for CH1 tests.
- Do not use `all_off()` as a substitute for semantic device testing.
- Do not run historical scripts that open a second relay serial connection.

### Test Commands

Read-only shared wiring check through the migrated Registry:

```bash
python - <<'PY'
from src.webapp.registry import DeviceRegistry

r = DeviceRegistry.from_config()
assert r.relay is not None
assert r.gripper is not None
assert r.gantry is not None
assert r.gripper._relay is r.relay
assert r.gantry._relay is r.relay
print("shared RelayBackend: PASS")
print("relay port:", r.relay.port)
print("gripper state:", r.gripper.get_state().model_dump())
PY
```

Use the configured Registry for semantic CH1/CH2 validation:

```bash
python - <<'PY'
import time
import uuid
from src.webapp.registry import DeviceRegistry

r = DeviceRegistry.from_config()
assert r.relay is not None
assert r.gripper is not None
assert r.gantry is not None
assert r.gripper._relay is r.relay
assert r.gantry._relay is r.relay

r.relay.connect()
try:
    r.gripper.close(idempotency_key=f"accept-close-{uuid.uuid4()}")
    print("after CH1 close:", r.relay.get_state().model_dump())
    time.sleep(1.0)
    r.gripper.open(idempotency_key=f"accept-open-{uuid.uuid4()}")
    print("after CH1 open:", r.relay.get_state().model_dump())
finally:
    r.gripper.emergency_release()
    r.relay.disconnect()
PY
```

CH1/CH2 coexistence test, only after Gantry acceptance:

1. Connect one Registry.
2. Close the empty Gripper through CH1.
3. Home Gantry.
4. Perform a short Z movement that exercises CH2 brake release/lock.
5. Confirm CH1 remains commanded ON and the Gripper remains closed.
6. Open Gripper through CH1.
7. Confirm CH2 ends in the brake-safe state.
8. Repeat five cycles while recording every relay error and `dmesg`.

### Expected Results

- Exactly one relay object is shared by Gantry and Gripper.
- CH1 commands do not alter CH2 state.
- CH2 commands do not alter CH1 state.
- Relay writes are at least `0.3 s` apart as enforced by RelayBackend.
- Five coexistence cycles complete without serial disconnect, stale handle,
  `Errno 5`, wrong-channel action, or missed physical relay action.

### Failure Conditions

- CH1 and CH2 act together unexpectedly.
- Z brake releases during a CH1-only command.
- Gripper changes during a CH2-only command.
- Multiple processes own the port.
- Any `SerialException`, `Errno 5`, reconnect storm, USB reset, or missing
  relay click/output.
- Software state changes without observed expected physical action.

### Recovery Procedure

1. Stop Gantry motion first.
2. Restore CH2 to the physical brake-safe state.
3. Release Gripper only if the landing area is safe.
4. Close all relay clients and remove duplicate port owners.
5. Inspect `dmesg -T`, USB cable/hub, relay supply, grounding, and EMI
   suppression.
6. Power-cycle the relay only after Gantry Z is mechanically supported.
7. Restart with read-only connect, then CH1-only, then CH2-only.

## 6. Gantry Acceptance

### Hardware Preparation

- Confirm GRBL controller power and `/dev/ttyUSB1` at `115200 baud`.
- Verify limit switches, Z brake, motor drivers, and physical travel envelope.
- Clear the entire homing path and target path.
- Confirm software limits:

```text
X: -310 to -5 mm
Y: -310 to -5 mm
Z: -110 to -5 mm
```

### Safety Checks

- Gripper is empty and open.
- Z axis is mechanically supported during initial brake verification.
- Physical emergency stop and controller power isolation are reachable.
- Start with the configured safe target `(-20, -20, -10)`.
- No person is inside the motion envelope.

### Test Commands

Default no-motion check:

```bash
python tools/gantry_runtime_smoke.py
```

Explicit real-hardware sequence:

```bash
python tools/gantry_runtime_smoke.py \
  --execute-hardware \
  --confirm MOVE_GANTRY_TO_X-20_Y-20_Z-10
```

This command must load configuration, connect, query status, issue `$H`, issue
an absolute coordinated XYZ move, query final position, and disconnect.

Emergency-stop test must be run separately with a low feed and clear path:

```bash
python - <<'PY'
import time
from src.hardware.types import Position
from src.webapp.registry import DeviceRegistry

r = DeviceRegistry.from_config()
g = r.gantry
assert g is not None
g.connect()
try:
    g.home(idempotency_key="accept-estop-home")
    g.start_move_async(
        Position(x_mm=-100.0, y_mm=-100.0, z_mm=-20.0),
        feed_mm_min=300.0,
        timeout_s=60.0,
    )
    time.sleep(1.0)
    print(g.abort_motion_immediate().model_dump_json(indent=2))
    print(g.get_status().model_dump_json(indent=2))
finally:
    g.disconnect()
PY
```

Run the emergency test only after an operator verifies that interruption at any
point on the selected path is mechanically safe.

### Expected Results

- GRBL connection and status query succeed.
- `$H` executes and the Backend reports homed.
- Move command is complete absolute XYZ:

```text
$J=G90 X... Y... Z... F...
```

- X, Y, and Z move coordinately to approximately `(-20, -20, -10)`.
- No Z-only command is emitted for an XYZ target.
- CH2 releases/locks the Z brake in the verified sequence.
- Ctrl-X (`0x18`) stops the emergency-test movement immediately.
- After emergency stop, homed state is invalidated and re-home is required.

### Failure Conditions

- `$H` moves in the wrong direction, fails, or produces an alarm.
- Any axis exceeds physical or software limits.
- XYZ target results in Z-only movement.
- Position report disagrees materially with observed motion.
- Brake releases or locks at the wrong time.
- Ctrl-X does not stop motion promptly.
- Backend reports success after GRBL alarm or disconnect.

### Recovery Procedure

1. Use physical emergency stop/power isolation if Ctrl-X is ineffective.
2. Mechanically support Z before changing relay or brake power.
3. Record GRBL alarm code and raw status.
4. Clear the obstruction or limit-switch issue.
5. Use `$X` only when the cause is understood and the envelope is safe.
6. Re-home before any further absolute motion.
7. Repeat only the low-feed safe-target test.

## 7. Gripper Acceptance

### Hardware Preparation

- Confirm the Gripper is IO-controlled through DSTUR-T80 CH1.
- Confirm CH1 ON physically closes and CH1 OFF physically opens.
- Remove samples for the polarity test.
- Place a sacrificial sample and protected landing tray for grip/emergency tests.
- Confirm the same RelayBackend is shared with Gantry CH2.

### Safety Checks

- Keep hands clear of jaws.
- Verify close force is mechanically acceptable before inserting a sample.
- Support the test sample during first close.
- Expect emergency release to drop a carried sample.
- Never infer physical state from `get_state()` alone.

### Test Commands

Use `DeviceRegistry.from_config()` and the semantic Backend:

```bash
python - <<'PY'
import time
import uuid
from src.webapp.registry import DeviceRegistry

r = DeviceRegistry.from_config()
relay = r.relay
g = r.gripper
assert relay is not None and g is not None

relay.connect()
try:
    print("initial:", g.get_state().model_dump())

    g.open(idempotency_key=f"accept-open-{uuid.uuid4()}")
    print("open:", g.get_state().model_dump())

    t0 = time.monotonic()
    g.close(idempotency_key=f"accept-close-{uuid.uuid4()}")
    elapsed = time.monotonic() - t0
    print("close elapsed_s:", elapsed)
    print("closed:", g.get_state().model_dump())

    g.emergency_release()
    print("emergency released:", g.get_state().model_dump())
finally:
    g.emergency_release()
    relay.disconnect()
PY
```

After polarity acceptance, run a supervised pick sequence:

```text
safe-Z XY approach
  -> Z down
  -> close CH1 ON
  -> wait at least 1.0 s
  -> Z up
  -> XY transfer
  -> Z down
  -> open CH1 OFF
  -> Z up
```

### Expected Results

- CH1 OFF opens the jaws.
- CH1 ON closes the jaws.
- `close()` does not return before approximately `1.0 s`.
- A representative sample remains held during Z lift and slow XY transfer.
- `emergency_release()` commands CH1 OFF and physically opens the Gripper.
- `get_state()` reports commanded state only and keeps
  `position_known=False`.
- No API claims grip detection or physical confirmation.

### Failure Conditions

- Polarity is inverted.
- Close returns before the configured stabilization interval.
- Sample slips after the one-second wait.
- Emergency release does not open the Gripper.
- CH1 action disturbs CH2 or Gantry status.
- Backend reports physical position/grip confirmation.
- Relay communication fails or enters repeated reconnect.

### Recovery Procedure

1. Stop Gantry before handling the Gripper.
2. Support any carried sample.
3. Request emergency release through GripperBackend.
4. If release communication fails, isolate relay/Gripper power according to the
   actuator wiring procedure; do not directly write serial frames.
5. Restore CH2 brake-safe state before relay power cycling.
6. Inspect wiring polarity, relay channel assignment, USB stability, and
   mechanical jaw alignment.
7. Restart from empty-jaw open/close testing.

## Shared RS485 Stability Acceptance

Spin Motor and Heater use `9600 baud`; Pipette and Linear Stage use
`115200 baud` on the configured shared adapter. Validate them sequentially
first, then test repeated switching through the existing bus abstraction.

Acceptance procedure:

1. Stop all device motion and heating.
2. Read Spin Motor status.
3. Read Heater PV.
4. Read Pipette status.
5. Read Linear Stage status.
6. Repeat the sequence 20 times.
7. Confirm no simultaneous direct serial client was opened.
8. Review logs for timeout, CRC, stale response, wrong-slave response, or baud
   transition failure.

Pass criteria:

- 20 complete cycles.
- Zero wrong-device responses.
- Zero unhandled timeout or CRC errors.
- No device acts during a read-only request.
- Bus remains usable after switching between 9600 and 115200 devices.

On failure, close all bus users, identify duplicate serial ownership, power
down active devices safely, inspect termination/biasing/grounding, and repeat
single-device read tests before another shared cycle.

## System Emergency-Stop Acceptance

Perform only after every individual emergency action passes.

Precondition state:

- Gantry performing a low-feed safe-path move.
- Empty Gripper closed over a protected landing zone.
- Spin Motor at no more than `500 rpm`.
- Linear Stage moving within the central travel region.
- Pipette contains water only.
- Heater at a low test SV.

Trigger the system emergency-stop through the deployed runtime entry point.

Expected ordered effects:

1. Gantry receives immediate abort/Ctrl-X.
2. Gripper receives emergency release/CH1 OFF.
3. Spin Motor stops with brake.
4. Linear Stage stops.
5. Pipette stops.
6. Heater SV is set to zero when requested.

Pass criteria:

- Every step is present in the structured report.
- One failed step does not suppress later emergency actions.
- Physical effects match the report.
- Gantry requires re-home afterward.
- Gripper is physically open.
- Heater demand ends and all motion reaches a safe state.

If any emergency step fails, isolate the corresponding energy source, preserve
the complete report, and mark platform acceptance FAIL.

## Final Acceptance Checklist

- [ ] Configuration and device paths verified
- [ ] No duplicate serial owners
- [ ] Spin Motor passed read/start/RPM/stop/brake
- [ ] Heater passed PV/SV/SSR/zero-SV recovery
- [ ] Pipette passed home/water aspirate/dispense/eject
- [ ] Linear Stage passed home/move/stop
- [ ] Relay passed CH1/CH2 isolation and five coexistence cycles
- [ ] Relay completed without `Errno 5` or USB resets
- [ ] Gantry passed `$H`
- [ ] Gantry passed `$J=G90` coordinated absolute XYZ movement
- [ ] Gantry passed Ctrl-X emergency stop
- [ ] Gripper passed CH1 ON close
- [ ] Gripper passed CH1 OFF open
- [ ] Gripper close stabilization was at least `1.0 s`
- [ ] Gripper emergency release passed
- [ ] Operators acknowledge Gripper has no physical feedback
- [ ] Shared RS485 completed 20 read cycles
- [ ] System emergency-stop order and fault isolation passed
- [ ] Logs, configuration checksums, and observations archived

## Exit Criteria

The platform is accepted only when every module is PASS and the system
emergency-stop rehearsal succeeds. Any PASS WITH LIMITATION requires written
approval identifying the limitation, operating restriction, owner, and closure
date.
