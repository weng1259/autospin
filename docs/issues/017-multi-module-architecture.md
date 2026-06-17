# Issue #017 — 多模块协同架构：整合师兄代码，搭建统一控制系统

**日期**：2026-03-24
**提出人**：Claude + Codex（合并讨论）
**状态**：open
**优先级**：高
**类型**：架构
**涉及文件**：`Code/perovskite_auto_system/`（师兄代码）、`firmware/`、`web_control/`

---

## 一、背景

项目有两套独立代码：

| | 我们的部分 | 师兄的部分 (`Code/perovskite_auto_system/`) |
|---|---|---|
| 语言 | Arduino C + HTML/JS | Python |
| 覆盖 | 固件、传感器、网页调试面板 | 旋涂电机、移液枪、继电器、XYZ上位机、工艺流程 |
| 硬件环境 | Mac，DSTUR-T80 继电器，闭环 3200 脉冲/转 | Windows，LCUS-8 继电器，开环 6400 细分 |
| 当前状态 | 已接线测试通过，网页可调试 | 有完整驱动代码，未在我们硬件上运行 |

旋涂模块和移液模块到货后，需要所有设备（XYZ 导轨 + 旋涂电机 + 移液枪 + 继电器 + 夹爪）协同工作。现在需要把两套代码整合成一个统一的控制系统。

---

## 二、现有代码分析

### 师兄代码的优点（直接复用）

1. **Modbus RTU 通信层完整** — `spin_motor/driver_communication.py` 实现了 CRC16 校验、字节序处理、重试机制、ID 自动对齐，可直接用于旋涂电机
2. **移液枪驱动完整** — `pipette/pipette_controller.py` 覆盖了吸液/吐液/退 Tip/液面探测/归位，基于 pymodbus 3.x
3. **ProfileRunner 声明式配方** — 用字典列表定义工艺流程 + `step_handlers` dispatch，扩展性好
4. **继电器协议一致** — LCUS-8 和我们的 DSTUR-T80 都是 `0xA0 + CH + ON/OFF + checksum`，代码通用
5. **日志系统完善** — CSV 数据记录 + 通信报文日志 + 故障报告 JSON

### 师兄代码的问题（需要改造）

1. **串口管理分散** — 每个模块自己 `serial.Serial()` 开串口。Arduino 和继电器都是 CH340（hwid `1A86:7523`），`auto_detect_port()` 只返回第一个匹配的，多同芯片设备时无法区分
2. **设备接口不统一** — `xyz_stage.emergency_all_stop()` vs `spin_motor.emergency_stop()` vs `relay_manager.emergency_stop()`，签名和语义各不相同
3. **状态模型是自由 dict** — 到处是 `data.get("speed_rpm", 0.0)` 式写法，拼错 key 不会报错，无类型检查
4. **ProfileRunner 只管旋涂+移液** — 不支持 XYZ 导轨移动步骤，无法编排完整实验流程
5. **安全联锁不足** — `SystemMonitor.is_safe()` 只查电机故障，没有限位传感器、Z 轴抱闸、旋涂转速等跨设备联锁
6. **硬件写死** — Windows COM 端口、开环步距参数 1280 步/mm，需适配 Mac + 闭环 640 步/mm
7. **资源无互斥** — 网页 Web Serial 和 Python 脚本会抢串口（已踩坑："上传固件前必须关网页面板"）

---

## 三、架构决策

以下是经讨论确认的架构选型：

### 决策 1：目录结构 — 新建 `server/`，保留 `Code/` 作参考

```
智能旋涂仪/
├── firmware/              # Arduino 固件（不动）
├── web_control/           # 网页调试面板（不动，后续逐步迁移到后端 API）
├── hardware/              # 硬件文档（不动）
├── docs/                  # 文档（不动）
├── Code/                  # 师兄原始代码（只读参考，不再修改）
└── server/                # ← 新的 Python 统一控制系统
```

理由：师兄代码保留原貌方便对照，`server/` 从零搭建保证架构干净、和项目其他部分统一。

### 决策 2：单 Python 进程管理所有设备

不拆微服务。4 个设备 + 1 个继电器，单进程足够。串口资源天然需要单一持有者。

