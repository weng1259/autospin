# AutoSpinmotorSystem/hardware/pipette/pipette_controller.py

import time
import logging
from typing import Dict, Any

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
    IDLE = 0x00
    HOME = 0x01
    ABS_MOVE = 0x02
    REL_FWD = 0x03
    REL_BWD = 0x04
    JOG_FWD = 0x05
    JOG_BWD = 0x06
    SLOW_STOP = 0x07
    IMM_STOP = 0x08
    LIQ_DETECT = 0x09
    ASPIRATE = 0x0A
    DISPENSE = 0x0B
    DROP_TIP = 0x0C


# 默认运动参数
DEFAULT_RUN_CURRENT = 50
DEFAULT_HOLD_CURRENT = 10
DEFAULT_SPEED = 150
DEFAULT_ACCEL = 1250
DEFAULT_DECEL = 1250
EJECT_RUN_CURRENT = 80
EJECT_SPEED = 100
EJECT_TRAVEL = 1000


class PipetteController:
    """
    移液枪控制器（28系列，Modbus RTU）
    """

    # 默认配置参数
    DEFAULT_MAX_VOLUME = 1000  # 最大量程 1000uL

    def __init__(
        self,
        mock: bool = False,
        port: str = None,
        logger: logging.Logger = None,
        *,
        slave_id: int = 1,
        baudrate: int = 115200,
        timeout: float = 2.0,
        max_volume_ul: float = 1000,
    ):
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

        self.max_volume = max_volume_ul
        self.port = port

        # 2. 实例化底层驱动通信 (将 YAML 参数注入)
        self.comm = PipetteDriver(
            port=self.port,
            slave_id=slave_id,
            baudrate=baudrate,
            timeout=timeout,
            mock=self.mock,
            logger=self.logger
        )

    def connect(self, initialize: bool = True) -> bool:
        """
        显式连接方法
        打开串口并执行设备的初始化（归位、设置速度）
        """
        if not self.comm.connect():
            self.logger.error(f"移液枪串口 {self.port} 打开失败！")
            return False

        if not initialize:
            self.logger.info(
                "Pipette serial connected without initialization "
                "(port=%s, max_volume=%suL)",
                self.port,
                self.max_volume,
            )
            return True

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
            self.set_run_current(DEFAULT_RUN_CURRENT)
            self.set_hold_current(DEFAULT_HOLD_CURRENT)
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
        high, low = self.comm.read_input_registers(Registers.POS_H, count=2)
        value = (high << 16) | low
        return value - (1 << 32) if value & (1 << 31) else value

    def _read_driver_fault(self) -> bool:
        """读取驱动器异常标志"""
        return self.comm.read_input_registers(Registers.DRIVER_FAULT)[0] != 0

    def _read_aspirate_state_raw(self) -> int:
        """读取原始吸液状态寄存器值"""
        return self.comm.read_input_registers(Registers.ASP_STATE)[0]

    def _read_dispense_state_raw(self) -> int:
        """读取原始吐液状态寄存器值"""
        return self.comm.read_input_registers(Registers.DISP_STATE)[0]

    def _read_pressure_p2p_raw(self) -> int:
        """Read the last aspirate pressure peak-to-peak value."""
        return self.comm.read_input_registers(Registers.PRESSURE_P2P)[0]

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

    def _wait_for_action_cycle(self, read_state, timeout=10) -> bool:
        """等待动作状态从空闲进入运行，再回到空闲。

        不能在写入动作命令后仅凭第一次读到 IDLE 就返回成功：设备可能尚未
        来得及置位动作状态。吸液和吐液分别使用手册定义的专用状态寄存器。
        """
        if self.mock:
            return True

        started = False
        start = time.time()
        while time.time() - start < timeout:
            active = bool(read_state())
            if active:
                if not started:
                    self.logger.info("Pipette action state entered active")
                started = True
            elif started:
                self.logger.info("Pipette action state returned idle")
                return True
            time.sleep(0.1)
        self.logger.warning("Pipette action did not complete within %.1fs; started=%s", timeout, started)
        return False

    # ---------- 运动参数设置 ----------

    def _diagnostic_snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        readers = {
            "status_word": self.get_status_word,
            "homed": self.is_homed,
            "tip_present": self.tip_present,
            "driver_fault": self._read_driver_fault,
            "position": self.get_actual_position,
            "aspirate_state": self._read_aspirate_state_raw,
            "dispense_state": self._read_dispense_state_raw,
            "liquid_detect_state": self._read_liquid_detect_state_raw,
            "pressure_p2p": self._read_pressure_p2p_raw,
        }
        for key, reader in readers.items():
            try:
                snapshot[key] = reader()
            except Exception as exc:
                snapshot[f"{key}_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

    def _log_diagnostic_delta(self, label: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        before_pos = before.get("position")
        after_pos = after.get("position")
        delta = None
        if isinstance(before_pos, int) and isinstance(after_pos, int):
            delta = after_pos - before_pos
        self.logger.info(
            "Pipette diagnostic %s: before=%s after=%s position_delta=%s",
            label,
            before,
            after,
            delta,
        )

    def set_speed(self, speed_01rps):
        """设置速度（0.1转/秒）"""
        self.comm.write_register(Registers.VEL, speed_01rps)

    def set_run_current(self, percent):
        """Set motor run current percentage."""
        self.comm.write_register(Registers.RUN_CURR, int(percent))

    def set_hold_current(self, percent):
        """Set motor holding current percentage."""
        self.comm.write_register(Registers.HOLD_CURR, int(percent))

    def set_accel(self, accel_01rpss):
        """设置加速度（0.1转/平方秒）"""
        self.comm.write_register(Registers.ACC, accel_01rpss)

    def set_decel(self, decel_01rpss):
        """设置减速度（0.1转/平方秒）"""
        self.comm.write_register(Registers.DEC, decel_01rpss)

    # ---------- 核心动作 ----------

    def ensure_initialized(self) -> bool:
        """Home and configure motion parameters if the controller is not ready."""
        if self._is_initialized:
            return True
        self._initialize_pipette()
        return self._is_initialized

    def _write_action(
        self,
        action: int,
        *,
        volume: int | None = None,
        motion_words: list[int] | None = None,
        run_current: int | None = None,
        hold_current: int | None = None,
        speed: int | None = None,
        acceleration: int | None = None,
        deceleration: int | None = None,
    ) -> None:
        values = [
            int(action),
            DEFAULT_RUN_CURRENT if run_current is None else int(run_current),
            DEFAULT_HOLD_CURRENT if hold_current is None else int(hold_current),
            DEFAULT_SPEED if speed is None else int(speed),
            DEFAULT_ACCEL if acceleration is None else int(acceleration),
            DEFAULT_DECEL if deceleration is None else int(deceleration),
        ]
        if motion_words is not None:
            values.extend([int(word) & 0xFFFF for word in motion_words])
        elif volume is not None:
            values.extend([
                (int(volume) >> 16) & 0xFFFF,
                int(volume) & 0xFFFF,
            ])
        self.logger.info(
            "Pipette action write: action=0x%02X volume=%s registers0..%d=%s",
            int(action),
            volume,
            len(values) - 1,
            values,
        )
        self.comm.write_register(Registers.CTRL, ActionCode.IDLE)
        time.sleep(0.05)
        self.comm.write_registers(Registers.CTRL, values)
        try:
            readback = self.comm.read_holding_registers(Registers.CTRL, len(values))
            self.logger.info("Pipette action readback registers0..%d=%s", len(values) - 1, readback)
        except Exception as exc:
            self.logger.warning("Pipette action readback failed: %s", exc)

    def home(self, timeout=30) -> bool:
        """执行原点回归，并等待设备明确置位归位标志。"""
        # The controller needs an IDLE -> HOME edge to accept another home command.
        self._write_action(ActionCode.HOME)
        if self.mock:
            return True

        started = False
        start = time.time()
        while time.time() - start < timeout:
            homed = self.is_homed()
            if not homed:
                started = True
            elif started and self.get_status_word() == 0:
                return True
            time.sleep(0.1)
        self.comm.write_register(Registers.CTRL, ActionCode.IMM_STOP)
        return False

    def aspirate(self, volume: int, detect_mask: int = 0) -> bool:
        """
        吸取液体

        Args:
            volume: 吸取体积 (uL)
            detect_mask: 检测使能掩码 (bit7:空吸, bit6:凝块, bit5:气泡)

        Returns:
            bool: 操作是否成功
        """
        if not self.ensure_initialized():
            self.logger.error("移液枪未初始化")
            return False

        if volume < 0 or volume > self.max_volume:
            self.logger.error(f"吸取体积超出范围: {volume}uL (0-{self.max_volume}uL)")
            return False

        if not self.tip_present():
            raise RuntimeError("未检测到Tip头，请安装Tip头")

        self.logger.info(f"吸取液体: {volume}uL")

        # Volume is written together with the action below, matching the vendor
        # ModbusPoll examples that write holding registers 0..7 in one frame.

        if not 0 <= int(detect_mask) <= 0x07:
            raise ValueError("detect_mask must be between 0 and 7")

        # 手册规定检测使能位为 bit7-bit5，低四位保持吸液动作码 0x0A。
        ctrl = (int(detect_mask) << 5) | ActionCode.ASPIRATE
        before = self._diagnostic_snapshot()
        self._write_action(ctrl, volume=volume)
        ok = self._wait_for_action_cycle(
            lambda: self._read_aspirate_state_raw() & 0x01,
            timeout=10,
        )
        self._log_diagnostic_delta("aspirate", before, self._diagnostic_snapshot())
        return ok

    def dispense(self, volume: int) -> bool:
        """
        释放液体

        Args:
            volume: 释放体积 (uL)

        Returns:
            bool: 操作是否成功
        """
        if not self.ensure_initialized():
            self.logger.error("移液枪未初始化")
            return False

        if volume < 0 or volume > self.max_volume:
            self.logger.error(f"释放体积超出范围: {volume}uL (0-{self.max_volume}uL)")
            return False

        if not self.tip_present():
            raise RuntimeError("未检测到Tip头")

        self.logger.info(f"释放液体: {volume}uL")

        before = self._diagnostic_snapshot()
        self._write_action(ActionCode.DISPENSE, volume=volume)
        ok = self._wait_for_action_cycle(
            lambda: self._read_dispense_state_raw() != 0,
            timeout=10,
        )
        self._log_diagnostic_delta("dispense", before, self._diagnostic_snapshot())
        return ok

    def blowout(self) -> bool:
        """吹出残留液体（吐液到底）"""
        try:
            self.logger.info("执行吹出操作")
            self._write_action(ActionCode.DISPENSE)
            return self.wait_for_idle(10)
        except Exception as e:
            self.logger.error(f"吹出操作失败: {e}")
            return False

    def tip_eject(self) -> bool:
        """弹出吸头（退Tip）"""
        try:
            self.logger.info("弹出吸头")
            if not self.ensure_initialized():
                self.logger.error("移液枪未初始化")
                return False
            if not self.tip_present():
                self.logger.warning("Tip present flag is false; sending eject command anyway")
            before = self._diagnostic_snapshot()
            self._write_action(
                ActionCode.DROP_TIP,
                motion_words=[EJECT_TRAVEL, 0],
                run_current=EJECT_RUN_CURRENT,
                speed=EJECT_SPEED,
            )
            ok = self._wait_for_action_cycle(
                lambda: self.get_status_word() != 0,
                timeout=10,
            )
            self._log_diagnostic_delta("tip_eject", before, self._diagnostic_snapshot())
            return ok
        except Exception as e:
            self.logger.error(f"弹出吸头失败: {e}")
            return False

    def liquid_detect(self, timeout=10) -> bool:
        """
        启动液面探测（需外部Z轴配合下降）
        返回探测结果：True成功，False失败/超时
        """
        self._write_action(ActionCode.LIQ_DETECT)
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

    def get_status(self, *, live: bool = False) -> Dict[str, Any]:
        """获取移液枪状态"""
        status = {
            'is_initialized': self._is_initialized,
            'max_volume': self.max_volume,
            'live_feedback': bool(live),
        }
        if not live:
            return status
        status.update({
            'status_word': self.get_status_word(),
            'homed': self.is_homed(),
            'tip_present': self.tip_present(),
            'position': self.get_actual_position(),
            'driver_fault': self._read_driver_fault(),
            'aspirate_state': self._read_aspirate_state_raw(),
            'dispense_state': self._read_dispense_state_raw(),
            'liquid_detect_state': self._read_liquid_detect_state_raw(),
        })
        return status

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
