"""Generate multi-round low-level JSON recipes for example_experiment.py."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from autospin_system.protocol.sequence_generator import (
    DEFAULT_LAYOUT,
    DEFAULT_REPLACEMENT_INDEXES,
    ReplacementIndexes,
    SequenceLayout,
    generate_multi_round_recipe,
)


DEFAULT_INPUT = Path(__file__).resolve().parent / "examples" / "one_round_full_spin_hotplate_cycle.json"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent
    / "examples"
    / "one_round_full_spin_hotplate_cycle_18_rounds.json"
)

# Direct-run defaults. These switches are useful when running this file from an
# IDE without typing long command-line arguments.
PROMPT_FOR_SETTINGS_ON_START = True
AUTO_RUN_AFTER_GENERATE = False
AUTO_RUN_MOCK = True # 真实硬件运行把True 改为 False
AUTO_RUN_TIME_SCALE = 0.0
AUTO_RUN_USE_GANTRY = True
AUTO_RUN_USE_Z2 = False
AUTO_RUN_KEYBOARD_STOP = False
AUTO_RUN_LOG_LEVEL = "INFO"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a multi-round operation/params recipe from a single-round template."
    )
    parser.add_argument("--rounds", type=int, default=18, help="Number of rounds to generate")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Single-round recipe JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output recipe JSON")
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Override the generated experiment name",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for generation settings before writing JSON",
    )
    parser.add_argument(
        "--accept-defaults",
        action="store_true",
        help="Use displayed defaults in interactive mode without prompting for each value",
    )
    parser.add_argument(
        "--no-start-prompt",
        action="store_true",
        help="Do not ask whether to edit generation settings; use current/default values.",
    )
    parser.add_argument(
        "--run-after-generate",
        action="store_true",
        default=AUTO_RUN_AFTER_GENERATE,
        help="Run the generated recipe with example_experiment.py logic after writing JSON.",
    )
    parser.add_argument(
        "--no-run-after-generate",
        action="store_true",
        help="Do not run the generated recipe, even if AUTO_RUN_AFTER_GENERATE is true.",
    )
    parser.add_argument(
        "--real-run",
        action="store_true",
        help="Use real hardware for the post-generation run. Default is mock mode.",
    )
    parser.add_argument(
        "--run-time-scale",
        type=float,
        default=AUTO_RUN_TIME_SCALE,
        help="Time scale for the post-generation run; 0 is a fast software pass.",
    )
    parser.add_argument(
        "--run-no-gantry",
        action="store_true",
        default=not AUTO_RUN_USE_GANTRY,
        help="Skip gantry initialization during the post-generation run.",
    )
    parser.add_argument(
        "--run-use-z2",
        action="store_true",
        default=AUTO_RUN_USE_Z2,
        help="Enable Z2/A-axis initialization during the post-generation run.",
    )
    parser.add_argument(
        "--run-keyboard-stop",
        action="store_true",
        default=AUTO_RUN_KEYBOARD_STOP,
        help="Enable Esc/q keyboard emergency stop during the post-generation run.",
    )
    parser.add_argument(
        "--run-log-level",
        default=AUTO_RUN_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level for the post-generation run.",
    )
    return parser.parse_args()


def load_recipe(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        recipe = json.load(file)
    if not isinstance(recipe, dict):
        raise ValueError("Recipe JSON root must be an object")
    return recipe


def main() -> int:
    args = parse_args()
    should_prompt = PROMPT_FOR_SETTINGS_ON_START and not args.no_start_prompt
    if args.interactive or (should_prompt and ask_yes_no("Modify generation settings?", default=False)):
        args, layout, indexes = collect_interactive_options(args)
    else:
        layout = DEFAULT_LAYOUT
        indexes = DEFAULT_REPLACEMENT_INDEXES

    recipe = load_recipe(args.input)
    generated = generate_multi_round_recipe(
        recipe,
        rounds=args.rounds,
        experiment_name=args.experiment_name,
        layout=layout,
        indexes=indexes,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(generated, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print(f"Generated {args.rounds} rounds")
    print(f"Operations per round: {generated['operations_per_round']}")
    print(f"Total operations: {len(generated['operations'])}")
    print(f"Output: {args.output}")

    if args.run_after_generate and not args.no_run_after_generate:
        run_generated_recipe(args)
    return 0


def ask_yes_no(prompt: str, *, default: bool) -> bool:
    """Ask a yes/no question with a default for direct script runs."""

    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true", "t"}


def run_generated_recipe(args: argparse.Namespace) -> None:
    """Run the generated low-level recipe using the example experiment runner."""

    from autospin_system.example_experiment import (
        EmergencyStopRequested,
        ExperimentRunner,
        KeyboardEmergencyStopMonitor,
        load_recipe as load_experiment_recipe,
    )
    from autospin_system.maestro import Maestro

    real_requested = args.real_run or not AUTO_RUN_MOCK
    mock = not real_requested
    if real_requested:
        answer = input("This will use real hardware. Type EXECUTE to continue: ")
        if answer.strip() != "EXECUTE":
            print("Post-generation run cancelled.")
            return

    logging.basicConfig(
        level=getattr(logging, args.run_log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    print(
        "Running generated recipe "
        f"({'mock' if mock else 'real'} mode, time_scale={args.run_time_scale:g})"
    )

    recipe = load_experiment_recipe(args.output)
    maestro = Maestro(
        use_gantry=not args.run_no_gantry,
        mock=mock,
        use_z2=args.run_use_z2,
        use_standalone_relay=False,
    )
    runner = ExperimentRunner(maestro, time_scale=args.run_time_scale)

    try:
        with KeyboardEmergencyStopMonitor(runner, enabled=args.run_keyboard_stop):
            runner.run(recipe)
    except EmergencyStopRequested as exc:
        logging.getLogger("AutoSpinmotorSystem.ExperimentRunner").critical("%s", exc)
    finally:
        maestro.shutdown()

    print("Post-generation run complete.")


def collect_interactive_options(
    args: argparse.Namespace,
) -> tuple[argparse.Namespace, SequenceLayout, ReplacementIndexes]:
    """Show all editable fields first, then read one JSON override block."""

    print_interactive_form(args)
    if args.accept_defaults:
        return args, DEFAULT_LAYOUT, DEFAULT_REPLACEMENT_INDEXES

    overrides = read_overrides_block()
    args.rounds = int(overrides.get("rounds", args.rounds))
    args.input = Path(overrides.get("input", str(args.input)))
    args.output = Path(overrides.get("output", str(args.output)))
    args.experiment_name = overrides.get("experiment_name", args.experiment_name)

    layout = SequenceLayout(
        sample_rows=parse_rows_override(overrides.get("sample_rows", DEFAULT_LAYOUT.sample_rows)),
        sample_base_x=float(overrides.get("sample_base_x", DEFAULT_LAYOUT.sample_base_x)),
        sample_base_y=float(overrides.get("sample_base_y", DEFAULT_LAYOUT.sample_base_y)),
        sample_dx=float(overrides.get("sample_dx", DEFAULT_LAYOUT.sample_dx)),
        sample_dy=float(overrides.get("sample_dy", DEFAULT_LAYOUT.sample_dy)),
        sample_z_pick=float(overrides.get("sample_z_pick", DEFAULT_LAYOUT.sample_z_pick)),
        sample_z_lift=float(overrides.get("sample_z_lift", DEFAULT_LAYOUT.sample_z_lift)),
        liquid_base_x=float(overrides.get("liquid_base_x", DEFAULT_LAYOUT.liquid_base_x)),
        liquid_base_y=float(overrides.get("liquid_base_y", DEFAULT_LAYOUT.liquid_base_y)),
        liquid_dx_group=float(overrides.get("liquid_dx_group", DEFAULT_LAYOUT.liquid_dx_group)),
        liquid_dy_inner=float(overrides.get("liquid_dy_inner", DEFAULT_LAYOUT.liquid_dy_inner)),
        liquid_z=float(overrides.get("liquid_z", DEFAULT_LAYOUT.liquid_z)),
    )
    indexes = ReplacementIndexes(
        liquid_pick=int(overrides.get("liquid_pick_index", DEFAULT_REPLACEMENT_INDEXES.liquid_pick)),
        antisolvent_pick=int(
            overrides.get(
                "antisolvent_pick_index",
                overrides.get("liquid_return_index", DEFAULT_REPLACEMENT_INDEXES.antisolvent_pick),
            )
        ),
        sample_pick=int(overrides.get("sample_pick_index", DEFAULT_REPLACEMENT_INDEXES.sample_pick)),
        sample_return=int(
            overrides.get(
                "sample_return_index",
                overrides.get("sample_lift_index", DEFAULT_REPLACEMENT_INDEXES.sample_return),
            )
        ),
    )
    return args, layout, indexes


def print_interactive_form(args: argparse.Namespace) -> None:
    """Print the full form before asking for input."""

    print("\n=== Multi-round sequence generator settings ===")
    print("Paste one JSON object with only the fields you want to change.")
    print("Missing fields use defaults. Submit an empty line to use all defaults.")
    print("For multi-line JSON, finish with a line containing only END.\n")
    print("Field meanings:")
    print("  rounds: number of rounds to generate; default 18, max 18 for current sample layout.")
    print("  input: source single-round operation/params JSON file.")
    print("  output: generated machine-executable multi-round JSON file.")
    print("  experiment_name: optional output experiment name; null/omitted uses source name.")
    print("  sample_rows: sample slot columns per row; list form or string like '0,1;0,2'.")
    print("  sample_base_x/sample_base_y: coordinate of round 1 sample position.")
    print("  sample_dx/sample_dy: positive spacing values subtracted along X/Y for samples.")
    print("  sample_z_pick: Z used when picking a sample/glass.")
    print("  sample_z_lift: Z used after lifting the picked sample/glass.")
    print("  liquid_base_x/liquid_base_y: first liquid/cap coordinate.")
    print("  liquid_dx_group: positive X spacing subtracted between liquid groups.")
    print("  liquid_dy_inner: positive Y spacing added within each liquid group.")
    print("  liquid_z: Z used at liquid/cap position.")
    print("  liquid_pick_index/antisolvent_pick_index: zero-based move indexes patched with liquid position.")
    print("  sample_pick_index/sample_return_index: zero-based move indexes patched with sample position.\n")
    print("Defaults:")
    print(json.dumps(defaults_dict(args), ensure_ascii=False, indent=2))
    print("\nExample override:")
    print(
        json.dumps(
            {
                "rounds": 12,
                "output": "AutoSpinmotorSystem/examples/my_12_rounds.json",
                "sample_dx": 37.25,
                "sample_rows": "0,1,2,3,4;0,1,3,4;0,1,3,4;0,1,2,3,4",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("")


def defaults_dict(args: argparse.Namespace) -> dict:
    """Return the complete default form shown to the operator."""

    return {
        "rounds": args.rounds,
        "input": str(args.input),
        "output": str(args.output),
        "experiment_name": args.experiment_name,
        "sample_rows": DEFAULT_LAYOUT.sample_rows,
        "sample_base_x": DEFAULT_LAYOUT.sample_base_x,
        "sample_base_y": DEFAULT_LAYOUT.sample_base_y,
        "sample_dx": DEFAULT_LAYOUT.sample_dx,
        "sample_dy": DEFAULT_LAYOUT.sample_dy,
        "sample_z_pick": DEFAULT_LAYOUT.sample_z_pick,
        "sample_z_lift": DEFAULT_LAYOUT.sample_z_lift,
        "liquid_base_x": DEFAULT_LAYOUT.liquid_base_x,
        "liquid_base_y": DEFAULT_LAYOUT.liquid_base_y,
        "liquid_dx_group": DEFAULT_LAYOUT.liquid_dx_group,
        "liquid_dy_inner": DEFAULT_LAYOUT.liquid_dy_inner,
        "liquid_z": DEFAULT_LAYOUT.liquid_z,
        "liquid_pick_index": DEFAULT_REPLACEMENT_INDEXES.liquid_pick,
        "antisolvent_pick_index": DEFAULT_REPLACEMENT_INDEXES.antisolvent_pick,
        "sample_pick_index": DEFAULT_REPLACEMENT_INDEXES.sample_pick,
        "sample_return_index": DEFAULT_REPLACEMENT_INDEXES.sample_return,
    }


def read_overrides_block() -> dict:
    """Read one JSON override object from stdin."""

    print("Paste override JSON now. Empty line = all defaults. END = finish multi-line input.")
    first_line = input("> ")
    if not first_line.strip():
        return {}

    lines = [first_line]
    if first_line.strip() != "END":
        while True:
            line = input()
            if line.strip() == "END":
                break
            lines.append(line)

    text = "\n".join(lines).strip()
    if not text:
        return {}
    try:
        overrides = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON override block: {exc}") from exc
    if not isinstance(overrides, dict):
        raise ValueError("Override block must be a JSON object")
    return overrides


def parse_rows_override(value: str | list[list[int]]) -> list[list[int]]:
    """Accept sample_rows either as a compact string or as a JSON list."""

    if isinstance(value, str):
        return parse_rows(value)
    if not isinstance(value, list) or not value:
        raise ValueError("sample_rows must be a non-empty list or compact string")
    rows: list[list[int]] = []
    for row in value:
        if not isinstance(row, list) or not row:
            raise ValueError("each sample_rows row must be a non-empty list")
        rows.append([int(col) for col in row])
    return rows


def format_rows(rows: list[list[int]]) -> str:
    return ";".join(",".join(str(col) for col in row) for row in rows)


def parse_rows(value: str) -> list[list[int]]:
    rows: list[list[int]] = []
    for row_text in value.split(";"):
        row_text = row_text.strip()
        if not row_text:
            continue
        try:
            rows.append([int(part.strip()) for part in row_text.split(",") if part.strip()])
        except ValueError as exc:
            raise ValueError("sample_rows must look like 0,1,2;0,1,3") from exc
    if not rows or any(not row for row in rows):
        raise ValueError("sample_rows must contain at least one non-empty row")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
