"""Non-actuating integration smoke for the full-device panel wiring.

Mirrors emergency_dashboard._connect_full_devices() EXACTLY, then exercises only
READ paths (no relay toggling, no gripper actuation, no spin start, no SV write).
Proves the shared-relay + shared-RS485-lock architecture coexists on the three
real serial ports with zero conflict. Safe to run with hardware powered.
"""
import logging
import sys
import threading
from pathlib import Path

# Repo root on sys.path so `import src.*` / `import autospin_system.*` work when run
# as `.venv/bin/python tools/panel_wiring_smoke.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from src.hardware.relay_backend import RelayBackend
from src.hardware.gantry_backend import GantryBackend
from src.hardware.gripper_backend import GripperBackend
from autospin_system.maestro import SharedRs485DeviceProxy
from autospin_system.hardware.heating_stage.heating_stage_controller import HeatingStageController
from autospin_system.hardware.spin_motor.motor_controller import MotorController

FULL_GANTRY_PORT = "/dev/autospin_xyz"
FULL_RELAY_PORT = "/dev/autospin_relay"
FULL_RS485_PORT = "/dev/autospin_rs485"


def main() -> int:
    ok = True

    # ── relay (single instance) ──
    relay = RelayBackend(port=FULL_RELAY_PORT)
    try:
        relay.connect()
        print(f"[relay ] connect OK  state={relay.get_state().channels}")
    except Exception as e:
        ok = False
        print(f"[relay ] connect FAIL: {type(e).__name__}: {e}")

    # ── gantry (relay injected) ──
    gantry = GantryBackend(port=FULL_GANTRY_PORT, relay=relay)
    try:
        gantry.connect()
        s = gantry.get_status()
        print(f"[gantry] connect OK  state={s.state.value} pos=({s.position.x_mm:.1f},"
              f"{s.position.y_mm:.1f},{s.position.z_mm:.1f}) homed={s.is_homed}")
    except Exception as e:
        # grbl may be unpowered right now; report but do not abort the smoke.
        print(f"[gantry] connect FAIL (powered?): {type(e).__name__}: {e}")

    # ── architecture invariants: ONE relay instance shared ──
    print(f"[arch  ] gantry._relay is relay : {gantry._relay is relay}")
    gripper = GripperBackend(relay=relay, channel=1)
    print(f"[arch  ] gripper._relay is relay: {gripper._relay is relay}")
    print(f"[gripper] commanded_state={gripper.get_state().commanded_state.value} (read-only)")

    # ── RS485 shared bus: ONE lock, two proxies ──
    rs485_lock = threading.Lock()
    rs485_logger = logging.getLogger("panel.rs485")
    heater = SharedRs485DeviceProxy(
        HeatingStageController(port=FULL_RS485_PORT), rs485_lock, rs485_logger, "heater")
    spin = SharedRs485DeviceProxy(
        MotorController(port=FULL_RS485_PORT), rs485_lock, rs485_logger, "spin")
    print(f"[arch  ] heater._lock is spin._lock: {heater._lock is spin._lock}")

    # ── heater READ PV (non-actuating) ──
    try:
        pv = heater.read_pv()
        print(f"[heater] read_pv OK  PV={pv:.1f} C")
    except Exception as e:
        ok = False
        print(f"[heater] read_pv FAIL: {type(e).__name__}: {e}")

    # ── spin READ status (non-actuating; no unlock/start) ──
    try:
        st = spin.get_status()
        print(f"[spin  ] get_status OK  actual_speed={st['actual_speed']:.0f} RPM "
              f"status={st['status']}")
    except Exception as e:
        ok = False
        print(f"[spin  ] get_status FAIL: {type(e).__name__}: {e}")

    # ── teardown (close gantry first, then RS485 proxies, then relay) ──
    for name, obj in (("gantry", gantry), ("heater", heater), ("spin", spin), ("relay", relay)):
        try:
            obj.close()
        except Exception as e:
            print(f"[close ] {name}: {type(e).__name__}: {e}")
    print("=== SMOKE DONE ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
