# `autospin_system` Retirement Plan

Date: 2026-08-05  
Status: audit complete; removal not yet authorized

## 1. Authority correction

`autospin_system/` is an obsolete intermediate implementation located inside
the new `autospin` repository. It is **not** a migration source of truth and
must not be used to infer, restore, validate, or change experimental behavior.

The only authoritative legacy behavior source is:

```text
D:/study/pythonlearning/study/history_version/AutoSpinMerge/AutoSpinmotorSystem
```

When a replacement diagnostic needs hardware semantics that are absent from
the current `src` API, its requirements must be established from
`AutoSpinmotorSystem`, the applicable hardware manual, and measured hardware
behavior. No code or behavior should be migrated from `autospin_system`.

## 2. Production execution dependency result

### Conclusion

The current production execution path does **not** import or depend on
`autospin_system`.

The inspected production chain is:

```text
Web/API request
  → src.webapp routes and OperationGate
  → ExperimentService
  → protocol/workflow/multi-round action plan
  → ActionExecutor
  → DeviceRegistry
  → src.hardware backends
  → src.hardware.autospinmotor_adapters
  → src.hardware.drivers or direct backend communication
```

Static inspection found no `from autospin_system` or
`import autospin_system` statement under `src/`. Adapter factory methods import
controllers from `src/hardware/drivers/`, not from the obsolete package.

### Runtime construction check

The audit constructed both of the following without connecting or actuating
hardware:

- a mock `DeviceRegistry` and Web application;
- `DeviceRegistry.from_config()` using the configured production backend
  selection.

After each construction, `sys.modules` contained no module named
`autospin_system` and no `autospin_system.*` child module.

This confirms application construction independence. Hardware acceptance must
still repeat the check against the deployed Raspberry Pi service command and
environment, because an external startup wrapper can alter `PYTHONPATH` or use
the wrong entry point without appearing in repository `src` imports.

## 3. Remaining references outside the obsolete directory

References inside `autospin_system/` itself are self-references and disappear
with the directory. References inside `.git` history and generated cache
directories are not runtime dependencies. The actionable references outside
the directory are categorized below.

### 3.1 Executable diagnostic dependencies — must be rewritten or retired

| File | Current dependency | Required disposition |
|---|---|---|
| `tools/panel_wiring_smoke.py` | Imports `SharedRs485DeviceProxy`, legacy heater controller, and legacy spin motor controller | Rewrite using `DeviceRegistry.from_config()`, shared `src.hardware.rs485_bus.Rs485Bus`, and read-only status APIs on registered backends |
| `tools/spinmotor_bringup.py` | Imports legacy `MotorController` and `Registers` | Rewrite around `DeviceRegistry.spincoater`, `SpincoaterBackend`, or the `src.hardware.drivers.spin_motor` controller; preserve explicit confirmation gates |
| `tools/ui/emergency_dashboard.py` | Imports legacy shared proxy, heater controller, and motor controller | Retire in favor of the maintained FastAPI/Web console, or rewrite to receive one `DeviceRegistry` and invoke `registry.estop`/backend APIs only |

These three files are the only direct `autospin_system` imports found outside
the obsolete directory and frozen migration snapshots.

### 3.2 Test collection dependency — must be removed

| File | Current reference | Required replacement |
|---|---|---|
| `pytest.ini` | `pythonpath` comments describe dual-package topology; `testpaths = tests autospin_system` | Change test discovery to `testpaths = tests` and describe `src` as the maintained architecture |

The obsolete directory currently contains 14 top-level `test_*.py` scripts.
They are a mixture of hardware smoke scripts and tests of obsolete protocol,
Maestro, worker, and controller implementations. They must not be bulk-copied
into `tests/`.

For each script, classify it as one of:

1. obsolete implementation test — delete with the old directory;
2. useful hardware diagnostic procedure — rewrite against `src` APIs;
3. experimentally meaningful behavior claim — independently verify against
   `AutoSpinmotorSystem` before adding a new `src` test.

### 3.3 Deployment documentation dependency — must be rewritten

| File | Current reference | Required replacement |
|---|---|---|
| `deploy/udev/README.md` | Points serial mapping at `autospin_system/config/system_config.yaml` | Point to `config/hardware.yaml` and `config/devices.yaml` |
| `deploy/udev/README.md` | Runs `autospin_system.test_heating_stage_smoke` | Use a new read-only heater smoke based on `DeviceRegistry`/`HeaterBackend` |
| `deploy/udev/README.md` | Runs `autospin_system/test_port_mapping.py` | Add or use a `tests/` configuration/serial-resource test based on `src.hardware.serial_resources` and the new config models |

