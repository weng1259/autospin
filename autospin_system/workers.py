# AutoSpinmotorSystem/workers.py
# 系统架构中各硬件 Worker 类定义

import asyncio
import time
import logging
from collections import namedtuple
from typing import Dict, Any, List, Optional
import inspect

try:
    from AutoSpinmotorSystem.config.hardware_config import CONFIG
except ImportError:
    from config.hardware_config import CONFIG

# 【修改点 1：引入统一的 YAML 配置字典】
from config.hardware_config import CONFIG

# 定义任务的标准格式，包含执行函数、预估时长及协作 Worker 列表
task_tuple = namedtuple("task", ["function", "estimated_duration", "other_workers"])


class WorkerTemplate:
    """
    Worker 基础模板类
    包含任务调度、执行和异步队列管理的核心逻辑
    """

    def __init__(self, name, capacity, maestro=None, planning=False, initial_fill=0):
        self.name = name
        self.capacity = capacity
        self.initial_fill = initial_fill
        self.planning = planning
        self.logger = logging.getLogger("AutoSpinmotorSystem")
        self.maestro = maestro
        
        if not planning:
            self.working = False
            self.POLLINGRATE = 0.1  # seconds

    def prime(self, loop):
        """初始化异步队列"""
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.queue = asyncio.PriorityQueue()

    def start(self):
        """启动 Worker 监听"""
        self.logger.info(f"Starting {self.name}")
        self.loop.run_until_complete(self.prime(self.loop))
        def future_callback(future):
            try:
                future.result()
            except Exception as e:
                self.logger.exception(f"Exception in {self.name}")

        self.working = True
        self.logger.info(f"Worker {self.name} is now running")
        for _ in range(self.capacity):
            future = asyncio.run_coroutine_threadsafe(self.worker(), self.loop)
            future.add_done_callback(future_callback)

    def stop_workers(self):
        """Stop the worker"""
        self.logger.info(f"Stopping {self.name}")
        self.working = False
        self.logger.info(f"Worker {self.name} is now stopped")

    def add_task(self, task):
        """将带有开始时间戳的任务压入优先级队列"""
        payload = (task["start"], task)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, payload)
        task_description = f'{task["name"]}, {task["sample"]}'
        self.logger.info(f"Added task {task_description} to {self.name}'s queue")


    async def worker(self):
        """处理队列中的任务"""

        def future_callback(future):
            try:
                future.result()
            except Exception as e:
                self.logger.exception(f"Exception in {self}")

        while self.working:
            while True:
                if hasattr(self, 'queue') and len(self.queue._queue) > 0:
                    time_until_next = (
                        self.queue._queue[0][0] - self.maestro.experiment_time
                    )  # seconds until task is due

                    if time_until_next <= 1:  # within 1 second of start time
                        break
                await asyncio.sleep(0.2)

            _, task = await self.queue.get()  # blocking wait for next task
            task_description = f'{task["name"]}, {task["sample"]}'
            
            if task is None:  # finished flag
                break

            # execute this task
            function = self.functions[task["name"]].function
            details = task.get("details", {})
            
            if inspect.iscoroutinefunction(function):
                self.logger.info(f"executing {task_description} as coroutine")
                output_dict = await function(task, details)
            else:
                self.logger.info(f"executing {task_description} as thread")
                future = asyncio.gather(
                    self.loop.run_in_executor(
                        self.maestro.threadpool,
                        function,
                        task,
                        details,
                    )
                )
                future.add_done_callback(future_callback)
                output_dict = await future
                output_dict = output_dict[0]
            
            if output_dict is None:
                output_dict = {}
            
            self.logger.info(f"finished {task_description}")
            self.queue.task_done()

    def __hash__(self):
        return hash(str(type(self)))


