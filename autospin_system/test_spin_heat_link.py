"""Spin motor + AI-516 heating stage link test on one RS485 bus.

This script opens the shared RS485 serial port once, then talks to:
- AI-516 heating stage at the configured slave id, standard Modbus byte order.
- DBLS400 spin motor at slave id 2, vendor word byte order.

Keep this as a hardware smoke test. It intentionally avoids Maestro because the
current production controllers each own their own serial object, which is not
appropriate when both devices are on the same physical RS485 bus.
"""

import argparse
import logging
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

import serial


__test__ = False

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from AutoSpinmotorSystem.config.hardware_config import CONFIG


# ---- Safety/test profile -------------------------------------------------

SHARED_BUS_PORT = CONFIG["communication"].get("heating_stage_port", "COM6")
BAUDRATE = 9600
TIMEOUT = 2.0

HEATER_SLAVE_ID = CONFIG["devices"]["heating_stage"].get("slave_id", 3)
MOTOR_SLAVE_ID = CONFIG["devices"]["spin_motor"].get("slave_id", 2)

WRITE_HEATER_SV = False # 让脚本真正写入热台目标温度把这里改为True
HEATER_TARGET_C = 24
WAIT_FOR_TEMPERATURE = False # 等热台实际温度 PV 到达某个温度后，再启动旋涂电机，把这里改为True
START_SPIN_WHEN_PV_C = 40.0
MAX_WAIT_SECONDS = 120

MOTOR_POLE_PAIRS = CONFIG["devices"]["spin_motor"].get("pole_pairs", 4)
MOTOR_SPEED_FACTOR = 2.5

REG_HEATER_PV = CONFIG["devices"]["heating_stage"].get("pv_addr", 74)
REG_HEATER_SV = CONFIG["devices"]["heating_stage"].get("sv_addr", 0)

REG_MOTOR_CONTROL = 0x8000
REG_MOTOR_SPEED_SET = 0x8005
REG_MOTOR_ACTUAL_SPEED = 0x8018

SPIN_PROFILE = [
    {"speed": 500, "duration": 3.0},
    {"speed": 1000, "duration": 5.0},
    {"speed": 500, "duration": 3.0},
]


@dataclass
class SharedModbusBus:
    port: str
    baudrate: int = 9600
    timeout: float = 2.0

    def __post_init__(self):
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
        )
        time.sleep(0.1)

    @staticmethod
    def crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def transact(self, frame: bytes, response_len: int, label: str) -> bytes:
        frame += struct.pack("<H", self.crc16(frame))
        self.ser.reset_input_buffer()
        logging.debug("[%s TX] %s", label, frame.hex(" ").upper())
        self.ser.write(frame)
        self.ser.flush()
        time.sleep(0.05)
        response = self.ser.read(response_len)
        logging.debug("[%s RX] %s", label, response.hex(" ").upper())

        if len(response) != response_len:
            raise TimeoutError(
                f"{label} response timeout: expected {response_len} bytes, got {len(response)}"
            )
        received_crc = struct.unpack("<H", response[-2:])[0]
        calculated_crc = self.crc16(response[:-2])
        if received_crc != calculated_crc:
            raise IOError(
                f"{label} CRC mismatch: received=0x{received_crc:04X}, "
                f"calculated=0x{calculated_crc:04X}"
            )
        return response

    def read_holding(self, slave_id: int, address: int, count: int, *, low_high_words: bool) -> List[int]:
        request = struct.pack(">BBHH", slave_id, 0x03, address, count)
        response = self.transact(request, 5 + 2 * count, f"sid={slave_id} read 0x{address:04X}")
        if response[0] != slave_id or response[1] != 0x03:
            raise IOError(f"Unexpected read response header: {response.hex(' ').upper()}")

        values = []
        for index in range(count):
            first = response[3 + 2 * index]
            second = response[4 + 2 * index]
            if low_high_words:
                value = (second << 8) | first
            else:
                value = (first << 8) | second
            if value >= 32768:
                value -= 65536
            values.append(value)
        return values

    def write_register(self, slave_id: int, address: int, value: int, *, low_high_word: bool) -> None:
        if low_high_word:
            value_bytes = bytes([value & 0xFF, (value >> 8) & 0xFF])
        else:
            value_bytes = struct.pack(">H", value & 0xFFFF)

        request = struct.pack(">BBH", slave_id, 0x06, address) + value_bytes
        response = self.transact(request, 8, f"sid={slave_id} write 0x{address:04X}")
        if response[0] != slave_id or response[1] != 0x06:
            raise IOError(f"Unexpected write response header: {response.hex(' ').upper()}")


