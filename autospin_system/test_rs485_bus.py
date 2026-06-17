"""RS485/Modbus connectivity smoke test for spin motor and pipette.

Default mode is read-only: it opens each configured serial port and reads a
small set of registers to confirm the device answers on the bus. Use
``--full-init`` only when it is safe for the controllers to run their normal
initialization routines; the pipette controller may home the device.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


__test__ = False

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from AutoSpinmotorSystem.config.hardware_config import CONFIG
from AutoSpinmotorSystem.hardware.pipette import PipetteController, PipetteDriver
from AutoSpinmotorSystem.hardware.pipette.pipette_controller import Registers as PipetteRegisters
from AutoSpinmotorSystem.hardware.spin_motor import DriverCommunication, MotorController


LOGGER = logging.getLogger("rs485_bus_test")
DEFAULT_SHARED_BUS_PORT = "COM9"
DEFAULT_PIPETTE_SCAN_SLAVE_IDS = "1-10"
DEFAULT_PIPETTE_SCAN_BAUDRATES = "9600,19200,38400,115200"


@dataclass
class TestResult:
    device: str
    ok: bool
    port: str
    slave_id: int
    baudrate: int
    details: str
    error: Optional[str] = None

    def format(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        line = (
            f"[{status}] {self.device}: port={self.port}, "
            f"slave_id={self.slave_id}, baudrate={self.baudrate}"
        )
        if self.details:
            line += f", {self.details}"
        if self.error:
            line += f", error={self.error}"
        return line


def _safe_call(label: str, func: Callable[[], Any]) -> tuple[bool, Any]:
    try:
        value = func()
        return True, value
    except Exception as exc:  # Hardware smoke tests should report and continue.
        return False, f"{label}: {exc}"


def _parse_int_list(value: str) -> List[int]:
    items: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if end < start:
                raise ValueError(f"invalid range: {part}")
            items.extend(range(start, end + 1))
        else:
            items.append(int(part))
    return items


def _read_pipette_status_register(
    driver: PipetteDriver,
    name: str,
    address: int,
) -> tuple[bool, Optional[int], str, Optional[str]]:
    ok, value = _safe_call(
        f"read input {name}",
        lambda: driver.read_input_registers(address, 1),
    )
    if ok and value is not None:
        return True, value[0], "input", None

    input_error = str(value)
    ok, value = _safe_call(
        f"read holding {name}",
        lambda: driver.read_holding_registers(address, 1),
    )
    if ok and value is not None:
        return True, value[0], "holding", input_error

    return False, None, "input/holding", f"{input_error}; {value}"


def test_motor_read_only(mock: bool, port_override: Optional[str] = None) -> TestResult:
    comm_cfg = CONFIG["communication"]
    motor_cfg = CONFIG["devices"]["spin_motor"]
    port = port_override or comm_cfg["motor_port"]
    slave_id = motor_cfg["slave_id"]
    baudrate = motor_cfg["baudrate"]

    driver = DriverCommunication(
        port=port,
        slave_id=slave_id,
        baudrate=baudrate,
        timeout=motor_cfg["timeout"],
        mock=mock,
        logger=LOGGER,
    )

    try:
        if not driver.connect():
            return TestResult(
                "spin_motor",
                False,
                port,
                slave_id,
                baudrate,
                "open serial failed",
            )

        ok, value = _safe_call(
            "read actual speed",
            lambda: driver.read_register(driver.REG_ACTUAL_SPEED, 1),
        )
        if ok and value is not None:
            return TestResult(
                "spin_motor",
                True,
                port,
                slave_id,
                baudrate,
                f"REG_ACTUAL_SPEED={value[0]}",
            )

        first_error = value
        ok, value = _safe_call(
            "read bus voltage",
            lambda: driver.read_register(driver.REG_BUS_VOLTAGE, 1),
        )
        if ok and value is not None:
            return TestResult(
                "spin_motor",
                True,
                port,
                slave_id,
                baudrate,
                f"REG_BUS_VOLTAGE={value[0]}",
            )

        return TestResult(
            "spin_motor",
            False,
            port,
            slave_id,
            baudrate,
            "no valid Modbus response",
            str(value or first_error),
        )
    except Exception as exc:
        return TestResult(
            "spin_motor",
            False,
            port,
            slave_id,
            baudrate,
            "exception during read-only test",
            str(exc),
        )
    finally:
        driver.close()


def test_pipette_read_only(
    mock: bool,
    port_override: Optional[str] = None,
    slave_id_override: Optional[int] = None,
    baudrate_override: Optional[int] = None,
    timeout_override: Optional[float] = None,
) -> TestResult:
    comm_cfg = CONFIG["communication"]
    pipette_cfg = CONFIG["devices"]["pipette"]
    port = port_override or comm_cfg["pipette_port"]
    slave_id = slave_id_override or pipette_cfg["slave_id"]
    baudrate = baudrate_override or pipette_cfg["baudrate"]
    timeout = timeout_override or pipette_cfg["timeout"]

    driver = PipetteDriver(
        port=port,
        slave_id=slave_id,
        baudrate=baudrate,
        timeout=timeout,
        mock=mock,
        logger=LOGGER,
    )

    try:
        if not driver.connect():
            return TestResult(
                "pipette",
                False,
                port,
                slave_id,
                baudrate,
                "open serial failed",
            )

        reads: Dict[str, int] = {}
        register_spaces: Dict[str, str] = {}
        fallback_notes: List[str] = []
        for name, address in [
            ("STATUS", PipetteRegisters.STATUS),
            ("HOMED", PipetteRegisters.HOMED),
            ("TIP_PRESENT", PipetteRegisters.TIP_PRESENT),
        ]:
            ok, register_value, register_space, fallback_error = (
                _read_pipette_status_register(driver, name, address)
            )
            if not ok or register_value is None:
                return TestResult(
                    "pipette",
                    False,
                    port,
                    slave_id,
                    baudrate,
                    f"failed while reading {name} via {register_space} registers",
                    fallback_error,
                )
            reads[name] = register_value
            register_spaces[name] = register_space
            if fallback_error:
                fallback_notes.append(
                    f"{name}: input failed, holding succeeded ({fallback_error})"
                )

        details = ", ".join(f"{key}={value}" for key, value in reads.items())
        spaces = ",".join(
            f"{key}:{value}" for key, value in register_spaces.items()
        )
        details = f"{details}, register_space={spaces}"
        if fallback_notes:
            details = f"{details}, fallback={' | '.join(fallback_notes)}"
        return TestResult("pipette", True, port, slave_id, baudrate, details)
    except Exception as exc:
        return TestResult(
            "pipette",
            False,
            port,
            slave_id,
            baudrate,
            "exception during read-only test",
            str(exc),
        )
    finally:
        driver.close()


def scan_pipette_read_only(
    mock: bool,
    port: str,
    slave_ids: List[int],
    baudrates: List[int],
    timeout: float,
) -> TestResult:
    attempts = 0
    errors: List[str] = []

    for baudrate in baudrates:
        for slave_id in slave_ids:
            attempts += 1
            LOGGER.info(
                "Scanning pipette candidate: port=%s, slave_id=%s, baudrate=%s",
                port,
                slave_id,
                baudrate,
            )
            result = test_pipette_read_only(
                mock,
                port_override=port,
                slave_id_override=slave_id,
                baudrate_override=baudrate,
                timeout_override=timeout,
            )
            if result.ok:
                result.device = "pipette_scan"
                result.details = f"matched after {attempts} attempt(s), {result.details}"
                return result
            if len(errors) < 5 and result.error:
                errors.append(
                    f"id={slave_id}, baud={baudrate}: {result.error}"
                )

    return TestResult(
        "pipette_scan",
        False,
        port,
        -1,
        -1,
        f"no pipette response in {attempts} candidate(s)",
        " | ".join(errors) if errors else None,
    )


def test_motor_full_init(mock: bool, port_override: Optional[str] = None) -> TestResult:
    comm_cfg = CONFIG["communication"]
    motor_cfg = CONFIG["devices"]["spin_motor"]
    port = port_override or comm_cfg["motor_port"]
    slave_id = motor_cfg["slave_id"]
    baudrate = motor_cfg["baudrate"]
    controller = MotorController(port=port, mock=mock, logger=LOGGER)

    try:
        ok = controller.connect()
        if not ok:
            return TestResult(
                "spin_motor",
                False,
                port,
                slave_id,
                baudrate,
                "MotorController.connect() returned False",
            )
        status = controller.get_status()
        return TestResult(
            "spin_motor",
            True,
            port,
            slave_id,
            baudrate,
            f"controller_status={status}",
        )
    except Exception as exc:
        return TestResult(
            "spin_motor",
            False,
            port,
            slave_id,
            baudrate,
            "exception during full initialization",
            str(exc),
        )
    finally:
        controller.close()


def test_pipette_full_init(mock: bool, port_override: Optional[str] = None) -> TestResult:
    comm_cfg = CONFIG["communication"]
    pipette_cfg = CONFIG["devices"]["pipette"]
    port = port_override or comm_cfg["pipette_port"]
    slave_id = pipette_cfg["slave_id"]
    baudrate = pipette_cfg["baudrate"]
    controller = PipetteController(port=port, mock=mock, logger=LOGGER)

    try:
        ok = controller.connect()
        if not ok:
            return TestResult(
                "pipette",
                False,
                port,
                slave_id,
                baudrate,
                "PipetteController.connect() returned False",
            )
        status = controller.get_status()
        return TestResult(
            "pipette",
            True,
            port,
            slave_id,
            baudrate,
            f"controller_status={status}",
        )
    except Exception as exc:
        return TestResult(
            "pipette",
            False,
            port,
            slave_id,
            baudrate,
            "exception during full initialization",
            str(exc),
        )
    finally:
        controller.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test RS485/Modbus connectivity for spin motor and pipette."
    )
    parser.add_argument(
        "--device",
        choices=["motor", "pipette", "all"],
        default="all",
        help="Device to test. Default: all.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock drivers instead of opening real serial ports.",
    )
    parser.add_argument(
        "--bus-port",
        default=DEFAULT_SHARED_BUS_PORT,
        help=(
            "Use one physical USB-RS485 port for both devices, for example COM9. "
            "This overrides configured motor and pipette ports unless a "
            "device-specific override is also provided. "
            f"Default: {DEFAULT_SHARED_BUS_PORT}."
        ),
    )
    parser.add_argument(
        "--motor-port",
        help="Override the configured spin motor serial port.",
    )
    parser.add_argument(
        "--pipette-port",
        help="Override the configured pipette serial port.",
    )
    parser.add_argument(
        "--pipette-slave-id",
        type=int,
        help="Override the configured pipette Modbus slave ID.",
    )
    parser.add_argument(
        "--pipette-baudrate",
        type=int,
        help="Override the configured pipette baudrate.",
    )
    parser.add_argument(
        "--scan-pipette",
        action="store_true",
        help=(
            "Scan pipette slave IDs and baudrates with read-only requests. "
            "This can take a while when the device does not answer."
        ),
    )
    parser.add_argument(
        "--scan-slave-ids",
        default=DEFAULT_PIPETTE_SCAN_SLAVE_IDS,
        help=(
            "Comma-separated slave IDs or ranges for --scan-pipette. "
            f"Default: {DEFAULT_PIPETTE_SCAN_SLAVE_IDS}."
        ),
    )
    parser.add_argument(
        "--scan-baudrates",
        default=DEFAULT_PIPETTE_SCAN_BAUDRATES,
        help=(
            "Comma-separated baudrates for --scan-pipette. "
            f"Default: {DEFAULT_PIPETTE_SCAN_BAUDRATES}."
        ),
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=0.5,
        help="Per-request serial timeout for --scan-pipette. Default: 0.5.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose communication logs.",
    )
    parser.add_argument(
        "--full-init",
        action="store_true",
        help=(
            "Use high-level controllers and run their initialization routines. "
            "This may write registers and may home the pipette."
        ),
    )
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def run_tests(args: argparse.Namespace) -> List[TestResult]:
    results: List[TestResult] = []
    motor_port = args.motor_port or args.bus_port
    pipette_port = args.pipette_port or args.bus_port

    if args.device in ("motor", "all"):
        if args.full_init:
            results.append(test_motor_full_init(args.mock, motor_port))
        else:
            results.append(test_motor_read_only(args.mock, motor_port))

    if args.device in ("pipette", "all"):
        if args.scan_pipette:
            results.append(
                scan_pipette_read_only(
                    args.mock,
                    pipette_port,
                    _parse_int_list(args.scan_slave_ids),
                    _parse_int_list(args.scan_baudrates),
                    args.scan_timeout,
                )
            )
        elif args.full_init:
            results.append(test_pipette_full_init(args.mock, pipette_port))
        else:
            results.append(
                test_pipette_read_only(
                    args.mock,
                    pipette_port,
                    slave_id_override=args.pipette_slave_id,
                    baudrate_override=args.pipette_baudrate,
                )
            )

    return results


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    mode = "full-init" if args.full_init else "read-only"
    LOGGER.info("Starting RS485 connectivity test: device=%s, mode=%s, mock=%s", args.device, mode, args.mock)
    if args.bus_port:
        LOGGER.info("Using shared USB-RS485 bus port for tests: %s", args.bus_port)
    if args.motor_port:
        LOGGER.info("Using spin motor port override: %s", args.motor_port)
    if args.pipette_port:
        LOGGER.info("Using pipette port override: %s", args.pipette_port)
    if args.pipette_slave_id:
        LOGGER.info("Using pipette slave ID override: %s", args.pipette_slave_id)
    if args.pipette_baudrate:
        LOGGER.info("Using pipette baudrate override: %s", args.pipette_baudrate)
    if args.scan_pipette:
        LOGGER.info(
            "Scanning pipette candidates: slave_ids=%s, baudrates=%s, timeout=%s",
            args.scan_slave_ids,
            args.scan_baudrates,
            args.scan_timeout,
        )
    if args.full_init:
        LOGGER.warning("Full initialization can write registers and may move/home hardware.")

    results = run_tests(args)
    print()
    for result in results:
        print(result.format())

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
