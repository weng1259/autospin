# AutoSpinmotorSystem/hardware/pipette/driver_communication.py
# 移液枪通信模块 —— 基于 pymodbus 的真实 Modbus RTU 实现
# 保持与 v1 相同的底层通信逻辑，类名改为 PipetteDriver 以兼容 v2 架构

import logging
import inspect
from pymodbus.client import ModbusSerialClient


class PipetteDriver:
    """Modbus RTU 通信客户端(适配 Pymodbus 3.x)"""

    def __init__(self, port: str, slave_id: int = 1, baudrate: int = 9600,
                 timeout: float = 2.0, logger: logging.Logger = None,
                 mock: bool = False):
        self.slave_id = slave_id
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.mock = mock
        self.logger = logger or logging.getLogger(__name__)
        self._client = None
        self._connected = False

    def connect(self) -> bool:
        """建立串口连接。mock=True 时直接返回成功。"""
        if self.mock:
            self._connected = True
            self.logger.info(f"[MOCK] PipetteDriver 连接成功 (port={self.port}, slave_id={self.slave_id})")
            return True

        self._client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            stopbits=1,
            bytesize=8,
            parity='N',
        )
        success = self._client.connect()
        if success:
            self._connected = True
            self.logger.info(f"串口 {self.port} 连接成功，从站 ID={self.slave_id}")
        else:
            self.logger.error(f"串口 {self.port} 连接失败")
        return success

    def _call_with_slave_id(self, method_name: str, **kwargs):
        """Call pymodbus while supporting both old and new slave-id keywords."""
        method = getattr(self._client, method_name)
        parameters = inspect.signature(method).parameters
        if "device_id" in parameters:
            kwargs["device_id"] = self.slave_id
        elif "slave" in parameters:
            kwargs["slave"] = self.slave_id
        elif "unit" in parameters:
            kwargs["unit"] = self.slave_id
        return method(**kwargs)

    def close(self):
        """关闭连接"""
        if self.mock:
            self._connected = False
            self.logger.info("[MOCK] PipetteDriver 已断开")
            return
        if self._client:
            self._client.close()
        self._connected = False
        self.logger.info("串口连接已关闭")

    # ---------- 底层读写 ----------

    def read_holding_registers(self, address, count=1):
        """读取保持寄存器"""
        if self.mock:
            self.logger.debug(f"[MOCK] read_holding_registers addr=0x{address:02X} count={count}")
            return [0] * count
        self.logger.debug(f"[TX] Read Holding | Addr: 0x{address:02X} | Count: {count}")
        result = self._call_with_slave_id(
            "read_holding_registers",
            address=address,
            count=count,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"读保持寄存器失败: {result}")
        self.logger.debug(f"[RX] Registers: {result.registers}")
        return result.registers

    def write_register(self, address, value):
        """写入单个保持寄存器"""
        if self.mock:
            self.logger.debug(f"[MOCK] write_register addr=0x{address:02X} value={value}")
            return
        self.logger.debug(f"[TX] Write Single HR | Addr: 0x{address:02X} | Value: {value}")
        result = self._call_with_slave_id(
            "write_register",
            address=address,
            value=value,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"写保持寄存器失败: {result}")
        self.logger.debug("[RX] OK")

    def write_registers(self, start_address, values):
        """写入多个连续保持寄存器"""
        if self.mock:
            self.logger.debug(f"[MOCK] write_registers addr=0x{start_address:02X} values={values}")
            return
        self.logger.debug(f"[TX] Write Multiple HR | Addr: 0x{start_address:02X} | Values: {values}")
        result = self._call_with_slave_id(
            "write_registers",
            address=start_address,
            values=values,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"写多个寄存器失败: {result}")
        self.logger.debug("[RX] OK")

    def read_input_registers(self, address, count=1):
        """读取输入寄存器"""
        if self.mock:
            self.logger.debug(f"[MOCK] read_input_registers addr=0x{address:02X} count={count}")
            # 归位标志(0x01)返回1表示已归位，其余返回0
            # 0x0D: Tip头在位标志 (返回1表示有Tip头)
            if address == 0x01 or address == 0x0D:
                return [1] * count
            return [0] * count
        self.logger.debug(f"[TX] Read Input | Addr: 0x{address:02X} | Count: {count}")
        result = self._call_with_slave_id(
            "read_input_registers",
            address=address,
            count=count,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"读输入寄存器失败: {result}")
        self.logger.debug(f"[RX] Registers: {result.registers}")
        return result.registers

    def read_coils(self, address, count=1):
        """读取线圈"""
        if self.mock:
            self.logger.debug(f"[MOCK] read_coils addr=0x{address:02X} count={count}")
            return [False] * count
        self.logger.debug(f"[TX] Read Coils | Addr: 0x{address:02X} | Count: {count}")
        result = self._call_with_slave_id(
            "read_coils",
            address=address,
            count=count,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"读线圈失败: {result}")
        return result.bits

    def write_coil(self, address, value):
        """写入单个线圈"""
        if self.mock:
            self.logger.debug(f"[MOCK] write_coil addr=0x{address:02X} value={value}")
            return
        self.logger.debug(f"[TX] Write Coil | Addr: 0x{address:02X} | Value: {value}")
        result = self._call_with_slave_id(
            "write_coil",
            address=address,
            value=value,
        )
        if result.isError():
            self.logger.error(f"[RX] Error: {result}")
            raise IOError(f"写线圈失败: {result}")
        self.logger.debug("[RX] OK")