### 决策 3：设备状态用 dataclass，不用自由 dict

```python
@dataclass
class DeviceState:
    connected: bool = False
    status: str = "unknown"    # idle / ready / running / fault / estop
    fault_message: str = ""
    extra: dict = field(default_factory=dict)  # 设备特有数据
```

理由：类型安全、IDE 补全、拼错字段会报错。`extra` 字段允许每个设备塞自己的附加数据（如转速、坐标）。

### 决策 4：同步串行执行模型，不引入事件驱动

当前实验流程完全是顺序的（移到 A 点 → 吸液 → 移到 B 点 → 旋涂 → 清理）。SafetyManager 在每个步骤前后做轮询检查即可，不需要 asyncio。

如果未来确实需要"一边旋涂一边监控"，再引入后台线程，不预设。

### 决策 5：保留字典配方，不引入步骤类

师兄的 `{"type": "RAMP", "from": 0, "to": 1000}` + `step_handlers` dispatch 模式简洁灵活，直接扩展新步骤类型即可。不需要为每种步骤定义 class（`StageMoveStep`、`SpinRampStep` 等），对实验室项目来说是过度设计。

### 决策 6：网页调试面板暂时保留 Web Serial

现阶段 `web_control/` 继续用 Web Serial 直连调试，和 Python 上位机不同时运行。Python 上位机跑通后，再用 WebSocket API 替代 Web Serial，统一后端。

### 决策 7：先 CLI 跑通，再加 API 层

不预写 FastAPI/WebSocket 框架。先纯命令行把完整实验流程跑通，验证所有设备协同。树莓派迁移时再加网络层。

---

## 四、详细解决方案

### 4.1 目录结构

```
server/
├── __init__.py
├── main.py                      # 入口：初始化 → 连接设备 → 执行配方
│
├── config/
│   ├── __init__.py
│   ├── hardware.py              # 硬件参数（端口、波特率、步距、限位）
│   ├── recipes.py               # 实验配方（字典列表）
│   └── ports.json               # 自动生成的端口缓存（首次探测后写入）
│
├── drivers/                     # 设备驱动层 — 每个设备一个文件
│   ├── __init__.py
│   ├── base.py                  # DeviceDriver 基类 + DeviceState
│   ├── xyz_stage.py             # XYZ 三轴导轨
│   ├── spin_motor.py            # DBLS400 旋涂电机
│   ├── pipette.py               # 电动移液枪
│   └── relay.py                 # DSTUR-T80 USB 继电器（含夹爪/刹车/阀门子设备）
│
├── runtime/                     # 运行时层 — 协调与安全
│   ├── __init__.py
│   ├── system.py                # SystemRuntime：设备注册 + 生命周期 + 全局状态机
│   ├── safety.py                # SafetyManager：联锁规则 + 急停广播
│   └── executor.py              # RecipeExecutor：实验流程执行器
│
└── utils/
    ├── __init__.py
    ├── port_manager.py          # 串口发现 + 分配 + 缓存
    └── logger.py                # 实验数据记录（CSV + 日志）
```

总计约 **12 个 Python 文件**，结构紧凑。

### 4.2 设备驱动层 (`server/drivers/`)

#### 4.2.1 统一基类

```python
# server/drivers/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DeviceState:
    """所有设备共用的状态结构"""
    connected: bool = False
    status: str = "unknown"       # idle / ready / running / fault / estop
    fault_message: str = ""
    extra: dict = field(default_factory=dict)

class DeviceDriver(ABC):
    """所有硬件驱动的基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """设备名称，如 'xyz_stage', 'spin_motor'"""
        ...

    @abstractmethod
    def connect(self) -> bool:
        """连接设备，返回是否成功"""
        ...

    @abstractmethod
    def disconnect(self):
        """断开连接，释放串口资源"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def emergency_stop(self):
        """紧急停止 — 每个设备必须实现，语义：立即停止一切动作并进入安全状态"""
        ...

    @abstractmethod
    def get_state(self) -> DeviceState:
        """返回当前设备状态"""
        ...
```

#### 4.2.2 各驱动的实现来源

