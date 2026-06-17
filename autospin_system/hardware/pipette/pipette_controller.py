# AutoSpinmotorSystem/hardware/pipette/pipette_controller.py

import time
import logging
from typing import Dict, Any

try:
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ModuleNotFoundError:  # Direct execution from the project root.
    from config.hardware_config import CONFIG
from .driver_communication import PipetteDriver


# 寄存器地址常量（参考手册，与 v1 保持一致）
class Registers:
    # 输入寄存器
    STATUS = 0x00
    HOMED = 0x01
    DRIVER_FAULT = 0x02
    POS_H = 0x03
    POS_L = 0x04
    ENC_H = 0x05
    ENC_L = 0x06
    SPEED = 0x07
    LIQ_DET_STATE = 0x08
    ASP_STATE = 0x09
    DISP_STATE = 0x0A
    PRESSURE_P2P = 0x0C
    TIP_PRESENT = 0x0D

    # 保持寄存器
    CTRL = 0x00
    RUN_CURR = 0x01
    HOLD_CURR = 0x02
    VEL = 0x03
    ACC = 0x04
    DEC = 0x05
    VOL_H = 0x06
    VOL_L = 0x07
    NODE_ID = 0x08
    BAUDRATE = 0x09
    OFFSET = 0x0A
    TIP_CAP = 0x0D
    CALIB_A = 0x0E
    CALIB_B = 0x0F
    LIQ_THRESH = 0x10
    BUBBLE_THRESH = 0x11
    CLOT_THRESH = 0x12
    AIR_THRESH = 0x13
    LIQ_TIMEOUT = 0x14

    # 线圈
    COIL_AUTO_HOME = 0x00
    COIL_SAVE = 0x01
    COIL_RESET = 0x02
    COIL_TERM = 0x03


# 动作码
class ActionCode:
    HOME = 0x00
    ABS_MOVE = 0x01
    REL_FWD = 0x02
    REL_BWD = 0x03
    JOG_FWD = 0x04
    JOG_BWD = 0x05
    SLOW_STOP = 0x06
    IMM_STOP = 0x07
    LIQ_DETECT = 0x08
    ASPIRATE = 0x0A
    DISPENSE = 0x0B
    DROP_TIP = 0x0C


# 默认运动参数
DEFAULT_SPEED = 10
DEFAULT_ACCEL = 20
DEFAULT_DECEL = 20


