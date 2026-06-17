# AutoSpinmotorSystem/hardware/relay/relay_manager.py
# 继电器管理模块(已适配 V2.0 YAML 配置架构与 LCUS-8 物理协议)

import time
import logging
from typing import Dict

import serial

try:
    from autospin_system.config.hardware_config import CONFIG
except ModuleNotFoundError:  # Direct execution from the project root.
    from config.hardware_config import CONFIG


class RelayManager:
    """
    继电器管理器
    负责控制各种继电器通道，如 Z 轴抱闸、氮气电磁阀等
    """

    def __init__(self, port: str = None, mock: bool = False, logger: logging.Logger = None):
        """
        初始化继电器管理器。
        """
        self.logger = logger or logging.getLogger(__name__)
        self.mock = mock
        self.port = port

        # 从 YAML 配置中动态读取参数，消除硬编码
        relay_cfg = CONFIG['devices']['relay']
        self.baudrate = relay_cfg['baudrate']
        self._channels = relay_cfg['channels']  # 获取通道字典

        self._cmd_prefix = relay_cfg['commands']['prefix']  # 160 (0xA0)
        self._cmd_on = relay_cfg['commands']['on']  # 1 (0x01)
        self._cmd_off = relay_cfg['commands']['off']  # 0 (0x00)

        self._is_initialized = False
        self._connected = False
        self._serial = None  # 真实的串口对象

        # 初始化状态记录字典为 False
        self._states = {channel: False for channel in self._channels}

    def connect(self) -> bool:
        """打开串口连接。mock=True 时直接返回成功。"""
        if self.mock:
            self._connected = True
            self._is_initialized = True
            self.logger.info(f"[MOCK] RelayManager 连接成功 (port={self.port})")
            return True
        if self._serial and self._serial.is_open:
            self._connected = True
            return True
        try:
            self.logger.info(f"正在连接继电器: {self.port}")
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.5,
            )
            time.sleep(0.1)
            self._initialize_relay()
            self._connected = True
            return True
        except Exception as e:
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            self._connected = False
            self._is_initialized = False
            self.logger.error(f"继电器连接失败: {e}")
            return False

    def disconnect(self):
        """关闭串口。"""
        if self.mock:
            self._connected = False
            self._is_initialized = False
            self.logger.info("[MOCK] RelayManager 已断开")
            return
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._serial = None
        self._connected = False
        self._is_initialized = False
        self.logger.info("继电器已断开")

    def _send_command(self, channel_num: int, state: int):
        """
        【新增方法】：构建并发送 LCUS-8 控制报文
        协议格式: [0xA0, 通道号, 状态, 校验和(前三者之和的低8位)]
        """
        checksum = (self._cmd_prefix + channel_num + state) & 0xFF
        cmd = bytes([self._cmd_prefix, channel_num, state, checksum])

        self.logger.debug(f"[TX] Relay CH{channel_num} -> {'ON' if state else 'OFF'} | Hex: {cmd.hex().upper()}")

        if self.mock:
            return
        if not self._serial or not self._serial.is_open:
            raise ConnectionError("Relay serial port is not open")
        self._serial.write(cmd)
        self._serial.flush()
        # 继电器机械动作响应较慢，给予极短暂的缓冲延时
        time.sleep(0.05)

    def _initialize_relay(self):
        """初始化继电器:将所有通道强制置为关闭状态（connect() 内部调用）"""
        try:
            self.logger.info("初始化继电器：正在关闭所有通道...")
            channel_nums = tuple(dict.fromkeys(self._channels.values()))
            if not self.mock and self._serial and self._serial.is_open:
                commands = bytearray()
                for channel_num in channel_nums:
                    checksum = (self._cmd_prefix + channel_num + self._cmd_off) & 0xFF
                    commands.extend((self._cmd_prefix, channel_num, self._cmd_off, checksum))
                self.logger.debug(f"[TX] Relay all OFF | Hex: {commands.hex().upper()}")
                self._serial.write(commands)
                self._serial.flush()
                time.sleep(0.05)
            else:
                for channel_num in channel_nums:
                    self._send_command(channel_num, self._cmd_off)
            for channel_name in self._channels:
                self._states[channel_name] = False
            self._is_initialized = True
            self.logger.info("继电器初始化成功")
        except Exception as e:
            self.logger.error(f"继电器初始化失败: {e}")
            raise

    def turn_on(self, channel: str) -> bool:
        """
        打开指定通道
        Args:
            channel: 通道名称
            
        Returns:
            bool: 操作是否成功
        """
        if not self._is_initialized:
            self.logger.error("继电器未初始化")
            return False

        if channel not in self._channels:
            self.logger.error(f"无效的通道名称: {channel}")
            return False

        try:
            channel_num = self._channels[channel]
            self.logger.info(f"打开通道: {channel} (CH{channel_num})")
            self._send_command(channel_num, self._cmd_on)
            self._states[channel] = True
            return True
        except Exception as e:
            self.logger.error(f"打开通道失败: {e}")
            return False

    def turn_off(self, channel: str) -> bool:
        """
        关闭指定通道
        
        Args:
            channel: 通道名称
            
        Returns:
            bool: 操作是否成功
        """
        if not self._is_initialized:
            self.logger.error("继电器未初始化")
            return False

        if channel not in self._channels:
            self.logger.error(f"无效的通道名称: {channel}")
            return False

        try:
            channel_num = self._channels[channel]
            self.logger.info(f"关闭通道: {channel} (CH{channel_num})")
            self._send_command(channel_num, self._cmd_off)
            self._states[channel] = False
            return True
        except Exception as e:
            self.logger.error(f"关闭通道失败: {e}")
            return False

    def open_vacuum_valve(self) -> bool:
        """Open the spin chuck vacuum valve."""
        return self.turn_on("vacuum_valve")

    def close_vacuum_valve(self) -> bool:
        """Close the spin chuck vacuum valve."""
        return self.turn_off("vacuum_valve")

    def get_state(self, channel: str) -> bool:
        """
        获取通道状态
        
        Args:
            channel: 通道名称
            
        Returns:
            bool: 通道状态
        """
        if channel not in self._states:
            self.logger.error(f"无效的通道名称: {channel}")
            return False
        return self._states[channel]

    def get_all_states(self) -> Dict[str, bool]:
        """
        获取所有通道状态
        
        Returns:
            Dict[str, bool]: 所有通道状态
        """
        return self._states.copy()

    def emergency_stop(self):
        """
        紧急停止，关闭所有通道
        """
        self.logger.warning("执行紧急停止，强制关闭所有通道！")
        if self.mock:
            for channel in self._channels:
                self._states[channel] = False
            return
        for channel in self._channels:
            self.turn_off(channel)

    def shutdown(self):
        """
        关闭继电器
        """
        try:
            self.logger.info("关闭继电器")
            # 这里添加实际的关闭操作
            # 关闭所有通道
            self.emergency_stop()
        except Exception as e:
            self.logger.error(f"关闭继电器失败: {e}")

    def close(self):
        """
        关闭管理器
        """
        try:
            self.shutdown()
        finally:
            self.disconnect()