| 文件 | 复用师兄代码 | 主要改动 |
|------|-------------|----------|
| `xyz_stage.py` | `hardware/xyz_stage/` 全部 | 继承 `DeviceDriver`；步距从 1280 改为 config 可配；端口从构造参数传入而非自己打开 |
| `spin_motor.py` | `hardware/spin_motor/` 全部 | 继承 `DeviceDriver`；`__init__.py` 的 `SpinMotor` 类直接搬过来 |
| `pipette.py` | `hardware/pipette/` 全部 | 继承 `DeviceDriver`；加 `connect()` 包装 pymodbus 连接逻辑 |
| `relay.py` | `hardware/relay/usb_relay.py` + `relay_manager.py` + `gripper.py` + `valve.py` | 合并为一个文件；继承 `DeviceDriver`；`emergency_stop()` = 所有通道 OFF |

#### 4.2.3 每个驱动的内部结构

以 `xyz_stage.py` 为例，保留师兄的分层（通信层 + 控制层），但收进一个文件：

```python
# server/drivers/xyz_stage.py

class ArduinoMotorBridge:
    """底层串口通信（复用师兄 motor_control.py）"""
    def __init__(self, ser: serial.Serial): ...   # 注意：接收已打开的 Serial 对象，不自己创建
    def send_cmd(self, cmd: str): ...
    def read_line(self, timeout=0.2) -> Optional[str]: ...

class XYZStage(DeviceDriver):
    """XYZ 三轴导轨（复用师兄 xyz_stage.py，加统一接口）"""

    def __init__(self, ser: serial.Serial, relay_driver, config: dict):
        self.bridge = ArduinoMotorBridge(ser)
        self.relay = relay_driver          # 用于 Z 轴刹车联动
        self.steps_per_mm = config["steps_per_mm"]  # 从 config 读取，不写死
        ...

    # === DeviceDriver 接口 ===
    @property
    def name(self) -> str: return "xyz_stage"
    def connect(self) -> bool: ...         # 等待 READY 信号
    def disconnect(self): ...
    def emergency_stop(self): ...          # 发 'S' + 锁刹车
    def get_state(self) -> DeviceState: ...

    # === 业务方法（复用师兄逻辑）===
    def move_to(self, x, y, z, wait=True): ...
    def safe_move_to_mm(self, x_mm, y_mm, z_mm): ...
    def get_position(self) -> dict: ...
    def home(self): ...                    # 新增：调用固件 HR 命令
```

**关键改动**：串口对象 `ser` 由外部（PortManager）创建后注入，驱动不再自己打开端口。这解决了"串口管理分散"的问题。

### 4.3 运行时层 (`server/runtime/`)

#### 4.3.1 SystemRuntime — 设备注册 + 全局状态机

```python
# server/runtime/system.py
from enum import Enum

class SystemState(Enum):
    """全局状态机"""
    IDLE    = "idle"      # 刚启动，设备未连接
    READY   = "ready"     # 所有设备已连接（归零可选）
    RUNNING = "running"   # 正在执行实验配方
    FAULT   = "fault"     # 某设备报错，暂停等待处理
    ESTOP   = "estop"     # 急停触发，锁死一切，只能手动复位

class SystemRuntime:
    """单例，管理所有设备的生命周期和全局状态"""

    def __init__(self):
        self.state = SystemState.IDLE
        self.devices: dict[str, DeviceDriver] = {}
        self.safety: SafetyManager = None       # 后续注入

    def register(self, device: DeviceDriver):
        """注册设备"""
        self.devices[device.name] = device

    def connect_all(self) -> bool:
        """依次连接所有已注册设备，全部成功则进入 READY"""
        for name, dev in self.devices.items():
            if not dev.connect():
                self.state = SystemState.FAULT
                return False
        self.state = SystemState.READY
        return True

    def disconnect_all(self):
        """断开所有设备"""
        for dev in self.devices.values():
            dev.disconnect()
        self.state = SystemState.IDLE

    def emergency_stop_all(self, reason: str = ""):
        """全局急停：遍历所有设备调用 emergency_stop()，进入 ESTOP 状态"""
        for dev in self.devices.values():
            try:
                dev.emergency_stop()
            except Exception:
                pass  # 急停时不能因为某个设备异常而中断其他设备的停止
        self.state = SystemState.ESTOP

    def reset(self) -> bool:
        """从 ESTOP/FAULT 恢复到 READY（需要人工确认后调用）"""
        if self.state not in (SystemState.ESTOP, SystemState.FAULT):
            return False
        # 重新连接检查所有设备状态
        all_ok = all(dev.is_connected() for dev in self.devices.values())
        if all_ok:
            self.state = SystemState.READY
        return all_ok

    def get_device(self, name: str) -> DeviceDriver:
        """按名称获取设备"""
        if name not in self.devices:
            raise RuntimeError(f"设备未注册: {name}")
        return self.devices[name]
```

