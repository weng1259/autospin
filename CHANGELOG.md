# Changelog

This file records project-level software, configuration, test, deployment, and
documentation changes. Runtime logs, experiment data, caches, and generated
results are not changelog content.

## 2026-09-20 - Spin timing epoch correction

### Fixed

- Anchored the spin-profile clock immediately before the first motor start
  command. Backend stop-state checks, startup delay, acceleration, and pipette
  travel now consume the configured spin/timed-dispense schedule instead of
  postponing its epoch until `spin.start()` returns.
- Kept the optimized sequence in which the motor starts immediately after the
  timed antisolvent has been aspirated; reaching the dispense coordinate does
  not reset the spin or antisolvent timer.
- Added a regression covering a four-second blocking motor start plus a
  three-second pipette transfer against a ten-second dispense deadline.

## 2026-09-20 - P0 standalone runtime and local recipe storage

### Added

- Added project-local `recipes/` as the single maintained experiment recipe
  directory, including a four-round perovskite starter recipe.
- Added `AUTOSPIN_RECIPES_DIR` and `create_app(..., recipes_path=...)` storage
  overrides plus resolved-path diagnostics in `/api/config/runtime`.
- Added `deploy/runtime-package.json` and the non-destructive
  `tools/build_runtime_package.py` minimal Raspberry Pi package builder.
- Added an isolated package regression that imports and constructs the Web app
  with legacy and development-only top-level directories absent.
- Added `docs/guides/standalone-runtime-package.md` deployment instructions.

### Changed

- Changed the experiment builder and multi-round executor to use the same
  project-local recipe directory instead of the sibling `AutoSpinmotorSystem`.
- Changed recipe execution hints to reference the maintained Web API.

### Safety

- No source directory was deleted. Top-level `firmware/` and `hardware/` remain
  in the development repository and are excluded only from generated runtime
  packages; active `src/hardware/` remains included.

## 2026-09-20 - Hotplate boundary-exit alarm protection

### Fixed

- Ported the verified `AutoSpinmotorSystem` boundary-exit mitigation for the
  four-round failure after placing sample 2 on the hotplate. When the gantry
  starts near an X/Y limit and the destination is inward, automation now
  performs a low-speed single-axis `10 mm` retreat before the long XY move.
- Added `gantry.boundary_escape_mm` to hardware configuration; the existing
  boundary feed remains `1000 mm/min`.
- Preserved asynchronous `ALARM:n` lines during GRBL status polling and added
  alarm code, `Pn:` pins, current position, and target position to motion
  errors. Hard-limit alarms are still never auto-unlocked or auto-retried.

### Verification

- Added regression coverage for the `Y=-5 -> Y=-15 -> destination` hotplate
  departure and for `ALARM:1` plus `Pn:Y` diagnostic preservation.

## 2026-09-20 - Start spin while positioning timed antisolvent

### Changed

- Timed-antisolvent workflows now start the first spin segment immediately
  after aspiration and vacuum confirmation, before moving the pipette to the
  spin-center dispense coordinate.
- The confirmed motor-start timestamp remains the timing origin for segment
  changes and antisolvent dispensing. A late pipette arrival causes an
  immediate safe-position dispense rather than dispensing during travel.
- Added an ordering regression test proving motor start precedes pipette
  positioning and timed dispense.

## 2026-09-20 - Origin-based coordinate grids

### Changed

- Moved tip-rack, substrate-rack, hotplate, and reagent-rack dimensions and
  spacing from Python constants into `config/coordinates.yaml` grid sections.
- Derived every regular rack position from one explicit origin coordinate, so
  an origin edit translates the complete layout without duplicated XYZ edits.
- Derived spin-coater and hotplate approach/retract X/Y from their operation
  origins while retaining an explicit safe Z of `-30 mm`.
- Preserved the user's current calibrated origin values and the existing
  confirmation/executable policy for generated reagent positions.
- Documented the origin-only edit, validation, deployment, and restart flow.

## 2026-09-20 - Configurable spin deceleration and coordinate deployment guide

### Added

- Added an independent normal-stop deceleration setting (`50..6000 RPM/s`) to
  the spincoater backend, API, live status, mock backend, and Web controls.
- Added `docs/guides/coordinates-and-spin-deceleration.md` as the authoritative
  edit, validation, SCP upload, restart, and low-speed verification procedure.

