"""Run an AutoSpinmotorSystem experiment from a JSON recipe.

Example:
    python example_experiment.py --mock --time-scale 0
    python example_experiment.py examples/one_round_full_spin_hotplate_cycle.json --mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows console only
    msvcrt = None


PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from autospin_system.config.hardware_config import CONFIG
from autospin_system.maestro import Maestro
from src.hardware.gripper_backend import GripperBackend


LOG = logging.getLogger("AutoSpinmotorSystem.ExperimentRunner")
TARGET_ALIASES = {
    "substrate_center": "spin_center",
    "spin_center": "spin_center",
    "clean_station": "clean_station",
    "waste_bin": "waste_bin",
    "home": "home",
}
XY_SAFE_Z_MM = -100.0
GRIPPER_SETTLE_SECONDS = 1.0
VACUUM_SETTLE_SECONDS = 0.5
EMERGENCY_KEYS = {"\x1b", "q", "Q"}


class EmergencyStopRequested(RuntimeError):
    """Raised when the operator requests a keyboard emergency stop."""


class KeyboardEmergencyStopMonitor:
    """Watch the Windows console for an emergency-stop keypress."""

    def __init__(self, runner: "ExperimentRunner", *, enabled: bool = True) -> None:
        self.runner = runner
        self.enabled = enabled and msvcrt is not None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "KeyboardEmergencyStopMonitor":
        if not self.enabled:
            if msvcrt is None:
                LOG.warning("Keyboard emergency stop unavailable: msvcrt is not available")
            return self
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="KeyboardEmergencyStopMonitor",
            daemon=True,
        )
        self._thread.start()
        LOG.warning("Emergency shortcut armed: press Esc or q to stop gantry motion immediately")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)

    def _watch_loop(self) -> None:
        assert msvcrt is not None
        while not self._stop_event.wait(0.05):
            if not msvcrt.kbhit():
                continue
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0") and msvcrt.kbhit():
                msvcrt.getwch()
                continue
            if key in EMERGENCY_KEYS:
                self.runner.request_emergency_stop("keyboard shortcut")
                return


class MockGripper:
    """Small gripper stand-in for JSON dry runs."""

    def __init__(self) -> None:
        self.closed = False

    def open(self, *, idempotency_key: str) -> dict[str, Any]:
        self.closed = False
        LOG.info("[MOCK] gripper open (%s)", idempotency_key)
        return {"success": True, "commanded_state_after": "open"}

    def close(self, *, idempotency_key: str) -> dict[str, Any]:
        self.closed = True
        LOG.info("[MOCK] gripper close (%s)", idempotency_key)
        return {"success": True, "commanded_state_after": "closed"}


class ExperimentRunner:
    """Small JSON-operation runner over the currently integrated controllers."""

    def __init__(self, maestro: Maestro, *, time_scale: float = 1.0) -> None:
        self.maestro = maestro
        self.time_scale = max(0.0, float(time_scale))
        self._gripper: GripperBackend | MockGripper | None = None
        self._mounted_tip_index: int | None = None
        self._emergency_stop_event = threading.Event()
        self._emergency_stop_lock = threading.Lock()
        self._emergency_stop_reason = ""

    def run(self, recipe: dict[str, Any]) -> None:
        operations = recipe.get("operations")
        if not isinstance(operations, list) or not operations:
            raise ValueError("Recipe must contain a non-empty operations list")

        name = recipe.get("experiment_name", "unnamed experiment")
        LOG.info("Starting recipe: %s", name)
        self.maestro.start_experiment()

        for index, item in enumerate(operations, start=1):
            self._raise_if_emergency_stop_requested()
            operation = item.get("operation")
            params = item.get("params", {})
            if not isinstance(params, dict):
                raise ValueError(f"Operation #{index} params must be an object")

            LOG.info("Operation %d/%d: %s", index, len(operations), operation)
            if operation == "DispenseLiquid":
                self.dispense_liquid(params)
            elif operation == "SpinCoat":
                self.spin_coat(params)
            elif operation == "Anneal":
                self.anneal(params)
            elif operation == "MoveGantry":
                self.move_gantry(params)
            elif operation == "MoveGantrySafe":
                self.move_gantry_safe(params)
            elif operation == "PipetteAspirate":
                self.pipette_aspirate(params)
            elif operation == "PipetteDispense":
                self.pipette_dispense(params)
            elif operation == "PipetteMountTip":
                self.pipette_mount_tip(params)
            elif operation == "SpinStart":
                self.spin_start(params)
            elif operation == "SpinSetSpeed":
                self.spin_set_speed(params)
            elif operation == "SpinStop":
                self.spin_stop()
            elif operation == "SetHotplateTemperature":
                self.set_hotplate_temperature(params)
            elif operation == "CheckHotplateTemperature":
                self.check_hotplate_temperature(params)
            elif operation == "AnnealWait":
                self.anneal_wait(params)
            elif operation == "GripperOpen":
                self.gripper_open()
            elif operation == "GripperClose":
                self.gripper_close()
            elif operation == "VacuumOn":
                self.vacuum_on()
            elif operation == "VacuumOff":
                self.vacuum_off()
            elif operation == "HomeGantry":
                self.home_gantry()
            elif operation == "ParkZ2":
                self.park_z2()
            elif operation == "ReadStatus":
                self.read_status(params)
            elif operation in ("Delay", "Wait"):
                self.sleep(float(params["time_s"]), label=operation)
            else:
                raise ValueError(f"Unsupported operation: {operation}")
            self._raise_if_emergency_stop_requested()

        LOG.info("Recipe complete: %s", name)

    def request_emergency_stop(self, reason: str) -> None:
        with self._emergency_stop_lock:
            if self._emergency_stop_event.is_set():
                return
            self._emergency_stop_reason = reason
            self._emergency_stop_event.set()

        LOG.critical("Emergency stop requested by %s", reason)
        if self.maestro.gantry:
            try:
                self.maestro.gantry.emergency_stop()
            except Exception:
                LOG.exception("Gantry emergency_stop failed")
        if self.maestro.spincoater:
            try:
                self.maestro.spincoater.stop(use_brake=True)
            except Exception:
                LOG.exception("Spincoater stop failed during emergency stop")
        try:
            self._open_gripper_for_emergency_stop()
        except Exception:
            LOG.exception("Gripper open failed during emergency stop")

    def _open_gripper_for_emergency_stop(self) -> None:
        gripper = self._get_gripper()
        get_state = getattr(gripper, "get_state", None)
        if callable(get_state):
            state = get_state()
            commanded_state = str(getattr(state, "commanded_state", "")).lower()
            if commanded_state.endswith("open"):
                LOG.info("Gripper is already open during emergency stop")
                return
        LOG.critical("Opening gripper during emergency stop")
        self._require(
            gripper.open(idempotency_key=self._fresh_key("emergency-gripper-open")),
            "emergency gripper open",
        )

    def _raise_if_emergency_stop_requested(self) -> None:
        if self._emergency_stop_event.is_set():
            reason = self._emergency_stop_reason or "operator request"
            raise EmergencyStopRequested(f"Emergency stop requested: {reason}")

    def dispense_liquid(self, params: dict[str, Any]) -> None:
        source = str(params.get("source", "current pipette position"))
        target = str(params["target"])
        volume = int(params["volume_uL"])

        LOG.info("DispenseLiquid: source=%s, target=%s, volume=%s uL", source, target, volume)
        self._aspirate_from_source(source, volume)
        self._move_pipette_to_target(target)
        self._require(self.maestro.liquidhandler.dispense(volume), "pipette dispense")

    def pipette_aspirate(self, params: dict[str, Any]) -> None:
        volume = int(params.get("volume_uL", 0))
        LOG.info("PipetteAspirate: volume=%s uL at current position", volume)
        self._require(self.maestro.liquidhandler.aspirate(volume), "pipette aspirate")

    def pipette_dispense(self, params: dict[str, Any]) -> None:
        volume = int(params["volume_uL"])
        LOG.info("PipetteDispense: volume=%s uL at current position", volume)
        self._require(self.maestro.liquidhandler.dispense(volume), "pipette dispense")

    def pipette_mount_tip(self, params: dict[str, Any]) -> None:
        tip_index = int(params["tip_index"])
        self._mounted_tip_index = tip_index
        LOG.info("PipetteMountTip: tip_index=%s (operator/template checkpoint only)", tip_index)
        tip_present = getattr(self.maestro.liquidhandler, "tip_present", None)
        if callable(tip_present):
            try:
                LOG.info("Pipette tip_present after mount checkpoint: %s", tip_present())
            except Exception as exc:
                LOG.warning("Pipette tip_present check failed after mount checkpoint: %s", exc)

    def spin_start(self, params: dict[str, Any]) -> None:
        direction = str(params.get("direction", "forward"))
        rpm = params.get("rpm")
        LOG.info("SpinStart: direction=%s rpm=%s", direction, rpm)
        self._require(self.maestro.spincoater.unlock(), "spin motor unlock")
        ok, message = self.maestro.spincoater.start(direction=direction)
        self._require(ok, f"spin motor start: {message}")
        if rpm is not None:
            self.spin_set_speed({"rpm": rpm})

    def spin_set_speed(self, params: dict[str, Any]) -> None:
        rpm = float(params["rpm"])
        LOG.info("SpinSetSpeed: %.0f rpm", rpm)
        ok, message = self.maestro.spincoater.set_speed(rpm)
        self._require(ok, f"spin motor set_speed: {message}")

    def spin_stop(self) -> None:
        LOG.info("SpinStop")
        self._require(self.maestro.spincoater.stop(use_brake=True), "spin motor stop")

    def spin_coat(self, params: dict[str, Any]) -> None:
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("SpinCoat params.steps must be a non-empty list")

        antisolvent = params.get("antisolvent")
        antisolvent_drop_at_s = None
        total_duration = sum(float(step["time_s"]) for step in steps)
        elapsed = 0.0

        prepare_during_spin = bool(antisolvent and antisolvent.get("prepare_during_spin", False))
        antisolvent_ready = False

        if antisolvent and not prepare_during_spin:
            volume = int(antisolvent["volume_uL"])
            source = str(antisolvent.get("source", "current pipette position"))
            drop_remaining = float(antisolvent["drop_at_remaining_time_s"])
            antisolvent_drop_at_s = max(0.0, total_duration - drop_remaining)
            LOG.info(
                "Preloading antisolvent: source=%s, volume=%s uL, drop_at=%.1f s",
                source,
                volume,
                antisolvent_drop_at_s,
            )
            self._aspirate_from_source(source, volume)
            antisolvent_ready = True
        elif antisolvent:
            drop_remaining = float(antisolvent["drop_at_remaining_time_s"])
            antisolvent_drop_at_s = max(0.0, total_duration - drop_remaining)
            LOG.info(
                "Antisolvent will be prepared during spin: source=%s, volume=%s uL, drop_at=%.1f s",
                antisolvent.get("source", "current pipette position"),
                antisolvent["volume_uL"],
                antisolvent_drop_at_s,
            )

        try:
            self.vacuum_on()
            self._require(self.maestro.spincoater.unlock(), "spin motor unlock")
            ok, message = self.maestro.spincoater.start(direction=params.get("direction", "forward"))
            self._require(ok, f"spin motor start: {message}")

            for step in steps:
                rpm = float(step["rpm"])
                duration = float(step["time_s"])
                LOG.info("Spin step: %.0f rpm for %.1f s", rpm, duration)
                ok, message = self.maestro.spincoater.set_speed(rpm)
                self._require(ok, f"spin motor set_speed: {message}")

                next_elapsed = elapsed + duration
                if antisolvent and prepare_during_spin and not antisolvent_ready:
                    self._aspirate_from_source(
                        str(antisolvent.get("source", "current pipette position")),
                        int(antisolvent["volume_uL"]),
                    )
                    antisolvent_ready = True

                if (
                    antisolvent
                    and antisolvent_drop_at_s is not None
                    and elapsed <= antisolvent_drop_at_s <= next_elapsed
                ):
                    before_drop = antisolvent_drop_at_s - elapsed
                    self.sleep(before_drop, label="Spin before antisolvent")
                    self._dispense_antisolvent(antisolvent)
                    self.sleep(next_elapsed - antisolvent_drop_at_s, label="Spin after antisolvent")
                else:
                    self.sleep(duration, label="Spin")
                elapsed = next_elapsed
        finally:
            self.maestro.spincoater.stop(use_brake=True)
            self.vacuum_off()

    def anneal(self, params: dict[str, Any]) -> None:
        temperature = float(params["temperature_C"])
        duration_s = float(params.get("time_s", float(params.get("time_min", 0)) * 60.0))
        hotplate_name = str(params.get("hotplate", "Hotplate1"))
        hotplate = self._get_hotplate(hotplate_name)

        LOG.info("Anneal: hotplate=%s, temperature=%.1f C, duration=%.1f s", hotplate_name, temperature, duration_s)
        hotplate.write_sv(temperature)

        poll_interval = float(params.get("poll_interval_s", 30.0))
        remaining = duration_s
        while remaining > 0:
            try:
                LOG.info("%s PV = %.1f C", hotplate_name, hotplate.read_pv())
            except Exception as exc:
                LOG.warning("%s PV read failed: %s", hotplate_name, exc)
            chunk = min(poll_interval, remaining)
            self.sleep(chunk, label="Anneal")
            remaining -= chunk

    def set_hotplate_temperature(self, params: dict[str, Any]) -> None:
        temperature = float(params["temperature_C"])
        hotplate_name = str(params.get("hotplate", "Hotplate1"))
        hotplate = self._get_hotplate(hotplate_name)
        LOG.info("SetHotplateTemperature: hotplate=%s target=%.1f C", hotplate_name, temperature)
        hotplate.write_sv(temperature)

    def check_hotplate_temperature(self, params: dict[str, Any]) -> None:
        target = float(params["target_C"])
        tolerance = float(params.get("tolerance_C", 2.0))
        timeout_s = float(params.get("timeout_s", 300.0))
        poll_interval = float(params.get("poll_interval_s", 5.0))
        hotplate_name = str(params.get("hotplate", "Hotplate1"))
        hotplate = self._get_hotplate(hotplate_name)

        if self.maestro.mock:
            LOG.info(
                "[MOCK] CheckHotplateTemperature: hotplate=%s target=%.1f C tolerance=%.1f C",
                hotplate_name,
                target,
                tolerance,
            )
            return

        deadline = time.time() + max(0.0, timeout_s) * self.time_scale
        while True:
            self._raise_if_emergency_stop_requested()
            pv = float(hotplate.read_pv())
            LOG.info(
                "CheckHotplateTemperature: hotplate=%s PV=%.1f C target=%.1f C tolerance=%.1f C",
                hotplate_name,
                pv,
                target,
                tolerance,
            )
            if abs(pv - target) <= tolerance:
                return
            if time.time() >= deadline:
                raise TimeoutError(
                    f"{hotplate_name} did not reach {target:.1f} +/- {tolerance:.1f} C "
                    f"within {timeout_s:.1f} s; last PV={pv:.1f} C"
                )
            self.sleep(min(poll_interval, max(0.0, deadline - time.time())), label="Hotplate check")

    def anneal_wait(self, params: dict[str, Any]) -> None:
        self.sleep(float(params["time_s"]), label="AnnealWait")

    def move_gantry(self, params: dict[str, Any]) -> None:
        target = params.get("target")
        if target:
            self._move_stage_to_named_target(str(target))
            return
        self._safe_move(float(params["x"]), float(params["y"]), float(params["z"]))

    def move_gantry_safe(self, params: dict[str, Any]) -> None:
        self._safe_move(
            float(params["x"]),
            float(params["y"]),
            float(params["z"]),
            safe_z=float(params.get("safe_z", XY_SAFE_Z_MM)),
        )

    def gripper_open(self) -> None:
        LOG.info("GripperOpen")
        self._require(
            self._get_gripper().open(idempotency_key=self._fresh_key("recipe-gripper-open")),
            "gripper open",
        )
        self.sleep(GRIPPER_SETTLE_SECONDS, label="Gripper open settle")

    def gripper_close(self) -> None:
        LOG.info("GripperClose")
        self._require(
            self._get_gripper().close(idempotency_key=self._fresh_key("recipe-gripper-close")),
            "gripper close",
        )
        self.sleep(GRIPPER_SETTLE_SECONDS, label="Gripper close settle")

    def vacuum_on(self) -> None:
        LOG.info("VacuumOn")
        self._set_vacuum_valve(True)
        self.sleep(VACUUM_SETTLE_SECONDS, label="Vacuum settle")

    def vacuum_off(self) -> None:
        LOG.info("VacuumOff")
        self._set_vacuum_valve(False)

    def home_gantry(self) -> None:
        if not self.maestro.gantry:
            raise RuntimeError("Gantry is not initialized")
        ok = self.maestro.gantry.home()
        if not ok:
            try:
                LOG.error("Gantry status after failed home: %s", self.maestro.gantry.get_status())
                diagnostics = self.maestro.gantry.get_homing_diagnostics()
                LOG.error("Gantry homing diagnostics: %s", diagnostics)
            except Exception as exc:
                LOG.exception("Failed to collect gantry homing diagnostics: %s", exc)
        self._require(ok, "gantry home")

    def park_z2(self) -> None:
        z2 = self.maestro.z2_stage
        if z2 is None or not hasattr(z2, "park_z2"):
            LOG.info("ParkZ2 skipped: Z2 stage is disabled or not initialized")
            return
        self._require(z2.park_z2(), "park Z2")

    def read_status(self, params: dict[str, Any]) -> None:
        device = str(params.get("device", "all"))
        if device in ("all", "gantry") and self.maestro.gantry:
            LOG.info("Gantry status: %s", self.maestro.gantry.get_status())
        if device in ("all", "spincoater") and self.maestro.spincoater:
            LOG.info("Spincoater status: %s", self.maestro.spincoater.get_status())
        if device in ("all", "pipette") and self.maestro.liquidhandler:
            LOG.info("Pipette status: %s", self.maestro.liquidhandler.get_status())
        if device in ("all", "heating_stage", "hotplate"):
            hotplate = self.maestro.hotplates.get("Hotplate1") or self.maestro.hotplate
            if hotplate:
                LOG.info("Heating stage status: %s", hotplate.get_status())

    def _dispense_antisolvent(self, params: dict[str, Any]) -> None:
        volume = int(params["volume_uL"])
        target = str(params.get("target", "substrate_center"))
        LOG.info("Dispensing antisolvent: target=%s, volume=%s uL", target, volume)
        self._move_pipette_to_target(target)
        self._require(self.maestro.liquidhandler.dispense(volume), "antisolvent dispense")

    def _aspirate_from_source(self, source: str, volume: int) -> None:
        if self._has_named_target(source):
            self._move_pipette_to_target(source)
        else:
            LOG.info("Source %r is not a configured coordinate; aspirating at current pipette position", source)
        self._require(self.maestro.liquidhandler.aspirate(volume), "pipette aspirate")

    def _move_pipette_to_target(self, target: str) -> None:
        coord_name = self._resolve_target(target)
        lab_x, lab_y, lab_z = CONFIG["geometry"]["lab_coordinates"][coord_name]
        offset = CONFIG["geometry"]["tool_offsets"]["pipette"]
        tip_length = float(offset.get("tip_length", 0.0))
        real_x = float(lab_x) - float(offset["x"])
        real_y = float(lab_y) - float(offset["y"])
        real_z = float(lab_z) - float(offset["z"]) - tip_length
        self._safe_move(real_x, real_y, real_z)

    def _move_stage_to_named_target(self, target: str) -> None:
        coord_name = self._resolve_target(target)
        x, y, z = CONFIG["geometry"]["lab_coordinates"][coord_name]
        self._safe_move(float(x), float(y), float(z))

    def _safe_move(self, x: float, y: float, z: float, *, safe_z: float | None = None) -> None:
        if not self.maestro.gantry:
            raise RuntimeError("Gantry is not initialized")
        current = self.maestro.gantry.get_position()
        xy_changes = abs(x - current["X"]) > 1e-6 or abs(y - current["Y"]) > 1e-6
        motion_z = current["Z"]
        safe_z_target = XY_SAFE_Z_MM if safe_z is None else safe_z

        if xy_changes and safe_z is not None and abs(current["Z"] - safe_z_target) > 1e-6:
            LOG.info(
                "MoveGantrySafe: moving Z from %.3f to safe Z %.3f before XY",
                current["Z"],
                safe_z_target,
            )
            self._require(
                self.maestro.gantry.move_to(current["X"], current["Y"], safe_z_target),
                "gantry move to explicit safe Z before XY",
            )
            motion_z = safe_z_target
        elif xy_changes and current["Z"] < safe_z_target:
            LOG.info(
                "Current Z %.3f is below %.3f; lift to safe Z %.3f before XY",
                current["Z"],
                safe_z_target,
                safe_z_target,
            )
            self._require(
                self.maestro.gantry.move_to(current["X"], current["Y"], safe_z_target),
                "gantry lift to safe Z before XY",
            )
            motion_z = safe_z_target

        if xy_changes:
            self._require(self.maestro.gantry.move_to(x, y, motion_z), "gantry XY move")
        if abs(z - motion_z) > 1e-6:
            self._require(self.maestro.gantry.move_to(x, y, z), "gantry Z move")

    def _get_hotplate(self, hotplate_name: str = "Hotplate1") -> Any:
        hotplate = self.maestro.hotplates.get(hotplate_name)
        if hotplate is None:
            raise RuntimeError(f"Hotplate not found: {hotplate_name}")
        return hotplate

    def _get_gripper(self) -> GripperBackend | MockGripper:
        if self._gripper is not None:
            return self._gripper
        if self.maestro.mock:
            self._gripper = MockGripper()
            return self._gripper
        if not self.maestro.gantry:
            raise RuntimeError("Gantry is not initialized; cannot share relay with gripper")
        relay = getattr(self.maestro.gantry, "_relay_backend", None)
        if relay is None:
            raise RuntimeError("Gantry relay backend is not available for gripper control")
        if not relay.is_connected():
            relay.connect()
        channel = int(CONFIG["devices"]["relay"]["channels"].get("gripper", 1))
        self._gripper = GripperBackend(relay, channel=channel)
        return self._gripper

    def _get_shared_relay(self) -> Any:
        if not self.maestro.gantry:
            raise RuntimeError("Gantry is not initialized; cannot share relay for vacuum valve")
        relay = getattr(self.maestro.gantry, "_relay_backend", None)
        if relay is None:
            raise RuntimeError("Gantry relay backend is not available for vacuum valve control")
        if not relay.is_connected():
            relay.connect()
        return relay

    def _set_vacuum_valve(self, opened: bool) -> None:
        channel = int(CONFIG["devices"]["relay"]["channels"].get("vacuum_valve", 3))
        if self.maestro.mock:
            LOG.info("[MOCK] vacuum valve CH%s %s", channel, "ON" if opened else "OFF")
            return
        relay = self._get_shared_relay()
        key = self._fresh_key("vacuum-valve-on" if opened else "vacuum-valve-off")
        if opened:
            self._require(relay.ch_on(channel, idempotency_key=key), "vacuum valve open")
        else:
            self._require(relay.ch_off(channel, idempotency_key=key), "vacuum valve close")

    @staticmethod
    def _fresh_key(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def _has_named_target(self, name: str) -> bool:
        resolved = TARGET_ALIASES.get(name, name)
        return resolved in CONFIG["geometry"]["lab_coordinates"]

    def _resolve_target(self, name: str) -> str:
        resolved = TARGET_ALIASES.get(name, name)
        if resolved not in CONFIG["geometry"]["lab_coordinates"]:
            available = ", ".join(sorted(CONFIG["geometry"]["lab_coordinates"]))
            raise ValueError(f"Unknown coordinate target {name!r}. Available: {available}")
        return resolved

    def sleep(self, seconds: float, *, label: str) -> None:
        scaled = max(0.0, seconds) * self.time_scale
        LOG.info("%s wait: %.1f s (scaled to %.1f s)", label, seconds, scaled)
        deadline = time.time() + scaled
        while time.time() < deadline:
            self._raise_if_emergency_stop_requested()
            time.sleep(min(0.1, deadline - time.time()))
        self._raise_if_emergency_stop_requested()

    @staticmethod
    def _require(ok: Any, action: str) -> None:
        if not ok:
            raise RuntimeError(f"Failed action: {action}")


def load_recipe(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        recipe = json.load(file)
    if not isinstance(recipe, dict):
        raise ValueError("Recipe JSON root must be an object")
    return recipe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an experiment recipe JSON file.")
    default_recipe = Path(__file__).resolve().parent / "examples" / "one_round_full_spin_hotplate_cycle.json"
    parser.add_argument(
        "recipe",
        nargs="?",
        type=Path,
        default=default_recipe,
        help=f"Path to the experiment JSON file. Default: {default_recipe}",
    )
    parser.add_argument("--mock", action="store_true", help="Run all hardware controllers in mock mode")
    parser.add_argument("--no-gantry", action="store_true", help="Skip gantry initialization")
    parser.add_argument("--use-z2", action="store_true", help="Enable Z2/A-axis initialization and park operations")
    parser.add_argument("--no-keyboard-stop", action="store_true", help="Disable Esc/q keyboard emergency stop")
    parser.add_argument("--time-scale", type=float, default=1.0, help="Scale waits; use 0 for a fast dry software pass")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    recipe = load_recipe(args.recipe)
    maestro = Maestro(
        use_gantry=not args.no_gantry,
        mock=args.mock,
        use_z2=args.use_z2,
        use_standalone_relay=False,
        logger=LOG,
    )
    runner = ExperimentRunner(maestro, time_scale=args.time_scale)

    try:
        with KeyboardEmergencyStopMonitor(runner, enabled=not args.no_keyboard_stop):
            runner.run(recipe)
    except EmergencyStopRequested as exc:
        LOG.critical("%s", exc)
    except Exception:
        LOG.exception("Experiment failed")
        raise
    finally:
        maestro.shutdown()


if __name__ == "__main__":
    main()