def motor_control_word(run: bool = False, reverse: bool = False, brake: bool = False) -> int:
    control_bits = 0x08
    if run:
        control_bits |= 0x01
    if reverse:
        control_bits |= 0x02
    if brake:
        control_bits |= 0x04
    return (MOTOR_POLE_PAIRS << 8) | control_bits


def read_heater_pv(bus: SharedModbusBus) -> float:
    raw = bus.read_holding(HEATER_SLAVE_ID, REG_HEATER_PV, 1, low_high_words=False)[0]
    return raw / 10.0


def write_heater_sv(bus: SharedModbusBus, temp_c: float) -> None:
    bus.write_register(
        HEATER_SLAVE_ID,
        REG_HEATER_SV,
        int(round(temp_c * 10)),
        low_high_word=False,
    )


def motor_write(bus: SharedModbusBus, address: int, value: int) -> None:
    bus.write_register(MOTOR_SLAVE_ID, address, value, low_high_word=True)


def motor_read_actual_speed(bus: SharedModbusBus) -> float:
    raw = bus.read_holding(
        MOTOR_SLAVE_ID,
        REG_MOTOR_ACTUAL_SPEED,
        1,
        low_high_words=True,
    )[0]
    return float(raw) * MOTOR_SPEED_FACTOR


def motor_stop(bus: SharedModbusBus, brake: bool = True) -> None:
    motor_write(bus, REG_MOTOR_CONTROL, motor_control_word(run=False, brake=brake))


def motor_start(bus: SharedModbusBus) -> None:
    motor_stop(bus, brake=False)
    time.sleep(0.3)
    motor_write(bus, REG_MOTOR_CONTROL, motor_control_word(run=True))
    time.sleep(0.5)


def motor_set_speed(bus: SharedModbusBus, rpm: float) -> None:
    rpm_int = int(max(0, min(rpm, 6000)))
    motor_write(bus, REG_MOTOR_SPEED_SET, rpm_int)


def wait_for_temperature(bus: SharedModbusBus) -> None:
    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        pv = read_heater_pv(bus)
        logging.info("Heating PV %.1f C; waiting for %.1f C", pv, START_SPIN_WHEN_PV_C)
        if pv >= START_SPIN_WHEN_PV_C:
            return
        time.sleep(2)
    raise TimeoutError(f"Heating stage did not reach {START_SPIN_WHEN_PV_C:.1f} C")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the shared RS485 spin motor + heating stage link test."
    )
    parser.add_argument(
        "--bus-port",
        default=SHARED_BUS_PORT,
        help="Shared RS485 serial port. Default comes from system_config.yaml or COM6.",
    )
    return parser.parse_args()


def run_link_test(port: str = SHARED_BUS_PORT):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    logging.info(
        "Opening shared RS485 bus %s @ %s 8N1; heater sid=%s, motor sid=%s",
        port,
        BAUDRATE,
        HEATER_SLAVE_ID,
        MOTOR_SLAVE_ID,
    )

    bus = SharedModbusBus(port, BAUDRATE, TIMEOUT)
    try:
        pv = read_heater_pv(bus)
        logging.info("Initial heating PV = %.1f C", pv)

        if WRITE_HEATER_SV:
            write_heater_sv(bus, HEATER_TARGET_C)
            logging.info("Heating SV written = %.1f C", HEATER_TARGET_C)
        else:
            logging.info("Heating SV write skipped; WRITE_HEATER_SV = False")

        if WAIT_FOR_TEMPERATURE:
            wait_for_temperature(bus)

        logging.info("Initializing and starting spin motor")
        motor_start(bus)

        for step in SPIN_PROFILE:
            target = step["speed"]
            duration = step["duration"]
            motor_set_speed(bus, target)
            logging.info("Motor target = %s RPM for %.1f s", target, duration)

            step_end = time.time() + duration
            while time.time() < step_end:
                pv = read_heater_pv(bus)
                actual = motor_read_actual_speed(bus)
                logging.info("Linked status: PV=%.1f C, motor_actual=%.1f RPM", pv, actual)
                time.sleep(1)

        logging.info("Profile complete")

    finally:
        try:
            logging.info("Stopping motor")
            motor_stop(bus, brake=True)
        finally:
            bus.close()
            logging.info("Shared bus closed")


if __name__ == "__main__":
    args = parse_args()
    run_link_test(args.bus_port)