class PipetteController:
    """
    移液枪控制器（28系列，Modbus RTU）
    """

    # 默认配置参数
    DEFAULT_MAX_VOLUME = 1000  # 最大量程 1000uL

    def __init__(self, mock: bool = False, port: str = None,
                 logger: logging.Logger = None):
        """
        初始化移液枪控制器
        Args:
            port: 串口号 (由上层传入，或在 yaml 中指定)
            mock: 是否启用软件模拟模式
            logger: 日志记录器
        """
        self.logger = logger or logging.getLogger(__name__)
        self.mock = mock
        self._is_initialized = False

        # 1. 从统一配置中读取移液枪参数
        pipette_cfg = CONFIG['devices']['pipette']
        self.max_volume = pipette_cfg['max_volume_ul']
        self.port = port

        # 2. 实例化底层驱动通信 (将 YAML 参数注入)
        self.comm = PipetteDriver(
            port=self.port,
            slave_id=pipette_cfg['slave_id'],
            baudrate=pipette_cfg['baudrate'],
            timeout=pipette_cfg['timeout'],
            mock=self.mock,
            logger=self.logger
        )

    def connect(self) -> bool:
        """
        显式连接方法
        打开串口并执行设备的初始化（归位、设置速度）
        """
        if not self.comm.connect():
            self.logger.error(f"移液枪串口 {self.port} 打开失败！")
            return False

        self.logger.info(f"串口已连接。准备初始化移液枪，最大量程: {self.max_volume}uL")
        self._initialize_pipette()
        return self._is_initialized

    def _initialize_pipette(self):
        """初始化设备：确保已归位，设置默认运动参数"""
        try:
            if not self.is_homed():
                if not self.home(timeout=30):
                    self.logger.error("归位失败，移液枪初始化未完成")
                    return
            self.set_speed(DEFAULT_SPEED)
            self.set_accel(DEFAULT_ACCEL)
            self.set_decel(DEFAULT_DECEL)
            self._is_initialized = True
            self.logger.info("移液枪初始化成功")
        except Exception as e:
            self.logger.error(f"移液枪初始化失败: {e}")

    # ---------- 状态查询 ----------

    def get_status_word(self) -> int:
        """读取状态字 (0:IDLE,1:STOP,2:ACC,3:DEC,4:CONST,5:POSCORR)"""
        return self.comm.read_input_registers(Registers.STATUS)[0]

    def is_homed(self) -> bool:
        """位置是否已归位"""
        return self.comm.read_input_registers(Registers.HOMED)[0] == 1

    def tip_present(self) -> bool:
        """Tip头是否在位"""
        return self.comm.read_input_registers(Registers.TIP_PRESENT)[0] == 1

    def get_actual_position(self) -> int:
        """读取当前脉冲位置(32位)"""
        high = self.comm.read_input_registers(Registers.POS_H)[0]
        low = self.comm.read_input_registers(Registers.POS_L)[0]
        return (high << 16) | low

    def _read_driver_fault(self) -> bool:
        """读取驱动器异常标志"""
        return self.comm.read_input_registers(Registers.DRIVER_FAULT)[0] != 0

    def _read_aspirate_state_raw(self) -> int:
        """读取原始吸液状态寄存器值"""
        return self.comm.read_input_registers(Registers.ASP_STATE)[0]

    def _read_dispense_state_raw(self) -> int:
        """读取原始吐液状态寄存器值"""
        return self.comm.read_input_registers(Registers.DISP_STATE)[0]

    def _read_liquid_detect_state_raw(self) -> int:
        """读取原始液面探测状态寄存器值"""
        return self.comm.read_input_registers(Registers.LIQ_DET_STATE)[0]

    def get_aspirate_state(self):
        """
        返回吸液状态的解析结果
        :return: (空闲标志, 异常标志) 异常标志为bit5-7
        """
        val = self._read_aspirate_state_raw()
        idle = (val & 0x01) == 0
        abnormal = (val >> 5) & 0x07
        return idle, abnormal

    def wait_for_idle(self, timeout=10) -> bool:
        """等待移液枪进入空闲状态"""
        start = time.time()
        while time.time() - start < timeout:
            if self.get_status_word() == 0:
                return True
            time.sleep(0.1)
        return False

    # ---------- 运动参数设置 ----------

    def set_speed(self, speed_01rps):
        """设置速度（0.1转/秒）"""
        self.comm.write_register(Registers.VEL, speed_01rps)

    def set_accel(self, accel_01rpss):
        """设置加速度（0.1转/平方秒）"""
        self.comm.write_register(Registers.ACC, accel_01rpss)

    def set_decel(self, decel_01rpss):
        """设置减速度（0.1转/平方秒）"""
        self.comm.write_register(Registers.DEC, decel_01rpss)

    # ---------- 核心动作 ----------

    def home(self, timeout=30) -> bool:
        """原点回归"""
        self.comm.write_register(Registers.CTRL, ActionCode.HOME)
        return self.wait_for_idle(timeout)

    def aspirate(self, volume: int, detect_mask: int = 0) -> bool:
        """
        吸取液体

        Args:
            volume: 吸取体积 (uL)
            detect_mask: 检测使能掩码 (bit7:空吸, bit6:凝块, bit5:气泡)

        Returns:
            bool: 操作是否成功
        """
        if not self._is_initialized:
            self.logger.error("移液枪未初始化")
            return False

        if volume < 0 or volume > self.max_volume:
            self.logger.error(f"吸取体积超出范围: {volume}uL (0-{self.max_volume}uL)")
            return False

        if not self.tip_present():
            raise RuntimeError("未检测到Tip头，请安装Tip头")

        self.logger.info(f"吸取液体: {volume}uL")

        # 设置体积（拆分为高低16位）
        high = (volume >> 16) & 0xFFFF
        low = volume & 0xFFFF
        self.comm.write_registers(Registers.VOL_H, [high, low])

        # 构造控制字
        ctrl = (detect_mask << 4) | ActionCode.ASPIRATE
        self.comm.write_register(Registers.CTRL, ctrl)

        # 等待吸液完成（轮询吸液状态bit0）
        start = time.time()
        timeout = 10
        while time.time() - start < timeout:
            idle, _ = self.get_aspirate_state()
            if idle:
                return True
            time.sleep(0.05)
        return False

    def dispense(self, volume: int) -> bool:
        """
        释放液体

        Args:
            volume: 释放体积 (uL)

        Returns:
            bool: 操作是否成功
        """
        if not self._is_initialized:
            self.logger.error("移液枪未初始化")
            return False

        if volume < 0 or volume > self.max_volume:
            self.logger.error(f"释放体积超出范围: {volume}uL (0-{self.max_volume}uL)")
            return False

        if not self.tip_present():
            raise RuntimeError("未检测到Tip头")

        self.logger.info(f"释放液体: {volume}uL")

        high = (volume >> 16) & 0xFFFF
        low = volume & 0xFFFF
        self.comm.write_registers(Registers.VOL_H, [high, low])
        self.comm.write_register(Registers.CTRL, ActionCode.DISPENSE)
        return self.wait_for_idle(10)

    def blowout(self) -> bool:
        """吹出残留液体（吐液到底）"""
        try:
            self.logger.info("执行吹出操作")
            self.comm.write_register(Registers.CTRL, ActionCode.DISPENSE)
            return self.wait_for_idle(10)
        except Exception as e:
            self.logger.error(f"吹出操作失败: {e}")
            return False

    def tip_eject(self) -> bool:
        """弹出吸头（退Tip）"""
        try:
            self.logger.info("弹出吸头")
            if not self.tip_present():
                return True  # 已无Tip
            self.comm.write_register(Registers.CTRL, ActionCode.DROP_TIP)
            return self.wait_for_idle(10)
        except Exception as e:
            self.logger.error(f"弹出吸头失败: {e}")
            return False

    def liquid_detect(self, timeout=10) -> bool:
        """
        启动液面探测（需外部Z轴配合下降）
        返回探测结果：True成功，False失败/超时
        """
        self.comm.write_register(Registers.CTRL, ActionCode.LIQ_DETECT)
        start = time.time()
        while time.time() - start < timeout:
            state = self._read_liquid_detect_state_raw()
            if state == 2:    # 完成
                return True
            elif state == 3:  # 失败
                return False
            time.sleep(0.05)
        return False

    # ---------- 统一设备接口 ----------

    def start(self) -> bool:
        """启动设备（归位 + 设置默认参数）"""
        if not self.is_homed():
            if not self.home(timeout=30):
                return False
        self.set_speed(DEFAULT_SPEED)
        self.set_accel(DEFAULT_ACCEL)
        self.set_decel(DEFAULT_DECEL)
        return True

    def stop(self) -> bool:
        """立即停止当前运动（紧急停止）"""
        self.comm.write_register(Registers.CTRL, ActionCode.IMM_STOP)
        return True

    def reset(self) -> bool:
        """复位设备：先停止，清除故障，重新归位"""
        self.stop()
        time.sleep(0.1)
        try:
            return self.home(timeout=30)
        except Exception:
            return False

    def get_status(self) -> Dict[str, Any]:
        """获取移液枪状态"""
        return {
            'is_initialized': self._is_initialized,
            'max_volume': self.max_volume,
            'status_word': self.get_status_word(),
            'homed': self.is_homed(),
            'tip_present': self.tip_present(),
            'position': self.get_actual_position(),
            'driver_fault': self._read_driver_fault(),
            'aspirate_state': self._read_aspirate_state_raw(),
            'dispense_state': self._read_dispense_state_raw(),
            'liquid_detect_state': self._read_liquid_detect_state_raw(),
        }

    # ---------- 参数读写（高级）----------

    def set_liq_threshold(self, value):
        """设置液面探测阈值（写入0恢复默认）"""
        self.comm.write_register(Registers.LIQ_THRESH, value)

    def set_bubble_threshold(self, value):
        self.comm.write_register(Registers.BUBBLE_THRESH, value)

    def set_clot_threshold(self, value):
        self.comm.write_register(Registers.CLOT_THRESH, value)

    def set_air_threshold(self, value):
        self.comm.write_register(Registers.AIR_THRESH, value)

    def save_parameters(self):
        """保存参数到Flash"""
        self.comm.write_coil(Registers.COIL_SAVE, 1)

    def reset_parameters(self):
        """恢复出厂设置"""
        self.comm.write_coil(Registers.COIL_RESET, 1)

    def shutdown(self):
        """关闭移液枪"""
        try:
            self.logger.info("关闭移液枪")
            self.comm.close()
        except Exception as e:
            self.logger.error(f"关闭移液枪失败: {e}")

    def close(self):
        """关闭控制器"""
        self.shutdown()
