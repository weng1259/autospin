"""XYZ gantry adapter backed by the grbl L3 backend.

This module keeps the public ``XYZStage`` shape used by the existing
``Maestro``/workers code, while delegating real motion control to the bundled
grbl L3 backend in ``hardware.xyz_stage.l3_backend``.

In ``mock=True`` mode no serial port is opened and all motion is simulated in
memory. In real mode the wrapped backend provides homing, jog based absolute
motion, soft-limit validation, Z-brake handling, alarm recovery, run logging,
and structured L3 errors.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

try:  # Support both package and direct-script execution.
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ModuleNotFoundError:  # pragma: no cover - exercised by local scripts
    from config.hardware_config import CONFIG


class XYZStage:
    """Compatibility wrapper for the grbl ``GantryBackend``.

    Existing project code expects:
    - ``connect() -> bool``
    - ``move_to(x, y, z) -> bool``
    - ``move_rel(dx, dy, dz) -> bool``
    - ``home() -> bool``
    - ``get_position() -> {"X": float, "Y": float, "Z": float}``
    - ``emergency_stop()`` / ``close()``

    The wrapped L3 backend uses pydantic ``Position`` objects and raises
    structured ``L3Error`` subclasses. This adapter logs those errors and
    returns ``False`` for the legacy boolean API.
    """

    def __init__(
        self,
        port: str | None = None,
        relay_port: str | None = None,
        mock: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.mock = mock
        self.port = port

        stage_cfg = CONFIG["devices"]["xyz_stage"]
        comm_cfg = CONFIG.get("communication", {})
        motion_cfg = stage_cfg.get("grbl_motion", {})

        self.baudrate = int(stage_cfg.get("baudrate", 115200))
        self.timeout = float(stage_cfg.get("timeout", 0.5))
        self.default_feed_mm_min = float(motion_cfg.get("default_feed_mm_min", 2000.0))
        self.max_feed_mm_min = float(motion_cfg.get("max_feed_mm_min", 3000.0))

        # The grbl stack uses millimetres directly. Keep this attribute for
        # callers that still inspect it, but do not use it for conversion.
        self.steps_per_mm = float(stage_cfg.get("steps_per_mm", 682.67))
        self.safe_z_mm = float(stage_cfg.get("safe_z_mm", -5.0))

        self.relay_port = relay_port or comm_cfg.get("relay_port", "COM5")

        self._connected = False
        self._is_initialized = False
        self._manual_mode = False
        self._backend: Any | None = None
        self._relay_backend: Any | None = None
        self._Position: Any | None = None
        self._L3Error: type[Exception] | None = None
        self._current_position = {"X": 0.0, "Y": 0.0, "Z": 0.0, "Z2": 0.0}

    # ------------------------------------------------------------------
    # Backend loading
    # ------------------------------------------------------------------

    def _load_grbl_backend_classes(self) -> tuple[Any, Any, Any, type[Exception]]:
        """Import the companion grbl backend lazily.

        Lazy loading keeps ``mock=True`` usable even on a machine where optional
        serial/hardware dependencies have not been installed yet.
        """
        from .l3_backend.hardware.errors import L3Error
        from .l3_backend.hardware.gantry_backend import GantryBackend
        from .l3_backend.hardware.relay_backend import RelayBackend
        from .l3_backend.hardware.types import Position

        return GantryBackend, RelayBackend, Position, L3Error

    def _make_position(self, x: float, y: float, z: float) -> Any:
        if self._Position is None:
            if self.mock:
                return {"x_mm": float(x), "y_mm": float(y), "z_mm": float(z)}
            _, _, self._Position, self._L3Error = self._load_grbl_backend_classes()
        return self._Position(x_mm=float(x), y_mm=float(y), z_mm=float(z))

    def _record_position(self, position: Any) -> None:
        if isinstance(position, dict):
            self._current_position = {
                "X": float(position["x_mm"]),
                "Y": float(position["y_mm"]),
                "Z": float(position["z_mm"]),
                "Z2": float(position.get("z2_mm", position.get("a_mm", 0.0))),
            }
            return
        self._current_position = {
            "X": float(position.x_mm),
            "Y": float(position.y_mm),
            "Z": float(position.z_mm),
            "Z2": float(getattr(position, "z2_mm", 0.0)),
        }

    def _log_l3_error(self, action: str, exc: Exception) -> None:
        code = getattr(exc, "error_code", type(exc).__name__)
        suggestion = getattr(exc, "suggested_action_zh", "") or getattr(
            exc, "suggested_action", ""
        )
        agent_message = getattr(exc, "agent_message", str(exc))
        self.logger.error("%s failed [%s]: %s", action, code, agent_message)
        if suggestion:
            self.logger.error("Suggested action: %s", suggestion)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        if self.mock:
            self._connected = True
            self._is_initialized = True
            self.logger.info("[MOCK] XYZStage grbl adapter connected (port=%s)", self.port)
            return True

        try:
            GantryBackend, RelayBackend, Position, L3Error = self._load_grbl_backend_classes()
            self._Position = Position
            self._L3Error = L3Error
            self._relay_backend = RelayBackend(port=self.relay_port)
            self._backend = GantryBackend(
                port=self.port or CONFIG.get("communication", {}).get("gantry_port", "COM6"),
                baud=self.baudrate,
                relay=self._relay_backend,
            )
            self.logger.info(
                "Connecting XYZ gantry through grbl backend: %s @ %s",
                self._backend.port,
                self.baudrate,
            )
            self._backend.connect()
            self._connected = True
            self._is_initialized = True
            self._record_position(self._backend.get_position())
            return True
        except Exception as exc:
            self._connected = False
            self._is_initialized = False
            self._backend = None
            self._log_l3_error("XYZStage.connect", exc)
            return False

    def disconnect(self) -> None:
        if self.mock:
            self._connected = False
            self.logger.info("[MOCK] XYZStage disconnected")
            return

        if self._backend is not None:
            try:
                self._backend.close()
            except Exception as exc:
                self._log_l3_error("XYZStage.disconnect", exc)
        self._backend = None
        self._relay_backend = None
        self._connected = False
        self._is_initialized = False

    def close(self) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Motion API
    # ------------------------------------------------------------------

    def home(self, *, dry_run: bool = False) -> bool:
        if self.mock:
            self.logger.info("[MOCK] XYZStage home(dry_run=%s)", dry_run)
            if not dry_run:
                self._current_position.update({"X": 0.0, "Y": 0.0, "Z": 0.0})
            return True

        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot home")
            return False

        try:
            result = self._backend.home(
                idempotency_key=f"xyz-home-{uuid.uuid4().hex}",
                dry_run=dry_run,
            )
            if not dry_run:
                self._record_position(result.position_after_pulloff)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.home", exc)
            return False

    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        *,
        feed_mm_min: float | None = None,
        dry_run: bool = False,
    ) -> bool:
        if self._manual_mode:
            self.logger.error("XYZStage is in manual mode; exit manual mode before moving")
            return False

        target = self._make_position(x, y, z)
        feed = feed_mm_min if feed_mm_min is not None else self.default_feed_mm_min

        if self.mock:
            self.logger.info(
                "[MOCK] XYZStage move_to X=%.3f Y=%.3f Z=%.3f F=%.0f dry_run=%s",
                x,
                y,
                z,
                feed,
                dry_run,
            )
            if not dry_run:
                self._record_position(target)
            return True

        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot move")
            return False

        try:
            result = self._backend.move_to(
                target,
                feed_mm_min=feed,
                dry_run=dry_run,
            )
            if not dry_run:
                self._record_position(result.final_position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.move_to", exc)
            return False

    def move_rel(
        self,
        dx: float,
        dy: float,
        dz: float,
        *,
        feed_mm_min: float | None = None,
        dry_run: bool = False,
    ) -> bool:
        return self.move_to(
            self._current_position["X"] + dx,
            self._current_position["Y"] + dy,
            self._current_position["Z"] + dz,
            feed_mm_min=feed_mm_min,
            dry_run=dry_run,
        )

    def manual_jog_rel(
        self,
        dx: float,
        dy: float,
        dz: float,
        *,
        feed_mm_min: float = 300.0,
    ) -> bool:
        if self._manual_mode:
            self.logger.error("XYZStage is in manual mode; exit manual mode before jogging")
            return False

        if self.mock:
            if any(abs(float(v)) > 20.0 for v in (dx, dy, dz)):
                self.logger.error("Manual jog delta out of range: %s", (dx, dy, dz))
                return False
            self._current_position["X"] += float(dx)
            self._current_position["Y"] += float(dy)
            self._current_position["Z"] += float(dz)
            self.logger.info("[MOCK] XYZStage manual_jog_rel dx=%.3f dy=%.3f dz=%.3f", dx, dy, dz)
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot manual jog")
            return False
        try:
            status = self._backend.manual_jog_rel(
                float(dx),
                float(dy),
                float(dz),
                feed_mm_min=float(feed_mm_min),
            )
            self._record_position(status.position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.manual_jog_rel", exc)
            return False

    def set_z_brake_released(self, released: bool) -> bool:
        if self.mock:
            self.logger.info("[MOCK] XYZStage set_z_brake_released=%s", released)
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot control Z brake")
            return False
        try:
            status = self._backend.set_z_brake_released(bool(released))
            self._record_position(status.position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.set_z_brake_released", exc)
            return False

    def enter_manual_mode(
        self,
        *,
        release_xy: bool = True,
        release_z: bool = False,
    ) -> bool:
        if release_z:
            self.logger.error("Manual mode cannot release Z/Z2 vertical axes")
            return False
        if self.mock:
            self._manual_mode = True
            self._is_initialized = False
            self.logger.info(
                "[MOCK] XYZStage enter_manual_mode release_xy=%s release_z=%s",
                release_xy,
                release_z,
            )
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot enter manual mode")
            return False
        try:
            status = self._backend.enter_manual_mode(
                release_xy=bool(release_xy),
                release_z=bool(release_z),
            )
            self._record_position(status.position)
            self._manual_mode = True
            self._is_initialized = False
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.enter_manual_mode", exc)
            return False

    def exit_manual_mode(self, *, rehome: bool = True) -> bool:
        if self.mock:
            self._manual_mode = False
            self._is_initialized = bool(rehome)
            self.logger.info("[MOCK] XYZStage exit_manual_mode rehome=%s", rehome)
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot exit manual mode")
            return False
        try:
            status = self._backend.exit_manual_mode(rehome=bool(rehome))
            self._record_position(status.position)
            self._manual_mode = False
            self._is_initialized = bool(rehome)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.exit_manual_mode", exc)
            return False

    def initialize_z2_at_top(self) -> bool:
        """Declare the current Z2 slide position as top safe position A0."""
        if self.mock:
            self._current_position["Z2"] = 0.0
            self.logger.info("[MOCK] XYZStage initialize_z2_at_top -> Z2=0")
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot initialize Z2")
            return False
        try:
            status = self._backend.initialize_z2_at_top()
            self._record_position(status.position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.initialize_z2_at_top", exc)
            return False

    def declare_z2_position(self, z2_mm: float) -> bool:
        """Declare the current physical Z2 position without moving the axis."""
        target = float(z2_mm)
        if self.mock:
            if not (0.0 <= target <= 125.0):
                self.logger.error("Z2 declaration out of range: %.3f", target)
                return False
            self._current_position["Z2"] = target
            self.logger.info("[MOCK] XYZStage declare_z2_position -> Z2=%.3f", target)
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot declare Z2 position")
            return False
        try:
            status = self._backend.declare_z2_position(target)
            self._record_position(status.position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.declare_z2_position", exc)
            return False

    def move_z2_to(
        self,
        z2_mm: float,
        *,
        feed_mm_min: float = 100.0,
        timeout_s: float = 30.0,
    ) -> bool:
        if self._manual_mode:
            self.logger.error("XYZStage is in manual mode; exit manual mode before moving Z2")
            return False

        if self.mock:
            if not (0.0 <= float(z2_mm) <= 125.0):
                self.logger.error("Z2 target out of range: %.3f", z2_mm)
                return False
            self._current_position["Z2"] = float(z2_mm)
            self.logger.info("[MOCK] XYZStage move_z2_to Z2=%.3f", z2_mm)
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot move Z2")
            return False
        try:
            status = self._backend.move_z2_to(
                float(z2_mm),
                feed_mm_min=float(feed_mm_min),
                timeout_s=float(timeout_s),
            )
            self._record_position(status.position)
            return True
        except Exception as exc:
            self._log_l3_error("XYZStage.move_z2_to", exc)
            return False

    def move_z2_rel(
        self,
        dz2_mm: float,
        *,
        feed_mm_min: float = 100.0,
        timeout_s: float = 30.0,
    ) -> bool:
        return self.move_z2_to(
            self._current_position["Z2"] + float(dz2_mm),
            feed_mm_min=feed_mm_min,
            timeout_s=timeout_s,
        )

    def park_z2(self) -> bool:
        return self.move_z2_to(0.0, feed_mm_min=100.0)

    def dry_run_move_to(
        self,
        x: float,
        y: float,
        z: float,
        *,
        feed_mm_min: float | None = None,
    ) -> Any:
        """Return the underlying L3 movement plan without touching hardware."""
        target = self._make_position(x, y, z)
        feed = feed_mm_min if feed_mm_min is not None else self.default_feed_mm_min
        if self.mock:
            return {
                "target": target,
                "feed_mm_min": feed,
                "current_position": self._current_position.copy(),
            }
        if self._backend is None:
            # GantryBackend dry-run does not require a live serial connection.
            GantryBackend, RelayBackend, Position, L3Error = self._load_grbl_backend_classes()
            self._Position = Position
            self._L3Error = L3Error
            backend = GantryBackend(port=self.port or "COM6")
            return backend.move_to(target, feed_mm_min=feed, dry_run=True)
        return self._backend.move_to(target, feed_mm_min=feed, dry_run=True)

    def get_position(self) -> Dict[str, float]:
        if self.mock or self._backend is None:
            return self._current_position.copy()

        try:
            self._record_position(self._backend.get_position())
        except Exception as exc:
            self._log_l3_error("XYZStage.get_position", exc)
        return self._current_position.copy()

    def get_status(self) -> Any:
        if self.mock:
            return {
                "state": "mock",
                "position": self._current_position.copy(),
                "is_homed": self._is_initialized,
                "limit_pins": [],
            }
        if self._backend is None:
            return None
        return self._backend.get_status()

    def get_homing_diagnostics(self) -> dict[str, Any]:
        if self.mock:
            return {
                "status": self.get_status(),
                "limit_pins": [],
                "settings": {
                    "$5": "mock",
                    "$22": "1",
                    "$23": "mock",
                    "$24": "mock",
                    "$25": "mock",
                    "$26": "mock",
                    "$27": "mock",
                },
                "checks": ["mock 模式不会读取真实 GRBL 参数。"],
            }
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot read homing diagnostics")
            return {
                "status": self.get_status(),
                "limit_pins": [],
                "settings": {},
                "checks": ["导轨未连接，无法读取 ? / $$。"],
            }
        return self._backend.get_homing_diagnostics()

    def is_connected(self) -> bool:
        if self.mock:
            return self._connected
        return bool(self._backend is not None and self._backend.is_connected())

    def is_homed(self) -> bool:
        if self.mock:
            return self._is_initialized
        return bool(self._backend is not None and self._backend.is_homed())

    def set_speed(self, speed: int) -> bool:
        """Legacy speed-level API.

        The grbl backend uses feed rates per move. This method keeps the old
        1-9 level contract by mapping it onto this adapter's default feed.
        """
        if speed < 1 or speed > 9:
            self.logger.error("Speed level out of range: %s (expected 1-9)", speed)
            return False
        ratio = speed / 9.0
        self.default_feed_mm_min = max(1.0, self.max_feed_mm_min * ratio)
        self.logger.info(
            "XYZStage default feed set to %.0f mm/min from speed level %s",
            self.default_feed_mm_min,
            speed,
        )
        return True

    def emergency_stop(self) -> None:
        self.logger.warning("Emergency stop requested for XYZ gantry")
        if self.mock:
            return
        if self._backend is None:
            return
        try:
            self._backend.abort_motion_immediate()
        except Exception as exc:
            self._log_l3_error("XYZStage.emergency_stop/abort", exc)
            try:
                self._backend.halt()
            except Exception as reset_exc:
                self._log_l3_error("XYZStage.emergency_stop/halt", reset_exc)

    def recover_from_alarm(self, *, skip_rehome: bool = False) -> bool:
        if self.mock:
            return True
        if self._backend is None:
            self.logger.error("XYZStage is not connected; cannot recover")
            return False
        try:
            result = self._backend.recover_from_alarm(
                idempotency_key=f"xyz-recover-{uuid.uuid4().hex}",
                skip_rehome=skip_rehome,
            )
            self._record_position(result.final_status.position)
            return bool(result.success)
        except Exception as exc:
            self._log_l3_error("XYZStage.recover_from_alarm", exc)
            return False

    def shutdown(self) -> None:
        if self.mock:
            self.disconnect()
            return
        self.disconnect()
