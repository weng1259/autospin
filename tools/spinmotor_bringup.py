#!/usr/bin/env python3
"""Conservative DBLS400 bring-up through DeviceRegistry and current backend."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.webapp import DeviceRegistry  # noqa: E402

MAX_BRINGUP_RPM = 300.0
REAL_CONFIRMATION = "I_HAVE_CLEARED_AND_GUARDED_THE_SPINCOATER"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read spin status or perform a guarded low-speed bring-up"
    )
    parser.add_argument("--rpm", type=float, default=None)
    parser.add_argument("--hold-s", type=float, default=3.0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="permit a real start; otherwise --rpm produces a backend dry-run",
    )
    parser.add_argument(
        "--confirm-read-only-hardware",
        action="store_true",
        help="permit opening the configured port for a status-only check",
    )
    parser.add_argument(
        "--confirm",
        default=os.environ.get("SPIN_CONFIRM", ""),
        help=f"required for --apply: {REAL_CONFIRMATION}",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.hold_s < 0:
        parser.error("--hold-s must be non-negative")
    if args.rpm is not None and not 0 < args.rpm <= MAX_BRINGUP_RPM:
        parser.error(f"--rpm must be in (0, {MAX_BRINGUP_RPM:g}]")
    if args.apply and args.rpm is None:
        parser.error("--apply requires --rpm")
    if args.apply and args.confirm != REAL_CONFIRMATION:
        parser.error(
            "real spin requires --confirm " + REAL_CONFIRMATION
        )
    if args.rpm is None and not args.confirm_read_only_hardware:
        parser.error(
            "status-only hardware access requires --confirm-read-only-hardware"
        )

    registry = DeviceRegistry.from_config()
    spin = registry.spincoater
    if spin is None:
        print("Configured DeviceRegistry has no spincoater", file=sys.stderr)
        registry.shutdown()
        return 1

    real_started = False
    try:
        if args.apply or args.rpm is None:
            spin.connect()
        print(spin.status().model_dump_json(indent=2))
        if args.rpm is None:
            print("Status-only check complete; motor start was not requested.")
            return 0
        result = spin.start(
            args.rpm,
            idempotency_key=f"spin-bringup-start-{uuid4()}",
            dry_run=not args.apply,
        )
        print(result.model_dump_json(indent=2))
        if not args.apply:
            print("Dry-run complete. No motor command was sent.")
            return 0
        real_started = True
        time.sleep(args.hold_s)
        return 0
    finally:
        try:
            if real_started:
                stopped = spin.stop(
                    use_brake=True,
                    idempotency_key=f"spin-bringup-stop-{uuid4()}",
                )
                print(stopped.model_dump_json(indent=2))
        finally:
            registry.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