class Worker_GantryGripper(WorkerTemplate):
    """机械臂 Worker 定义了 12 种路径转换函数，负责样本的物理流转"""
    
    def __init__(self, maestro=None, planning=False):
        super().__init__(
            name="GantryGripper", maestro=maestro, planning=planning, capacity=1
        )

        # 动态读取 YAML 中的过渡耗时 (Transition Durations)
        t_durs = CONFIG.get('scheduler', {}).get('transition_durations', {})

        self.functions = {
            "idle_gantry": task_tuple(
                function=self.idle_gantry, estimated_duration=6, other_workers=[]
            ),
            "spincoater_to_hotplate": task_tuple(
                function=self.spincoater_to_hotplate,
                estimated_duration=t_durs.get('spincoater_to_hotplate', 27),
                other_workers=[],
            ),
            "spincoater_to_storage": task_tuple(
                function=self.spincoater_to_storage,
                estimated_duration=t_durs.get('spincoater_to_storage', 30),
                other_workers=[],
            ),
            "spincoater_to_characterization": task_tuple(
                function=self.spincoater_to_characterization,
                estimated_duration=t_durs.get('spincoater_to_characterization', 33),
                other_workers=[],
            ),
            "hotplate_to_spincoater": task_tuple(
                function=self.hotplate_to_spincoater,
                estimated_duration=t_durs.get('hotplate_to_spincoater', 33),
                other_workers=[],
            ),
            "hotplate_to_storage": task_tuple(
                function=self.hotplate_to_storage,
                estimated_duration=t_durs.get('hotplate_to_storage', 18),
                other_workers=[],
            ),
            "hotplate_to_characterization": task_tuple(
                function=self.hotplate_to_characterization,
                estimated_duration=t_durs.get('hotplate_to_characterization', 18),
                other_workers=[],
            ),
            "storage_to_spincoater": task_tuple(
                function=self.storage_to_spincoater,
                estimated_duration=t_durs.get('storage_to_spincoater', 33),
                other_workers=[],
            ),
            "storage_to_hotplate": task_tuple(
                function=self.storage_to_hotplate,
                estimated_duration=t_durs.get('storage_to_hotplate', 18),
                other_workers=[],
            ),
            "storage_to_characterization": task_tuple(
                function=self.storage_to_characterization,
                estimated_duration=t_durs.get('storage_to_characterization', 15),
                other_workers=[],
            ),
            "characterization_to_spincoater": task_tuple(
                function=self.characterization_to_spincoater,
                estimated_duration=t_durs.get('characterization_to_spincoater', 33),
                other_workers=[],
            ),
            "characterization_to_hotplate": task_tuple(
                function=self.characterization_to_hotplate,
                estimated_duration=t_durs.get('characterization_to_hotplate', 18),
                other_workers=[],
            ),
            "characterization_to_storage": task_tuple(
                function=self.characterization_to_storage,
                estimated_duration=t_durs.get('characterization_to_storage', 18),
                other_workers=[],
            ),
        }

    def _get_mechanical_pos(self, target_coord_name, tool="gripper", has_tip=False):
        """内部函数：计算带工具补偿的导轨物理位置 (适配 YAML 列表结构)"""
        # 1. 获取目标点的实验室绝对坐标 (转小写以匹配 YAML 键名)
        target_pos = CONFIG['geometry']['lab_coordinates'][target_coord_name.lower()]

        # 2. 获取所选工具的偏移量
        offset = CONFIG['geometry']['tool_offsets'][tool.lower()]

        # 3. 计算 Z 轴总偏移（如果是移液枪且带吸头，需要加吸头长）
        total_z_offset = offset["z"]
        if tool.lower() == "pipette" and has_tip:
            total_z_offset += offset.get("tip_length", 95.0)

        # 4. 根据公式计算导轨需要移动到的物理坐标 (YAML 中 target_pos 是 [x, y, z] 列表)
        real_x = target_pos[0] - offset["x"]
        real_y = target_pos[1] - offset["y"]
        real_z = target_pos[2] - total_z_offset

        return real_x, real_y, real_z

    def _safe_move_to_target(self, target_name, tool="gripper"):
        """封装好的安全移动流程 """
        real_x, real_y, real_z = self._get_mechanical_pos(target_name, tool)

        # 直接获取预计算好的安全高度 (mm)
        safe_z = self.maestro.xyz_stage.safe_z_mm
        current_pos = self.maestro.xyz_stage.get_position()

        # 1. 抬升到安全高度 (保持当前 XY 不变)
        self.maestro.xyz_stage.move_to(current_pos["X"], current_pos["Y"], safe_z)

        # 2. 水平移动到目标点正上方
        self.maestro.xyz_stage.move_to(real_x, real_y, safe_z)

        # 3. 垂直下降到目标深度
        self.maestro.xyz_stage.move_to(real_x, real_y, real_z)

    def idle_gantry(self, task, details):
        """Move gantry to idle position"""
        if hasattr(self.maestro, 'idle_gantry'):
            self.maestro.idle_gantry()

    def spincoater_to_hotplate(self, task, details):
        """Transfer sample from spincoater to hotplate"""
        # Implementation would use actual hardware control
        # 使用爪子抓取样片，自动应用 -74.8mm 和 51.5mm 的补偿 [cite: 76, 79]
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")
        # self.relay_manager.close_gripper() # 假定你已实现夹爪闭合
        
        # 2. 去目标加热板放置 (假设 details 中包含目标名，如 'HOTPLATE1')
        dest = details.get("destination", "HOTPLATE1")
        self._safe_move_to_target(dest, tool="GRIPPER")
        # self.relay_manager.open_gripper()

    def spincoater_to_storage(self, task, details):
        """Transfer sample from spincoater to storage"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("STORAGE_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def spincoater_to_characterization(self, task, details):
        """Transfer sample from spincoater to characterization"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def hotplate_to_storage(self, task, details):
        """Transfer sample from hotplate to storage"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("STORAGE_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def hotplate_to_characterization(self, task, details):
        """Transfer sample from hotplate to characterization"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def hotplate_to_spincoater(self, task, details):
        """Transfer sample from hotplate to spincoater"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def storage_to_spincoater(self, task, details):
        """Transfer sample from storage to spincoater"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("STORAGE_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def storage_to_hotplate(self, task, details):
        """Transfer sample from storage to hotplate"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("STORAGE_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def storage_to_characterization(self, task, details):
        """Transfer sample from storage to characterization"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("STORAGE_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def characterization_to_spincoater(self, task, details):
        """Transfer sample from characterization to spincoater"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def characterization_to_hotplate(self, task, details):
        """Transfer sample from characterization to hotplate"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")

    def characterization_to_storage(self, task, details):
        """Transfer sample from characterization to storage"""
        # 自动应用偏移，无需关心物理距离
        self._safe_move_to_target("CHARACTERIZATION_TRAY", tool="GRIPPER")
        # ...抓取逻辑...
        self._safe_move_to_target("SPIN_CENTER", tool="GRIPPER")