### Changed

- Normal Web and automation stops now ramp the commanded speed to zero before
  applying brake/coast stop. Global emergency stop explicitly bypasses the
  ramp and retains immediate braking behavior.
- Corrected stale documentation that claimed the static Web page already had a
  coordinate editor. `config/coordinates.yaml` remains the sole coordinate
  truth and a process restart is required after file/API changes.

## 2026-08-06 - Multi-round heater gating and hardware recovery corrections

### Changed

- Replaced the blocking multi-round heater start condition with two explicit
  stages. Upstream experiment work may start once the measured temperature is
  at least `SV - 10 C`; a strict `SV +/- 1 C` stability gate still runs before
  the gripper picks the coated substrate from the spin coater for hotplate
  placement.
- Kept the strict temperature gate, spin-coater pickup, and hotplate placement
  in one scheduler-safe block so an annealing return cannot be inserted after
  temperature validation and before placement.
- Preserved placement-anchored annealing timing: dwell begins only after the
  substrate has actually been placed on the hotplate.
- Restored the authoritative `AutoSpinmotorSystem` Emm-stage coordinate
  behavior for its relative-only `0xFD` command. Absolute moves now derive
  their distance from the last accepted commanded target rather than carrying
  encoder undershoot into the next command.
- Kept direct single-command stage moves, live position/fault monitoring,
  three stable in-tolerance reads, bounded timeout, and immediate stop.
- A failed stage move now invalidates the commanded origin and homed state, so
  another move requires a successful native home instead of continuing from
  an untrusted relative coordinate.
- Increased the mechanical settle interval after tip pressing from `0.2 s` to
  `0.4 s`. The action performs its first live `tip_present` check at the press
  position, always retracts the linear stage, and, if the first result was
  false, waits another `0.4 s` and verifies once more after mechanical release.
  A persistent false result still stops the experiment.
- Removed the unconditional linear-stage move to its `10 mm` safe position
  before every gantry transfer into a pipette process coordinate. After native
  home the stage now remains at home until a process command actually needs
  it; tip mounting, aspiration, dispensing, and timed dispensing still retract
  the stage to `10 mm` immediately after their process action.
- Opening the gripper at `stations.spin_coater.operation` now immediately
  energizes vacuum relay channel 3. Spin startup repeats the same idempotent
  vacuum-on command, and spin cleanup still turns the vacuum off.
- Restored the authoritative `AutoSpinmotorSystem` real-hardware recovery delay
  after tip ejection: the executor waits `2 s` after a confirmed eject before
  allowing XYZ to leave the waste station.
- Restored the hardware-verified automatic XY profile by reducing production
  `feed_rate_default` from `5000` to `1000 mm/min`. The legacy project recorded
  `ALARM:1`, simultaneous false limit inputs, and corrupt coordinates during
  5000 mm/min moves; Z-only and boundary motion remain capped at 1000 mm/min.
- Added an explicit `EjectTip` action to the action model. A timed antisolvent
  spin now completes in the legacy-safe order: stop spin, retract the linear
  stage, move to waste, eject the second tip, settle for `2 s`, then continue
  with gripper/sample handling. The scheduler keeps spin and this cleanup in
  one non-interruptible chunk.
- Before the final heater-stability gate, the gripper now moves to
  `stations.spin_coater.safe_above`. It waits there and only descends to pick
  the coated sample after the hotplate is ready.
- Removed a duplicate compile-time tip-slot offset. The Web recipe builder and
  saved-recipe rebuild path already allocate exact global tip slots from actual
  consumption; `compile_protocol()` now uses those assigned slots unchanged.
  A no-antisolvent batch therefore consumes slots `1, 2, 3, ...`, while a
  two-tip batch consumes `1/2, 3/4, ...`.
- Precursor source allocation now advances every four rounds and is bounded to
  the four configured source coordinates: rounds 1-4, 5-8, 9-12, and 13-16.
- Hotplate placement now waits `1 s` after the gripper opens before any next
  action can lift Z, allowing the glass substrate to separate and settle.
- Liquid-motion tip protection now requires three consecutive absent readings
  at the configured `0.1 s` polling interval before rejecting aspiration or
  dispensing. Any intervening `tip_present=1` is accepted; a genuinely missing
  tip still blocks all volume/action writes and stops the experiment safely.
