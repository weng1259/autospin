# Gantry Runtime Smoke Import Fix Result

## Problem

`tools/gantry_runtime_smoke.py` imports:

```python
from src.webapp.registry import DeviceRegistry
```

Python initializes `src.webapp` before loading the `registry` submodule. The
package previously imported `app.py` eagerly, and `registry.py` imported
`poller.py`, whose top-level imports required FastAPI. As a result, the Gantry
smoke test could not start in a hardware-only environment without FastAPI.

## Fix

The dependency path was made lazy without duplicating or redesigning
`DeviceRegistry`:

- `src/webapp/__init__.py` still exports `DeviceRegistry` immediately.
- `create_app` is now loaded through module `__getattr__` only when requested.
- `src/webapp/poller.py` imports FastAPI types only during type checking.
- `Request` and `StreamingResponse` are imported at runtime only when Web status
  routes are registered.

The smoke test can therefore import the existing Registry and call
`DeviceRegistry.from_config()` without importing FastAPI. Normal Web usage
through `from src.webapp import create_app` remains compatible and loads
FastAPI when the Web application is actually requested.

No package was installed.

## Hardware Safety

The existing hardware gate remains unchanged:

```bash
python tools/gantry_runtime_smoke.py \
  --execute-hardware \
  --confirm MOVE_GANTRY_TO_X-20_Y-20_Z-10
```

Without both arguments, the script only loads configuration, constructs the
Registry, verifies Gantry availability, and prints the dry-run message. It does
not connect, home, or move hardware.

## Regression Test

`tests/test_gantry_runtime_smoke.py` now starts a fresh Python subprocess with
an import finder that deliberately raises `ModuleNotFoundError` for `fastapi`
and all `fastapi.*` modules. In that environment it runs:

```text
python tools/gantry_runtime_smoke.py
```

The test verifies successful exit and the no-serial-connection dry-run output.
This validates the missing-dependency behavior even on a development machine
where FastAPI happens to be installed.

## Test Results

Focused smoke, Web import, poller, and configuration tests:

```text
24 passed
```

Full test invocation:

```text
394 passed, 1 skipped, 1 failed
```

The sole failure is the pre-existing timing boundary assertion in
`test_get_status_returns_fresh_snapshot_during_lock_hold`, which observed
`99.999755859375 ms` against a `>= 100.0 ms` assertion.

Full regression excluding that known unstable timing-boundary test:

```text
394 passed, 1 skipped, 1 deselected
```

The remaining warning is the existing Starlette `TestClient`/`httpx`
deprecation warning.

## Protected Files

This fix did not modify:

- `src/hardware/gantry_backend.py`
- `src/hardware/drivers/gantry/grbl_controller.py`
- Any hardware driver

Real hardware execution was not performed.

