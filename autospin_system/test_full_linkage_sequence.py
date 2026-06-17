"""Full linkage script for gantry, gripper, Z2 slide, pipette placeholder, and spin motor.

Run a dry software check first:

    python test_full_linkage_sequence.py --mock --yes

Real hardware run example:

    python test_full_linkage_sequence.py ^
        --a -20 -40 -20 ^
        --b -80 -40 -5 ^
        --c -80 -80 -5 ^
        --d -20 -80 -5

The sequence follows:
1. home XYZ, initialize Z2 at top,
2. move XYZ to A, close gripper, lift Z,
3. move to B, open gripper,
4. move Z2 by a configured relative distance,
5. move to C, return Z2 to A0, skip pipette action for now, return to B,
6. spin 500 rpm for 5 s, then 3000 rpm for a configured duration,
7. lower Z, close gripper, lift Z, move to D, then home.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


__test__ = False

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.spin_motor.motor_controller import MotorController
from src.hardware.gripper_backend import GripperBackend
from src.hardware.relay_backend import RelayBackend
from autospin_system.hardware.xyz_stage.xyz_stage import XYZStage


LOG = logging.getLogger("full-linkage-sequence")
SCRIPT_VERSION = "full-linkage-v1"


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    z: float


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

    def ch_on(self, channel: int, *, idempotency_key: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"channel": channel, "target_state": True, "would_write": True}
        self.channels[channel] = True
        LOG.info("[MOCK] relay CH%s ON (%s)", channel, idempotency_key)
        return {"success": True, "channel": channel, "state_after": True}

    def ch_off(self, channel: int, *, idempotency_key: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"channel": channel, "target_state": False, "would_write": True}
        self.channels[channel] = False
        LOG.info("[MOCK] relay CH%s OFF (%s)", channel, idempotency_key)
        return {"success": True, "channel": channel, "state_after": False}


class MockGripperBackend:
    """Gripper stand-in that avoids observable runlog writes in mock mode."""

    def __init__(self, relay: MockRelayBackend, channel: int) -> None:
        self._relay = relay
        self._channel = channel
        self._closed = False

    def open(self, *, idempotency_key: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"target_state": "open", "would_activate_relay": self._closed}
        result = self._relay.ch_off(self._channel, idempotency_key=idempotency_key)
        self._closed = False
        return {"success": True, "commanded_state_after": "open", "relay": result}

    def close(self, *, idempotency_key: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"target_state": "closed", "would_activate_relay": not self._closed}
        result = self._relay.ch_on(self._channel, idempotency_key=idempotency_key)
        self._closed = True
        return {"success": True, "commanded_state_after": "closed", "relay": result}


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def fresh_key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def confirm(message: str, *, assume_yes: bool) -> None:
    if assume_yes:
        LOG.info("%s", message)
        return
    input(f"\n{message}\nPress Enter to continue, or Ctrl+C to abort.")


def ports_from_config() -> dict[str, str]:
    comm_cfg = CONFIG.get("communication", {})
    rs485_port = comm_cfg.get("rs485_bus_port") or comm_cfg.get("motor_port") or "COM9"
    return {
        "gantry": comm_cfg.get("gantry_port", "COM11"),
        "relay": comm_cfg.get("relay_port", "COM10"),
        "rs485": rs485_port,
    }


def gripper_channel_from_config() -> int:
    return int(CONFIG["devices"]["relay"]["channels"].get("gripper", 1))


def parse_point(values: list[float] | None, fallback: Point | None) -> Point | None:
    if values is None:
        return fallback
    return Point(float(values[0]), float(values[1]), float(values[2]))


def validate_points(args: argparse.Namespace) -> None:
    missing = [name for name in ("a", "b", "c", "d") if getattr(args, name) is None]
    if missing and not args.mock:
        joined = ", ".join(f"--{name}" for name in missing)
        LOG.warning("Real hardware run is using built-in default coordinates for: %s", joined)


def get_shared_relay(stage: XYZStage, *, mock: bool, relay_port: str):
    if mock:
        relay = MockRelayBackend()
        relay.connect()
        return relay
    relay = stage._relay_backend
    if relay is None:
        relay = RelayBackend(port=relay_port)
        relay.connect()
    elif not relay.is_connected():
        relay.connect()
    return relay


def connect_motion_devices(args: argparse.Namespace):
    ports = ports_from_config()
    stage = XYZStage(
        port=args.gantry_port or ports["gantry"],
        relay_port=args.relay_port or ports["relay"],
        mock=args.mock,
        logger=LOG,
    )
    if not stage.connect():
        raise RuntimeError("Gantry connect failed")

    relay = get_shared_relay(
        stage,
        mock=args.mock,
        relay_port=args.relay_port or ports["relay"],
    )
    if args.mock:
        gripper = MockGripperBackend(relay, args.gripper_channel)
    else:
        gripper = GripperBackend(relay, channel=args.gripper_channel)
    return stage, relay, gripper


def connect_spin_motor(args: argparse.Namespace) -> MotorController:
    port = args.rs485_port or ports_from_config()["rs485"]
    motor = MotorController(port=port, mock=args.mock, logger=LOG)
    if not motor.connect():
        raise RuntimeError("Spin motor connect/initialize failed")
    return motor


def command_gripper_open(gripper: Any, args: argparse.Namespace, label: str) -> None:
    if args.invert_gripper:
        result = gripper.close(idempotency_key=fresh_key(f"{label}-open-inverted"))
    else:
        result = gripper.open(idempotency_key=fresh_key(f"{label}-open"))
    LOG.info("gripper.physical_open -> %s", dump(result))


def command_gripper_close(gripper: Any, args: argparse.Namespace, label: str) -> None:
    if args.invert_gripper:
        result = gripper.open(idempotency_key=fresh_key(f"{label}-close-inverted"))
    else:
        result = gripper.close(idempotency_key=fresh_key(f"{label}-close"))
    LOG.info("gripper.physical_close -> %s", dump(result))


def require_move(stage: XYZStage, target: Point, feed: float, label: str, timeout_s: float) -> None:
    LOG.info("%s: move XYZ to X=%.3f Y=%.3f Z=%.3f F=%.0f", label, target.x, target.y, target.z, feed)
    if stage.mock or stage._backend is None:
        ok = stage.move_to(target.x, target.y, target.z, feed_mm_min=feed)
        if not ok:
            raise RuntimeError(f"{label} failed")
        return

    try:
        target_position = stage._make_position(target.x, target.y, target.z)
        result = stage._backend.move_to(
            target_position,
            feed_mm_min=feed,
            timeout_s=timeout_s,
        )
        stage._record_position(result.final_position)
    except Exception as exc:
        stage._log_l3_error(f"XYZStage.move_to/{label}", exc)
        raise RuntimeError(f"{label} failed") from exc


def move_z_relative(stage: XYZStage, dz: float, feed: float, label: str, timeout_s: float) -> None:
    pos = stage.get_position()
    target = Point(float(pos["X"]), float(pos["Y"]), float(pos["Z"]) + float(dz))
    require_move(stage, target, feed, label, timeout_s)


def z2_settle_seconds(args: argparse.Namespace) -> float:
    if args.after_z2_settle_seconds is not None:
        return max(0.0, float(args.after_z2_settle_seconds))
    if args.z2_feed <= 0:
        return 1.0
    estimated_motion_s = abs(float(args.z2_delta_mm)) / float(args.z2_feed) * 60.0
    return max(1.0, estimated_motion_s + 0.5)


def run_spin_profile(motor: MotorController, args: argparse.Namespace) -> None:
    confirm(
        "Spin profile: confirm chuck area is clear and the substrate is ready.",
        assume_yes=args.yes,
    )
    ok, message = motor.start(direction="forward", wait_for_stop=True)
    if not ok:
        raise RuntimeError(f"Spin motor start failed: {message}")

    try:
        for rpm, seconds in (
            (args.spin_low_rpm, args.spin_low_seconds),
            (args.spin_high_rpm, args.spin_high_seconds),
        ):
            ok, message = motor.set_speed(rpm)
            if not ok:
                raise RuntimeError(f"Spin motor set_speed({rpm}) failed: {message}")
            LOG.info("Spin motor running at %.0f rpm for %.1f s", rpm, seconds)
            time.sleep(max(0.0, float(seconds)))
    finally:
        motor.stop(use_brake=True)
        LOG.info("Spin motor stopped")


def run_sequence(args: argparse.Namespace) -> None:
    demo_a = Point(-20.0, -40.0, -20.0)
    demo_b = Point(-80.0, -40.0, -5.0)
    demo_c = Point(-80.0, -80.0, -5.0)
    demo_d = Point(-20.0, -80.0, -5.0)
    points = {
        "A": parse_point(args.a, demo_a ),
        "B": parse_point(args.b, demo_b ),
        "C": parse_point(args.c, demo_c ),
        "D": parse_point(args.d, demo_d ),
    }
    if any(point is None for point in points.values()):
        raise RuntimeError("Missing motion points")

    stage = None
    gripper = None
    motor = None
    try:
        confirm("Confirm all hardware is powered and the work area is clear.", assume_yes=args.yes)
        stage, _relay, gripper = connect_motion_devices(args)
        motor = connect_spin_motor(args)

        if not args.skip_home:
            confirm("Initial XYZ home. This can move through a large range.", assume_yes=args.yes)
            if not stage.home():
                raise RuntimeError("Initial XYZ home failed")
        else:
            LOG.info("Initial XYZ home skipped by --skip-home")

        if not args.skip_z2_init:
            confirm(
                "Confirm Z2 slide is physically at the top safe position before setting A0.",
                assume_yes=args.yes,
            )
            if not stage.initialize_z2_at_top():
                raise RuntimeError("Z2 initialization failed")
        else:
            LOG.info("Z2 initialization skipped by --skip-z2-init")

        confirm("Open gripper before moving to point A.", assume_yes=args.yes)
        command_gripper_open(gripper, args, "linkage-start")

        require_move(stage, points["A"], args.feed, "Step A", args.move_timeout)
        confirm("Close gripper at point A.", assume_yes=args.yes)
        command_gripper_close(gripper, args, "linkage-a")
        time.sleep(max(0.0, args.grip_settle_seconds))

        move_z_relative(stage, abs(args.z_lift_mm), args.feed, "Lift after A", args.move_timeout)
        require_move(stage, points["B"], args.feed, "Move to B", args.move_timeout)

        confirm("Open gripper at point B.", assume_yes=args.yes)
        command_gripper_open(gripper, args, "linkage-b")

        LOG.info("Move Z2 relative by %.3f mm", args.z2_delta_mm)
        if not stage.move_z2_rel(args.z2_delta_mm, feed_mm_min=args.z2_feed, timeout_s=args.z2_timeout):
            raise RuntimeError("Z2 relative move failed")
        settle_s = z2_settle_seconds(args)
        if settle_s > 0:
            LOG.info("Wait %.1f s for Z2 motion to fully settle before XYZ move", settle_s)
            time.sleep(settle_s)

        require_move(stage, points["C"], args.feed, "Move to C", args.move_timeout)
        LOG.info("Return Z2 to origin A0")
        if not stage.move_z2_to(0.0, feed_mm_min=args.z2_feed, timeout_s=args.z2_timeout):
            raise RuntimeError("Z2 return to origin failed")
        settle_s = z2_settle_seconds(args)
        if settle_s > 0:
            LOG.info("Wait %.1f s for Z2 return to fully settle before continuing", settle_s)
            time.sleep(settle_s)
        LOG.info("Pipette action skipped: no tip head installed")
        require_move(stage, points["B"], args.feed, "Return to B", args.move_timeout)

        run_spin_profile(motor, args)

        move_z_relative(stage, -abs(args.z_drop_mm), args.feed, "Lower after spin", args.move_timeout)
        confirm("Close gripper after spin.", assume_yes=args.yes)
        command_gripper_close(gripper, args, "linkage-after-spin")
        time.sleep(max(0.0, args.grip_settle_seconds))

        move_z_relative(stage, abs(args.z_lift_mm), args.feed, "Lift after spin pickup", args.move_timeout)
        require_move(stage, points["D"], args.feed, "Move to D", args.move_timeout)

        if args.release_at_d:
            confirm("Open gripper at point D before homing.", assume_yes=args.yes)
            command_gripper_open(gripper, args, "linkage-d")

        confirm("Final XYZ home.", assume_yes=args.yes)
        if not stage.home():
            raise RuntimeError("Final XYZ home failed")
        LOG.info("Full linkage sequence finished")

    except KeyboardInterrupt:
        LOG.warning("Interrupted by user. Attempting safe stop.")
        raise
    finally:
        if motor is not None:
            try:
                motor.stop(use_brake=True)
            except Exception:
                LOG.exception("Failed to stop spin motor during cleanup")
            try:
                motor.comm.close()
            except Exception:
                LOG.exception("Failed to close spin motor port")
        if stage is not None:
            try:
                stage.close()
            except Exception:
                LOG.exception("Failed to close gantry stage")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full multi-device linkage sequence.")
    parser.add_argument("--mock", action="store_true", help="Run without opening serial ports.")
    parser.add_argument("--yes", action="store_true", help="Skip Enter confirmations. Use with care.")
    parser.add_argument("--skip-home", action="store_true", help="Skip the initial XYZ home.")
    parser.add_argument("--skip-z2-init", action="store_true", help="Skip Z2 A0 initialization.")
    parser.add_argument("--release-at-d", action="store_true", help="Open gripper at point D before final home.")
    parser.add_argument(
        "--invert-gripper",
        action="store_true",
        help="Swap physical open/close relay commands if the gripper wiring is active-low.",
    )

    parser.add_argument("--a", nargs=3, type=float, metavar=("X", "Y", "Z"), help="Point A in mm.")
    parser.add_argument("--b", nargs=3, type=float, metavar=("X", "Y", "Z"), help="Point B in mm.")
    parser.add_argument("--c", nargs=3, type=float, metavar=("X", "Y", "Z"), help="Point C in mm.")
    parser.add_argument("--d", nargs=3, type=float, metavar=("X", "Y", "Z"), help="Point D in mm.")

    parser.add_argument("--z-lift-mm", type=float, default=10.0, help="Relative upward Z lift in mm.")
    parser.add_argument("--z-drop-mm", type=float, default=10.0, help="Relative downward Z drop after spin in mm.")
    parser.add_argument("--z2-delta-mm", type=float, default=25.0, help="Relative Z2 movement in mm.")
    parser.add_argument("--feed", type=float, default=2000.0, help="XYZ feed in mm/min.")
    parser.add_argument("--move-timeout", type=float, default=30.0, help="XYZ move timeout in seconds.")
    parser.add_argument("--z2-feed", type=float, default=200.0, help="Z2 feed in mm/min.")
    parser.add_argument("--z2-timeout", type=float, default=30.0, help="Z2 move timeout in seconds.")
    parser.add_argument(
        "--after-z2-settle-seconds",
        type=float,
        help="Extra wait after Z2 move. Default estimates from z2 distance/feed plus 0.5 s.",
    )
    parser.add_argument("--grip-settle-seconds", type=float, default=0.5, help="Pause after gripper close.")

    parser.add_argument("--spin-low-rpm", type=float, default=500.0, help="First spin speed in rpm.")
    parser.add_argument("--spin-low-seconds", type=float, default=5.0, help="First spin duration in seconds.")
    parser.add_argument("--spin-high-rpm", type=float, default=3000.0, help="Second spin speed in rpm.")
    parser.add_argument("--spin-high-seconds", type=float, default=10.0, help="Second spin duration in seconds.")

    parser.add_argument("--gantry-port", help="Override gantry serial port.")
    parser.add_argument("--relay-port", help="Override relay serial port.")
    parser.add_argument("--rs485-port", help="Override shared spin motor RS485 port.")
    parser.add_argument(
        "--gripper-channel",
        type=int,
        default=gripper_channel_from_config(),
        help="Relay channel for gripper. Default comes from system_config.yaml.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    validate_points(args)

    ports = ports_from_config()
    LOG.info("SCRIPT_VERSION=%s", SCRIPT_VERSION)
    LOG.info(
        "Ports: gantry=%s relay=%s rs485=%s; gripper CH%s; mock=%s",
        args.gantry_port or ports["gantry"],
        args.relay_port or ports["relay"],
        args.rs485_port or ports["rs485"],
        args.gripper_channel,
        args.mock,
    )

    try:
        run_sequence(args)
        return 0
    except KeyboardInterrupt:
        LOG.warning("Sequence interrupted")
        return 130
    except Exception as exc:
        LOG.error("Full linkage sequence failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
