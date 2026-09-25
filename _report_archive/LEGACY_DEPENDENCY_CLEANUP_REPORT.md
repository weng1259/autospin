# Legacy `autospin_system` Dependency Cleanup Report

Date: 2026-08-05  
Scope: retirement Phase 1  
Result: active dependency removed; obsolete directory left untouched

## Authority and scope

`autospin_system/` was treated only as an obsolete intermediate directory. No
logic, tests, protocol behavior, register assumption, or experimental behavior
was migrated from it.

The only legacy behavior authority is:

```text
D:/study/pythonlearning/study/history_version/AutoSpinMerge/AutoSpinmotorSystem
```

This cleanup did not modify `ActionExecutor`, workflow compilation,
multi-round scheduling, experimental actions, or protocol behavior.

## Changes completed

### Diagnostic tools

- `tools/panel_wiring_smoke.py`
  - Rewritten around one `DeviceRegistry.from_config()` instance.
  - Uses registered current backends for read-only status/PV checks.
  - Uses registry serial-resource diagnostics instead of a parallel RS485 lock
    or controller proxy.
  - Requires `--confirm-read-only-hardware` before opening any configured port.
  - Does not home, move, toggle relays, set temperature, or start the motor.
- `tools/spinmotor_bringup.py`
  - Rewritten around `DeviceRegistry.from_config().spincoater`.
  - Uses the current `SpincoaterBackend` `status`, `start`, and `stop` APIs.
  - A supplied RPM is dry-run by default and does not connect to hardware.
  - Status-only hardware access requires
    `--confirm-read-only-hardware`.
  - Real rotation requires all of: RPM at or below 300, `--apply`, and the exact
    phrase `I_HAVE_CLEARED_AND_GUARDED_THE_SPINCOATER`.
  - A real start is followed by brake stop in `finally`.
- `tools/ui/emergency_dashboard.py`
  - Retired instead of maintaining a second hardware-controller stack.
  - It now opens no hardware and directs operators to the maintained FastAPI/Web
    service and authenticated `/api/estop` path.

### Test discovery

- `pytest.ini`
  - Changed `testpaths` from `tests autospin_system` to `tests`.
  - Maintained tests remain under `tests/`.
  - No obsolete-directory test was copied or migrated.

### Deployment documentation

- `deploy/udev/README.md`
  - Replaced the obsolete configuration path with `config/hardware.yaml` and
    `config/devices.yaml`.
  - Replaced obsolete smoke commands with maintained configuration/resource
    tests and the registry-backed read-only wiring smoke.
  - Documents the guarded current spin bring-up command.

### Provenance corrections

The following now identify `AutoSpinmotorSystem` as the authoritative legacy
source instead of the obsolete intermediate directory:

- `src/hardware/heater_backend.py`
- `src/hardware/pipette_backend.py`
- `src/hardware/spincoater_backend.py`
- `tests/test_heater_backend.py`
- `tests/test_pipette_backend.py`
- `tests/test_spincoater_backend.py`
- `docs/decision-log/task-w1.1-heater-backend.md`
- `docs/decision-log/task-w1.2-spincoater-backend.md`
- `docs/decision-log/task-w1.3-pipette-backend.md`

Additional documentation updates:

- `docs/verification/spinmotor-bringup.md` now points to the maintained
  registry-backed tool.
- `HARDWARE_ACCEPTANCE_TEST_PLAN.md` now explicitly prohibits use of the
  obsolete directory as a hardware or behavior comparison source and records
  completion of the active dependency cleanup.
- `CHANGELOG.md` records the Phase 1 cleanup.

## Files changed

```text
tools/panel_wiring_smoke.py
tools/spinmotor_bringup.py
tools/ui/emergency_dashboard.py
pytest.ini
deploy/udev/README.md
src/hardware/heater_backend.py
src/hardware/pipette_backend.py
src/hardware/spincoater_backend.py
tests/test_heater_backend.py
tests/test_pipette_backend.py
tests/test_spincoater_backend.py
docs/decision-log/task-w1.1-heater-backend.md
docs/decision-log/task-w1.2-spincoater-backend.md
docs/decision-log/task-w1.3-pipette-backend.md
docs/verification/spinmotor-bringup.md
HARDWARE_ACCEPTANCE_TEST_PLAN.md
CHANGELOG.md
LEGACY_DEPENDENCY_CLEANUP_REPORT.md
```

`autospin_system/` itself was not modified or deleted.

## Remaining `autospin_system` references

There are no executable imports of `autospin_system` under `src`, `tools`,
`tests`, `deploy`, or `config`.

Remaining references outside the obsolete directory are non-executable and
intentional:

- `LEGACY_AUTOSPIN_SYSTEM_RETIREMENT_PLAN.md` and this report name the retirement
  target.
- `HARDWARE_ACCEPTANCE_TEST_PLAN.md` names it only to prohibit its use and plan
  deletion.
- `CHANGELOG.md` records the audit/correction history.
- `tests/test_consolidated_drivers.py` contains a negative assertion ensuring
  consolidated drivers do not mention/import the obsolete package.
- `docs/decision-log/task-w0-residual-issue-028.md` describes preventing pytest
  from collecting obsolete top-level hardware scripts; it does not use them as
  behavior authority.
- `migration_snapshot/2026-07-25/` contains frozen historical copies. It is not
  in test discovery or the production path and remains a Phase 2 archive/removal
  decision.
- Self-references inside `autospin_system/` remain because the directory was
  explicitly required to stay untouched.

## Verification results

### Static import audit

Search scope: `src`, `tools`, `tests`, `deploy`, and `config`.

```text
ACTIVE_IMPORTS=0
```

### Pytest discovery audit

```text
COLLECTED_LEGACY_MATCHES=0
```

### Production construction audit

A mock Web application and a configured, non-connected
`DeviceRegistry.from_config()` were constructed. Inspection of `sys.modules`
returned:

```text
LOADED_LEGACY_MODULES=[]
```

### Tool safety checks

- `python tools/panel_wiring_smoke.py`
  - refused to open hardware without its explicit read-only confirmation;
- `python tools/spinmotor_bringup.py --rpm 150`
  - returned a backend dry-run result with `connected=false`; no motor command
    was sent;
- `python tools/ui/emergency_dashboard.py`
  - printed the maintained Web/API entry point and opened no hardware;
- changed tool modules passed `python -m py_compile`.

No real hardware command was issued during this cleanup.

### Maintained test suite

```text
python -m pytest -q
468 passed, 1 skipped, 1 warning
```

The count is lower than the earlier combined collection because pytest no
longer collects tests from the obsolete directory. The warning is the existing
Starlette `TestClient` deprecation warning.

## Remaining retirement work

- Perform the removal rehearsal described in
  `LEGACY_AUTOSPIN_SYSTEM_RETIREMENT_PLAN.md` with the obsolete directory absent
  from a disposable checkout.
- Decide whether to externally archive or remove `migration_snapshot` from the
  deployed application path.
- Run the rewritten read-only registry smoke and guarded spin bring-up during
  supervised hardware acceptance.
- Verify Raspberry Pi service commands and environment contain no obsolete
  package path.
- Delete `autospin_system/` only as a separate, explicitly reviewed Phase 2
  change after all prerequisites pass.

