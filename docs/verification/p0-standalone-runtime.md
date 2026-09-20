# P0 standalone runtime verification

Date: 2026-09-20

## Scope

- Moved maintained recipe generation and loading to project-local `recipes/`.
- Added explicit `recipes_path` injection and `AUTOSPIN_RECIPES_DIR` override.
- Added the minimal Raspberry Pi runtime manifest and non-destructive builder.
- Preserved top-level development assets; no legacy, firmware, or hardware
  directory was deleted.

## Evidence

Focused regression:

```text
python -m pytest -q tests/test_webapp_configuration.py tests/test_standalone_runtime_package.py
14 passed
```

Full maintained regression:

```text
python -m pytest -q
496 passed, 1 skipped
```

The package test builds a disposable runtime from `deploy/runtime-package.json`,
asserts that top-level `autospin_system`, `firmware`, `hardware`,
`migration_snapshot`, and `tests` are absent, then imports and constructs the
Web application with the disposable package as its working directory. It also
asserts that the resolved recipe directory is inside that package.

## Runtime boundary

Top-level `firmware/` and `hardware/` are excluded from the Pi runtime package
but retained as development and maintenance records. `src/hardware/` remains in
the package because it contains the active device implementations.
