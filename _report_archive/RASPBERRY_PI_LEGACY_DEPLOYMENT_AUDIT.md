# Raspberry Pi Legacy Deployment Path Audit

Date: 2026-08-05  
Audit target: production Raspberry Pi deployment  
Result: **repository audit passed for `autospin_system`; remote confirmation blocked**

## 1. Executive conclusion

The maintained repository production entry point and `src` runtime contain no
executable import of the obsolete intermediate package `autospin_system`.
Phase 2 local removal rehearsal also proved that tests, Web startup,
`DeviceRegistry.from_config()`, protocol generation, and dry-run experiments
operate with that directory absent.

However, this audit could not connect to the production Raspberry Pi. Therefore
the installed systemd unit, running process environment, Python installation,
installed packages, and actual `/home/pi/autospin` contents remain unverified.
Production deployment is **not yet confirmed legacy-free** until the remote
checks in this report are executed on the Pi.

No service, file, package, or hardware state was changed by this audit.

## 2. Connectivity attempts

Repository documentation contains these candidate targets:

- alias `AutoSpin2026`;
- LAN address `192.168.50.2`;
- older LAN address `192.168.1.207`;
- Tailscale address `100.90.0.36`.

Read-only, batch-mode SSH attempts produced:

| Target | Result |
|---|---|
| `pi@AutoSpin2026` | hostname could not be resolved |
| `pi@192.168.50.2` | TCP/SSH connection timed out |
| `pi@192.168.1.207` | TCP/SSH connection timed out |
| `pi@100.90.0.36` | TCP/SSH connection timed out |

`ssh -G AutoSpin2026` showed no configured host override; it resolved to the
literal hostname and the current execution account was not configured as the
documented `pi` deployment account.

The earlier `Migration record/RASPBERRY_PI_SYNC_REPORT.md` independently
records failed authentication and states that existing systemd definitions and
environment variables were not included in the deployment selection.

## 3. Systemd service audit

### Repository evidence

No versioned `.service`, `.timer`, or `.socket` unit exists in the repository:

```text
SYSTEMD_UNIT_COUNT=0
```

The maintained documented manual startup command is:

```bash
cd /home/pi/autospin
python3 tools/run_webserver.py --host 127.0.0.1 --port 8800
```

This starts `src.webapp` through one `DeviceRegistry`. It does not import
`autospin_system`.

### Remote evidence required

The following were requested but could not be collected:

- active/autostart units related to AutoSpin;
- unit `ExecStart`;
- `WorkingDirectory`;
- `Environment`, `EnvironmentFile`, and `PYTHONPATH`;
- the running process command line and environment.

Run these read-only commands on the Pi after obtaining the real unit name:

```bash
systemctl list-units --type=service --all --no-pager | grep -i autospin
systemctl list-unit-files --type=service --no-pager | grep -i autospin
systemctl cat ACTUAL_UNIT_NAME
systemctl show ACTUAL_UNIT_NAME \
  -p FragmentPath -p ExecStart -p WorkingDirectory \
  -p Environment -p EnvironmentFiles -p User -p Group
systemctl status ACTUAL_UNIT_NAME --no-pager
```

For the active PID:

```bash
pid="$(systemctl show ACTUAL_UNIT_NAME -p MainPID --value)"
tr '\0' ' ' <"/proc/$pid/cmdline"
tr '\0' '\n' <"/proc/$pid/environ" | grep -E '^(PYTHONPATH|PATH|VIRTUAL_ENV)='
readlink -f "/proc/$pid/cwd"
readlink -f "/proc/$pid/exe"
```

Pass criteria:

- `ExecStart` invokes `/home/pi/autospin/tools/run_webserver.py` or an equivalent
  maintained `src.webapp` entry;
- `WorkingDirectory=/home/pi/autospin` or the entry script resolves that root;
- neither command line nor environment contains `autospin_system`;
- `PYTHONPATH` does not add the obsolete directory or a stale deployment tree.

## 4. Startup-script audit

### Maintained startup path

`tools/run_webserver.py`:

1. resolves its repository root;
2. imports `DeviceRegistry` and `create_app` from `src.webapp`;
3. creates `DeviceRegistry.from_config()` in production mode;
4. starts Uvicorn with the maintained FastAPI application.

This entry was started successfully during the removal rehearsal with
`autospin_system/` absent and returned HTTP 200 from `/api/health`.

### Obsolete intermediate package search

No executable import of `autospin_system` exists in maintained `src`, tools,
tests, config, or deployment scripts. One ignored dormant backup remains:

```text
tools/ui/emergency_dashboard.py.bak.20260629
```

It contains old imports but is not a Python module, startup target, pytest
target, or documented production command. It should not be copied to the Pi
and should be archived/removed before final cleanup.

### `AutoSpinmotorSystem` paths

`AutoSpinmotorSystem` is the authoritative legacy behavior source, not the
obsolete intermediate package. Nevertheless, two maintained `src` routes still
resolve sibling legacy-project data paths at runtime:

- `src/webapp/routes_configuration.py` resolves the sibling
  `AutoSpinmotorSystem` root for baseline configuration handling;
- `src/webapp/routes_experiments.py` resolves
  `AutoSpinmotorSystem/examples` for recipe lookup.