class Worker_Hotplate(WorkerTemplate):
    def __init__(self, capacity, maestro=None, planning=False):
        super().__init__(
            name="Hotplate", maestro=maestro, planning=planning, capacity=capacity
        )
        """热板 Worker 定义了退火任务"""
        self.logger.info(f"Hotplate Worker {self.name} initialized with capacity {self.capacity}")
        self.functions = {
            "anneal": task_tuple(
                function=self.anneal, estimated_duration=None, other_workers=[]
            ),
        }

    async def anneal(self, task, details):
        """Perform annealing process"""
        duration = details.get("duration", 300)
        target_temp = details.get("target_temp", details.get("temperature"))
        hotplate_name = details.get("hotplate", self.name)

        if self.planning or self.maestro is None:
            await asyncio.sleep(duration)
            return

        hotplate = self.maestro.hotplates.get(hotplate_name)
        if hotplate is None:
            raise RuntimeError(f"Hotplate not found: {hotplate_name}")

        if target_temp is not None:
            hotplate.write_sv(float(target_temp))
            self.logger.info(f"{hotplate_name} SV set to {float(target_temp):.1f} C")

        start = self.maestro.experiment_time
        while self.maestro.experiment_time - start < duration:
            try:
                pv = hotplate.read_pv()
                self.logger.info(f"{hotplate_name} PV = {pv:.1f} C")
            except Exception as exc:
                self.logger.warning(f"{hotplate_name} PV read failed during anneal: {exc}")
            await asyncio.sleep(min(5, duration))


