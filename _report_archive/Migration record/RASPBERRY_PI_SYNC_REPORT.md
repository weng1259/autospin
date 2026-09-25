# Raspberry Pi Synchronization Report

## Status

Synchronization is **blocked before transfer** because SSH authentication to
the Raspberry Pi failed:

```text
pi@AutoSpin2026: Permission denied (publickey,password).
```

The host responded, so name resolution and network reachability are working.
No remote file was created, overwritten, or deleted.

## Paths

The source used for this deployment audit is the current AutoSpinMerge
workspace:

```text
D:\study\pythonlearning\study\history_version\AutoSpinMerge\autospin
```

The older path
`D:\study\pythonlearning\study\AutoSpinmotorSystem\autospin` was not read or
used.

Requested destination:

```text
pi@AutoSpin2026:/home/pi/autospin
```

The destination could not be resolved with `readlink -f` or checked for write
permission because authentication failed.

## Dry-run selection

The local white-list dry run selected:

| Path | Files | Purpose |
|---|---:|---|
| `src/` | 50 | Framework source, hardware backends, adapters, verified migrated drivers, and web application |
| `config/` | 3 | Coordinates, device definitions, and unified hardware configuration |
| `requirements.txt` | 1 | Python dependency pins |
| `tests/` | 35 | Optional Raspberry Pi smoke/unit tests |
| **Total** | **89** | **878,928 bytes** |

Each selected file was enumerated locally with its relative path, byte size,
and SHA-256 hash. A remote difference list could not be produced because the
destination was inaccessible.

## Transfer result

Files synchronized:

```text
0
```

The transfer was intentionally not attempted after authentication failed.
There was no fallback that could safely verify the destination or preserve
runtime files.

## Explicit exclusions

The following are not in the deployment white-list and were not transferred:

- `.venv/`
- `.git/`
- `__pycache__/`
- `.pytest_cache/`
- `.mypy_cache/`
- `*.pyc` and `*.pyo`
- `logs/`
- `data/`
- `temporary_logs/`
- runtime databases
- experiment results
- calibration data outside the explicit `config/` white-list
- user-generated files
- runtime caches

No `--delete`, recursive destination replacement, or whole-project copy is
permitted.

## Proposed safe transfer

After SSH authentication is restored, use a staging directory and copy only
the four white-listed paths:

```text
/home/pi/autospin/.deploy-staging/<deployment-id>/
    src/
    config/
    requirements.txt
    tests/
```

Required sequence:

1. Verify `whoami`, `hostname`, and `readlink -f /home/pi/autospin`.
2. Confirm `/home/pi/autospin` is writable by the deployment user.
3. Read the remote top-level listing and record protected runtime directories.
4. Compare local and remote relative paths and hashes.
5. Upload the selected paths to a new staging directory with `scp`.
6. Verify staged file count and hashes.
7. Copy staged `src/`, `config/`, `requirements.txt`, and optionally `tests/`
   into `/home/pi/autospin` without deleting unrelated destination files.
8. Remove the single staging directory only after its resolved path is
   verified and deployment succeeds.

This provides rsync-style white-list behavior on the current Windows machine,
where `ssh` and `scp` are installed but `rsync` is not.

## Local verification

Module import smoke test:

```text
module_imports=ok
registry_from_config=True
```

Local pytest result:

```text
374 passed, 1 skipped, 1 warning in 27.89s
```

The local repository's `.venv` is Linux-style (`.venv/bin`) and cannot run
under the Windows host. Tests therefore used the active Windows Python
environment. The virtual environment is correctly excluded from deployment.

Local `pip check` reported an unrelated global-environment conflict:

```text
scipy 1.14.1 requires numpy <2.3,>=1.23.5, but numpy 2.4.4 is installed.
```

Neither SciPy nor NumPy is directly listed in this project's
`requirements.txt`, so the Raspberry Pi environment must be checked
independently rather than assumed to share this conflict.

## Remote verification pending

The following commands have not run on the Raspberry Pi:

- Python version check
- package/module imports
- `DeviceRegistry.from_config()` availability
- `pip check`
- installed requirements comparison
- pytest smoke tests
- source/staging hash comparison

Suggested remote validation after transfer:

```bash
cd /home/pi/autospin
python3 -c "import src, src.config, src.webapp.registry"
python3 -c "from src.webapp.registry import DeviceRegistry; assert callable(DeviceRegistry.from_config)"
python3 -m pip check
python3 -m pytest -q tests/test_config_wiring.py tests/test_rs485_bus.py \
  tests/test_spincoater_backend.py tests/test_heater_backend.py \
  tests/test_pipette_backend.py tests/test_linearstage_backend.py \
  tests/test_relay_backend.py
```

These tests must run without connecting real hardware unless the Raspberry Pi
test configuration explicitly enables hardware access.

## Possible conflicts

1. The remote source and configuration versions are unknown because SSH
   authentication failed.
2. Remote `config/` may contain Raspberry Pi-specific serial ports or locally
   calibrated values. It must be diffed before overwrite.
3. `requirements.txt` may require package changes incompatible with the Pi's
   current Python version or architecture.
4. A currently running web service may retain old modules in memory until it is
   restarted.
5. Existing systemd service definitions and environment variables were not
   included in this deployment selection.
6. Tests may access hardware if remote environment flags are configured for
   real mode.
7. The source tree contains Gantry and Gripper modules; their presence in
   `src/` does not constitute approval for hardware validation or migration.

## Next hardware validation steps

After successful source synchronization and software smoke tests:

1. Keep all devices in mock mode and construct `DeviceRegistry.from_config()`.
2. Verify Raspberry Pi serial aliases and permissions without opening devices.
3. Validate RS485 ownership and ensure only one process controls each bus.
4. Validate SpinCoater, Heater, Pipette, LinearStage, and Relay independently.
5. Confirm emergency stop behavior before any sustained motion or heating.
6. Run low-risk read-only status checks before write commands.
7. Do not start Gantry or Gripper hardware validation as part of this sync.

## Required unblock

Configure a valid SSH key for `pi@AutoSpin2026` or provide an authenticated
session. Once authentication works, rerun destination verification and the
remote hash dry run before authorizing the staged transfer.
