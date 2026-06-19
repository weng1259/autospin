# AutoSpinmotorSystem/maestro.py
# 系统控制器，负责协调所有硬件和工作流程

import time
import logging
import threading
import concurrent.futures
from typing import Dict, Any

try:
    from autospin_system.config.hardware_config import CONFIG
    from autospin_system.hardware.spin_motor.motor_controller import MotorController
    from autospin_system.hardware.pipette.pipette_controller import PipetteController
    from autospin_system.hardware.relay.relay_manager import RelayManager
    from autospin_system.hardware.xyz_stage.xyz_stage import XYZStage
    from autospin_system.hardware.heating_stage.heating_stage_controller import HeatingStageController
except ImportError:
    from config.hardware_config import CONFIG
    from hardware.spin_motor.motor_controller import MotorController
    from hardware.pipette.pipette_controller import PipetteController
    from hardware.relay.relay_manager import RelayManager
    from hardware.xyz_stage.xyz_stage import XYZStage
    from hardware.heating_stage.heating_stage_controller import HeatingStageController


def resolve_serial_ports(comm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把 communication 配置解析成「设备 → 串口」映射（单一真相源）。

    旋涂电机 / 移液枪 / 加热台共用同一条 USB-RS485 总线：只要设了
    rs485_bus_port，三者都解析到它，并据此判定 shared_rs485（由 Maestro
    用 SharedRs485DeviceProxy 串行化共享口的 open-use-close 访问，每次按
    各设备自己的 baud 重开）。继电器与龙门各自独占串口。

    回退默认值用树莓派 udev 稳定名而非 Windows COM 口：即使某字段缺失，
    也绝不会把设备解析到龙门的 grbl 口（/dev/autospin_xyz）。
    """
    rs485_bus_port = comm_cfg.get('rs485_bus_port')
    motor_port = rs485_bus_port or comm_cfg.get('motor_port', '/dev/autospin_rs485')
    pipette_port = rs485_bus_port or comm_cfg.get('pipette_port', '/dev/autospin_rs485')
    heating_stage_port = rs485_bus_port or comm_cfg.get('heating_stage_port', '/dev/autospin_rs485')
    relay_port = comm_cfg.get('relay_port', '/dev/autospin_relay')
    gantry_port = comm_cfg.get('gantry_port', '/dev/autospin_xyz')
    rs485_ports = [motor_port, pipette_port, heating_stage_port]
    shared_rs485 = len(set(rs485_ports)) < len(rs485_ports)
    return {
        'motor': motor_port,
        'pipette': pipette_port,
        'heating_stage': heating_stage_port,
        'relay': relay_port,
        'gantry': gantry_port,
        'shared_rs485': shared_rs485,
    }


class SharedRs485DeviceProxy:
    """Serialize access to devices that share one USB-RS485 serial port."""

    def __init__(self, device, lock, logger: logging.Logger, label: str):
        self._device = device
        self._lock = lock
        self._logger = logger
        self._label = label

    def _comm(self):
        return getattr(self._device, "comm", None)

    def _is_device_connected(self):
        return bool(getattr(self._device, "_connected", False))

    def _open_if_needed(self):
        comm = self._comm()
        if comm is not None and not getattr(comm, "_connected", False):
            self._logger.debug("Opening shared RS485 port for %s", self._label)
            return comm.connect()
        if comm is None and not self._is_device_connected():
            self._logger.debug("Opening shared RS485 port for %s", self._label)
            return self._device.connect()
        return True

    def _close_if_needed(self):
        comm = self._comm()
        if comm is not None and getattr(comm, "_connected", False):
            self._logger.debug("Closing shared RS485 port for %s", self._label)
            comm.close()
        elif comm is None and self._is_device_connected():
            self._logger.debug("Closing shared RS485 port for %s", self._label)
            self._device.close()

    def connect(self):
        with self._lock:
            try:
                return self._device.connect()
            finally:
                self._close_if_needed()

    def close(self):
        with self._lock:
            self._close_if_needed()

    def __getattr__(self, name):
        attr = getattr(self._device, name)
        if not callable(attr):
            return attr

        def call_with_shared_port(*args, **kwargs):
            with self._lock:
                if not self._open_if_needed():
                    raise RuntimeError(f"Unable to open shared RS485 port for {self._label}")
                try:
                    return attr(*args, **kwargs)
                finally:
                    self._close_if_needed()

        return call_with_shared_port



class Maestro:
    """
    系统控制器
    负责协调所有硬件设备和工作流程
    """

    def __init__(self, use_gantry: bool = True, mock: bool = False,
                 use_z2: bool = True,
                 use_standalone_relay: bool = True,
                 logger: logging.Logger = None):
        """
        初始化系统控制器。

        Args:
            use_gantry: 是否使用机械臂
            mock: True 时所有硬件以 mock 模式运行，不打开任何串口
            use_z2: 是否初始化/回停 Z2(A) 轴
            use_standalone_relay: 是否单独初始化 RelayManager
            logger: 日志记录器
        """
        self.use_gantry = use_gantry
        self.mock = mock
        self.use_z2 = use_z2
        self.use_standalone_relay = use_standalone_relay
        self.logger = logger or logging.getLogger("AutoSpinmotorSystem")

        self._is_running = False
        self._experiment_time = 0.0
        self._nist_time = time.time()
        
        # 硬件设备
        self.gantry = None
        self.z2_stage = None
        self.relay = None     
        self.workers = {}     
        self.spincoater = None
        self.liquidhandler = None
        self.heating_stage = None
        self.hotplate = None
        self.hotplates = {}
        self.storage = {}
        self.characterization = None
        
        # 线程池
        self.threadpool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        
        # 锁
        self.lock_pendingtasks = threading.Lock()
        self.lock_completedtasks = threading.Lock()
        
        # 任务管理
        self.pending_tasks = []
        self.completed_tasks = {}
        
        # 外部控制标志
        self._under_external_control = False

        self.logger.info("===========================================")
        self.logger.info(f"🚀 初始化 Maestro 控制器 (Mock模式: {self.mock})")
        self.logger.info("===========================================")
        self._initialize_hardware()

    def _initialize_hardware(self):
        """
        初始化硬件设备。
        mock=True 时所有硬件以 mock 模式初始化，不打开任何串口。
        """
        try:
            # 端口解析集中到 resolve_serial_ports（单一真相源，见模块顶部）。
            ports = resolve_serial_ports(CONFIG.get('communication', {}))
            motor_port = ports['motor']
            pipette_port = ports['pipette']
            relay_port = ports['relay']
            stage_port = ports['gantry']
            heating_stage_port = ports['heating_stage']
            shared_rs485 = ports['shared_rs485']
            shared_rs485_lock = threading.RLock() if shared_rs485 else None

            if shared_rs485:
                self.logger.info(
                    "Shared RS485 bus enabled: spin motor=%s, pipette=%s, heating stage=%s",
                    motor_port,
                    pipette_port,
                    heating_stage_port,
                )

            # ── 1. 旋涂电机 ──────────────────────────────────────────
            self.logger.info("--> 正在初始化: 旋涂电机")
            spincoater = MotorController(port=motor_port, mock=self.mock, logger=self.logger)
            if shared_rs485:
                spincoater = SharedRs485DeviceProxy(
                    spincoater,
                    shared_rs485_lock,
                    self.logger,
                    "spin motor",
                )
            self.spincoater = spincoater
            self.spincoater.connect()

            # ── 2. 移液枪 ────────────────────────────────────────────
            self.logger.info("--> 正在初始化: 移液枪")
            liquidhandler = PipetteController(port=pipette_port, mock=self.mock, logger=self.logger)
            if shared_rs485:
                liquidhandler = SharedRs485DeviceProxy(
                    liquidhandler,
                    shared_rs485_lock,
                    self.logger,
                    "pipette",
                )
            self.liquidhandler = liquidhandler
            self.liquidhandler.connect()

            # ── 3. 继电器 ────────────────────────────────────────────
            if self.use_standalone_relay:
                self.logger.info("--> 正在初始化: 继电器模块")
                self.relay = RelayManager(port=relay_port, mock=self.mock, logger=self.logger)
                self.relay.connect()
            else:
                self.logger.info("--> 跳过单独初始化: 继电器模块 (use_standalone_relay=False)")

            # ── 4. XYZ 平台 (导轨) ──────────────────────────────────
            if self.use_gantry:
                self.logger.info("--> 正在初始化: XYZ 机械臂平台")
                self.xyz_stage = XYZStage(
                    port=stage_port,
                    relay_port=relay_port,
                    mock=self.mock,
                    logger=self.logger,
                )
                if self.xyz_stage.connect():
                    if self.use_z2 and self.xyz_stage.initialize_z2_at_top():
                        self.logger.info("Z2 initialized at top safe position (A0).")
                    elif self.use_z2:
                        self.logger.warning(
                            "Z2 initialization failed; call initialize_z2_at_top() "
                            "after manually placing Z2 at the top safe position."
                        )
                    else:
                        self.logger.info("Z2 initialization skipped (use_z2=False).")
                # 统一别名映射，方便其它方法调用
                self.gantry = self.xyz_stage
                self.z2_stage = self.xyz_stage if self.use_z2 else None
            else:
                self.logger.info("--> 跳过初始化: XYZ 机械臂平台 (use_gantry=False)")

            # ── 5. 纯软件模块 (存储托盘等) ───────────────────────
            # 5. Heating stage
            if self.use_gantry and stage_port == heating_stage_port and not self.mock:
                self.logger.warning(
                    "XYZ stage and heating stage are both configured on "
                    f"{heating_stage_port}; only one device can use a serial port at a time."
                )
            self.logger.info("--> Initializing heating stage")
            hotplate1 = HeatingStageController(
                port=heating_stage_port,
                mock=self.mock,
                logger=self.logger,
            )
            if shared_rs485:
                hotplate1 = SharedRs485DeviceProxy(
                    hotplate1,
                    shared_rs485_lock,
                    self.logger,
                    "heating stage",
                )
            hotplate1.connect()
            self.heating_stage = hotplate1
            self.hotplate = hotplate1
            self.hotplates["Hotplate1"] = hotplate1

            self.storage = {
                "Tray1": {"capacity": 45, "used": 0},
                "Tray2": {"capacity": 45, "used": 0},
            }

            self.logger.info("✅ 所有硬件模块初始化与连接完成！")

        except Exception as e:
            self.logger.error(f"❌ 硬件初始化失败: {e}")

    # ================= 实验控制方法 =================

    def start_experiment(self):
        """
        开始实验
        """
        self.logger.info("开始实验")
        self._is_running = True
        self._experiment_time = 0.0
        self._nist_time = time.time()

    def stop_experiment(self):
        """
        停止实验
        """
        self.logger.info("停止实验")
        self._is_running = False
        
        # 停止所有硬件
        self._park_z2_safely()
        if self.spincoater:
            try:
                self.spincoater.stop(use_brake=True)
            except Exception as exc:
                self.logger.warning("Spincoater stop failed during shutdown: %s", exc)
        if self.relay:
            try:
                self.relay.emergency_stop()
            except Exception as exc:
                self.logger.warning("Relay emergency_stop failed during shutdown: %s", exc)
        if self.gantry:
            try:
                self.gantry.emergency_stop()
            except Exception as exc:
                self.logger.warning("Gantry emergency_stop failed during shutdown: %s", exc)
        for hotplate in self.hotplates.values():
            try:
                hotplate.stop()
            except Exception as exc:
                self.logger.warning("Hotplate stop failed during shutdown: %s", exc)

    def _park_z2_safely(self):
        """Move Z2 back to A0 without blocking the rest of shutdown on failure."""
        if not self.use_z2:
            return
        z2 = self.z2_stage or self.gantry
        if z2 is None or not hasattr(z2, "park_z2"):
            return
        if getattr(z2, "_manual_mode", False):
            self.logger.info("Skipping Z2 park because gantry is in manual mode")
            return
        try:
            self.logger.info("Parking Z2 to top safe position (A0)")
            ok = z2.park_z2()
            if not ok:
                self.logger.warning("Z2 park_z2() returned False")
        except Exception as exc:
            self.logger.warning("Z2 park failed during shutdown: %s", exc)

    def idle_gantry(self):
        """机械臂回到空闲位置 (HOME)"""
        if self.gantry:
            self.logger.info("机械臂请求回到空闲位置 (HOME)")
            current_pos = self.gantry.get_position()
            safe_z = getattr(self.gantry, "safe_z_mm", current_pos["Z"])

            # 安全归位：先抬起 Z 轴，再移动 XY
            self.gantry.move_to(current_pos["X"], current_pos["Y"], safe_z)
            self.gantry.home()

    def idle_human(self):
        """
        人工操作空闲状态
        """
        self.logger.info("人工操作空闲")
        self._under_external_control = True

        if self.spincoater:
            try:
                self.spincoater.stop(use_brake=True)
            except Exception as exc:
                self.logger.warning("Spincoater stop failed while entering human idle: %s", exc)

        if self.liquidhandler:
            try:
                self.liquidhandler.stop()
            except Exception as exc:
                self.logger.warning("Pipette stop failed while entering human idle: %s", exc)

        if self.relay:
            for channel in ("nitrogen", "pump", "spin_power"):
                try:
                    self.relay.turn_off(channel)
                except Exception as exc:
                    self.logger.warning(
                        "Relay channel %s turn_off failed while entering human idle: %s",
                        channel,
                        exc,
                    )

        self._park_z2_safely()

        if self.gantry and hasattr(self.gantry, "enter_manual_mode"):
            try:
                ok = self.gantry.enter_manual_mode(release_xy=True, release_z=False)
                if not ok:
                    self.logger.warning("Gantry enter_manual_mode returned False")
            except Exception as exc:
                self.logger.warning("Gantry enter_manual_mode failed: %s", exc)

    def transfer(self, from_position, to_position):
        """
        转移样品
        
        Args:
            from_position: 起始位置
            to_position: 目标位置
        """
        self.logger.info(f"转移样品: {from_position} -> {to_position}")
        # 这里添加实际的转移操作

    def run_task(self, task: Dict[str, Any]):
        """
        运行任务
        
        Args:
            task: 任务信息
        """
        self.logger.info(f"运行任务: {task['name']}")
        # 这里添加实际的任务执行逻辑

    def update_experiment_time(self):
        """
        更新实验时间
        """
        if self._is_running:
            self._experiment_time = time.time() - self._nist_time

    def get_experiment_time(self) -> float:
        """
        获取当前实验时间
        
        Returns:
            float: 实验时间（秒）
        """
        self.update_experiment_time()
        return self._experiment_time

    def shutdown(self):
        """
        关闭系统
        """
        self.logger.info("关闭系统")
        try:
            self.stop_experiment()
        except Exception as exc:
            self.logger.warning("stop_experiment failed during shutdown: %s", exc)
        
        # 关闭所有硬件
        if self.spincoater:
            try:
                self.spincoater.close()
            except Exception as exc:
                self.logger.warning("Spincoater close failed during shutdown: %s", exc)
        if self.liquidhandler:
            try:
                self.liquidhandler.close()
            except Exception as exc:
                self.logger.warning("Pipette close failed during shutdown: %s", exc)
        if self.relay:
            try:
                self.relay.close()
            except Exception as exc:
                self.logger.warning("Relay close failed during shutdown: %s", exc)
        if self.gantry:
            try:
                self.gantry.close()
            except Exception as exc:
                self.logger.warning("Gantry close failed during shutdown: %s", exc)
        for hotplate in self.hotplates.values():
            try:
                hotplate.close()
            except Exception as exc:
                self.logger.warning("Hotplate close failed during shutdown: %s", exc)
        
        # 关闭线程池
        self.threadpool.shutdown()

    def run_task(self, task: Dict[str, Any]):
        """Run one compiled protocol task synchronously."""
        name = task["name"]
        details = task.get("details", {})
        self.logger.info("Run task: %s", name)

        # The protocol compiler emits a flat task list. This dispatcher keeps
        # that compiled representation independent from concrete Worker classes.
        if name == "move_sample":
            self.transfer(details.get("from"), details.get("to"))
            return {"ok": True, "task": name}

        if name == "dispense_liquid":
            return self._run_dispense_liquid(task, details)

        if name == "spincoat":
            try:
                from autospin_system.workers import Worker_SpincoaterLiquidHandler
            except ImportError:
                from workers import Worker_SpincoaterLiquidHandler
            # Use a short-lived worker instance so template execution can run
            # synchronously without starting the full async scheduler.
            return Worker_SpincoaterLiquidHandler(maestro=self).spincoat(task, details)

        if name == "anneal":
            return self._run_anneal(task, details)

        if name == "wait":
            duration = float(details.get("duration", 0))
            self.logger.info("Wait requested for %.1f s", duration)
            if not self.mock:
                time.sleep(duration)
            return {"ok": True, "task": name, "duration": duration}

        if name == "measure":
            self.logger.info("Measure placeholder: %s", details)
            return {"ok": True, "task": name, "placeholder": True}

        raise ValueError(f"Unsupported task: {name}")

    def _run_dispense_liquid(self, task: Dict[str, Any], details: Dict[str, Any]):
        """Move the pipette to the target and dispense a normalized uL volume."""

        volume_ul = int(round(float(details["volume_ul"])))
        liquid = details.get("liquid", "unknown_liquid")
        target = details.get("target", "spin_center")
        self.logger.info(
            "Dispense request: sample=%s liquid=%s volume=%suL target=%s",
            task.get("sample"),
            liquid,
            volume_ul,
            target,
        )

        if self.gantry:
            try:
                from autospin_system.workers import Worker_SpincoaterLiquidHandler
            except ImportError:
                from workers import Worker_SpincoaterLiquidHandler
            # Reuse the worker's coordinate helper so dispense positions follow
            # the same pipette TCP offset rules as timed additions in spincoat.
            worker = Worker_SpincoaterLiquidHandler(maestro=self)
            real_x, real_y, real_z = worker._get_pipette_pos(target.upper(), has_tip=True)
            worker._safe_pipette_move(real_x, real_y, real_z)

        if self.liquidhandler:
            ok = self.liquidhandler.dispense(volume_ul)
            if not ok:
                raise RuntimeError(f"Dispense failed: {volume_ul}uL {liquid}")
        return {"ok": True, "task": "dispense_liquid", "volume_ul": volume_ul}

    def _run_anneal(self, task: Dict[str, Any], details: Dict[str, Any]):
        """Set the requested hotplate SV and wait for the anneal duration."""

        hotplate_name = details.get("hotplate", "Hotplate1")
        target_temp = float(details["target_temp"])
        duration = float(details["duration"])
        hotplate = self.hotplates.get(hotplate_name)
        if hotplate is None:
            raise RuntimeError(f"Hotplate not found: {hotplate_name}")

        self.logger.info(
            "Anneal request: sample=%s hotplate=%s target=%.1fC duration=%.1fs",
            task.get("sample"),
            hotplate_name,
            target_temp,
            duration,
        )
        hotplate.write_sv(target_temp)
        if self.mock:
            # Mock execution proves the command path without blocking for long
            # thermal dwell times.
            self.logger.info("[MOCK] Skipping anneal wait of %.1f s", duration)
        else:
            time.sleep(duration)
        return {
            "ok": True,
            "task": "anneal",
            "hotplate": hotplate_name,
            "target_temp": target_temp,
            "duration": duration,
        }

    @property
    def experiment_time(self) -> float:
        """
        实验时间属性
        """
        return self.get_experiment_time()

    @property
    def nist_time(self) -> float:
        """
        当前时间属性
        """
        return time.time()
