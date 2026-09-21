"""Adapter for the verified AutoSpinmotorSystem pipette controller."""
from __future__ import annotations

import math
import time
from typing import Any, Protocol

from ..errors import ConnectionError as L3ConnectionError
from ..pipette_backend import (
    PipetteActionResult,
    PipetteCommunicationError,
    PipetteNotHomedError,
    PipetteStatus,
    PipetteTipMissingError,
    PipetteVolumeOutOfRangeError,
)
from ..rs485_bus import Rs485Bus


class PipetteControllerLike(Protocol):
    max_volume: float

    def connect(self, initialize: bool = True) -> bool: ...
    def close(self) -> None: ...
    def shutdown(self) -> None: ...
    def home(self, timeout: float = 30) -> bool: ...
    def aspirate(self, volume: int, detect_mask: int = 0) -> bool: ...
    def dispense(self, volume: int) -> bool: ...
    def blowout(self) -> bool: ...
    def tip_eject(self) -> bool: ...
    def liquid_detect(self, timeout: float = 10) -> bool: ...
    def stop(self) -> bool: ...
    def reset(self) -> bool: ...
    def get_status(self, *, live: bool = False) -> dict[str, Any]: ...


class PipetteControllerAdapter:
    """Translate verified PipetteController behavior into autospin L3 models."""

    def __init__(
        self,
        controller: PipetteControllerLike,
        *,
        bus: Rs485Bus | None = None,
        device_name: str = "pipette",
        baudrate: int = 115200,
        timeout_s: float = 2.0,
        max_volume_ul: float = 1000.0,
    ) -> None:
        self._controller = controller
        self._bus = bus
        self._device_name = device_name
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._max_volume_ul = max_volume_ul
        self._connected = False

    @classmethod
    def from_verified_controller(
        cls,
        *,
        port: str,
        mock: bool = False,
        bus: Rs485Bus | None = None,
        slave_id: int = 1,
        baudrate: int = 115200,
        timeout_s: float = 2.0,
        max_volume_ul: float = 1000.0,
    ) -> PipetteControllerAdapter:
        try:
            from ..drivers.pipette.pipette_controller import (
                PipetteController,
            )
        except ImportError as exc:
            raise PipetteCommunicationError(
                human_message="无法导入已验证的移液枪 PipetteController",
                agent_message=(
                    "Could not import src.hardware.drivers.pipette."
                    f"pipette_controller.PipetteController: {exc!r}."
                ),
            ) from exc

        return cls(
            PipetteController(
                port=port,
                mock=mock,
                slave_id=slave_id,
                baudrate=baudrate,
                timeout=timeout_s,
                max_volume_ul=max_volume_ul,
            ),
            bus=bus,
            baudrate=baudrate,
            timeout_s=timeout_s,
            max_volume_ul=max_volume_ul,
        )

    def connect(self) -> None:
        with self._guard():
            try:
                ok = self._controller.connect(initialize=False)
            except TypeError:
                ok = self._controller.connect()
            except Exception as exc:
                raise self._communication_error("connect", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪连接失败",
                agent_message="Verified PipetteController.connect() returned False.",
            )
        self._connected = True

    def close(self) -> None:
        try:
            self._controller.close()
        finally:
            self._connected = False

    def shutdown(self) -> None:
        try:
            self._controller.shutdown()
        finally:
            self._connected = False

    def home(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if dry_run:
            return self._result("home", None, True, "Dry-run verified pipette home.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.home(timeout=30)
            except Exception as exc:
                raise self._communication_error("home", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪归位失败",
                agent_message="Verified PipetteController.home() returned False.",
            )
        return self._result(
            "home",
            None,
            False,
            "Homed through verified PipetteController.",
            started=started,
        )

    def aspirate(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        volume = self._validate_volume(volume_ul)
        if dry_run:
            return self._result("aspirate", volume_ul, True, "Dry-run verified pipette aspirate.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.aspirate(volume)
            except RuntimeError as exc:
                if "Tip" in str(exc) or "tip" in str(exc):
                    raise PipetteTipMissingError(
                        human_message="未检测到移液枪吸头",
                        agent_message=str(exc),
                    ) from exc
                raise self._communication_error("aspirate", exc) from exc
            except Exception as exc:
                raise self._communication_error("aspirate", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪吸液失败",
                agent_message="Verified PipetteController.aspirate() returned False.",
            )
        return self._result(
            "aspirate",
            volume_ul,
            False,
            "Aspirated through verified PipetteController.",
            started=started,
        )

    def dispense(
        self,
        volume_ul: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        volume = self._validate_volume(volume_ul)
        if dry_run:
            return self._result("dispense", volume_ul, True, "Dry-run verified pipette dispense.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.dispense(volume)
            except RuntimeError as exc:
                if "Tip" in str(exc) or "tip" in str(exc):
                    raise PipetteTipMissingError(
                        human_message="未检测到移液枪吸头",
                        agent_message=str(exc),
                    ) from exc
                raise self._communication_error("dispense", exc) from exc
            except Exception as exc:
                raise self._communication_error("dispense", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪吐液失败",
                agent_message="Verified PipetteController.dispense() returned False.",
            )
        return self._result(
            "dispense",
            volume_ul,
            False,
            "Dispensed through verified PipetteController.",
            started=started,
        )

    def blowout(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if dry_run:
            return self._result("blowout", None, True, "Dry-run verified pipette blowout.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.blowout()
            except Exception as exc:
                raise self._communication_error("blowout", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪吹出失败",
                agent_message="Verified PipetteController.blowout() returned False.",
            )
        return self._result("blowout", None, False, "Blowout through verified PipetteController.", started=started)

    def tip_eject(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if dry_run:
            return self._result("eject_tip", None, True, "Dry-run verified pipette tip eject.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.tip_eject()
            except Exception as exc:
                raise self._communication_error("tip_eject", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪退吸头失败",
                agent_message="Verified PipetteController.tip_eject() returned False.",
            )
        return self._result("eject_tip", None, False, "Tip ejected through verified PipetteController.", started=started)

    def liquid_detect(
        self,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> PipetteActionResult:
        del idempotency_key
        if dry_run:
            return self._result("liquid_detect", None, True, "Dry-run verified pipette liquid detect.")
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.liquid_detect()
            except Exception as exc:
                raise self._communication_error("liquid_detect", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪液面检测失败",
                agent_message="Verified PipetteController.liquid_detect() returned False.",
            )
        return self._result("liquid_detect", None, False, "Liquid detected through verified PipetteController.", started=started)

    def stop(self) -> PipetteActionResult:
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.stop()
            except Exception as exc:
                raise self._communication_error("stop", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪停止失败",
                agent_message="Verified PipetteController.stop() returned False.",
            )
        return self._result("stop", None, False, "Stopped through verified PipetteController.", started=started)

    def reset(self) -> PipetteActionResult:
        started = time.monotonic()
        with self._guard():
            try:
                ok = self._controller.reset()
            except Exception as exc:
                raise self._communication_error("reset", exc) from exc
        if not ok:
            raise PipetteCommunicationError(
                human_message="移液枪复位失败",
                agent_message="Verified PipetteController.reset() returned False.",
            )
        return self._result("reset", None, False, "Reset through verified PipetteController.", started=started)

    def status(self) -> PipetteStatus:
        try:
            raw = self._controller.get_status(live=False)
        except Exception as exc:
            raise self._communication_error("status", exc) from exc
        return PipetteStatus(
            connected=self._connected,
            homed=bool(raw.get("homed", raw.get("is_initialized", False))),
            status_word=raw.get("status_word"),
            driver_fault=raw.get("driver_fault"),
            position_steps=raw.get("position"),
            tip_present=raw.get("tip_present"),
        )

    def _validate_volume(self, volume_ul: float) -> int:
        if not math.isfinite(volume_ul) or volume_ul < 0 or volume_ul > self._max_volume_ul:
            raise PipetteVolumeOutOfRangeError(
                human_message=f"移液体积 {volume_ul!r} uL 超出安全范围",
                agent_message=(
                    f"Requested pipette volume {volume_ul!r} uL is outside "
                    f"[0, {self._max_volume_ul:g}]."
                ),
            )
        return int(round(volume_ul))

    def _guard(self):
        if self._bus is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._bus.guard(self._device_name, self._baudrate, timeout_s=self._timeout_s)

    def _communication_error(self, action: str, exc: Exception) -> PipetteCommunicationError:
        if isinstance(exc, PipetteNotHomedError | PipetteTipMissingError | L3ConnectionError):
            raise exc
        return PipetteCommunicationError(
            human_message=f"移液枪 {action} 通信失败",
            agent_message=(
                f"Verified PipetteController failed during {action}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    @staticmethod
    def _result(
        action: str,
        volume_ul: float | None,
        dry_run: bool,
        description: str,
        *,
        started: float | None = None,
    ) -> PipetteActionResult:
        return PipetteActionResult(
            success=True,
            action=action,  # type: ignore[arg-type]
            volume_ul=volume_ul,
            dry_run=dry_run,
            action_description=description,
            duration_ms=0.0 if started is None else (time.monotonic() - started) * 1000.0,
            event_id="",
        )