**状态流转图**：
```
IDLE ──connect_all()──→ READY ──run_recipe()──→ RUNNING ──recipe完成──→ READY
 ↑                        │                       │
 └── reset() ←── FAULT ←─┘←──────────────────────┘
                   │                               │
                   └───→ ESTOP ←── emergency_stop_all() ←──────┘
                           │
                           └── reset()（人工确认）──→ IDLE/READY
```

#### 4.3.2 SafetyManager — 联锁规则

```python
# server/runtime/safety.py

class SafetyManager:
    """
    集中管理所有跨设备安全联锁。
    所有涉及物理动作的操作，都应先调用 pre_check()。
    """

    def __init__(self, runtime: SystemRuntime):
        self.runtime = runtime

    def pre_check(self, action: str, **params) -> tuple[bool, str]:
        """
        执行前安全检查。
        返回 (允许执行, 原因说明)。
        """
        # 规则 0：ESTOP 状态下拒绝一切
        if self.runtime.state == SystemState.ESTOP:
            return False, "系统处于急停状态，请先复位"

        # 规则 1：Z 轴移动前必须松刹车
        if action == "z_move":
            relay = self.runtime.get_device("relay")
            if not relay.is_brake_released():
                return False, "Z 轴刹车未释放"

        # 规则 2：XY 移动前 Z 必须在安全高度
        if action == "xy_move":
            stage = self.runtime.get_device("xyz_stage")
            pos = stage.get_position()
            safe_z = params.get("safe_z", 64000)
            if pos.get("Z", 0) < safe_z:
                return False, f"Z 轴未抬升到安全高度 (当前: {pos.get('Z', 0)}, 需要: {safe_z})"

        # 规则 3：旋涂电机运转中禁止 XYZ 移动
        if action in ("xy_move", "z_move", "xyz_move"):
            if "spin_motor" in self.runtime.devices:
                spin = self.runtime.get_device("spin_motor")
                spin_state = spin.get_state()
                if spin_state.status == "running":
                    actual_speed = spin_state.extra.get("actual_speed", 0)
                    if actual_speed > 50:  # 容差：< 50 RPM 视为已停
                        return False, f"旋涂电机正在运转 ({actual_speed} RPM)，禁止移动导轨"

        # 规则 4：通信超时视为故障
        if action != "emergency_stop":
            for name, dev in self.runtime.devices.items():
                state = dev.get_state()
                if state.status == "fault":
                    return False, f"设备 {name} 处于故障状态: {state.fault_message}"

        return True, "OK"

    def post_check(self, action: str, **params):
        """执行后检查（如 Z 停止后锁刹车）"""
        if action == "z_move_done":
            relay = self.runtime.get_device("relay")
            relay.lock_brake()
```

**联锁规则完整表**：

| # | 场景 | 检查时机 | 规则 |
|---|------|---------|------|
| 0 | ESTOP 状态 | 所有操作前 | 拒绝一切非复位操作 |
| 1 | Z 轴移动 | pre_check | 刹车必须先释放 |
| 2 | XY 移动 | pre_check | Z 轴必须在安全高度 |
| 3 | XYZ 移动 | pre_check | 旋涂电机必须停止 |
| 4 | 任何操作 | pre_check | 所有设备无故障 |
| 5 | Z 轴停止 | post_check | 自动锁刹车 |
| 6 | 任何通信超时 | 驱动层上报 | 触发全局急停 |