class Worker_Storage(WorkerTemplate):
    """存储 Worker 定义了样本的休息任务"""
    def __init__(self, capacity, maestro=None, planning=False, initial_fill=0):
        super().__init__(
            name="Storage",
            maestro=maestro,
            planning=planning,
            capacity=capacity,
            initial_fill=initial_fill,
        )
        """存储 Worker 定义了样本的休息任务"""
        self.logger.info(f"Storage Worker {self.name} initialized with capacity {self.capacity}")
        self.functions = {
            "rest": task_tuple(
                function=self.rest, estimated_duration=180, other_workers=[]
            ),
        }

    async def rest(self, task, details):
        """Sample resting period"""
        duration = details.get("duration", 180)
        await asyncio.sleep(duration)


class Worker_SpincoaterLiquidHandler(WorkerTemplate):
    """旋涂液处理 Worker 定义了旋涂和混液任务"""
    def __init__(self, maestro=None, planning=False):
        super().__init__(
            name="SpincoaterLiquidhandler",
            maestro=maestro,
            planning=planning,
            capacity=1,
        )
        self.functions = {
            "spincoat": task_tuple(
                function=self.spincoat, estimated_duration=None, other_workers=[]
            ),
            "mix": task_tuple(
                function=self.mix, estimated_duration=None, other_workers=[]
            ),
        }

    def _get_pipette_pos(self, target_coord_name, has_tip=True):
        """适配 YAML 结构的移液枪偏移计算"""
        target_pos = CONFIG['geometry']['lab_coordinates'][target_coord_name.lower()]
        offset = CONFIG['geometry']['tool_offsets']['pipette']

        total_z_offset = offset["z"]
        if has_tip:
            total_z_offset += offset.get("tip_length", 95.0)

        real_x = target_pos[0] - offset["x"]
        real_y = target_pos[1] - offset["y"]
        real_z = target_pos[2] - total_z_offset

        return real_x, real_y, real_z

    def _safe_pipette_move(self, real_x, real_y, real_z):
        """移液枪专属的安全移动逻辑 (抬升 -> 平移 -> 下降)"""
        safe_z = self.maestro.xyz_stage.safe_z_mm
        current_pos = self.maestro.xyz_stage.get_position()

        # 1. 抬升到安全高度
        self.maestro.xyz_stage.move_to(current_pos["X"], current_pos["Y"], safe_z)
        # 2. 水平移动到目标点上方
        self.maestro.xyz_stage.move_to(real_x, real_y, safe_z)
        # 3. 垂直下降
        self.maestro.xyz_stage.move_to(real_x, real_y, real_z)

    def _legacy_spincoat_positioning_demo(self, task, details):
        """Legacy positioning demo kept for reference during hardware tuning."""
        self.logger.info(f"开始旋涂工艺：样品 {task['sample']}")
        
        # 1. 计算滴液位置（旋涂中心）的补偿坐标 [cite: 10, 15]
        # 自动应用 +71.5mm 的 X 轴偏移和 Z 轴深度补偿 
        real_x, real_y, real_z = self._get_pipette_pos("SPIN_CENTER", has_tip=True)
        
        # 2. 移动到滴液高度上方（安全移动） 
        self.maestro.xyz_stage.move_to(real_x, real_y, real_z + 10) # 先悬停在上方 10mm
        
        # 3. 执行工艺曲线 (这里会调用具体的电机转速控制) [cite: 11, 16]
        # 在 RAMP 或 HOLD 阶段，下降至 real_z 执行 dispense [cite: 11]
        self.maestro.xyz_stage.move_to(real_x, real_y, real_z)
        # self.maestro.pipette.dispense(details.get("volume", 500))
        
        self.logger.info("旋涂与滴加完成")

    def spincoat(self, task, details):
        """Execute a compiled SpinCoat protocol."""
        sample = task.get("sample", "unknown_sample")
        profile = details.get("profile", [])
        # Timed events are scheduled from the start of the whole spin profile,
        # so sorting once lets the loop consume them in chronological order.
        timed_events = sorted(details.get("timed_events", []), key=lambda event: event["at_s"])
        self.logger.info("Start spincoat protocol for sample %s", sample)

        if self.maestro is None:
            raise RuntimeError("Spincoat worker requires a Maestro instance")

        if self.maestro.gantry:
            # Move the pipette to a safe hover position above the spin center
            # before the motor profile starts.
            real_x, real_y, real_z = self._get_pipette_pos("SPIN_CENTER", has_tip=True)
            self.maestro.xyz_stage.move_to(real_x, real_y, real_z + 10)

        if self.maestro.spincoater:
            # The motor controller performs its own zero-speed check before
            # starting, which helps avoid restarting a still-spinning chuck.
            ok, message = self.maestro.spincoater.start(direction="forward")
            if not ok:
                raise RuntimeError(f"Spincoater start failed: {message}")

        elapsed = 0.0
        event_index = 0
        try:
            for step_index, step in enumerate(profile, start=1):
                speed = float(step["speed"])
                duration = float(step["duration"])
                self.logger.info(
                    "Spin step %s: %.0f rpm for %.1f s",
                    step_index,
                    speed,
                    duration,
                )
                if self.maestro.spincoater:
                    ok, message = self.maestro.spincoater.set_speed(speed)
                    if not ok:
                        raise RuntimeError(f"Spincoater set_speed failed: {message}")

                step_end = elapsed + duration
                # Trigger all events whose absolute timestamp falls inside
                # this spin segment. Waiting is skipped in mock mode.
                while event_index < len(timed_events) and timed_events[event_index]["at_s"] <= step_end:
                    event = timed_events[event_index]
                    wait_s = max(0.0, event["at_s"] - elapsed)
                    self._sleep_or_log(wait_s)
                    elapsed += wait_s
                    self._run_timed_dispense(event)
                    event_index += 1

                remaining = max(0.0, step_end - elapsed)
                self._sleep_or_log(remaining)
                elapsed = step_end
        finally:
            # Always stop the motor even if a timed dispense or set_speed call
            # fails mid-profile.
            if self.maestro.spincoater:
                self.maestro.spincoater.stop(use_brake=True)

        self.logger.info("Spincoat protocol completed for sample %s", sample)
        return {"ok": True, "task": "spincoat", "sample": sample}

    def _sleep_or_log(self, duration_s):
        """Wait during real execution, but keep mock runs fast."""

        if duration_s <= 0:
            return
        if self.maestro.mock:
            self.logger.info("[MOCK] Skip wait %.1f s", duration_s)
        else:
            time.sleep(duration_s)

    def _run_timed_dispense(self, event):
        """Execute one liquid addition scheduled inside a SpinCoat operation."""

        operation = event["operation"]
        volume_ul = int(round(float(operation["volume_ul"])))
        liquid = operation.get("liquid", "unknown_liquid")
        target = operation.get("target", "spin_center")
        self.logger.info(
            "Timed dispense at %.1f s: %s uL %s to %s",
            float(event["at_s"]),
            volume_ul,
            liquid,
            target,
        )

        if self.maestro.gantry:
            real_x, real_y, real_z = self._get_pipette_pos(target.upper(), has_tip=True)
            self._safe_pipette_move(real_x, real_y, real_z)

        if self.maestro.liquidhandler:
            ok = self.maestro.liquidhandler.dispense(volume_ul)
            if not ok:
                raise RuntimeError(f"Timed dispense failed: {volume_ul}uL {liquid}")

    def mix(self, task, details):
        """Mix solutions"""
        # Implementation would use actual hardware control
        real_x, real_y, real_z = self._get_pipette_pos("CLEAN_STATION", has_tip=True)
        self.maestro.xyz_stage.move_to(real_x, real_y, real_z)
        # self.maestro.pipette.aspirate(details.get("volume"))
        self.logger.info("混液完成")


