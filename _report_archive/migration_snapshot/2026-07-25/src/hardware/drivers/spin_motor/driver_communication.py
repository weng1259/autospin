# driver_communication.py
# ============================================================
# DBLS400 无刷驱动器 Modbus RTU 通讯底层 
#
# 职责：
# 1. 管理 RS485 / 串口
# 2. 生成 Modbus RTU 帧
# 3. 读写驱动器寄存器 (严格遵守 DBLS400 字节序协议)
# 4. 记录所有通信报文和操作日志

# ============================================================

import serial
import time
import struct
import logging
import threading
from typing import Optional, List


class DriverCommunication:
    # 寄存器地址
    REG_CONTROL = 0x8000
    REG_SPEED_SET = 0x8005
    REG_ACTUAL_SPEED = 0x8018
    REG_BUS_VOLTAGE = 0x8019
    REG_FAULT_STATUS = 0x801B

    def __init__(self, port: str, baudrate: int = 9600, slave_id: int = 2,
                 timeout: float = 0.5, logger: logging.Logger = None,
                 mock: bool = False, **kwargs):
        """
        匹配 Maestro 架构的初始化函数。
        mock=True 时不打开串口，所有读写返回模拟值。
        """
        self.slave_id = slave_id
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.mock = mock

        # 匹配 Maestro 的日志系统
        self.logger = logger or logging.getLogger(__name__)

        # 核心逻辑开关：DBLS400 必须反转字节
        self.reverse_word_bytes = True
        self.ser_lock = threading.Lock()

        self.tx_count = 0
        self.rx_count = 0
        self.error_count = 0
        self._connected = False
        self.ser = None

    # --------------------------------------------------------
    # 连接管理
    # --------------------------------------------------------

    def connect(self) -> bool:
        """打开串口连接。mock=True 时直接返回成功。"""
        if self.mock:
            self._connected = True
            self.logger.info(f"[MOCK] DriverCommunication 连接成功 (port={self.port}, slave_id={self.slave_id})")
            return True
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=self.timeout
            )
            time.sleep(0.1)
            self._connected = True
            self.logger.info(f"成功连接到串口: {self.port}, 从站 ID: {self.slave_id}")
            return True
        except Exception as e:
            self.logger.error(f"无法打开串口 {self.port}: {e}")
            return False

    def disconnect(self):
        """关闭串口连接。"""
        if self.mock:
            self._connected = False
            self.logger.info("[MOCK] DriverCommunication 已断开")
            return
        if self.ser and self.ser.is_open:
            self.ser.close()
        self._connected = False

    @staticmethod
    def _crc16(data: bytes) -> int:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc

    def _send(self, frame: bytes, resp_len: int) -> bytes:
        """带锁的发送逻辑，防止 Maestro 多线程冲突"""
        with self.ser_lock:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.tx_count += 1

            # 关键：给驱动器响应时间
            time.sleep(0.05)

            resp = self.ser.read(resp_len)
            if resp:
                self.rx_count += 1
            return resp

    def _verify_crc(self, response: bytes) -> bool:
        if len(response) < 2: return False
        received_crc = struct.unpack('<H', response[-2:])[0]
        calculated_crc = self._crc16(response[:-2])
        return received_crc == calculated_crc

    # --------------------------------------------------------
    # 核心接口：匹配 MotorController 调用 (addr, count, retries)
    # --------------------------------------------------------

    def read_register(self, addr: int, count: int = 1, retries: int = 3) -> Optional[List[int]]:
        """
        读取寄存器。
        注意：为了兼容 MotorController 的调用方式，必须接受 count 和 retries。
        """
        if self.mock:
            self.logger.debug(f"[MOCK] read_register addr=0x{addr:04X} count={count}")
            return [0] * count

        for attempt in range(retries):
            try:
                # 构造指令（读 count 个寄存器）
                frame = struct.pack('>BBHH', self.slave_id, 0x03, addr, count)
                frame += struct.pack('<H', self._crc16(frame))

                # DBLS400 读 1 个寄存器的响应长度是 7
                expected_len = 5 + 2 * count
                resp = self._send(frame, expected_len)

                if len(resp) < expected_len or not self._verify_crc(resp):
                    continue

                # 解析数据
                data = []
                for i in range(count):
                    # 关键：DBLS400 低位在前，高位在后
                    lo = resp[3 + i * 2]
                    hi = resp[4 + i * 2]
                    value = (hi << 8) | lo
                    data.append(value)
                return data  # 返回列表，匹配 MotorController 的 data[0] 调用方式
            except Exception as e:
                self.logger.warning(f"读取重试 {attempt + 1}: {e}")
        return None

    def write_register(self, addr: int, value: int, retries: int = 3) -> bool:
        """写入寄存器 - 严格执行字节反转逻辑"""
        if self.mock:
            self.logger.debug(f"[MOCK] write_register addr=0x{addr:04X} value=0x{value:04X}")
            return True

        for attempt in range(retries):
            try:
                # 将 16 位值按小端序拆分发送
                low_byte = value & 0xFF
                high_byte = (value >> 8) & 0xFF

                frame = struct.pack('>BBH', self.slave_id, 0x06, addr)
                frame += struct.pack('>BB', low_byte, high_byte)  # 字节翻转
                frame += struct.pack('<H', self._crc16(frame))

                resp = self._send(frame, 8)
                if len(resp) == 8 and self._verify_crc(resp):
                    return True
            except Exception as e:
                self.logger.warning(f"写入寄存器重试 {attempt + 1}: {e}")
        return False

    def close(self):
        self.disconnect()