- Corrected a GRBL completion race observed on the second-round first tip
  pickup. A previous command's cached `Idle` snapshot can no longer complete a
  newly accepted jog: the driver now requires both `Idle` and reported XYZ
  arrival within `1 mm` before returning, locking the Z brake, or permitting a
  following actuator action. The action layer independently rejects a mismatched
  final XYZ and blocks the linear stage from pressing the tip rack. No taught
  coordinates were changed.
- Changed post-mount tip verification to tolerate the observed intermittent
  sensor false while a tip is physically attached. After the stage retracts,
  up to three live checks are made at `0.4 s` intervals; any positive reading
  continues the workflow, while three consecutive absent readings still stop
  safely. This prevents a false abort from suppressing the following Z-safe
  lift. No coordinates or motion order were changed.
- Updated the stale wiring test to assert the production udev alias
  `/dev/autospin_xyz` and the restored `1000 mm/min` automatic feed profile.

### Diagnosed

- Real hardware logs showed a successful `10 -> 74 mm` tip-press move followed
  by a `74 -> 10 mm` retract timeout. The process move completed at about
  `72.64 mm`, and the failed retract was stopped at about `11.20 mm`. This
  matched the previous implementation's use of observed position as the next
  relative-command origin.
- Subsequent multi-round execution advanced beyond the stage timeout but
  stopped safely because input register `0x0D` still reported
  `tip_present=false` after tip mounting.
- Verified that both `AutoSpinmotorSystem` and the current backend read Tip
  presence from input register `0x0D`, with value `1` meaning present. The
  real-hardware diagnostic returned `1/true` with the tip installed and
  `0/false` after removal, confirming the register, polarity, sensor, and RS485
  path are correct. The automatic failure is therefore treated as transient
  feedback while the press-fit mechanism is still loaded.
- A later real run completed tip ejection and entered gantry Alarm while
  departing the waste station. The legacy changelog showed that the new action
  path had omitted its required `2 s` post-eject settle and that the production
  configuration had restored the previously rejected `5000 mm/min` XY speed.

### Coordinates

- No production coordinate changes are part of this update. A proposed
  tip-rack coordinate replacement was fully reverted at operator request;
  existing calibrated values remain unchanged.

### Verification

- Heater/multi-round targeted tests passed: `27 passed`.
- Linear-stage, adapter, and multi-round targeted tests passed: `39 passed`.
- Coordinate, action, generation, linear-stage, and multi-round regression
  tests passed after the coordinate revert: `77 passed`.
- Tip-change, multi-round, and pipette-backend regression tests after the
  post-retraction verification change: `43 passed`.
- Action generation, multi-round scheduling, coordinates, production gate,
  relay, and emergency-stop regression tests after the stage/vacuum timing
  correction: `101 passed`.
- Tip timing, configuration, coordinate, scheduler, gantry backend, and GRBL
  controller regression run after the Alarm recovery migration: `121 passed`.
- Post-spin cleanup and 16-round resource-allocation regression set:
  `92 passed`.
- Hotplate release and actual-consumption tip-allocation regression set:
  `52 passed`.
- Pipette transient-tip, action, and multi-round regression set: `49 passed`.
- Complete suite after the transient tip-signal confirmation correction:
  `479 passed, 1 skipped`.
- The latest complete suite before the Tip settle addition reported
  `470 passed, 1 skipped`; its one unrelated failure expects `/dev/ttyUSB1`
  while production configuration intentionally uses `/dev/autospin_xyz`.

## 2026-08-06 - Direct single-command linear-stage moves

### Changed

- Removed the automatic midpoint split for linear-stage moves longer than
  50 mm. Every absolute move now sends one direct command from the current
  position to the requested target through the current backend/adapter.
- Preserved homing requirements, position bounds, target tolerance, three
  stable feedback reads, live fault checks, bounded timeout, and immediate stop
  on failure.
- Replaced native-backend and adapter regression tests for two-segment motion
  with assertions that an 80 mm or 70 mm move produces exactly one command.

### Verification

- Linear-stage targeted tests: `25 passed`.
- Complete maintained suite: `468 passed, 1 skipped`.
- Changed runtime module passed `python -m py_compile`.

## 2026-08-05 - Phase 2 hardware acceptance planning

### Added

