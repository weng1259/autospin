# AGENTS.md

## Project Context

This repo is the intelligent spin-coater project (`智能旋涂仪`): a GH40 XYZ gantry platform evolving into an Agent-first laboratory instrument platform.

The spin-coater is the first proof of concept. The long-term product direction is natural-language control of lab instruments through an LLM Agent calling typed L3 APIs.

Core priority order:

1. Hardware and low-level stability
2. L3 API quality
3. UI

UI is mainly for emergency control, monitoring, and PM acceptance. The Agent/API path is the primary product surface.

## Start Here

For a new session, read these first:

1. `docs/decision-log/architecture-roadmap.md`
2. `docs/decision-log/ADR-003-agent-first-vision.md`
3. `docs/decision-log/ADR-004-l3-api-design-principles.md`
4. `docs/decision-log/ADR-005-claude-agent-sdk-for-l3.md`
5. `docs/decision-log/migration-checklist.md`

Then inspect current git status before editing. The repo may contain in-progress user changes.

## Architecture

Current intended stack:

- L1 firmware: grbl-Mega-5X on Arduino Mega 2560
- L2 emergency/manual layer: selected only as needed; cncjs is historical/emergency, not the product center
- L3 product core: Python orchestrator in `src/`
- L5 UI: Streamlit emergency dashboard and later experiment/Agent panels
- L6 Agent: Claude Agent SDK / tool-use over L3 APIs

L3 is the product body. All hardware modules should become typed backend APIs plus Agent-safe tools.

## L3 API Rules

Follow ADR-004 for all public backend APIs:

- Typed inputs and outputs, preferably Pydantic models or explicit dataclasses.
- Structured `L3Error` subclasses with stable error codes, human message, agent message, recoverability, and suggested action.
- Idempotency for physical actions. Use `idempotency_key` where retries could duplicate effects.
- State query methods must be read-only, fast, and safe during ongoing operations.
- Safety boundaries must be enforced in backend code, not only in UI or Agent hooks.
- Every operation should be observable through runlog/events.
- Destructive or irreversible workflows should support `dry_run` where practical.

Hooks and Agent tool permissions are optimizations, not the safety boundary. Backend methods must remain safe if called directly.

## Agent Tool Safety

Allowed Agent tools should be semantic device-level tools, for example:

- `gantry_get_status`
- `gantry_home`
- `gantry_move_to`
- `gantry_halt`
- `gantry_recover_from_alarm`
- `gripper_open`
- `gripper_close`
- `gripper_get_state`
- future `spincoater_run_recipe`

Never expose these to the Agent allowlist:

- raw gcode senders
- raw relay channel on/off tools
- direct `/dev/cu.*` writes
- tools that modify `constants.yaml` safety boundaries

Raw hardware escape hatches may exist for human-confirmed CLI/debug use, but not for autonomous Agent use.

## Hardware Safety Notes

Important current facts:

- Gantry: GH40-D75-X300S-Y300S-Z100S-57.
- Motor drivers: 2HSS57-C, 51200 pulses/rev, 682.67 steps/mm.
- Controller: Arduino Mega 2560, CH340 variant.
- Sensors: DS-ES61 NPN light-on.
- grbl `$5` must be `1` for sensor inversion.
- grbl `$3=6` means Y/Z direction inverted in grbl convention.
- grbl `$23` is a homing direction invert mask, not a generic direction mask.
- Z movement must release the Z brake through DSTUR-T80 CH2 before motion and lock it after motion.
- `constants.yaml` soft limits must be inset from grbl physical travel by at least `$27` pull-off distance. Current rule: L3 boundaries are not equal to `$130/$131/$132`.

Before changing hardware parameters, read the relevant manual first and record the source in project docs. Do not use trial-and-error when the parameter is documented.

## Firmware Notes

grbl-Mega-5X settings have historically been hardcoded into `firmware/grbl_spike/grbl-Mega-5X/grbl/defaults.h` because Mega EEPROM proved unreliable.

Do not treat runtime `$xxx=yyy` commands as persistent configuration. For durable parameter changes, update firmware defaults, compile, upload, and document the change.

Before uploading firmware, ensure any serial users such as cncjs, dashboards, or scripts are stopped.

## Development Workflow

Prefer vertical slices that the PM can verify in browser or CLI:

- A slice should show visible behavior or a concrete script result.
- Do not spend multiple days only building invisible internal layers unless the task is explicitly infrastructure-only.
- Each slice should leave a lightweight verification note in `docs/verification/` when appropriate.

Verification docs are not tutorials. During active phases, keep verification docs short and evidence-focused. Write full tutorials only after APIs stabilize.

## Streamlit Note

If changing `tools/ui/emergency_dashboard.py`, Streamlit rerun may be enough.

If changing `src/*` or backend classes, kill and restart the Streamlit process. Browser refresh alone can keep stale imported modules or stale `session_state` backend instances.

## Common Commands

Use the project virtualenv under `tools/spikes/.venv/` when available.

Typical checks:

```bash
tools/spikes/.venv/bin/python -m pytest tests/ -v
tools/spikes/.venv/bin/mypy --strict src/
tools/spikes/.venv/bin/python -m src.schema_export --out docs/api-v1.json
```

Hardware and Agent smoke scripts live under `tools/` and `tools/spikes/`. Read the relevant verification document before rerunning hardware scripts.

## Documentation Hygiene

When project phase status changes, update the source-of-truth docs instead of relying on memory:

- `docs/decision-log/migration-checklist.md`
- `docs/decision-log/architecture-roadmap.md`
- relevant ADRs
- relevant `docs/verification/*.md`

Avoid copying stale status from old README sections or old memory files without checking current docs and git state.