Deployment scripts themselves did not contain an obsolete-package import in
this audit; the actionable deployment references are in the udev README.

### 3.4 Maintained source and test comments — correct provenance

The following are non-executable references, but they incorrectly make the
obsolete directory appear authoritative and therefore must be corrected:

- `src/hardware/heater_backend.py`
- `src/hardware/pipette_backend.py`
- `src/hardware/spincoater_backend.py`
- `tests/test_heater_backend.py`
- `tests/test_pipette_backend.py`
- `tests/test_spincoater_backend.py`

Replace provenance text with the exact authoritative
`AutoSpinmotorSystem/...` path and, where relevant, the hardware manual or
measured acceptance record. This is a documentation/provenance correction,
not permission to change hardware behavior.

`tests/test_consolidated_drivers.py` contains a negative guard asserting that
new driver source does not contain the text `autospin_system`. Keep the intent,
but replace it with a repository-wide AST/import guard that rejects obsolete
package imports anywhere under `src/`, `tools/`, and production entry points.
The test should not treat mere comments as runtime dependencies.

### 3.5 Historical decision and verification documents — mark superseded

The following documents describe prior decisions that cited
`autospin_system` as a reference:

- `docs/decision-log/task-w1.1-heater-backend.md`
- `docs/decision-log/task-w1.2-spincoater-backend.md`
- `docs/decision-log/task-w1.3-pipette-backend.md`
- `docs/decision-log/task-w0-residual-issue-028.md`
- `docs/verification/spinmotor-bringup.md`

Do not silently rewrite historical claims. Add a prominent supersession note
stating that `autospin_system` was an intermediate implementation, is not an
accepted behavioral authority, and that any behavior must be revalidated
against `AutoSpinmotorSystem` and hardware evidence. Replace active commands
with their new diagnostic equivalents.

### 3.6 Current project documentation

- `HARDWARE_ACCEPTANCE_TEST_PLAN.md` contains the retirement assessment. Amend
  it to state explicitly that the obsolete directory must not be used as a
  behavioral comparison during acceptance; only manuals, current `src`
  contracts, `AutoSpinmotorSystem`, and measured hardware evidence are valid.
- `CHANGELOG.md` records the retirement audit. Its reference is descriptive
  and can remain, provided it does not label the directory authoritative.
- This retirement plan necessarily names the directory being removed and is
  expected to remain as the decision record after deletion.

### 3.7 Frozen migration snapshot

`migration_snapshot/2026-07-25/` contains copies of tools and comments that
reference `autospin_system`. It is excluded from production execution, but it
will keep repository-wide searches noisy and can allow accidental execution.

Choose one explicit disposition before removal:

- move the snapshot to an external, checksum-recorded read-only archive; or
- retain it under a clearly non-executable historical archive with a README
  stating that neither its code nor `autospin_system` is behavior authority.

It must not remain on the deployed Raspberry Pi application path.

## 4. Replacement plan using the `src` architecture

### 4.1 Read-only panel wiring smoke

Rewrite `tools/panel_wiring_smoke.py` as follows:

1. construct exactly one `DeviceRegistry.from_config()`;
2. call the registry's deterministic connect path or explicit read-only
   backend connect methods;
3. verify the registry owns one relay instance used by gantry and gripper;
4. verify heater, spincoater, and pipette share the configured `Rs485Bus`
   resource rather than a legacy proxy;
5. read status/PV only—no relay toggle, SV write, homing, or motor start;
6. close via registry lifecycle methods in a `finally` block.

### 4.2 Spin motor bring-up

Rewrite `tools/spinmotor_bringup.py` around the registered spincoater backend.
Keep the safety properties—dry-run default, explicit real-hardware confirmation,
low RPM ceiling, guaranteed stop in `finally`, and fault/status logging.

If bus-voltage or raw-register diagnostics are not exposed by the current
backend, add a narrowly scoped read-only diagnostic API only after confirming
the register and scaling from the DBLS400 manual and authoritative
`AutoSpinmotorSystem`. Do not reproduce the obsolete intermediate driver's
register assumptions.

### 4.3 Emergency dashboard

Preferred action: retire `tools/ui/emergency_dashboard.py`, because the
maintained Web application already exposes DeviceRegistry-backed device panels,
the global operation gate, and `/api/estop`.

If a separate acceptance dashboard is still required, it must consume the
same backend API or one injected `DeviceRegistry`; it must not create parallel
serial controllers or a second shared-bus abstraction.

