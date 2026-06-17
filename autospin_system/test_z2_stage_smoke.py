"""Manual Z2/A-axis smoke test.

Run only when the Z2 slide serial port is free and the stage is in a safe
starting position. This file is ignored by pytest collection.
"""

from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from AutoSpinmotorSystem.hardware.xyz_stage.z2_stage import Z2Stage


__test__ = False


def main() -> None:
    z2 = Z2Stage(port="COM11")  # Change to the actual Arduino Mega COM port.

    if not z2.connect():
        raise RuntimeError(
            "Z2Stage connection failed; check COM port and serial monitor occupancy."
        )

    try:
        # The slide should start at the top; declare the current position as A0.
        z2.unlock()
        z2.set_hard_limits(False)
        z2._send_line_wait_ok("$20=0", timeout_s=3.0)
        z2._send_line_wait_ok("$22=0", timeout_s=3.0)
        z2._send_line_wait_ok("G92 A0", timeout_s=3.0)

        print("Initial status:", z2.get_status())

        z2.move_relative(5.0, feed_mm_min=100.0)
        print("After moving down 5 mm:", z2.get_status())

        z2.move_relative(-5.0, feed_mm_min=100.0)
        print("Back near top:", z2.get_status())

    finally:
        z2.close()


if __name__ == "__main__":
    main()