class Worker_Characterization(WorkerTemplate):
    def __init__(self, maestro=None, planning=False):
        super().__init__(
            name="Characterization", maestro=maestro, planning=planning, capacity=1
        )
        self.functions = {
            "characterize": task_tuple(
                function=self.characterize, estimated_duration=160, other_workers=[]
            ),
        }

    def characterize(self, task, details):
        """Perform characterization"""
        # Implementation would use actual hardware control
        pass


class Worker_HumanOperator(WorkerTemplate):
    def __init__(self, maestro=None, planning=False):
        super().__init__(
            name = "HumanOperator",
            maestro = maestro,
            planning = planning,
            capacity = 1
        )
        self.functions = {
            "idle_gantry": task_tuple(
                function = self.idle_human,
                estimated_duration = 1,
                other_workers = []
            ),
            "spincoater_to_hotplate": task_tuple(
                function = self.spincoater_to_hotplate,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "spincoater_to_storage": task_tuple(
                function = self.spincoater_to_storage,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "spincoater_to_characterization": task_tuple(
                function = self.spincoater_to_characterization,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "hotplate_to_spincoater": task_tuple(
                function = self.hotplate_to_spincoater,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "hotplate_to_storage": task_tuple(
                function = self.hotplate_to_storage,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "hotplate_to_characterization": task_tuple(
                function = self.hotplate_to_characterization,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "storage_to_spincoater": task_tuple(
                function = self.storage_to_spincoater,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "storage_to_hotplate": task_tuple(
                function = self.storage_to_hotplate,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "storage_to_characterization": task_tuple(
                function = self.storage_to_characterization,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "characterization_to_spincoater": task_tuple(
                function = self.characterization_to_spincoater,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "characterization_to_hotplate": task_tuple(
                function = self.characterization_to_hotplate,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
            "characterization_to_storage": task_tuple(
                function = self.characterization_to_storage,
                estimated_duration = 30, # assume 30 seconds for manual pick/place
                other_workers = []
            ),
        }

    def idle_human(self, task, details):
        """Human operator idle state"""
        pass

    def spincoater_to_hotplate(self, task, details):
        """Human transfer from spincoater to hotplate"""
        print(f"Please move sample from spincoater to hotplate {details.get('destination', 'Hotplate1')}")

    def spincoater_to_storage(self, task, details):
        """Human transfer from spincoater to storage"""
        print(f"Please move sample from spincoater to storage")

    def spincoater_to_characterization(self, task, details):
        """Human transfer from spincoater to characterization"""
        print(f"Please move sample from spincoater to characterization")

    def hotplate_to_storage(self, task, details):
        """Human transfer from hotplate to storage"""
        print(f"Please move sample from hotplate to storage")

    def hotplate_to_characterization(self, task, details):
        """Human transfer from hotplate to characterization"""
        print(f"Please move sample from hotplate to characterization")

    def hotplate_to_spincoater(self, task, details):
        """Human transfer from hotplate to spincoater"""
        print(f"Please move sample from hotplate to spincoater")

    def storage_to_spincoater(self, task, details):
        """Human transfer from storage to spincoater"""
        print(f"Please move sample from storage to spincoater")

    def storage_to_hotplate(self, task, details):
        """Human transfer from storage to hotplate"""
        print(f"Please move sample from storage to hotplate {details.get('destination', 'Hotplate1')}")

    def storage_to_characterization(self, task, details):
        """Human transfer from storage to characterization"""
        print(f"Please move sample from storage to characterization")

    def characterization_to_spincoater(self, task, details):
        """Human transfer from characterization to spincoater"""
        print(f"Please move sample from characterization to spincoater")

    def characterization_to_hotplate(self, task, details):
        """Human transfer from characterization to hotplate"""
        print(f"Please move sample from characterization to hotplate {details.get('destination', 'Hotplate1')}")

    def characterization_to_storage(self, task, details):
        """Human transfer from characterization to storage"""
        print(f"Please move sample from characterization to storage")