These are not `autospin_system` imports and do not change the conclusion about
the obsolete package. They do mean that the current application is `src`-based
but is not yet fully self-contained if those endpoints are used. On the Pi,
verify whether `/home/pi/AutoSpinmotorSystem` (or another sibling path) is
present and intentionally supported. Do not remove it merely as part of the
`autospin_system` retirement.

Several older runbook passages still instruct operators to launch low-level
recipes from `~/AutoSpinmotorSystem`. Those commands are separate from the new
Web entry and must not be mistaken for `autospin_system` dependencies.

## 5. Python environment audit

### Repository/local evidence

- The tested interpreter was `D:\python\python3.11.0\python.exe` on the audit
  workstation, not the Raspberry Pi interpreter.
- `tools/run_webserver.py` places its repository root on `sys.path`.
- `pytest.ini` uses `pythonpath = .` and collects only `tests/`.
- Local construction of the configured registry loaded no
  `autospin_system` module.

### Remote evidence required

The following Pi facts remain unknown:

- `python3` executable and version;
- virtual environment path;
- `pip` bound to that interpreter;
- installed distributions and dependency consistency;
- runtime `sys.path`;
- whether an obsolete package was installed into site-packages.

Collect them with the same interpreter used by systemd:

```bash
cd /home/pi/autospin
python3 -c 'import sys; print(sys.executable); print(sys.version); print(*sys.path, sep="\n")'
python3 -m pip --version
python3 -m pip check
python3 -m pip list --format=freeze
python3 - <<'PY'
import importlib.util
import sys
print("executable:", sys.executable)
print("autospin_system spec:", importlib.util.find_spec("autospin_system"))
print("src spec:", importlib.util.find_spec("src"))
PY
```

Pass criteria:

- interpreter and pip refer to the intended virtual environment or approved
  system Python;
- `src` resolves under `/home/pi/autospin`;
- `autospin_system` does not resolve from site-packages or another directory;
- no `sys.path` item points to a stale project checkout.

## 6. Deployment-directory audit

The expected application directory is `/home/pi/autospin`, but its live content
could not be read.

Required read-only search:

```bash
readlink -f /home/pi/autospin
find /home/pi/autospin -type d -name autospin_system -print
grep -RIn --exclude='*.pyc' --exclude-dir='__pycache__' \
  --exclude-dir='.git' --exclude-dir='.venv' \
  'autospin_system' /home/pi/autospin
grep -RIn --exclude='*.pyc' --exclude-dir='__pycache__' \
  --exclude-dir='.git' --exclude-dir='.venv' \
  'AutoSpinmotorSystem' /home/pi/autospin
find /home/pi/autospin -type f \( -name '*.service' -o -name '*.sh' -o -name '*.env' \) -print
```

Also search common external deployment locations without crossing unrelated
data mounts:

```bash
find /home/pi /etc/systemd/system /etc/default /etc/environment.d \
  -maxdepth 5 -iname '*autospin*' -print 2>/dev/null
grep -RIn 'autospin_system' \
  /etc/systemd/system /etc/default /etc/environment /etc/environment.d \
  2>/dev/null
```

Pass criteria:

- no deployed executable, service, environment file, or startup script names or
  imports `autospin_system`;
- any retained report text is clearly non-executable;
- the dormant `.bak` dashboard is absent from the deployment;
- any `AutoSpinmotorSystem` path is documented and limited to intentionally
  supported legacy data/recipe behavior.

## 7. Udev deployment documentation

`deploy/udev/README.md` passes repository inspection:

- it uses `config/hardware.yaml` and `config/devices.yaml`;
- it points to maintained `tests/` and the registry-backed read-only smoke;
- it contains no `autospin_system` path;
- udev rules only create stable serial aliases and do not choose a Python
  application package.

The installed `/etc/udev/rules.d` content was not remotely inspected. Rules are
unlikely to import Python, but their comments/scripts should still be included
in the Pi search.

## 8. Current production entry assessment

| Question | Result |
|---|---|
| Repository production entry is `src`-based | Confirmed |
| Maintained code imports `autospin_system` | No |
| Local project works without `autospin_system/` | Confirmed by removal rehearsal |
| Installed Pi systemd unit uses current entry | Not verified |
| Installed Pi `PYTHONPATH` excludes old package | Not verified |
| Installed Pi site-packages exclude old package | Not verified |
| `/home/pi/autospin` contains no obsolete directory | Not verified |
| Application is completely independent of `AutoSpinmotorSystem` data paths | No; two maintained routes still resolve legacy data directories |

## 9. Failures and blockers

### Audit failures

- All documented SSH targets timed out or failed name resolution.
- No versioned systemd unit exists to substitute for live-unit inspection.
- Pi Python/pip/sys.path and deployed filesystem could not be queried.

### Required unblock

Provide network reachability and non-interactive SSH authentication to the
actual Pi hostname/address, or run the read-only commands above on the Pi and
return their complete output. The audit should then be amended with observed
unit names, paths, interpreter, environment, package resolution, and search
results.

## 10. Final status

**Repository status:** pass — the maintained production entry is `src`-based
and has no executable dependency on `autospin_system`.

**Raspberry Pi deployment status:** unverified/blocked — production cannot yet
be certified free of the obsolete path because the live host was unreachable.

Do not delete the deployed legacy directory solely on the basis of this local
audit. Final approval requires live systemd, process environment, Python import,
and deployment-directory evidence.

