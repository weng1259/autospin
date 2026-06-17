"""Local web control server for the XYZ gantry.

Run from the project root:

    python web_control/server.py

Then open http://127.0.0.1:8765/

The server intentionally uses only the Python standard library plus the
project's existing hardware dependencies. It exposes a small JSON API used by
``web_control/index.html`` and delegates all hardware behavior to
``hardware.xyz_stage.XYZStage``.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autospin_system.hardware.xyz_stage.xyz_stage import XYZStage
from src.hardware.errors import L3Error
from autospin_system.hardware.pipette.pipette_controller import PipetteController
from autospin_system.config.hardware_config import CONFIG


LOG = logging.getLogger("xyz-web-control")
POSITION_RECORD_PATH = STATIC_ROOT / "position_records.json"
Z2_MIN_MM = 0.0
Z2_MAX_MM = 125.0
Z2_PRESET_MM = (0.0, 25.0, 50.0, 75.0, 100.0, 125.0)
Z2_STEP_MM = (-50.0, -25.0, 25.0, 50.0)


class GantrySession:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._manual_jog_lock = threading.Lock()
        self._operation_lock = threading.RLock()
        self._operation: dict[str, Any] | None = None
        self.stage: XYZStage | None = None
        self.pipette: PipetteController | None = None
        self.mock = False
        self.gantry_port: str | None = None
        self.relay_port: str | None = None
        self.pipette_port: str | None = None
        self.pipette_error: str | None = None
        self._position_records: list[dict[str, Any]] = []
        self._position_records_persisted = True

    def connect(
        self,
        gantry_port: str,
        relay_port: str,
        pipette_port: str | None = None,
        mock: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self.close()
            self.mock = mock
            self.gantry_port = gantry_port
            self.relay_port = relay_port
            self.pipette_port = pipette_port
            self.pipette_error = None
            stage = XYZStage(
                port=gantry_port,
                relay_port=relay_port,
                mock=mock,
                logger=LOG,
            )
            ok = stage.connect()
            if not ok:
                self.stage = None
                return {"ok": False, "connected": False, "error": "connect failed"}
            self.stage = stage
            if pipette_port:
                pipette = PipetteController(port=pipette_port, mock=mock, logger=LOG)
                try:
                    if pipette.connect():
                        self.pipette = pipette
                    else:
                        self.pipette_error = "pipette connect failed"
                        pipette.close()
                except Exception as exc:
                    self.pipette_error = f"{type(exc).__name__}: {exc}"
                    LOG.exception("Pipette connection failed")
                    try:
                        pipette.close()
                    except Exception:
                        LOG.exception("Failed to close pipette after connection error")
            return {"ok": True, "connected": True, "status": self.status()}

    def close(self) -> None:
        if self.pipette is not None:
            try:
                self.pipette.close()
            except Exception:
                LOG.exception("Failed to close pipette")
        self.pipette = None
        if self.stage is not None:
            try:
                self.stage.close()
            except Exception:
                LOG.exception("Failed to close gantry stage")
        self.stage = None

    def require_stage(self) -> XYZStage:
        if self.stage is None:
            raise RuntimeError("gantry is not connected")
        return self.stage

    def require_pipette(self) -> PipetteController:
        if self.pipette is None:
            raise RuntimeError(self.pipette_error or "pipette is not connected")
        return self.pipette

    def _pipette_status(self) -> dict[str, Any]:
        if self.pipette is None:
            return {
                "connected": False,
                "port": self.pipette_port,
                "error": self.pipette_error,
            }
        try:
            return {
                "connected": True,
                "port": self.pipette_port,
                "error": None,
                **self.pipette.get_status(),
            }
        except Exception as exc:
            self.pipette_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("Pipette status poll failed")
            return {
                "connected": False,
                "port": self.pipette_port,
                "error": self.pipette_error,
            }

    def _operation_snapshot(self) -> dict[str, Any] | None:
        with self._operation_lock:
            return dict(self._operation) if self._operation is not None else None

    def _set_operation(self, operation: dict[str, Any] | None) -> None:
        with self._operation_lock:
            self._operation = operation

    def _abort_operation_snapshot(self) -> None:
        with self._operation_lock:
            if self._operation is not None and self._operation.get("running"):
                self._operation.update({
                    "running": False,
                    "ok": False,
                    "error": "aborted by emergency stop",
                    "type": "EmergencyStop",
                })

    def _start_operation(self, name: str, target: Any) -> dict[str, Any]:
        with self._operation_lock:
            if self._operation is not None and self._operation.get("running"):
                return {
                    "ok": False,
                    "accepted": False,
                    "error": f"{self._operation.get('name')} is already running",
                    "operation": dict(self._operation),
                }
            operation = {"name": name, "running": True, "ok": None, "error": None}
            self._operation = operation

        def runner() -> None:
            try:
                ok = bool(target())
                with self._operation_lock:
                    if self._operation is not None:
                        self._operation.update({
                            "running": False,
                            "ok": ok,
                            "error": None if ok else f"{name} returned False",
                        })
                LOG.info("Background operation finished: %s ok=%s", name, ok)
            except Exception as exc:
                LOG.exception("Background operation failed: %s", name)
                with self._operation_lock:
                    if self._operation is not None:
                        self._operation.update({
                            "running": False,
                            "ok": False,
                            "error": str(exc),
                            "type": type(exc).__name__,
                        })

        threading.Thread(target=runner, name=f"xyz-{name}", daemon=True).start()
        return {"ok": True, "accepted": True, "operation": operation}

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.stage is None:
                return {
                    "connected": False,
                    "mock": self.mock,
                    "gantry_port": self.gantry_port,
                    "relay_port": self.relay_port,
                    "pipette": self._pipette_status(),
                    "operation": self._operation_snapshot(),
                }
            try:
                raw = self.stage.get_status()
                pos = self.stage.get_position()
            except Exception as exc:
                LOG.error("Status poll failed; marking gantry disconnected: %s", exc)
                try:
                    self.stage.close()
                except Exception:
                    LOG.exception("Failed to close gantry after status error")
                self.stage = None
                return {
                    "connected": False,
                    "mock": self.mock,
                    "gantry_port": self.gantry_port,
                    "relay_port": self.relay_port,
                    "pipette": self._pipette_status(),
                    "state": "disconnected",
                    "position": {"x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0, "z2_mm": 0.0},
                    "is_homed": False,
                    "limit_pins": [],
                    "alarm_code": None,
                    "raw": f"status error: {type(exc).__name__}: {exc}",
                    "operation": self._operation_snapshot(),
                }
            if hasattr(raw, "model_dump"):
                data = raw.model_dump()
                state = data.get("state")
                if hasattr(state, "value"):
                    data["state"] = state.value
                if "position" in data and hasattr(data["position"], "model_dump"):
                    data["position"] = data["position"].model_dump()
                return {
                    "connected": self.stage.is_connected(),
                    "mock": self.mock,
                    "gantry_port": self.gantry_port,
                    "relay_port": self.relay_port,
                    "pipette": self._pipette_status(),
                    "state": data.get("state"),
                    "position": data.get("position", {
                        "x_mm": pos["X"],
                        "y_mm": pos["Y"],
                        "z_mm": pos["Z"],
                    }),
                    "is_homed": data.get("is_homed"),
                    "limit_pins": data.get("limit_pins", []),
                    "alarm_code": data.get("alarm_code"),
                    "raw": data.get("raw", ""),
                    "last_update_ms_ago": data.get("last_update_ms_ago"),
                    "operation": self._operation_snapshot(),
                }
            return {
                "connected": self.stage.is_connected(),
                "mock": self.mock,
                "gantry_port": self.gantry_port,
                "relay_port": self.relay_port,
                "pipette": self._pipette_status(),
                "state": raw.get("state", "unknown") if isinstance(raw, dict) else "unknown",
                "position": {"x_mm": pos["X"], "y_mm": pos["Y"], "z_mm": pos["Z"]},
                "is_homed": self.stage.is_homed(),
                "limit_pins": [],
                "alarm_code": None,
                "raw": "",
                "operation": self._operation_snapshot(),
            }

    def home(self) -> dict[str, Any]:
        stage = self.require_stage()
        payload = self._start_operation("home", stage.home)
        payload["status"] = self.status()
        return payload

    def move_abs(self, x: float, y: float, z: float, feed: float) -> dict[str, Any]:
        if self._operation_snapshot() and self._operation_snapshot().get("running"):
            return {"ok": False, "error": "another operation is running", "status": self.status()}
        with self._lock:
            ok = self.require_stage().move_to(x, y, z, feed_mm_min=feed)
            return {"ok": ok, "status": self.status()}

    def move_rel(self, dx: float, dy: float, dz: float, feed: float) -> dict[str, Any]:
        if self._operation_snapshot() and self._operation_snapshot().get("running"):
            return {"ok": False, "error": "another operation is running", "status": self.status()}
        with self._lock:
            ok = self.require_stage().move_rel(dx, dy, dz, feed_mm_min=feed)
            return {"ok": ok, "status": self.status()}

    def manual_jog_rel(self, dx: float, dy: float, dz: float, feed: float) -> dict[str, Any]:
        if not self._manual_jog_lock.acquire(blocking=False):
            return {"ok": False, "error": "manual jog is already running", "status": self.status()}
        try:
            with self._lock:
                if self._operation_snapshot() and self._operation_snapshot().get("running"):
                    self.require_stage().emergency_stop()
                    self._abort_operation_snapshot()
                safe_feed = min(max(float(feed), 1.0), 600.0)
                ok = self.require_stage().manual_jog_rel(
                    dx,
                    dy,
                    dz,
                    feed_mm_min=safe_feed,
                )
                return {"ok": ok, "status": self.status()}
        finally:
            self._manual_jog_lock.release()

    def dry_run(self, x: float, y: float, z: float, feed: float) -> dict[str, Any]:
        with self._lock:
            plan = self.require_stage().dry_run_move_to(x, y, z, feed_mm_min=feed)
            return {"ok": True, "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan}

    def dry_run_rel(self, dx: float, dy: float, dz: float, feed: float) -> dict[str, Any]:
        with self._lock:
            pos = self.require_stage().get_position()
            target = {
                "x": float(pos["X"]) + float(dx),
                "y": float(pos["Y"]) + float(dy),
                "z": float(pos["Z"]) + float(dz),
            }
            plan = self.require_stage().dry_run_move_to(
                target["x"],
                target["y"],
                target["z"],
                feed_mm_min=feed,
            )
            payload = plan.model_dump() if hasattr(plan, "model_dump") else plan
            return {"ok": True, "target": target, "plan": payload}

    def halt(self) -> dict[str, Any]:
        with self._lock:
            self.require_stage().emergency_stop()
            self._abort_operation_snapshot()
            return {"ok": True, "status": self.status()}

    def recover(self) -> dict[str, Any]:
        with self._lock:
            ok = self.require_stage().recover_from_alarm()
            return {"ok": ok, "status": self.status()}

    def homing_diagnostics(self) -> dict[str, Any]:
        with self._lock:
            diag = self.require_stage().get_homing_diagnostics()
            status = diag.get("status")
            if hasattr(status, "model_dump"):
                diag["status"] = status.model_dump()
            return {"ok": True, "diagnostics": diag, "status": self.status()}

    def initialize_z2_at_top(self) -> dict[str, Any]:
        with self._lock:
            ok = self.require_stage().initialize_z2_at_top()
            return {"ok": bool(ok), "status": self.status()}

    def declare_z2_preset(self, z2_mm: float) -> dict[str, Any]:
        target = float(z2_mm)
        if not (Z2_MIN_MM <= target <= Z2_MAX_MM):
            return {
                "ok": False,
                "error": f"Z2 declaration must be within [{Z2_MIN_MM}, {Z2_MAX_MM}] mm",
                "status": self.status(),
            }
        with self._lock:
            ok = self.require_stage().declare_z2_position(target)
            return {"ok": bool(ok), "target": target, "status": self.status()}

    def move_z2_preset(self, z2_mm: float, feed: float) -> dict[str, Any]:
        target = float(z2_mm)
        if target not in Z2_PRESET_MM:
            return {
                "ok": False,
                "error": f"Z2 target must be one of {list(Z2_PRESET_MM)}",
                "status": self.status(),
            }
        if self._operation_snapshot() and self._operation_snapshot().get("running"):
            return {"ok": False, "error": "another operation is running", "status": self.status()}
        with self._lock:
            safe_feed = min(max(float(feed), 1.0), 300.0)
            ok = self.require_stage().move_z2_to(target, feed_mm_min=safe_feed)
            return {"ok": bool(ok), "target": target, "status": self.status()}

    def move_z2_step(self, dz2_mm: float, feed: float) -> dict[str, Any]:
        delta = float(dz2_mm)
        if delta not in Z2_STEP_MM:
            return {
                "ok": False,
                "error": f"Z2 step must be one of {list(Z2_STEP_MM)}",
                "status": self.status(),
            }
        if self._operation_snapshot() and self._operation_snapshot().get("running"):
            return {"ok": False, "error": "another operation is running", "status": self.status()}
        with self._lock:
            safe_feed = min(max(float(feed), 1.0), 300.0)
            ok = self.require_stage().move_z2_rel(delta, feed_mm_min=safe_feed)
            return {"ok": bool(ok), "delta": delta, "status": self.status()}

    def set_z_brake_released(self, released: bool) -> dict[str, Any]:
        with self._lock:
            ok = self.require_stage().set_z_brake_released(released)
            return {"ok": bool(ok), "released": bool(released), "status": self.status()}

    def aspirate(self, volume_ul: float, detect_mask: int = 0) -> dict[str, Any]:
        volume = int(round(float(volume_ul)))
        if volume <= 0:
            return {"ok": False, "error": "aspirate volume must be greater than 0 uL", "status": self.status()}
        with self._lock:
            pipette = self.require_pipette()
            if volume > int(pipette.max_volume):
                return {
                    "ok": False,
                    "error": f"aspirate volume must be <= {pipette.max_volume} uL",
                    "status": self.status(),
                }
            ok = pipette.aspirate(volume, detect_mask=int(detect_mask))
            return {"ok": bool(ok), "volume_ul": volume, "status": self.status()}

    def dispense(self, volume_ul: float) -> dict[str, Any]:
        volume = int(round(float(volume_ul)))
        if volume <= 0:
            return {"ok": False, "error": "dispense volume must be greater than 0 uL", "status": self.status()}
        with self._lock:
            pipette = self.require_pipette()
            if volume > int(pipette.max_volume):
                return {
                    "ok": False,
                    "error": f"dispense volume must be <= {pipette.max_volume} uL",
                    "status": self.status(),
                }
            ok = pipette.dispense(volume)
            return {"ok": bool(ok), "volume_ul": volume, "status": self.status()}

    def record_position(self, name: str, note: str = "") -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            clean_name = datetime.now().strftime("point_%Y%m%d_%H%M%S")
        with self._lock:
            pos = self.require_stage().get_position()
            record = {
                "name": clean_name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "position": {
                    "x_mm": float(pos["X"]),
                    "y_mm": float(pos["Y"]),
                    "z_mm": float(pos["Z"]),
                    "z2_mm": float(pos.get("Z2", 0.0)),
                },
                "note": str(note or "").strip(),
            }
            records = self._load_position_records()
            records.append(record)
            persisted = self._save_position_records(records)
            return {
                "ok": True,
                "record": record,
                "records": records,
                "persisted": persisted,
                "status": self.status(),
            }

    def list_position_records(self) -> dict[str, Any]:
        return {
            "ok": True,
            "records": self._load_position_records(),
            "persisted": self._position_records_persisted,
        }

    def _load_position_records(self) -> list[dict[str, Any]]:
        if self._position_records:
            return list(self._position_records)
        if not POSITION_RECORD_PATH.exists():
            return []
        try:
            payload = json.loads(POSITION_RECORD_PATH.read_text(encoding="utf-8"))
        except Exception:
            LOG.exception("Failed to read position records")
            return []
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            self._position_records = payload["records"]
            return list(self._position_records)
        if isinstance(payload, list):
            self._position_records = payload
            return list(self._position_records)
        return []

    def _save_position_records(self, records: list[dict[str, Any]]) -> bool:
        self._position_records = list(records)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "records": records,
        }
        try:
            POSITION_RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
            POSITION_RECORD_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            self._position_records_persisted = True
            return True
        except OSError as exc:
            self._position_records_persisted = False
            LOG.warning("Position records kept in memory only: %s", exc)
            return False


SESSION = GantrySession()


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoSpinmotorXYZWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("Client disconnected before response was written")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_file(self, rel: str) -> None:
        path = (STATIC_ROOT / rel).resolve()
        if not str(path).startswith(str(STATIC_ROOT.resolve())) or not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file("index.html")
            return
        if path == "/api/status":
            self._json(200, {"ok": True, "status": SESSION.status()})
            return
        if path == "/api/position_records":
            self._json(200, SESSION.list_position_records())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/connect":
                comm_cfg = CONFIG.get("communication", {})
                payload = SESSION.connect(
                    gantry_port=str(body.get("gantry_port") or "COM11"),
                    relay_port=str(body.get("relay_port") or "COM10"),
                    pipette_port=str(body.get("pipette_port") or comm_cfg.get("pipette_port") or ""),
                    mock=bool(body.get("mock", False)),
                )
            elif path == "/api/disconnect":
                SESSION.close()
                payload = {"ok": True, "status": SESSION.status()}
            elif path == "/api/home":
                payload = SESSION.home()
            elif path == "/api/move_abs":
                payload = SESSION.move_abs(
                    float(body["x"]),
                    float(body["y"]),
                    float(body["z"]),
                    float(body.get("feed", 1000)),
                )
            elif path == "/api/move_rel":
                payload = SESSION.move_rel(
                    float(body.get("dx", 0)),
                    float(body.get("dy", 0)),
                    float(body.get("dz", 0)),
                    float(body.get("feed", 1000)),
                )
            elif path == "/api/manual_jog":
                payload = SESSION.manual_jog_rel(
                    float(body.get("dx", 0)),
                    float(body.get("dy", 0)),
                    float(body.get("dz", 0)),
                    float(body.get("feed", 300)),
                )
            elif path == "/api/dry_run":
                payload = SESSION.dry_run(
                    float(body["x"]),
                    float(body["y"]),
                    float(body["z"]),
                    float(body.get("feed", 1000)),
                )
            elif path == "/api/dry_run_rel":
                payload = SESSION.dry_run_rel(
                    float(body.get("dx", 0)),
                    float(body.get("dy", 0)),
                    float(body.get("dz", 0)),
                    float(body.get("feed", 1000)),
                )
            elif path == "/api/halt":
                payload = SESSION.halt()
            elif path == "/api/recover":
                payload = SESSION.recover()
            elif path == "/api/homing_diagnostics":
                payload = SESSION.homing_diagnostics()
            elif path == "/api/initialize_z2":
                payload = SESSION.initialize_z2_at_top()
            elif path == "/api/declare_z2_preset":
                payload = SESSION.declare_z2_preset(float(body.get("z2", 0)))
            elif path == "/api/move_z2_preset":
                payload = SESSION.move_z2_preset(
                    float(body.get("z2", 0)),
                    float(body.get("feed", 100)),
                )
            elif path == "/api/move_z2_step":
                payload = SESSION.move_z2_step(
                    float(body.get("dz2", 0)),
                    float(body.get("feed", 100)),
                )
            elif path == "/api/z_brake":
                payload = SESSION.set_z_brake_released(bool(body.get("released", False)))
            elif path == "/api/aspirate":
                payload = SESSION.aspirate(
                    float(body.get("volume_ul", 0)),
                    int(body.get("detect_mask", 0)),
                )
            elif path == "/api/dispense":
                payload = SESSION.dispense(float(body.get("volume_ul", 0)))
            elif path == "/api/record_position":
                payload = SESSION.record_position(
                    str(body.get("name") or ""),
                    str(body.get("note") or ""),
                )
            else:
                self.send_error(404)
                return
            http_status = 200 if payload.get("ok", True) else 400
            self._json(http_status, payload if "ok" in payload else {"ok": True, **payload})
        except Exception as exc:
            if isinstance(exc, L3Error):
                LOG.error("API L3 error on %s: %s", path, exc.agent_message or exc.human_message)
                payload = {
                    "ok": False,
                    "error": exc.human_message,
                    "type": type(exc).__name__,
                    "error_code": exc.error_code,
                    "suggested_action": exc.suggested_action_zh or exc.suggested_action,
                    "status": SESSION.status(),
                }
                self._json(400, payload)
                return
            LOG.exception("API error on %s", path)
            self._json(500, {
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
                "status": SESSION.status(),
            })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("XYZ web control running at http://%s:%s/", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SESSION.close()
        httpd.server_close()


if __name__ == "__main__":
    main()
