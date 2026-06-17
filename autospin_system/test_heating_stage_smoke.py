# AI-516 heating stage smoke test.
#
# Keep this file as a direct test entrypoint, but use the same controller as
# Maestro so the test path and production path stay aligned.
import logging
import sys
import time
from pathlib import Path


PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.heating_stage import HeatingStageController


WRITE_AFTER_PV_READ = False
TARGET_TEMP_C = 40.0
__test__ = False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("HeatingStageSmoke")

    port = CONFIG["communication"].get("heating_stage_port", "COM6")
    heater = HeatingStageController(port=port, mock=False, logger=logger)

    if not heater.connect():
        print(f"Serial open failed: {port}")
        return

    cfg = CONFIG["devices"]["heating_stage"]
    print(
        "Config: "
        f"PORT={port}, SLAVE_ID={cfg['slave_id']}, BAUDRATE={cfg['baudrate']}, "
        f"8{cfg['parity']}1, READ_FUNC={cfg['read_func']}, "
        f"PV_ADDR={cfg['pv_addr']}, timeout={cfg['timeout']}s"
    )
    print("Manual check: AI-516 AFC must be 0 for standard Modbus.")

    try:
        pv = heater.read_pv()
        print(f"Current PV = {pv:.1f} C")

        if not WRITE_AFTER_PV_READ:
            print("SV write skipped because WRITE_AFTER_PV_READ = False")
            return

        heater.write_sv(TARGET_TEMP_C)
        print(f"SV written = {TARGET_TEMP_C:.1f} C")

        for _ in range(10):
            pv = heater.read_pv()
            print(f"PV = {pv:.1f} C")
            time.sleep(1)

    except RuntimeError as exc:
        print(f"Test stopped: {exc}")

    finally:
        heater.close()


if __name__ == "__main__":
    main()
