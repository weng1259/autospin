"""Adapter for the verified AutoSpinmotorSystem AI-516 heater controller."""
from __future__ import annotations

import math
import time
from typing import Any, Protocol

from ..errors import ConnectionError as L3ConnectionError
from ..heater_backend import (
    HeaterActionResult,
    HeaterCommunicationError,
    HeaterSetpointOutOfRangeError,
    HeaterStatus,
)
from ..rs485_bus import Rs485Bus


class HeatingStageControllerLike(Protocol):
    def connect(self) -> bool: ...
    def close(self) -> None: ...
    def shutdown(self) -> None: ...
    def read_pv(self) -> float: ...
    def read_sv(self) -> float: ...
    def write_sv(self, temp_c: float) -> None: ...
    def run(self) -> None: ...
    def stop(self) -> bool: ...
    def get_status(self) -> dict[str, Any]: ...


class HeaterControllerAdapter:
    """Translate verified HeatingStageController behavior into L3 results."""

    def __init__(
        self,
        controller: HeatingStageControllerLike,
        *,
        bus: Rs485Bus | None = None,
        device_name: str = "heater",
        baudrate: int = 9600,
        timeout_s: float = 3.0,
        sv_max_c: float = 150.0,
    ) -> None:
        self._controller = controller
        self._bus = bus
        self._device_name = device_name
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._sv_max_c = sv_max_c
        self._connected = False
        self._last_pv_c: float | None = None
        self._last_sv_c: float | None = None

    @classmethod
    def from_verified_controller(
        cls,
        *,
        port: str,
        mock: bool = False,
        bus: Rs485Bus | None = None,
        slave_id: int = 3,
        baudrate: int = 9600,
        timeout_s: float = 3.0,
        sv_max_c: float = 150.0,
        pv_register: int = 74,
        sv_register: int = 0,
        srun_register: int = 27,
        run_on_sv_write: bool = True,
        scale: float = 10.0,
    ) -> HeaterControllerAdapter:
        """Construct from the bundled verified AutoSpinmotorSystem controller."""
        try:
            from ..drivers.heater.heating_stage_controller import (
                HeatingStageController,
            )
        except ImportError as exc:
            raise HeaterCommunicationError(
                human_message="无法导入已验证的加热台 HeatingStageController",
                agent_message=(
                    "Could not import src.hardware.drivers.heater."
                    f"heating_stage_controller.HeatingStageController: {exc!r}."
                ),
            ) from exc

        controller = HeatingStageController(
            port=port,
            mock=mock,
            slave_id=slave_id,
            baudrate=baudrate,
            timeout=timeout_s,
            pv_addr=pv_register,
            sv_addr=sv_register,
            srun_addr=srun_register,
            run_on_sv_write=run_on_sv_write,
            scale=scale,
        )
        return cls(
            controller,
            bus=bus,
            baudrate=baudrate,
            timeout_s=timeout_s,
            sv_max_c=sv_max_c,
        )

    def connect(self) -> None:
        with self._guard():
            try:
                ok = self._controller.connect()
            except Exception as exc:
                raise self._communication_error("connect", exc) from exc
        if not ok:
            raise HeaterCommunicationError(
                human_message="加热台连接失败",
                agent_message="Verified HeatingStageController.connect() returned False.",
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

    def read_pv(self) -> HeaterStatus:
        with self._guard():
            try:
                self._last_pv_c = float(self._controller.read_pv())
            except Exception as exc:
                raise self._communication_error("read_pv", exc) from exc
        return self.status()

    def set_sv(
        self,
        sv_c: float,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> HeaterActionResult:
        del idempotency_key
        self._validate_sv(sv_c)
        if dry_run:
            return HeaterActionResult(
                success=True,
                target_sv_c=sv_c,
                dry_run=True,
                action_description=(
                    "Dry-run HeatingStageController-backed AI-516 SV write; "
                    "no bytes sent."
                ),
                duration_ms=0.0,
                event_id="",
            )

        started = time.monotonic()
        with self._guard():
            try:
                self._controller.write_sv(sv_c)
            except Exception as exc:
                raise self._communication_error("set_sv", exc) from exc
        self._last_sv_c = sv_c
        return HeaterActionResult(
            success=True,
            target_sv_c=sv_c,
            dry_run=False,
            action_description=(
                "Set SV through verified HeatingStageController; controller "
                "preserves run_on_sv_write behavior."
            ),
            duration_ms=(time.monotonic() - started) * 1000.0,
            event_id="",
        )

    def status(self) -> HeaterStatus:
        try:
            raw = self._controller.get_status()
        except Exception as exc:
            raise self._communication_error("status", exc) from exc

        pv = raw.get("pv", self._last_pv_c)
        sv = raw.get("sv", self._last_sv_c)
        self._last_pv_c = float(pv) if pv is not None else None
        self._last_sv_c = float(sv) if sv is not None else None
        return HeaterStatus(
            connected=bool(raw.get("connected", self._connected)),
            pv_c=self._last_pv_c,
            sv_c=self._last_sv_c,
        )

    def stop(self) -> bool:
        with self._guard():
            try:
                return bool(self._controller.stop())
            except Exception as exc:
                raise self._communication_error("stop", exc) from exc

    def _validate_sv(self, sv_c: float) -> None:
        if not math.isfinite(sv_c) or sv_c < 0.0 or sv_c > self._sv_max_c:
            raise HeaterSetpointOutOfRangeError(
                human_message=f"加热台设定温度 {sv_c!r} °C 超出安全范围",
                agent_message=(
                    f"Requested heater SV {sv_c!r} °C is outside "
                    f"[0, {self._sv_max_c:g}]."
                ),
            )

    def _guard(self):
        if self._bus is None:
            from contextlib import nullcontext

            return nullcontext()
        return self._bus.guard(
            self._device_name,
            self._baudrate,
            timeout_s=self._timeout_s,
        )

    def _communication_error(self, action: str, exc: Exception) -> HeaterCommunicationError:
        return HeaterCommunicationError(
            human_message=f"加热台 {action} 通信失败",
            agent_message=(
                f"Verified HeatingStageController failed during {action}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