- Added `HARDWARE_ACCEPTANCE_TEST_PLAN.md` with gated acceptance procedures for
  empty XYZ motion, tip pickup, gripper cycling, nine-slot hotplate operation,
  three-sample overlapping annealing, spin/antisolvent timing, emergency stop,
  and power-loss recovery.
- Documented measured evidence, stop conditions, acceptance records, and the
  required supervised progression from empty motion to real process liquids.
- Audited `autospin_system/` retirement readiness and documented the remaining
  pytest, diagnostic-tool, deployment-documentation, and historical-reference
  dependencies that currently prevent safe deletion.

### Corrected

- Clarified that `autospin_system/` is an obsolete intermediate implementation
  and must not be used as a behavior-migration authority. The sole authoritative
  legacy behavior source is the separate `AutoSpinmotorSystem` project.
- Added `LEGACY_AUTOSPIN_SYSTEM_RETIREMENT_PLAN.md` with a complete reference
  inventory, production-path independence evidence, `src`-based diagnostic
  replacements, and deletion prerequisites.

### Phase 1 dependency cleanup

- Rewrote panel wiring and spin bring-up diagnostics to use one configured
  `DeviceRegistry` and current backend APIs, retaining explicit hardware safety
  confirmations and guaranteed spin stop behavior.
- Retired the parallel Streamlit emergency dashboard in favor of the maintained
  DeviceRegistry-backed Web/API emergency-stop path.
- Removed the obsolete directory from pytest discovery and updated udev
  verification commands to current configuration, tests, and smoke tools.
- Corrected active backend/test/document provenance to name
  `AutoSpinmotorSystem` as the sole authoritative legacy behavior source.
- Verified zero active obsolete-package imports, zero obsolete tests collected,
  and `468 passed, 1 skipped` in the maintained test suite.

### Phase 2 removal rehearsal

- Created an isolated current-tree copy with `autospin_system/` absent while
  leaving the main working tree untouched.
- Verified the maintained test suite, real mock Web-server startup/health,
  configured DeviceRegistry construction, protocol generation, single dry-run,
  and three-round dry-run without loading any obsolete module.
- Confirmed software-level removal safety and documented two final deployment
  blockers: an ignored dormant emergency-dashboard backup and the absence of
  Raspberry Pi systemd/environment definitions from the repository.
- Added `LEGACY_REMOVAL_REHEARSAL_REPORT.md` with commands, evidence, failures,
  deployment assumptions, blockers, and the conditional deletion decision.

### Raspberry Pi deployment legacy-path audit

- Audited repository startup, Python-path assumptions, udev documentation, and
  runtime references for the production Raspberry Pi deployment.
- Confirmed the maintained entry is `tools/run_webserver.py` into `src.webapp`
  with no executable `autospin_system` dependency.
- Documented that two maintained routes still resolve authoritative
  `AutoSpinmotorSystem` data directories, distinct from the obsolete package.
- Attempted read-only SSH access through every documented Pi address; all were
  unreachable, so live systemd, process environment, Python installation, and
  deployment-directory status remain explicitly unverified.
- Added `RASPBERRY_PI_LEGACY_DEPLOYMENT_AUDIT.md` with ready-to-run Pi commands,
  pass criteria, blockers, and a non-release conclusion for live deletion.

## 2026-08-05 - Deadline-aware multi-round annealing migration

### Added

- Added a semantic L3 multi-round planner that overlaps hotplate dwell with
  later rounds and tracks an absolute annealing deadline for every substrate.
- Added safe pre-positioning above the due hotplate slot followed by an
  absolute `WaitUntilElapsed` action, so prior action timing errors do not
  accumulate into annealing dwell errors.
- Added generated 4x4 substrate-rack and 3x3 hotplate semantic coordinates,
  including validated hotplate-slot reuse after the prior substrate is removed.
- Added protected scheduling blocks for precursor dispense and for second-tip,
  antisolvent, and timed spin operations.
- Added runtime `WaitUntilSpinElapsed`, placement-anchored annealing deadlines,
  sample-state tracking, and explicit nine-slot hotplate ownership.
- Added cooperative experiment cancellation wired to emergency stop and live
  pipette tip-replacement verification through the adapter/backend boundary.

### Fixed

- Fixed the single-round annealing lifecycle so it explicitly places the
  substrate on the hotplate, waits, picks it up, and returns it to its source
  rack slot.
