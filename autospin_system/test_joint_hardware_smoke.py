"""Joint hardware smoke test for gantry, gripper, pipette, and spin motor.

This script is intentionally conservative. By default it:
- opens the gripper,
- optionally homes the gantry after confirmation,
- moves the gantry by a small relative offset and returns,
- reads pipette status without aspirating/dispensing,
- reads spin motor speed without spinning.

Use ``--spin-low-speed`` only when the spin chuck area is safe.
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

from config.hardware_config import CONFIG
from hardware.pipette.pipette_controller import PipetteController
from hardware.spin_motor.motor_controller import MotorController
from hardware.xyz_stage.l3_backend.hardware.gripper_backend import GripperBackend
from hardware.xyz_stage.l3_backend.hardware.relay_backend import RelayBackend
from hardware.xyz_stage.xyz_stage import XYZStage


LOG = logging.getLogger("joint-hardware-smoke")
SCRIPT_VERSION = "joint-smoke-v1"


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


def get_configured_ports() -> dict[str, str]:
    comm_cfg = CONFIG.get("communication", {})
    rs485_port = comm_cfg.get("rs485_bus_port") or comm_cfg.get("motor_port") or comm_cfg.get("pipette_port")
    return {
        "gantry": comm_cfg.get("gantry_port", "COM6"),
        "relay": comm_cfg.get("relay_port", "COM5"),
        "rs485": rs485_port or "COM9",
    }


def get_gripper_channel() -> int:
    return int(CONFIG["devices"]["relay"]["channels"].get("gripper", 1))


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


def connect_gantry_and_gripper(args: argparse.Namespace):
    ports = get_configured_ports()
    stage = XYZStage(
        port=ports["gantry"],
        relay_port=ports["relay"],
        mock=args.mock,
        logger=LOG,
    )
    if not stage.connect():
        raise RuntimeError("Gantry connect failed")

    relay = get_shared_relay(stage, mock=args.mock, relay_port=ports["relay"])
    if args.mock:
        gripper = MockGripperBackend(relay, args.gripper_channel)
    else:
        gripper = GripperBackend(relay, channel=args.gripper_channel)
    return stage, relay, gripper


def exercise_gripper(gripper: GripperBackend, *, assume_yes: bool) -> None:
    confirm("Step 1: open the gripper.", assume_yes=assume_yes)
    LOG.info("gripper.open -> %s", dump(gripper.open(idempotency_key=fresh_key("smoke-open"))))

    confirm("Step 2: close the gripper briefly, then reopen it.", assume_yes=assume_yes)
    LOG.info("gripper.close -> %s", dump(gripper.close(idempotency_key=fresh_key("smoke-close"))))
    time.sleep(0.5)
    LOG.info("gripper.open -> %s", dump(gripper.open(idempotency_key=fresh_key("smoke-reopen"))))


def exercise_gantry(stage: XYZStage, args: argparse.Namespace) -> None:
    if not args.skip_home:
        confirm("Step 3: home the gantry. Confirm the motion area is clear.", assume_yes=args.yes)
        if not stage.home():
            raise RuntimeError("Gantry home failed")
    else:
        LOG.info("Gantry home skipped by --skip-home")

    start = stage.get_position()
    LOG.info("Gantry start position: %s", start)
    target = {
        "X": start["X"] + args.move_dx,
        "Y": start["Y"] + args.move_dy,
        "Z": start["Z"] + args.move_dz,
    }

    confirm(
        "Step 4: perform a small gantry relative move and return.",
        assume_yes=args.yes,
    )
    LOG.info("Gantry move target: %s", target)
    if not stage.move_to(target["X"], target["Y"], target["Z"], feed_mm_min=args.feed):
        raise RuntimeError("Gantry small move failed")
    time.sleep(0.2)
    if not stage.move_to(start["X"], start["Y"], start["Z"], feed_mm_min=args.feed):
        raise RuntimeError("Gantry return move failed")
    LOG.info("Gantry returned to: %s", stage.get_position())


def read_pipette_status(*, port: str, mock: bool) -> None:
    pipette = PipetteController(port=port, mock=mock, logger=LOG)
    try:
        if not pipette.comm.connect():
            raise RuntimeError("Pipette serial connect failed")
        status = pipette.get_status()
        LOG.info("Pipette status: %s", status)
    finally:
        pipette.comm.close()


def read_motor_status(*, port: str, mock: bool) -> MotorController:
    motor = MotorController(port=port, mock=mock, logger=LOG)
    if not motor.comm.connect():
        raise RuntimeError("Spin motor serial connect failed")
    actual_speed = motor.get_actual_speed()
    LOG.info("Spin motor actual speed: %.1f RPM", actual_speed)
    return motor


def exercise_rs485_devices(args: argparse.Namespace) -> None:
    rs485_port = get_configured_ports()["rs485"]

    confirm("Step 5: read pipette status only. No aspirate or dispense.", assume_yes=args.yes)
    read_pipette_status(port=rs485_port, mock=args.mock)

    confirm("Step 6: read spin motor speed only.", assume_yes=args.yes)
    motor = read_motor_status(port=rs485_port, mock=args.mock)
    try:
        if args.spin_low_speed:
            confirm(
                f"Step 7: spin motor at {args.spin_rpm} RPM for {args.spin_seconds} seconds.",
                assume_yes=args.yes,
            )
            if not motor._is_initialized:
                motor._initialize_driver()
            ok, message = motor.start(direction="forward", wait_for_stop=True)
            if not ok:
                raise RuntimeError(f"Spin motor start failed: {message}")
            ok, message = motor.set_speed(args.spin_rpm)
            if not ok:
                raise RuntimeError(f"Spin motor set_speed failed: {message}")
            time.sleep(max(0.0, args.spin_seconds))
            motor.stop(use_brake=True)
            LOG.info("Spin motor low-speed test complete")
    finally:
        try:
            motor.stop(use_brake=True)
        finally:
            motor.comm.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Joint gantry/gripper/pipette/spin motor smoke test.")
    parser.add_argument("--mock", action="store_true", help="Run without opening serial ports.")
    parser.add_argument("--yes", action="store_true", help="Skip Enter confirmations. Use with care.")
    parser.add_argument("--skip-home", action="store_true", help="Do not run gantry home().")
    parser.add_argument("--move-dx", type=float, default=0.0, help="Small gantry relative X move in mm.")
    parser.add_argument("--move-dy", type=float, default=0.0, help="Small gantry relative Y move in mm.")
    parser.add_argument("--move-dz", type=float, default=1.0, help="Small gantry relative Z move in mm.")
    parser.add_argument("--feed", type=float, default=600.0, help="Gantry move feed in mm/min.")
    parser.add_argument(
        "--gripper-channel",
        type=int,
        default=get_gripper_channel(),
        help="Relay channel for gripper. Default comes from system_config.yaml.",
    )
    parser.add_argument("--spin-low-speed", action="store_true", help="Run a short low-speed spin test.")
    parser.add_argument("--spin-rpm", type=float, default=300.0, help="RPM for --spin-low-speed.")
    parser.add_argument("--spin-seconds", type=float, default=2.0, help="Duration for --spin-low-speed.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ports = get_configured_ports()
    LOG.info("SCRIPT_VERSION=%s", SCRIPT_VERSION)
    LOG.info(
        "Configured ports: gantry=%s relay=%s shared_rs485=%s",
        ports["gantry"],
        ports["relay"],
        ports["rs485"],
    )
    LOG.info(
        "Move offset: dX=%.3f dY=%.3f dZ=%.3f feed=%.0f; gripper CH%s",
        args.move_dx,
        args.move_dy,
        args.move_dz,
        args.feed,
        args.gripper_channel,
    )

    stage = None
    gripper = None

    try:
        confirm("Confirm all hardware is powered and the work area is clear.", assume_yes=args.yes)
        stage, _relay, gripper = connect_gantry_and_gripper(args)
        exercise_gripper(gripper, assume_yes=args.yes)
        exercise_gantry(stage, args)
        exercise_rs485_devices(args)
        LOG.info("Joint hardware smoke test finished.")
        return 0
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user. Attempting safe cleanup.")
        return 130
    except Exception as exc:
        LOG.error("Joint hardware smoke test failed: %s", exc)
        return 1
    finally:
        if stage is not None:
            try:
                stage.emergency_stop()
            except Exception:
                LOG.exception("Failed to emergency-stop gantry during cleanup.")
        if gripper is not None:
            try:
                LOG.info(
                    "cleanup gripper.open -> %s",
                    dump(gripper.open(idempotency_key=fresh_key("cleanup-open"))),
                )
            except Exception:
                LOG.exception("Failed to open gripper during cleanup.")
        if stage is not None:
            stage.close()


if __name__ == "__main__":
    raise SystemExit(main())
