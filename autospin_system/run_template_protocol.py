"""Command-line entry point for fixed-template protocol execution.

This script demonstrates the complete non-LLM path:
template -> typed protocol -> validation -> simulation -> compiled tasks ->
Maestro execution. By default it only performs a dry run. With --execute it
uses mock hardware unless --real is explicitly provided and confirmed.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    # Allow this file to be run directly from the workspace root while still
    # importing AutoSpinmotorSystem as a package.
    sys.path.insert(0, str(ROOT_DIR))

from AutoSpinmotorSystem.maestro import Maestro
from AutoSpinmotorSystem.protocol import (
    ExperimentProtocol,
    compile_protocol,
    load_template,
    simulate_protocol,
    validate_protocol,
)


def main() -> int:
    """Run the selected template through the protocol pipeline."""

    parser = argparse.ArgumentParser(
        description="Run a fixed template protocol through validation, simulation, and Maestro."
    )
    parser.add_argument(
        "--template",
        default="perovskite_basic",
        help="Template name: perovskite_basic or spin_only_test",
    )
    parser.add_argument("--execute", action="store_true", help="Execute compiled tasks")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use real hardware. Default is mock mode.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Pydantic turns the raw template dictionary into typed operation objects.
    protocol = ExperimentProtocol.model_validate(load_template(args.template))
    validation = validate_protocol(protocol)

    print("=== Protocol JSON ===")
    print(json.dumps(protocol.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False))
    print("\n=== Validation ===")
    print("PASS" if validation.valid else "FAIL")
    for warning in validation.warnings:
        print(f"WARNING: {warning}")
    for error in validation.errors:
        print(f"ERROR: {error}")

    print("\n=== Simulation ===")
    for line in simulate_protocol(protocol):
        print(line)

    # Compilation happens after simulation so the operator can see both the
    # protocol-level view and the Maestro-level task representation.
    tasks = compile_protocol(protocol)
    print("\n=== Compiled Tasks ===")
    print(json.dumps(tasks, indent=2, ensure_ascii=False))

    if not validation.valid:
        return 2

    if not args.execute:
        print("\nDry run only. Add --execute to send tasks to Maestro(mock=True by default).")
        return 0

    if args.real:
        # Real hardware requires a deliberate confirmation string. This keeps
        # accidental "--real" invocations from moving motors or heating plates.
        answer = input("This will use real hardware. Type EXECUTE to continue: ")
        if answer.strip() != "EXECUTE":
            print("Cancelled.")
            return 1

    maestro = Maestro(use_gantry=True, mock=not args.real)
    maestro.start_experiment()
    try:
        for task in tasks:
            maestro.run_task(task)
    finally:
        maestro.shutdown()

    print("\nExecution complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