- Replaced sequential whole-round execution in the multi-round Web route with
  one safety-validated interleaved semantic action plan.
- Fixed dispense actions so saved pipette clearances control the linear-stage
  process position and precursor sources advance deterministically by round.
- Added safety validation for the final dynamically scheduled action plan.

### Verification

- `pytest -q`: `486 passed, 1 skipped`.
- `python -m py_compile` passed for all changed runtime modules.

## 2026-07-29 - Multi-round execution, taught coordinates, and linear-stage reliability

### Spincoater acceleration control

- Added a spincoater-page control for setting rotational acceleration in
  `RPM/s`; the selected value is shown in the live device status.
- Reduced the default acceleration to `500 RPM/s`.
- Implemented acceleration as a 200 ms software speed ramp through the verified
  speed register. No undocumented DBLS400 acceleration register is guessed or
  written.
- Starting from rest and changing speed while already running both use the
  configured ramp. Spin stop/brake behavior is unchanged.
- Added request validation for `50` through `6000 RPM/s` and backend tests for
  ramp steps and status reporting.

### Web experiment builder and multi-round execution

- Fixed experiment-builder values being overwritten by asynchronously loaded
  defaults, which previously made values such as total/repeat rounds appear
  stuck at `4`.
- Kept total rounds as the calculated sum of parameter-group repeat counts.
- Added a human-readable saved-recipe summary containing the experiment
  parameters, group repeat counts, total rounds, round ranges, spin stages,
  dispense timing, liquid volumes, tip height, and annealing settings.
- Fixed the multi-round parameter summary becoming misaligned because it still
  inherited the five-column `.routine-table` widths. It now uses an independent
  eight-column table, and both JavaScript and stylesheet cache versions are
  updated so deployed browsers do not reuse the previous layout.
- Made both spin-stage cells show labeled speed and time on separate lines, and
  made the annealing cell show labeled temperature and time on separate lines.
- Added Web controls for Dry-run and formal execution of a generated and saved
  multi-round JSON recipe.
- Added `POST /api/experiments/multi-round/execute`.
- Newly generated recipes now include executable `protocols`; older builder
  recipes can be reconstructed from `parameter_groups`.
- All rounds are submitted as one gated operation and report completed/total
  round counts.
- Improved HTTP 422 messages to identify the rejected round and each safety
  issue instead of showing only a generic request failure.
- Improved background failures to report the failed round and underlying
  device/action error instead of only `L3.INTERNAL_ERROR`.
- Throttled `/api/operations/current` browser polling to at most once per
  second.
- Added a per-parameter-group `use_antisolvent` option. When disabled, recipe
  generation removes the second tip pickup, antisolvent aspiration, timed
  antisolvent dispense, timing validation, and associated spin timed event.
- Pipette tip slots are now allocated globally across generated rounds based
  on actual consumption. A round without antisolvent consumes one tip, so its
  following round uses the immediately following slot rather than skipping a
  slot.
- Added generated `stations.tip_rack.slot_1` through `slot_96` coordinates
  using the calibrated 8-column by 12-row rack grid (`X +9 mm`, `Y -9 mm`).
- Generated protocols persist their liquid-to-tip-slot mapping in
  `pipette_tip_slots`, keeping saved JSON execution deterministic.
- Reworked the human-readable parameter-group summary into eight fixed-layout
  columns. Repeat count and round range share one column, while antisolvent
  volume and timing share another, preserving header/body alignment and
  reducing crowding.

### Automatic hardware preparation

- Formal execution of a saved multi-round recipe now automatically connects,
  in deterministic order:
  - relay;
  - gantry;
  - spincoater;
  - pipette;
  - linear stage;
  - heater.
- The gripper is not treated as an independently connected device because it
  is actuated through the relay.
- After connecting, formal execution automatically homes the gantry, pipette,
  and linear stage sequentially before safety preflight and experiment
  execution.
- Dry-run does not connect or home real hardware.
- A connection or homing failure stops preparation immediately and reports the
  affected device. No experiment round is started after a failed preparation.
- Production preflight now reports disconnected required devices and an
  unhomed gantry before accepting a real run.

### Coordinate semantics and configuration cleanup

- Established that configured process XYZ values are taught gantry machine
  positions for the corresponding installed tool. Tool identity selects the
  gripper or pipette action but does not apply a second geometric translation.
