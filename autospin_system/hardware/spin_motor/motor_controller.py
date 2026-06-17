# AutoSpinmotorSystem/hardware/spin_motor/motor_controller.py
# DBLS400 电机控制逻辑模块
# 高层控制：启动、停止、设转速、读转速

import time
import logging
from enum import Enum
from typing import Tuple, Dict, Any

try:
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ModuleNotFoundError:  # Direct execution from the project root.
    from config.hardware_config import CONFIG
from .driver_communication import DriverCommunication


class Registers:
    CONTROL = 0x8000  # 控制字寄存器  控制电机启动、停止、方向、刹车
    SPEED_SET = 0x8005  # 目标转速寄存器  设置目标转速
    ACTUAL_SPEED = 0x8018  # 实时转速反馈寄存器  读取实时转速
    BUS_VOLTAGE = 0x8019  # 母线电压寄存器  读取母线电压


class MotorDirection(str, Enum):
    FORWARD = 'forward'
    REVERSE = 'reverse'


class MotorStatus(Enum):
    STOPPED = 'stopped'
    RUNNING = 'running'
    FAULT = 'fault'
    BRAKING = 'braking'
    UNKNOWN = 'unknown'


class MotorController:
    """
    针对 DBLS400 驱动器优化的控制器
    极对数位移为 8,控制逻辑 Bit 3 必须常置 1
    """

    # 默认配置参数
    DEFAULT_POLE_PAIRS = 4
    DEFAULT_HALL_ANGLE = 1
    STARTUP_DELAY = 0.5
    DIRECTION_CHANGE_DELAY = 1.0
    MAX_SPEED_RPM = 6000

    def __init__(self, port: str = None, mock: bool = False, logger: logging.Logger = None):
        self.logger = logger or logging.getLogger(__name__)
        self.mock = mock

        # 从 YAML 配置树中提取电机专属配置
        motor_cfg = CONFIG['devices']['spin_motor']

        self.port = port
        self.pole_pairs = motor_cfg.get('pole_pairs', 4)
        # 允许通过配置覆盖最大转速，默认 6000
        self.max_speed_rpm = motor_cfg.get('max_rpm', MotorController.MAX_SPEED_RPM)

        # 实例化底层通信驱动
        self.comm = DriverCommunication(
            port=self.port,
            slave_id=motor_cfg['slave_id'],
            baudrate=motor_cfg['baudrate'],
            timeout=motor_cfg['timeout'],
            mock=self.mock,
            logger=self.logger
        )

        self.current_id = self.comm.slave_id

        # 状态变量
        self._status = MotorStatus.UNKNOWN
        self._direction = MotorDirection.FORWARD
        self._target_speed = 0
        self._actual_speed = 0
        self._is_initialized = False

        self.logger.info(f"电机控制器实例已创建 (Port: {self.port}, 目标ID: {self.current_id}, Mock: {self.mock})")

    def connect(self) -> bool:
        """建立串口连接并初始化驱动器
            显式连接方法，防止初始化时串口未开导致崩溃
        """
        if not self.comm.connect():
            self.logger.error(f"旋涂电机串口 {self.port} 打开失败！")
            return False

        self.logger.info("串口已连接，正在下发初始控制字...")
        self._initialize_driver()
        return self._is_initialized

    def _get_control_word(self, run: bool = False, reverse: bool = False, brake: bool = False) -> int:
        """
        合成控制字，电机控制核心逻辑：
        - Bit 0 (EN): 使能位，1=运行，0=停止
        - Bit 1 (FR): 方向位，1=反转，0=正转
        - Bit 2 (BK): 刹车位，1=刹车，0=不刹车
        - Bit 3 (基础位): 必须常置 1，表示商家驱动器的运行状态
        根据实测 0x0409 (运行) 和 0x0408 (停止) 推导
        """
        # 基础位 0x08 (Bit 3) 是商家驱动器运行的必要前提
        control_bits = 0x08

        # |= : 把变量最低位（第 0 位）强制置 1，其余位保持不变。
        if run:
            control_bits |= 0x01  # Bit 0: 使能 (EN)

        if reverse:
            control_bits |= 0x02  # Bit 1: 方向 (FR)

        if brake:
            control_bits |= 0x04  # Bit 2: 刹车 (BK)

        # 极对数左移8位 (高字节)，控制位在低字节
        # | 按位或，高字节不动，低字节替换成 control_bits，等效高低字节拼接
        return (self.pole_pairs << 8) | control_bits

    def _initialize_driver(self):
        """初始化驱动器配置"""
        try:
            # 写入初始停止状态 (0x0408)
            stop_val = self._get_control_word(run=False)
            success = self.comm.write_register(self.comm.REG_CONTROL, stop_val)

            if success:
                self._is_initialized = True
                self._status = MotorStatus.STOPPED
                self.logger.info(f"驱动器初始化成功: 极对数={self.pole_pairs}, 控制字=0x{stop_val:04X}")
            else:
                self.logger.error("驱动器初始化写入失败")
        except Exception as e:
            self.logger.error(f"初始化异常: {e}")

    def unlock(self) -> bool:
        """
        使能驱动器（准备就绪状态）
        对应 Bit 3 置 1，但 Bit 0 (EN) 为 0 的状态
        """
        self.logger.info("正在使能/解锁驱动器（进入就绪状态）...")
        # 获取停止状态的控制字 (run=False 会保持 Bit 3 为 1，Bit 0 为 0)
        unlock_val = self._get_control_word(run=False)
        success = self.comm.write_register(self.comm.REG_CONTROL, unlock_val)

        if success:
            self._is_initialized = True
            self._status = MotorStatus.STOPPED
            self.logger.info(f"驱动器已解锁并就绪: 0x{unlock_val:04X}")
            return True
        else:
            self.logger.error("驱动器解锁写入失败")
            return False

    def lock(self):
        """锁定电机（开启抱闸，禁止转动）"""
        self.logger.info("电机已锁定（抱闸开启）")
        # 0x08 (基础位) | 0x04 (刹车位) = 0x0C
        # 极对数位移 8 位 -> 0x040C
        stop_val = self._get_control_word(run=False, brake=True)
        self.comm.write_register(self.comm.REG_CONTROL, stop_val)

    def start(self, direction: str = 'forward', wait_for_stop: bool = True) -> Tuple[bool, str]:
        """
        启动电机
        :param direction: 运动方向 'forward' 或 'reverse'
        :param wait_for_stop: 是否在启动前等待电机完全静止
        """
        if not self._is_initialized:
            return False, "控制器未初始化"

        # ---------- 零速启动校验逻辑 ----------
        if wait_for_stop:
            self.logger.info("启动前校验：确保转盘处于静止状态...")
            # 发送一次停止指令，以防之前的指令序列还在执行
            stop_val = self._get_control_word(run=False)
            self.comm.write_register(self.comm.REG_CONTROL, stop_val)

            max_retries = 10  # 约等待 2 秒
            for i in range(max_retries):
                # 获取绝对速度值
                actual_speed = abs(self.get_actual_speed())
                if actual_speed < 5.0:
                    self.logger.info(f"电机已静止 (当前速度: {actual_speed} RPM)，准备执行启动。")
                    break
                if i == max_retries - 1:
                    self.logger.warning(f"警告：电机未完全静止 (当前速度: {actual_speed} RPM)，强制启动可能会有冲击。")
                time.sleep(0.2)
            # ------------------------------------------

        is_reverse = (direction.lower() == 'reverse')
        start_val = self._get_control_word(run=True, reverse=is_reverse)

        self.logger.info(f"启动电机: 方向={direction}, 指令=0x{start_val:04X}")

        success = self.comm.write_register(self.comm.REG_CONTROL, start_val) # 发送启动指令
        if success:
            time.sleep(self.STARTUP_DELAY)
            self._status = MotorStatus.RUNNING
            self._direction = MotorDirection.REVERSE if is_reverse else MotorDirection.FORWARD
            return True, "启动指令已发送"
        return False, "通讯失败"

    def stop(self, use_brake: bool = True) -> bool:
        """停止电机"""
        stop_val = self._get_control_word(run=False, brake=use_brake)
        success = self.comm.write_register(self.comm.REG_CONTROL, stop_val)
        if success:
            self._status = MotorStatus.STOPPED
            self._target_speed = 0
            return True
        return False

    def set_speed(self, rpm: float, **kwargs) -> Tuple[bool, str]:
        """设置闭环转速"""
        if self._status != MotorStatus.RUNNING:
            return False, "电机未运行"

        target_rpm = int(min(max(rpm, 0), self.MAX_SPEED_RPM))
        success = self.comm.write_register(self.comm.REG_SPEED_SET, target_rpm)

        if success:
            self._target_speed = target_rpm
            return True, f"转速已设为 {target_rpm} RPM"
        return False, "设置速度失败"

    def get_actual_speed(self) -> float:
        """
        从驱动器读取实时转速反馈 (Actual RPM)
        """
        try:
            data = self.comm.read_register(Registers.ACTUAL_SPEED, 1)

            if data is not None and len(data) > 0:
                raw_rpm = float(data[0])
                speed_factor = 2.5
                self._actual_speed = raw_rpm * speed_factor
                return self._actual_speed
            else:
                self.logger.warning("无法读取实时转速，使用上次记录值")
                return self._actual_speed if self._actual_speed is not None else 0.0

        except Exception as e:
            self.logger.error(f"读取转速异常: {e}")
            return self._actual_speed if self._actual_speed is not None else 0.0

    def emergency_stop(self):
        """紧急刹车"""
        self.stop(use_brake=True)

    def shutdown(self):
        """安全关机"""
        self.stop()
        self.comm.close()

    def get_status(self) -> Dict[str, Any]:
        """获取状态汇总"""
        return {
            'status': self._status.value,
            'direction': self._direction.value,
            'target_speed': self._target_speed,
            'actual_speed': self.get_actual_speed(),
            'is_initialized': self._is_initialized
        }

    def close(self):
        """关闭控制器"""
        self.shutdown()