#### 4.3.3 RecipeExecutor — 实验流程执行器

从师兄的 `ProfileRunner` 演进，扩展支持 XYZ 导轨和夹爪步骤：

```python
# server/runtime/executor.py

class RecipeExecutor:
    """
    实验配方执行器。
    演进自师兄的 ProfileRunner，保持字典配方 + step_handlers dispatch 模式。
    新增步骤类型：XYZ_HOME / XYZ_MOVE / GRIPPER / WAIT
    """

    def __init__(self, runtime: SystemRuntime, safety: SafetyManager, logger):
        self.runtime = runtime
        self.safety = safety
        self.logger = logger

        # 步骤处理器注册表（可扩展）
        self.step_handlers = {
            # XYZ 导轨
            "XYZ_HOME":     self._exec_xyz_home,
            "XYZ_MOVE":     self._exec_xyz_move,
            # 旋涂电机
            "SPIN_START":   self._exec_spin_start,
            "SPIN_RAMP":    self._exec_spin_ramp,
            "SPIN_HOLD":    self._exec_spin_hold,
            "SPIN_STOP":    self._exec_spin_stop,
            # 移液枪
            "PIPETTE":      self._exec_pipette,
            # 继电器/夹爪
            "GRIPPER":      self._exec_gripper,
            # 等待
            "WAIT":         self._exec_wait,
        }

    def run(self, recipe: list[dict]):
        """执行实验配方"""
        if self.runtime.state != SystemState.READY:
            raise RuntimeError(f"系统未就绪 (当前状态: {self.runtime.state.value})")

        self.runtime.state = SystemState.RUNNING
        self.logger.start_test()

        try:
            for i, step in enumerate(recipe):
                step_type = step.get("type")
                desc = step.get("desc", step_type)
                self.logger.log("INFO", f"步骤 {i+1}/{len(recipe)}: {desc}")

                handler = self.step_handlers.get(step_type)
                if handler is None:
                    raise RuntimeError(f"未知步骤类型: {step_type}")

                handler(step)

            self.runtime.state = SystemState.READY
            self.logger.end_test(success=True)

        except Exception as e:
            self.logger.log("ERROR", f"配方执行失败: {e}")
            self.runtime.emergency_stop_all(reason=str(e))
            self.logger.end_test(success=False)
            raise

    # === XYZ 步骤 ===

    def _exec_xyz_home(self, step):
        stage = self.runtime.get_device("xyz_stage")
        ok, reason = self.safety.pre_check("xyz_home")
        if not ok:
            raise RuntimeError(f"安全检查失败: {reason}")
        stage.home()

    def _exec_xyz_move(self, step):
        """
        支持两种写法：
        - {"type": "XYZ_MOVE", "target": "SPIN_CENTER"}    # 预定义坐标名
        - {"type": "XYZ_MOVE", "x_mm": 50.0, "y_mm": 50.0, "z_mm": 5.0}  # 直接坐标
        """
        stage = self.runtime.get_device("xyz_stage")

        # 解析目标坐标
        if "target" in step:
            from server.config.recipes import LAB_COORDINATES
            coord = LAB_COORDINATES[step["target"]]
            x_mm, y_mm, z_mm = coord["x"], coord["y"], coord["z"]
        else:
            x_mm = step["x_mm"]
            y_mm = step["y_mm"]
            z_mm = step["z_mm"]

        # 安全检查
        ok, reason = self.safety.pre_check("xyz_move")
        if not ok:
            raise RuntimeError(f"安全检查失败: {reason}")

        # 执行安全移动（内部自动处理：升Z → 平移XY → 降Z → 刹车联动）
        stage.safe_move_to_mm(x_mm, y_mm, z_mm)

    # === 旋涂步骤（复用师兄 ProfileRunner 逻辑）===

    def _exec_spin_start(self, step):
        spin = self.runtime.get_device("spin_motor")
        direction = step.get("direction", "forward")
        ok, msg = spin.start(direction=direction)
        if not ok:
            raise RuntimeError(f"旋涂电机启动失败: {msg}")

    def _exec_spin_ramp(self, step):
        """斜坡升/降速（复用师兄的线性插值逻辑）"""
        spin = self.runtime.get_device("spin_motor")
        v0, v1, duration = step["from"], step["to"], step["duration"]
        interval = 0.1
        steps = max(1, int(duration / interval))
        dv = (v1 - v0) / steps

        for i in range(steps):
            target = v1 if i == steps - 1 else v0 + (i + 1) * dv
            ok, msg = spin.set_speed(target)
            if not ok:
                raise RuntimeError(f"设置速度失败: {msg}")
            self.logger.record_speed(target, spin.get_actual_speed())
            time.sleep(interval)

    def _exec_spin_hold(self, step):
        spin = self.runtime.get_device("spin_motor")
        speed, duration = step["speed"], step["duration"]
        ok, msg = spin.set_speed(speed)
        if not ok:
            raise RuntimeError(f"设置速度失败: {msg}")

        elapsed = 0.0
        interval = 0.1
        while elapsed < duration:
            self.logger.record_speed(speed, spin.get_actual_speed())
            time.sleep(interval)
            elapsed += interval

    def _exec_spin_stop(self, step):
        spin = self.runtime.get_device("spin_motor")
        spin.stop()

    # === 移液枪步骤 ===

    def _exec_pipette(self, step):
        pipette = self.runtime.get_device("pipette")
        action = step["action"]
        volume = step.get("volume", 0)

        if action == "aspirate":
            ok, _ = pipette.aspirate(volume)
            if not ok:
                raise RuntimeError(f"吸液失败 ({volume} uL)")
        elif action == "dispense":
            pipette.dispense(volume)
        elif action == "drop_tip":
            pipette.drop_tip()
        else:
            raise ValueError(f"未知移液操作: {action}")

    # === 辅助步骤 ===

    def _exec_gripper(self, step):
        relay = self.runtime.get_device("relay")
        action = step["action"]  # "close" 或 "open"
        if action == "close":
            relay.gripper_close()
        elif action == "open":
            relay.gripper_open()

    def _exec_wait(self, step):
        duration = step["duration"]
        self.logger.log("INFO", f"等待 {duration} 秒...")
        time.sleep(duration)
```