- Removed the legacy runtime tool-offset configuration:

  ```yaml
  tool_offsets: {}
  ```

- Explicit coordinate authorization is controlled by:

  ```yaml
  requires_hardware_confirmation: false
  executable: true
  ```

  The `verification` field remains provenance/audit information and no longer
  overrides those explicit execution flags.
- Removed quarantined legacy/placeholder coordinate conflicts after selecting
  the current coordinate set as authoritative:

  ```yaml
  unresolved_coordinates: {}
  ```

- Added `linear_stage_position_mm` to pipette process positions and propagated
  it through coordinate resolution.
- Pipette semantic operations now move the linear stage to its safe position,
  move the gantry, move the linear stage to the process position, execute the
  pipette action, and return the linear stage to the safe position.
- Generated rack coordinates use the configured grid offsets for substrates,
  pipette tips, reagents, and hotplate positions. Generated points remain
  subject to configured machine soft limits.

### Linear-stage movement reliability

- Changed the accepted final-position tolerance to `0.7 mm`.
- All linear-stage movement paths now read the current position before moving.
- Absolute movements longer than `50 mm` are split into two midpoint segments.
- Relative moves route through the same verified absolute segmented movement
  path.
- The behavior applies to Web-triggered moves and automated experiment
  execution because both use the shared linear-stage backend.
- Each segment retains bounded timeout, immediate-stop-on-failure, idempotency,
  final-position verification, and configured travel-limit checks.
- Restored the hardware-proven settled-position rule: a move completes only
  after three consecutive live position readings are within the configured
  `±0.7 mm` tolerance. This prevents automation from advancing on the first
  edge crossing of the tolerance band while the stage is still moving.
- Tip-rack points retain the commanded `74 mm` press-in position but declare a
  dedicated `3.0 mm` contact tolerance. Ordinary stage moves remain at
  `±0.7 mm`; only tip pickup can complete under expected mechanical compression.
- Spin execution now energizes relay channel 3 (the vacuum valve) before the
  first spin segment and always de-energizes it after motor stop. Nested
  cleanup also closes channel 3 when spin start, timed dispense, or stop fails.
- Added dedicated CH3 vacuum ON/OFF controls and a CH3 state readout to the
  spincoater Web card. These controls and automated spin now use a forced relay
  write that bypasses the optimistic software memo, ensuring each request sends
  a physical DSTUR command even when the cached state already matches.
- Forced CH3 controls now connect the relay automatically when necessary;
  automated real runs do the same while dry-runs remain hardware-free.
- Corrected DBLS400 `0x801B` presentation: only low-byte bits 0–7 are faults.
  A nonzero high-byte run state such as `0x05` no longer appears in the Web
  fault list or triggers alarm styling.
- Fixed the spincoater card remaining disabled after a CH3 request. Vacuum
  operations are now tracked under their actual `relay` operation device, so
  the relay card is briefly pending while spincoater controls remain usable.

### Automated gantry motion profile

- Changed the normal automated gantry feed to `5000 mm/min`.
- Increased the configured GRBL X/Y maximum rates (`$110`-`$111`) to
  `5000 mm/min`.
- Restored an independent Z feed and GRBL Z maximum rate (`$112`) of
  `1000 mm/min`. Z must not inherit the `5000 mm/min` XY feed because the
  brake-equipped Z axis has previously produced vibration, lost steps, invalid
  position reports, and unconfirmed motion state at excessive speed.
- Increased the configured GRBL X/Y/Z accelerations (`$120`-`$122`) from
  `200` to `500 mm/s²`.
- Added a `30 mm` soft-limit boundary zone. If either the current XY position
  or commanded XY target is inside this zone, the complete XY segment uses
  `1000 mm/min`, including movement away from a boundary.
- Automated semantic-coordinate moves now follow the complete legacy
  AutoSpinmotorSystem safe-move behavior: when current Z is below the safe
  travel height, lift Z to `-30 mm`; complete XY; then issue and complete the
  final Z move. Both Z segments use the dedicated `1000 mm/min` feed.
- XY and Z receive independent boundary-speed decisions, so an XY target near
  an X/Y limit can slow to `1000 mm/min` without unnecessarily slowing a safe
  Z move.
- Z feed lookup now has a compatibility fallback of `1000 mm/min`, preventing
  a stale deployed `config.py` from crashing a real run with
  `AttributeError: GantryHardwareConfig has no attribute z_feed_mm_min`.
