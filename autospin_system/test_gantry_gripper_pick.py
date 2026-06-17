"""Interactive gantry + gripper glass-pick smoke test.

Run from the project root:

    python test_gantry_gripper_pick.py

Hardware defaults:
- gantry / GRBL controller: COM11
- relay / gripper / Z brake: COM10

Important: the gripper and the gantry Z brake share the same DSTUR-T80 relay
serial port. This script lets XYZStage create the real RelayBackend, then
passes that same object to GripperBackend so COM10 is opened only once.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

__test__ = False

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.hardware.gripper_backend import GripperBackend
from src.hardware.relay_backend import RelayBackend
from autospin_system.hardware.xyz_stage.xyz_stage import XYZStage


SCRIPT_VERSION = "shared-relay-interactive-v2"
LOG = logging.getLogger("gantry-gripper-test")
INVALID_POSITION_ABS_MM = 100000.0


class MockRelayBackend:
    """Small relay stand-in for checking the workflow without hardware."""

    def __init__(self) -> None:
        self.channels = {i: False for i in range(1, 9)}
        self.connected = False

    def connect(self) -> None:
        self.connected = True
        LOG.info("[MOCK] relay connected")

    def close(self) -> None:
        self.connected = False
        LOG.info("[MOCK] relay closed")

    def is_connected(self) -> bool:
        return self.connected

    def ch_on(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return {"channel": channel, "target_state": True, "would_write": True}
        self.channels[channel] = True
        LOG.info("[MOCK] relay CH%s ON (%s)", channel, idempotency_key)
        return {"success": True, "channel": channel, "state_after": True}

    def ch_off(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return {"channel": channel, "target_state": False, "would_write": True}
        self.channels[channel] = False
        LOG.info("[MOCK] relay CH%s OFF (%s)", channel, idempotency_key)
        return {"success": True, "channel": channel, "state_after": False}


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def fresh_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def confirm(message: str, *, assume_yes: bool = False) -> None:
    if assume_yes:
        LOG.info("%s", message)
        return
    input(f"\n{message}\nPress Enter to continue, or Ctrl+C to abort.")


def get_shared_relay(stage: XYZStage, *, mock: bool) -> RelayBackend | MockRelayBackend:
    """Return the one relay backend that both gantry and gripper must share."""
    if mock:
        relay = MockRelayBackend()
        relay.connect()
        return relay

    relay = stage._relay_backend
    if relay is None:
        raise RuntimeError("XYZStage connected without exposing _relay_backend")
    if not relay.is_connected():
        relay.connect()
    return relay


def clamp_feed(stage: XYZStage, feed: float) -> float:
    max_feed = float(getattr(stage, "max_feed_mm_min", feed))
    if feed > max_feed:
        LOG.warning(
            "Requested feed %.0f mm/min exceeds max %.0f mm/min; using %.0f mm/min.",
            feed,
            max_feed,
            max_feed,
        )
        return max_feed
    return feed


def is_soft_limit_error(exc: Exception) -> bool:
    return str(getattr(exc, "error_code", "")) == "L3.SOFT_LIMIT_EXCEEDED"


def is_valid_position(position: dict[str, float]) -> bool:
    return all(abs(float(position[axis])) < INVALID_POSITION_ABS_MM for axis in ("X", "Y", "Z"))


def warn_invalid_position(position: dict[str, float]) -> None:
    LOG.warning(
        "Gantry position looks invalid: X=%.3f Y=%.3f Z=%.3f. "
        "Do not use relative moves; run 'diag', then 'home' or 'recover'.",
        position["X"],
        position["Y"],
        position["Z"],
    )


def target_is_inside_soft_limits(stage: XYZStage, x: float, y: float, z: float, feed: float) -> bool:
    try:
        stage.dry_run_move_to(x, y, z, feed_mm_min=feed)
        return True
    except Exception as exc:
        if is_soft_limit_error(exc):
            LOG.warning(
                "Move skipped by soft limit: %s",
                getattr(exc, "human_message", str(exc)),
            )
            return False
        raise


def move_absolute(stage: XYZStage, x: float, y: float, z: float, feed: float, z_feed: float) -> bool:
    feed = clamp_feed(stage, feed)
    z_feed = clamp_feed(stage, z_feed)
    current = stage.get_position()
    if not is_valid_position(current):
        warn_invalid_position(current)
        return True
    current_x = current["X"]
    current_y = current["Y"]
    current_z = current["Z"]
    xy_changes = abs(x - current_x) > 1e-6 or abs(y - current_y) > 1e-6
    z_changes = abs(z - current_z) > 1e-6

    LOG.info(
        "move_abs staged target X=%.3f Y=%.3f Z=%.3f from X=%.3f Y=%.3f Z=%.3f Fxy=%.0f Fz=%.0f",
        x,
        y,
        z,
        current_x,
        current_y,
        current_z,
        feed,
        z_feed,
    )

    if xy_changes and not target_is_inside_soft_limits(stage, x, y, current_z, feed):
        return True
    if z_changes and not target_is_inside_soft_limits(stage, x, y, z, z_feed):
        return True

    if xy_changes:
        LOG.info("move_abs step 1/2 XY: X=%.3f Y=%.3f Z=%.3f F=%.0f", x, y, current_z, feed)
        if not stage.move_to(x, y, current_z, feed_mm_min=feed):
            return False

    if z_changes:
        LOG.info("move_abs step 2/2 Z: X=%.3f Y=%.3f Z=%.3f F=%.0f", x, y, z, z_feed)
        return stage.move_to(x, y, z, feed_mm_min=z_feed)
    return True


def move_relative(stage: XYZStage, dx: float, dy: float, dz: float, feed: float, z_feed: float) -> bool:
    current = stage.get_position()
    if not is_valid_position(current):
        warn_invalid_position(current)
        return True
    target_x = current["X"] + dx
    target_y = current["Y"] + dy
    target_z = current["Z"] + dz
    LOG.info(
        "move_rel dX=%.3f dY=%.3f dZ=%.3f -> X=%.3f Y=%.3f Z=%.3f F=%.0f",
        dx,
        dy,
        dz,
        target_x,
        target_y,
        target_z,
        feed,
    )
    return move_absolute(stage, target_x, target_y, target_z, feed, z_feed)


def unlock_gantry_alarm(stage: XYZStage) -> bool:
    """Clear a GRBL alarm without intentionally moving the gantry."""
    if stage.mock:
        LOG.info("[MOCK] unlock gantry alarm")
        return True

    backend = getattr(stage, "_backend", None)
    unlock_alarm = getattr(backend, "unlock_alarm", None)
    if backend is None or unlock_alarm is None:
        LOG.error("Connected XYZStage does not expose unlock_alarm().")
        return False

    try:
        status = unlock_alarm()
        if hasattr(stage, "_record_position") and hasattr(status, "position"):
            stage._record_position(status.position)
        LOG.info("unlock_alarm -> state=%s position=%s", getattr(status, "state", None), stage.get_position())
        return True
    except Exception as exc:
        LOG.exception("unlock_alarm failed: %s", exc)
        return False


def halt_gantry_motion(stage: XYZStage) -> bool:
    """Cancel an in-progress jog without intentionally re-homing."""
    if stage.mock:
        LOG.info("[MOCK] halt gantry motion")
        return True

    backend = getattr(stage, "_backend", None)
    halt = getattr(backend, "halt", None)
    if backend is None or halt is None:
        LOG.error("Connected XYZStage does not expose halt().")
        return False

    try:
        status = halt()
        if hasattr(stage, "_record_position") and hasattr(status, "position"):
            stage._record_position(status.position)
        LOG.info("halt -> state=%s position=%s", getattr(status, "state", None), stage.get_position())
        return True
    except Exception as exc:
        LOG.exception("halt failed: %s", exc)
        return False


def log_homing_diagnostics(stage: XYZStage, context: str) -> None:
    try:
        LOG.info("%s diagnostics: %s", context, stage.get_homing_diagnostics())
    except Exception:
        LOG.exception("Failed to read gantry diagnostics for %s.", context)


def open_gripper_for_home(gripper: GripperBackend, label: str) -> bool:
    try:
        LOG.info(
            "gripper.open before %s -> %s",
            label,
            dump(gripper.open(idempotency_key=fresh_key(f"{label}-open-gripper"))),
        )
        return True
    except Exception:
        LOG.exception("Failed to open gripper before %s.", label)
        return False


def home_gantry_with_open_gripper(stage: XYZStage, gripper: GripperBackend, label: str) -> bool:
    if not open_gripper_for_home(gripper, label):
        return False
    if stage.home():
        return True
    LOG.error("%s home failed.", label)
    log_homing_diagnostics(stage, f"{label} home failed")
    return False


def interactive_adjust(stage: XYZStage, gripper: GripperBackend, *, feed: float, z_feed: float) -> bool:
    """Let the operator tune gantry position before closing the gripper."""
    print(
        "\nInteractive adjustment commands:\n"
        "  x y z              absolute move, example: -20 -40 -25\n"
        "  rel dx dy dz       relative move, example: rel 0 0 -1\n"
        "  x <value>          absolute single-axis move, example: z -25\n"
        "  dx <value>         relative single-axis move, example: dz -1\n"
        "  open | close       gripper relay commands\n"
        "  home               home gantry and return to origin\n"
        "  halt               cancel current jog/motion\n"
        "  unlock             clear GRBL alarm without homing\n"
        "  recover            soft-reset, unlock, and home the gantry\n"
        "  diag               print homing/limit diagnostics\n"
        "  pos                print current position\n"
        "  done               accept current position and continue\n"
        "  quit               stop the script\n"
    )
    while True:
        current = stage.get_position()
        raw = input(
            f"[X={current['X']:.3f} Y={current['Y']:.3f} Z={current['Z']:.3f}] command> "
        ).strip()
        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("done", "d"):
                return True
            if cmd in ("quit", "q", "exit"):
                return False
            if cmd in ("pos", "p", "status"):
                LOG.info("Current gantry position: %s", stage.get_position())
                continue
            if cmd == "open":
                LOG.info("gripper.open -> %s", dump(gripper.open(idempotency_key=fresh_key("adjust-open"))))
                continue
            if cmd == "close":
                LOG.info("gripper.close -> %s", dump(gripper.close(idempotency_key=fresh_key("adjust-close"))))
                continue
            if cmd in ("home", "origin", "zero"):
                LOG.info("Homing gantry from interactive adjustment.")
                home_gantry_with_open_gripper(stage, gripper, "interactive-home")
                continue
            if cmd in ("halt", "stop", "cancel"):
                halt_gantry_motion(stage)
                continue
            if cmd in ("unlock", "clear", "clear-alarm"):
                unlock_gantry_alarm(stage)
                continue
            if cmd in ("recover", "restore"):
                LOG.info("Recovering gantry from alarm; opening gripper, halting first, then re-homing.")
                if not open_gripper_for_home(gripper, "interactive-recover"):
                    continue
                halt_gantry_motion(stage)
                if not stage.recover_from_alarm(skip_rehome=False):
                    LOG.error("Gantry recover failed.")
                    log_homing_diagnostics(stage, "interactive-recover failed")
                continue
            if cmd in ("diag", "diagnostics"):
                LOG.info("Gantry diagnostics: %s", stage.get_homing_diagnostics())
                continue
            if cmd in ("rel", "r"):
                if len(parts) != 4:
                    print("Expected: rel dx dy dz")
                    continue
                ok = move_relative(stage, float(parts[1]), float(parts[2]), float(parts[3]), feed, z_feed)
            elif cmd in ("x", "y", "z"):
                if len(parts) != 2:
                    print(f"Expected: {cmd} value")
                    continue
                current = stage.get_position()
                target = {"x": current["X"], "y": current["Y"], "z": current["Z"]}
                target[cmd] = float(parts[1])
                ok = move_absolute(stage, target["x"], target["y"], target["z"], feed, z_feed)
            elif cmd in ("dx", "dy", "dz"):
                if len(parts) != 2:
                    print(f"Expected: {cmd} value")
                    continue
                delta = {"dx": 0.0, "dy": 0.0, "dz": 0.0}
                delta[cmd] = float(parts[1])
                ok = move_relative(stage, delta["dx"], delta["dy"], delta["dz"], feed, z_feed)
            else:
                if len(parts) != 3:
                    print("Unknown command. Enter three numbers for absolute X Y Z, or type pos.")
                    continue
                ok = move_absolute(stage, float(parts[0]), float(parts[1]), float(parts[2]), feed, z_feed)

            if not ok:
                LOG.error(
                    "Move command failed. Staying in interactive mode; "
                    "use 'unlock' for a non-moving alarm clear, or 'recover' to re-home."
                )
                continue
        except ValueError:
            print("Could not parse number. Try again.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely test gantry + gripper glass pickup.")
    parser.add_argument("--gantry-port", default="COM11", help="GRBL gantry serial port.")
    parser.add_argument("--relay-port", default="COM10", help="DSTUR-T80 relay serial port.")
    parser.add_argument("--gripper-channel", type=int, default=1, help="Relay channel for gripper.")
    parser.add_argument("--mock", action="store_true", help="Run without opening serial ports.")
    parser.add_argument("--yes", action="store_true", help="Skip Enter confirmations. Use with care.")
    parser.add_argument("--skip-home", action="store_true", help="Do not run gantry home().")
    parser.add_argument("--feed", type=float, default=3000.0, help="Motion feed in mm/min.")
    parser.add_argument("--z-feed", type=float, default=1000.0, help="Z-axis motion feed in mm/min.")
    parser.add_argument("--start-x", type=float, default=-20.0, help="Initial fixed X move, mm.")
    parser.add_argument("--start-y", type=float, default=-40.0, help="Initial fixed Y move, mm.")
    parser.add_argument("--start-z", type=float, default=-5.0, help="Initial fixed Z move, mm.")
    parser.add_argument("--safe-z", type=float, default=-5.0, help="Safe raised Z after gripping, mm.")
    parser.add_argument("--hold-seconds", type=float, default=1.0, help="Pause after close before lifting.")
    parser.add_argument("--skip-final-home", action="store_true", help="Do not return gantry to origin at the end.")
    parser.add_argument(
        "--keep-closed",
        action="store_true",
        help="Leave gripper closed at the end instead of opening it.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

    stage: XYZStage | None = None
    relay: RelayBackend | MockRelayBackend | None = None
    gripper: GripperBackend | None = None

    LOG.info("SCRIPT_VERSION=%s", SCRIPT_VERSION)
    LOG.info(
        "Initial target: X=%.3f Y=%.3f Z=%.3f safe Z=%.3f feed=%.0f z_feed=%.0f",
        args.start_x,
        args.start_y,
        args.start_z,
        args.safe_z,
        args.feed,
        args.z_feed,
    )
    LOG.info(
        "Ports: gantry=%s relay=%s; gripper CH%s; CH ON=close, CH OFF=open",
        args.gantry_port,
        args.relay_port,
        args.gripper_channel,
    )

    try:
        confirm("Confirm the gantry area is clear and hands are away.", assume_yes=args.yes)

        stage = XYZStage(
            port=args.gantry_port,
            relay_port=args.relay_port,
            mock=args.mock,
            logger=LOG,
        )
        if not stage.connect():
            LOG.error(
                "Gantry connect failed. Check dependencies, port, power, "
                "and whether another app holds the port."
            )
            return 2

        relay = get_shared_relay(stage, mock=args.mock)
        gripper = GripperBackend(relay, channel=args.gripper_channel)

        confirm("Step 1: open the gripper.", assume_yes=args.yes)
        LOG.info("gripper.open -> %s", dump(gripper.open(idempotency_key=fresh_key("glass-test-open"))))

        if not args.skip_home:
            confirm(
                "Step 2: home the gantry. This can move through a large range.",
                assume_yes=args.yes,
            )
            if not home_gantry_with_open_gripper(stage, gripper, "initial-home"):
                LOG.error("Gantry home failed.")
                return 3

        confirm("Step 3: adjust gantry position from keyboard input.", assume_yes=args.yes)
        if not interactive_adjust(stage, gripper, feed=args.feed, z_feed=args.z_feed):
            LOG.error("Interactive adjustment stopped before completion.")
            return 5

        confirm("Step 5: close the gripper. Confirm the glass is between the jaws.", assume_yes=args.yes)
        LOG.info("gripper.close -> %s", dump(gripper.close(idempotency_key=fresh_key("glass-test-close"))))
        time.sleep(max(0.0, args.hold_seconds))

        confirm(f"Step 6: lift to safe Z={args.safe_z} and observe the grip.", assume_yes=args.yes)
        current = stage.get_position()
        if not move_absolute(stage, current["X"], current["Y"], args.safe_z, args.feed, args.z_feed):
            LOG.error("Lift after grip failed.")
            return 6

        LOG.info("Current gantry position: %s", stage.get_position())

        if not args.keep_closed:
            confirm("Final step: open the gripper to release the glass.", assume_yes=args.yes)
            LOG.info("gripper.open -> %s", dump(gripper.open(idempotency_key=fresh_key("glass-test-open"))))

        if not args.skip_final_home:
            confirm(
                "Final step: return the gantry to origin. This can move through a large range.",
                assume_yes=args.yes,
            )
            if not home_gantry_with_open_gripper(stage, gripper, "final-home"):
                LOG.error("Final gantry home failed.")
                return 7

        LOG.info("Test finished.")
        return 0

    except KeyboardInterrupt:
        LOG.warning("Interrupted by user. Attempting safe stop/open.")
        if stage is not None:
            try:
                stage.emergency_stop()
            except Exception:
                LOG.exception("Failed to halt gantry during interrupt.")
        if gripper is not None:
            try:
                LOG.info(
                    "gripper.open -> %s",
                    dump(gripper.open(idempotency_key=fresh_key("glass-test-interrupt-open"))),
                )
            except Exception:
                LOG.exception("Failed to open gripper during interrupt.")
        return 130
    finally:
        if stage is not None:
            stage.close()
        elif relay is not None:
            relay.close()


if __name__ == "__main__":
    raise SystemExit(main())