### 4.4 串口管理 (`server/utils/port_manager.py`)

策略：**配置优先，探测兜底，结果缓存**

```python
# server/utils/port_manager.py

class PortManager:
    """
    统一串口管理器。
    策略：
    1. 先读 config/ports.json 缓存
    2. 缓存中的端口验证可用性
    3. 不可用则自动探测（协议指纹识别）
    4. 探测成功后更新缓存
    """

    CACHE_FILE = "config/ports.json"

    def detect_all(self) -> dict:
        """
        返回 {"arduino": Serial对象, "relay": Serial对象, "rs485": Serial对象}
        """
        # 1. 尝试读缓存
        cached = self._load_cache()

        # 2. 验证缓存端口是否可用
        if cached:
            result = self._verify_cached(cached)
            if result:
                return result

        # 3. 自动探测
        result = self._auto_detect()

        # 4. 缓存结果
        self._save_cache(result)
        return result

    def _auto_detect(self) -> dict:
        """
        协议指纹探测（复用师兄 port_detector.py 逻辑）：
        - 115200 发 'Q\n'，回 'X...Y...Z...' → Arduino
        - 9600 发 0xFF，回 8 字节 → DSTUR-T80 继电器
        - 9600 Modbus 读寄存器，有响应 → RS485 设备
        """
        ...
```

### 4.5 配置 (`server/config/`)

