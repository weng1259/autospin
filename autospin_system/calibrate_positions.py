"""Interactive coordinate locator for gantry, gripper, pipette, and stations.

Run from the project root:
    python calibrate_positions.py
    python calibrate_positions.py --mock

The script jogs the gantry, lets you save named positions, and writes a JSON
record that can be copied back into config/system_config.yaml after review.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from AutoSpinmotorSystem.config.hardware_config import CONFIG
from AutoSpinmotorSystem.hardware.xyz_stage.xyz_stage import XYZStage


LOG = logging.getLogger("PositionLocator")
DEFAULT_POINTS = [
    "home",
    "glass_pick_gripper",
    "spin_center",
    "pipette_tip_position",
    "precursor_source",
    "antisolvent_source",
    "hotplate_center",
    "clean_station",
    "waste_bin",
]


def parse_args() -> argparse.Namespace:
    comm_cfg = CONFIG.get("communication", {})
    parser = argparse.ArgumentParser(description="Interactively locate AutoSpinmotorSystem coordinates.")
    parser.add_argument("--gantry-port", default=comm_cfg.get("gantry_port", "COM11"))
    parser.add_argument("--relay-port", default=comm_cfg.get("relay_port", "COM10"))
    parser.add_argument("--mock", action="store_true", help="Run without opening serial ports")
    parser.add_argument("--feed", type=float, default=300.0, help="Manual jog feed in mm/min")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "config" / "calibrated_positions.json",
        help="Where to save captured coordinates",
    )
    parser.add_argument("--no-home", action="store_true", help="Do not home before calibration")
    return parser.parse_args()


def print_help() -> None:
    print(
        "\nCommands:\n"
        "  pos                         show current position\n"
        "  save <name> [note...]        save current XYZ as a named lab coordinate\n"
        "  list                        show captured coordinates\n"
        "  names                       show suggested point names\n"
        "  goto <name>                  move to captured or configured coordinate\n"
        "  x|y|z <value>                absolute single-axis move\n"
        "  dx|dy|dz <value>             relative single-axis jog\n"
        "  rel <dx> <dy> <dz>           relative XYZ jog\n"
        "  abs <x> <y> <z>              absolute XYZ move\n"
        "  safe                        lift Z to configured safe_z_mm\n"
        "  home                        home gantry\n"
        "  help                        show this help\n"
        "  quit                        save and exit\n"
    )


def format_pos(pos: dict[str, float]) -> str:
    return f"X={pos['X']:.3f}, Y={pos['Y']:.3f}, Z={pos['Z']:.3f}, Z2={pos.get('Z2', 0.0):.3f}"


def lab_config_positions() -> dict[str, list[float]]:
    return {
        name: [float(values[0]), float(values[1]), float(values[2])]
        for name, values in CONFIG["geometry"]["lab_coordinates"].items()
    }


def save_capture(output: Path, captured: dict[str, dict[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "coordinates": captured,
    }
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def print_yaml_snippet(captured: dict[str, dict[str, Any]]) -> None:
    if not captured:
        return
    print("\nSuggested system_config.yaml snippet:")
    print("geometry:")
    print("  lab_coordinates:")
    for name, item in captured.items():
        x, y, z = item["position"]
        print(f"    {name}: [{x:.3f}, {y:.3f}, {z:.3f}]")


def move_abs(stage: XYZStage, x: float, y: float, z: float, feed: float) -> None:
    if not stage.move_to(x, y, z, feed_mm_min=feed):
        raise RuntimeError("move_to failed")


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    stage = XYZStage(
        port=args.gantry_port,
        relay_port=args.relay_port,
        mock=args.mock,
        logger=LOG,
    )
    captured: dict[str, dict[str, Any]] = {}
    configured = lab_config_positions()

    if not stage.connect():
        raise RuntimeError("Gantry connection failed")

    try:
        if not args.no_home:
            input("Confirm gantry area is clear, then press Enter to home. Ctrl+C to abort.")
            if not stage.home():
                raise RuntimeError("Gantry home failed")

        print_help()
        print("Suggested point names:", ", ".join(DEFAULT_POINTS))

        while True:
            pos = stage.get_position()
            raw = input(f"[{format_pos(pos)}] locator> ").strip()
            if not raw:
                continue
            parts = raw.split()
            cmd = parts[0].lower()

            try:
                if cmd in ("quit", "q", "exit"):
                    break
                if cmd in ("help", "h", "?"):
                    print_help()
                    continue
                if cmd == "names":
                    print(", ".join(DEFAULT_POINTS))
                    continue
                if cmd in ("pos", "p", "status"):
                    print(format_pos(stage.get_position()))
                    continue
                if cmd == "list":
                    for name, item in captured.items():
                        print(f"{name}: {item['position']}  {item.get('note', '')}")
                    continue
                if cmd == "save":
                    if len(parts) < 2:
                        print("Usage: save <name> [note...]")
                        continue
                    name = parts[1]
                    note = " ".join(parts[2:])
                    current = stage.get_position()
                    captured[name] = {
                        "position": [current["X"], current["Y"], current["Z"]],
                        "z2": current.get("Z2", 0.0),
                        "note": note,
                    }
                    save_capture(args.output, captured)
                    print(f"Saved {name}: {captured[name]['position']} -> {args.output}")
                    continue
                if cmd == "goto":
                    if len(parts) != 2:
                        print("Usage: goto <name>")
                        continue
                    name = parts[1]
                    if name in captured:
                        x, y, z = captured[name]["position"]
                    elif name in configured:
                        x, y, z = configured[name]
                    else:
                        print(f"Unknown point: {name}")
                        continue
                    move_abs(stage, x, y, z, args.feed)
                    continue
                if cmd == "home":
                    stage.home()
                    continue
                if cmd == "safe":
                    current = stage.get_position()
                    safe_z = getattr(stage, "safe_z_mm", current["Z"])
                    move_abs(stage, current["X"], current["Y"], safe_z, args.feed)
                    continue
                if cmd == "rel" and len(parts) == 4:
                    dx, dy, dz = map(float, parts[1:])
                    if not stage.manual_jog_rel(dx, dy, dz, feed_mm_min=args.feed):
                        raise RuntimeError("manual_jog_rel failed")
                    continue
                if cmd == "abs" and len(parts) == 4:
                    x, y, z = map(float, parts[1:])
                    move_abs(stage, x, y, z, args.feed)
                    continue
                if cmd in ("x", "y", "z") and len(parts) == 2:
                    current = stage.get_position()
                    target = {"X": current["X"], "Y": current["Y"], "Z": current["Z"]}
                    target[cmd.upper()] = float(parts[1])
                    move_abs(stage, target["X"], target["Y"], target["Z"], args.feed)
                    continue
                if cmd in ("dx", "dy", "dz") and len(parts) == 2:
                    delta = float(parts[1])
                    dx = delta if cmd == "dx" else 0.0
                    dy = delta if cmd == "dy" else 0.0
                    dz = delta if cmd == "dz" else 0.0
                    if not stage.manual_jog_rel(dx, dy, dz, feed_mm_min=args.feed):
                        raise RuntimeError("manual_jog_rel failed")
                    continue

                print("Unknown command. Type 'help'.")
            except Exception as exc:
                LOG.error("Command failed: %s", exc)

    finally:
        save_capture(args.output, captured)
        print_yaml_snippet(captured)
        stage.close()


if __name__ == "__main__":
    main()