### 4.4 Hardware smoke replacements

Provide new, explicitly named diagnostic entry points for the udev README:

- read-only heater PV/status check through `HeaterBackend`;
- port/config identity check through current config and serial-resource code;
- full read-only registry wiring check through the rewritten panel smoke.

Hardware-actuating tests must live under `tools/` with explicit confirmation,
not be automatically collected by pytest. Offline tests and mocks remain under
`tests/`.

## 5. Files requiring modification before deletion

### Required executable/configuration changes

- `pytest.ini`
- `tools/panel_wiring_smoke.py`
- `tools/spinmotor_bringup.py`
- `tools/ui/emergency_dashboard.py` — rewrite or delete as a single explicit
  retirement action
- `deploy/udev/README.md`

### Required provenance and documentation corrections

- `src/hardware/heater_backend.py`
- `src/hardware/pipette_backend.py`
- `src/hardware/spincoater_backend.py`
- `tests/test_heater_backend.py`
- `tests/test_pipette_backend.py`
- `tests/test_spincoater_backend.py`
- `tests/test_consolidated_drivers.py`
- `docs/decision-log/task-w0-residual-issue-028.md`
- `docs/decision-log/task-w1.1-heater-backend.md`
- `docs/decision-log/task-w1.2-spincoater-backend.md`
- `docs/decision-log/task-w1.3-pipette-backend.md`
- `docs/verification/spinmotor-bringup.md`
- `HARDWARE_ACCEPTANCE_TEST_PLAN.md`

### Archive decision

- `migration_snapshot/2026-07-25/`

## 6. Deletion prerequisites

All of the following must be true before deleting `autospin_system/`:

- [ ] The three direct-import diagnostic tools have been rewritten or formally
      retired.
- [ ] `pytest.ini` collects only maintained tests.
- [ ] Valuable old-directory tests have been classified; no test has been
      copied merely because it existed in the intermediate package.
- [ ] New diagnostics derive behavior only from `AutoSpinmotorSystem`, current
      `src` contracts, hardware manuals, and hardware acceptance evidence.
- [ ] Udev/deployment instructions use current configuration and diagnostics.
- [ ] Active source comments and tests no longer cite the intermediate package
      as implementation authority.
- [ ] Historical decision documents carry a supersession notice.
- [ ] The migration snapshot has an explicit non-production archive decision.
- [ ] Repository search finds no executable import of `autospin_system` outside
      the directory scheduled for deletion.
- [ ] `python -m pytest -q` passes with the obsolete directory temporarily
      absent from import/test discovery.
- [ ] Web application construction, `DeviceRegistry.from_config()`, protocol
      generation, dry-run single experiment, and dry-run multi-round experiment
      all pass without the obsolete directory.
- [ ] Rewritten read-only hardware smoke tests pass on the Raspberry Pi.
- [ ] Deployed service/unit files and process command lines use the maintained
      `src` entry point and do not add an archived directory to `PYTHONPATH`.
- [ ] A source-control tag or checksum-recorded archival artifact exists for
      forensic history; it is not installed in the production application
      path.
- [ ] The deletion is reviewed as a standalone change with no experimental
      behavior modifications mixed into it.

## 7. Removal rehearsal and final verification

Because directory removal is broad and difficult to review when mixed with
other work, perform it in a dedicated branch/change set:

1. complete all prerequisite rewrites;
2. temporarily make the obsolete directory unavailable in a disposable copy
   or clean checkout;
3. run static import search, test collection, the complete test suite, Web app
   startup, and dry-run experiment paths;
4. deploy that candidate to a non-production Raspberry Pi location and run the
   read-only registry smoke;
5. review evidence and obtain software plus equipment-owner approval;
6. remove `autospin_system/` as one explicit deletion change;
7. repeat all verification after deletion and record the commit/tag in this
   plan.

This plan does not authorize deleting the directory now. Under the repository
file-operation policy, the eventual directory removal must also be performed
through an approved, reviewable process rather than an unbounded recursive
deletion command.

## 8. Exit criteria

Retirement is complete when:

- production, diagnostics, tests, and deployment operate exclusively through
  the `src` architecture;
- no executable code imports `autospin_system`;
- no active document presents it as a behavioral authority;
- `AutoSpinmotorSystem` is the only named legacy behavior source;
- hardware acceptance evidence confirms the rewritten diagnostics and current
  production path; and
- the obsolete directory is absent from both the repository working tree and
  deployed application filesystem.

