import logging
from typing import Any, Dict

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException

class HeatingStageController:
    """AI-516 heating stage controller over Modbus RTU."""

    def __init__(
        self,
        port: str = None,
        mock: bool = False,
        logger: logging.Logger = None,
        *,
        slave_id: int = 3,
        baudrate: int = 9600,
        timeout: float = 3.0,
        parity: str = "N",
        pv_addr: int = 74,
        sv_addr: int = 0,
        srun_addr: int = 27,
        run_on_sv_write: bool = True,
        scale: float = 10.0,
        read_func: str = "holding",
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.mock = mock

        self.port = port
        self.slave_id = slave_id
        self.baudrate = baudrate
        self.timeout = timeout
        self.parity = parity
        self.pv_addr = pv_addr
        self.sv_addr = sv_addr
        self.srun_addr = srun_addr
        self.run_on_sv_write = run_on_sv_write
        self.scale = scale
        self.read_func = read_func

        self._client = None
        self._connected = False
        self._last_pv = None
        self._last_sv = None

    def connect(self) -> bool:
        if self.mock:
            self._connected = True
            self.logger.info(
                "[MOCK] HeatingStageController connected "
                f"(port={self.port}, slave_id={self.slave_id})"
            )
            return True

        self._client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity=self.parity,
            stopbits=1,
            timeout=self.timeout,
        )
        self._connected = bool(self._client.connect())
        if self._connected:
            self.logger.info(
                "Heating stage connected "
                f"(port={self.port}, slave_id={self.slave_id}, "
                f"baudrate={self.baudrate}, parity={self.parity})"
            )
        else:
            self.logger.error(f"Heating stage serial open failed: {self.port}")
        return self._connected

    def _ensure_connected(self):
        if not self._connected:
            raise RuntimeError("Heating stage is not connected")

    def _read_register(self, address: int) -> int:
        if self.mock:
            self.logger.debug(f"[MOCK] heating read address={address}")
            return 250

        self._ensure_connected()
        try:
            if self.read_func == "holding":
                result = self._client.read_holding_registers(
                    address=address,
                    count=1,
                    device_id=self.slave_id,
                )
            elif self.read_func == "input":
                result = self._client.read_input_registers(
                    address=address,
                    count=1,
                    device_id=self.slave_id,
                )
            else:
                raise ValueError('read_func must be "holding" or "input"')
        except ModbusIOException as exc:
            raise RuntimeError(
                f"Heating stage read timeout: address={address}, "
                f"slave_id={self.slave_id}, read_func={self.read_func}"
            ) from exc

        if result.isError():
            raise RuntimeError(f"Heating stage read failed: address={address}, result={result}")

        value = result.registers[0]
        if value >= 32768:
            value -= 65536
        return value

    def _write_register(self, address: int, value: int) -> None:
        if self.mock:
            self.logger.debug(f"[MOCK] heating write address={address}, value={value}")
            return

        self._ensure_connected()
        try:
            result = self._client.write_register(
                address=address,
                value=value,
                device_id=self.slave_id,
            )
        except ModbusIOException as exc:
            raise RuntimeError(
                f"Heating stage write timeout: address={address}, "
                f"value={value}, slave_id={self.slave_id}"
            ) from exc

        if result.isError():
            raise RuntimeError(
                f"Heating stage write failed: address={address}, value={value}, result={result}"
            )

    def read_pv(self) -> float:
        raw = self._read_register(self.pv_addr)
        self._last_pv = raw / self.scale
        return self._last_pv

    def write_sv(self, temp_c: float) -> None:
        raw = int(round(temp_c * self.scale))
        self._write_register(self.sv_addr, raw)
        self._last_sv = temp_c
        if self.run_on_sv_write:
            self.run()

    def read_sv(self) -> float:
        raw = self._read_register(self.sv_addr)
        self._last_sv = raw / self.scale
        return self._last_sv

    def run(self) -> None:
        self._write_register(self.srun_addr, 0)

    def get_status(self) -> Dict[str, Any]:
        status = {
            "connected": self._connected,
            "port": self.port,
            "slave_id": self.slave_id,
            "baudrate": self.baudrate,
            "parity": self.parity,
            "pv": self._last_pv,
            "sv": self._last_sv,
        }
        if self._connected:
            try:
                status["pv"] = self.read_pv()
            except Exception as exc:
                status["error"] = str(exc)
        return status

    def stop(self) -> bool:
        """Generic stop hook for Maestro; does not change SV implicitly."""
        self.logger.info("Heating stage stop requested; leaving SV unchanged")
        return True

    def shutdown(self):
        self.close()

    def close(self):
        if self.mock:
            self._connected = False
            self.logger.info("[MOCK] HeatingStageController disconnected")
            return
        if self._client:
            self._client.close()
        self._connected = False