```python
# server/config/hardware.py

# === XYZ 导轨 ===
STAGE_CONFIG = {
    "baudrate": 115200,
    "steps_per_mm": 682.67,       # ← 51200 脉冲/转 ÷ 75mm/转 (HTD3M 25齿)，旧值 68.3 有误
    "limit_mm": {
        "X": {"min": 0, "max": 300},
        "Y": {"min": 0, "max": 300},
        "Z": {"min": 0, "max": 100},
    },
    "safe_z_mm": 50.0,            # 避障安全高度
    "default_speed": 5,           # Arduino 速度档位 1-9
}

# === DSTUR-T80 USB 继电器 ===
RELAY_CONFIG = {
    "baudrate": 9600,
    "channels": {
        "gripper":    1,   # CH1 → 夹爪（通电=夹合）
        "z_brake":    2,   # CH2 → Z 轴刹车（通电=释放）
        "nitrogen":   3,   # CH3 → 氮气阀（预留）
        "pump":       4,   # CH4 → 蠕动泵（预留）
        "spin_power": 5,   # CH5 → 旋涂仪电源（预留）
    },
}

# === 旋涂电机 (DBLS400, RS485 Modbus RTU) ===
SPIN_MOTOR_CONFIG = {
    "baudrate": 9600,
    "slave_id": 2,
    "pole_pairs": 4,
    "hall_angle": 1,
    "max_speed_rpm": 4500,
}

# === 移液枪 (28系列, RS485 Modbus RTU) ===
PIPETTE_CONFIG = {
    "baudrate": 115200,
    "slave_id": 1,
    "max_volume_ul": 1000,
}
```

```python
# server/config/recipes.py

# 预定义实验坐标 (mm)
LAB_COORDINATES = {
    "HOME":          {"x": 0,     "y": 0,     "z": 0},
    "SPIN_CENTER":   {"x": 50.0,  "y": 50.0,  "z": 5.0},
    "PIPETTE_RACK":  {"x": 10.0,  "y": 150.0, "z": 20.0},
    "WASTE_BIN":     {"x": 180.0, "y": 180.0, "z": 10.0},
}

# 钙钛矿旋涂完整配方
PEROVSKITE_FULL = [
    # 阶段 1：准备
    {"type": "XYZ_HOME",  "desc": "三轴归零"},
    {"type": "XYZ_MOVE",  "target": "PIPETTE_RACK",  "desc": "移到吸液位"},
    {"type": "PIPETTE",   "action": "aspirate", "volume": 500, "desc": "吸取 500uL 前驱体"},
    {"type": "XYZ_MOVE",  "target": "SPIN_CENTER",   "desc": "移到旋涂台中心"},

    # 阶段 2：旋涂
    {"type": "SPIN_START", "direction": "forward"},
    {"type": "SPIN_RAMP",  "from": 0, "to": 1000, "duration": 2, "desc": "升速到 1000RPM"},
    {"type": "SPIN_HOLD",  "speed": 1000, "duration": 3},
    {"type": "PIPETTE",    "action": "dispense", "volume": 500, "desc": "旋转中滴加"},
    {"type": "SPIN_RAMP",  "from": 1000, "to": 3000, "duration": 3},
    {"type": "SPIN_HOLD",  "speed": 3000, "duration": 30, "desc": "高速旋涂 30s"},
    {"type": "SPIN_RAMP",  "from": 3000, "to": 0, "duration": 3, "desc": "降速停止"},
    {"type": "SPIN_STOP"},

    # 阶段 3：清理
    {"type": "XYZ_MOVE",  "target": "WASTE_BIN",    "desc": "移到废液位"},
    {"type": "PIPETTE",   "action": "drop_tip",      "desc": "退 Tip"},
    {"type": "XYZ_MOVE",  "target": "HOME",          "desc": "回零"},
]
```

### 4.6 程序入口 (`server/main.py`)

```python
# server/main.py

def main():
    # 1. 发现并打开串口
    port_mgr = PortManager()
    ports = port_mgr.detect_all()

    # 2. 创建设备驱动
    relay  = RelayDriver(ports["relay"], RELAY_CONFIG)
    stage  = XYZStageDriver(ports["arduino"], relay, STAGE_CONFIG)
    # 以下设备等到货后启用：
    # spin   = SpinMotorDriver(ports["rs485"], SPIN_MOTOR_CONFIG)
    # pipette = PipetteDriver(ports["rs485"], PIPETTE_CONFIG)

    # 3. 创建运行时
    runtime = SystemRuntime()
    runtime.register(relay)
    runtime.register(stage)
    # runtime.register(spin)
    # runtime.register(pipette)

    safety = SafetyManager(runtime)
    runtime.safety = safety

    # 4. 连接所有设备
    if not runtime.connect_all():
        print("设备连接失败，请检查硬件")
        return

    # 5. 执行配方
    executor = RecipeExecutor(runtime, safety, logger)
    try:
        input("设备已就绪，按回车开始实验...")
        executor.run(PEROVSKITE_FULL)
        print("实验完成")
    except KeyboardInterrupt:
        runtime.emergency_stop_all("用户中断")
    except Exception as e:
        print(f"实验异常: {e}")
    finally:
        runtime.disconnect_all()
```