- After confirmation that the current taught coordinates are authoritative,
  aligned GRBL travel settings with the configured `-310/-110 mm` software
  envelope: `$130=305`, `$131=305`, and `$132=105`.

### Validation

- Relevant coordinate, action, Web configuration, and multi-round tests pass.
- The latest focused automatic connection/homing and execution test run
  completed with `19 passed`.
- The broader coordinate/action/configuration test run completed with
  `40 passed`.

## 2026-07-29 - Raspberry Pi SSH/SCP upload failure diagnosis

### Environment

- Host: Windows 11, OpenSSH_for_Windows_9.5p1.
- Target: Raspberry Pi 5, Debian 13,
  `Linux 6.12.75+rpt-rpi-2712`.
- Target SSH: OpenSSH_10.0p2 Debian-7+deb13u2.

### Symptom

- Uploading selected files with the default Windows `scp` command could fail
  immediately at 0%:

  ```powershell
  scp src\webapp\static\index.html pi@192.168.50.2:/home/pi/autospin/src/webapp/static/index.html
  ```

- The client reported:

  ```text
  index.html 0% 0.0KB/s --:-- ETA
  client_loop: send disconnect: Connection reset
  scp.exe: Connection closed
  ```

- Existing SSH sessions then stopped responding and new connections timed out
  until the Raspberry Pi was restarted.

### Diagnosis

- CPU remained approximately 95-100% idle.
- Memory usage remained approximately 500 MB out of 4 GB, with more than
  3 GB available and no swap usage.
- No OOM killer event was found.
- Root filesystem usage was normal: approximately 8.5 GB of 58 GB (16%).
- No AutoSpin, Python, Flask, FastAPI, Uvicorn, Node, or Vite service was
  running, excluding application hot reload and file watchers.
- `vcgencmd get_throttled` returned `throttled=0x0`, with no evidence of
  undervoltage or thermal throttling.
- Kernel logs contained:

  ```text
  EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs
  ```

  This was consistent with recovery after an earlier abnormal shutdown. No
  `mmc timeout`, I/O error, or EXT4 filesystem error was found, and a 100 MB
  write test completed successfully.

### Protocol Isolation Tests

- Direct SFTP upload succeeded:

  ```powershell
  sftp pi@192.168.50.2
  ```

  ```sftp
  put src/webapp/static/index.html /home/pi/index_test.html
  ```

- Legacy SCP protocol mode succeeded:

  ```powershell
  scp -O src\webapp\static\index.html pi@192.168.50.2:/home/pi/index_test.html
  ```

- Default `scp` mode failed for the same source and destination.
- Verbose logging showed the failure after:

  ```text
  Sending SSH2_FXP_WRITE
  ```

- The failure was therefore isolated to the SFTP write path used by the
  default modern `scp` mode, rather than SSH authentication, file permissions,
  AutoSpin services, CPU, RAM, power, or available disk space.

### Root Cause

- Modern OpenSSH `scp` uses SFTP by default. In the tested combination of
  Windows OpenSSH 9.5p1 and Debian 13 OpenSSH 10.0p2, the default
  `scp`-over-SFTP write path exhibited a connection-reset compatibility
  problem.
- Forcing legacy SCP protocol with `-O` bypassed the failing SFTP path and
  completed the same transfer successfully.

### Resolution and Deployment Rule

- Raspberry Pi deployments from Windows must use legacy SCP protocol mode:

  ```powershell
  scp -O file pi@192.168.50.2:/home/pi/path/
  ```

- Recursive copies must also include `-O`:

  ```powershell
  scp -O -r src pi@192.168.50.2:/home/pi/autospin/
  ```

- Interactive `sftp` with `put` remains an alternative because it succeeded
  during isolation testing.
- For larger deployments, package the update locally and transfer one archive:

  ```powershell
  tar czf autospin_update.tar.gz src config requirements.txt
  scp -O autospin_update.tar.gz pi@192.168.50.2:/home/pi/
  ```

  Then extract it on the Raspberry Pi:

  ```bash
  tar xzf autospin_update.tar.gz -C ~/autospin
  ```

- Do not use the default `scp` mode for the current Raspberry Pi deployment
  environment. Always include `-O` unless the OpenSSH/SFTP compatibility issue
  has been independently retested and confirmed resolved.

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