---

## 五、步距参数确认

| 参数 | 师兄代码 | 当前值 | 说明 |
|------|---------|-------|------|
| 细分设置 | 6400（开环） | 51200（拨码 SW3-6=OFF/OFF/OFF/ON） | 驱动器手册 p15 |
| 传动方式 | 丝杠 5mm 导程 | HTD3M 同步带 × 25 齿 = 75mm/转 | 产品画册 p8 |
| **steps_per_mm** | **1280** | **682.67** | 51200 ÷ 75 = 682.67 |

**结论**：统一使用 **682.67 步/mm**（51200 脉冲/转 ÷ 75mm 导程）。此值必须放在 config 中，不能硬编码。

> ~~旧值 68.3 步/mm 来自早期"闭环 ÷10"的错误假设。~~
> **2026-03-26 已实测确认**：MOVE 6827 步 ≈ 10mm，682.67 步/mm 正确。

---

## 六、实施路径

```
Phase 1 — 验证链路（1-2天）
├── 确认步距：682.67 步/mm（2026-03-26 尺子实测已确认）
├── 在 Mac 上跑师兄的 xyz_stage + relay 驱动（改端口+步距即可）
└── 验证 Python → Arduino → 电机 + Python → DSTUR-T80 → 刹车 完整链路

Phase 2 — 搭框架（2-3天）
├── 创建 server/ 目录结构
├── 写 base.py（DeviceDriver + DeviceState）
├── 写 system.py（SystemRuntime + 状态机）
├── 写 safety.py（SafetyManager + 联锁规则表）
├── 写 port_manager.py（端口发现+缓存）
└── 写 config/hardware.py + config/recipes.py

Phase 3 — 迁移驱动（2-3天）
├── 迁移 relay 驱动 → server/drivers/relay.py
├── 迁移 xyz_stage 驱动 → server/drivers/xyz_stage.py
├── 写 executor.py（先支持 XYZ_HOME / XYZ_MOVE / GRIPPER / WAIT）
└── 用 main.py 跑通：归零 → 移到 A 点 → 夹爪 → 移到 B 点 → 回零

Phase 4 — 接入 RS485 设备（等硬件到货）
├── 迁移 spin_motor 驱动 → server/drivers/spin_motor.py
├── 迁移 pipette 驱动 → server/drivers/pipette.py
├── executor 扩展 SPIN_* / PIPETTE 步骤
└── 跑通完整钙钛矿旋涂配方

Phase 5 — 加 API 层（上树莓派时）
├── 加 FastAPI + WebSocket 服务器
├── 网页面板从 Web Serial 改为调 WebSocket API
└── 部署到树莓派
```

---

## 七、非目标

本 issue 不要求一次性完成：
- 不要求立即重写所有师兄代码，优先复用
- 不要求立即替换网页调试面板
- 不要求引入数据库、消息队列、微服务
- 不要求现在就实现事件驱动或异步模型
- 不要求现在就考虑树莓派部署

---

## 八、验收标准

- [ ] `server/` 目录结构创建完成，所有文件就位
- [ ] `DeviceDriver` 基类 + `DeviceState` dataclass 定义完成
- [ ] `SystemRuntime` 可注册设备、管理生命周期、全局急停
- [ ] `SafetyManager` 实现联锁规则表中的所有规则
- [ ] `PortManager` 可自动发现 Arduino 和继电器
- [ ] `RecipeExecutor` 可执行包含 XYZ + 继电器步骤的配方
- [x] 步距参数 682.67 步/mm 已实测确认（2026-03-26）
- [ ] `main.py` 可端到端跑通：发现设备 → 连接 → 执行配方 → 断开
