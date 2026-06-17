================================================================
  AutoSpinmotorSystem 代码详细说明文档
================================================================
  版本: 2.1.0 (YAML 配置解耦与 GRBL 架构版)
  作者: AutoSpinmotorSystem Team
  简介: 自动旋涂电机控制系统，集成工作流管理与多硬件协同控制功能
================================================================

================================================================
  一、项目总体结构
================================================================

AutoSpinmotorSystem/
│
├── __init__.py                          # 包入口，版本与描述信息
├── maestro.py                           # 系统总控制器（核心）
├── system.py                            # Worker 注册与系统构建
├── workers.py                           # 各硬件 Worker 类定义
├── test_spin_motor_profile.py           # 电机调试主入口脚本
├── test_maestro_mock.py                 # Maestro mock 测试入口
├── test_heating_stage_smoke.py          # 加热台单机 smoke test 入口
├── test_z2_stage_smoke.py               # Z2/A 轴单机 smoke test 入口
├── test_spin_heat_link.py               # 旋涂电机 + 加热台共享 RS485 总线联动测试脚本
├── example_experiment.py                # 示例实验脚本
│
├── config/
│   ├── __init__.py                      # 配置包导出入口
│   ├── hardware_config.py               # YAML 配置解析器与目录管理器
│   └── system_config.yaml               # 系统的灵魂：全部物理与几何配置文件
│
└── hardware/
    ├── __init__.py                      # 硬件包导出入口
    ├── spin_motor/
    │   ├── __init__.py
    │   ├── motor_controller.py          # DBLS400 电机高层控制 (读取 YAML)
    │   └── driver_communication.py      # Modbus RTU 底层通信
    ├── pipette/
    │   ├── __init__.py
    │   ├── pipette_controller.py        # 移液枪高层控制 (读取 YAML)
    │   └── driver_communication.py      # 移液枪 Modbus 底层通信
    ├── relay/
    │   ├── __init__.py
    │   └── relay_manager.py             # 继电器通道管理 (LCUS-8 Hex 协议)
    ├── heating_stage/
    │   ├── __init__.py
    │   └── heating_stage_controller.py  # AI-516 加热台高层控制 (Modbus RTU)
    ├── heating stage/                   # 旧加热台实验目录；正式 smoke test 已移到根目录
    └── xyz_stage/
        ├── __init__.py
        └── xyz_stage.py                 # XYZ 三轴平台控制 (GRBL G-code 协议)

================================================================
  二、各模块详细说明
================================================================

----------------------------------------------------------------
  2.1  __init__.py  —— 包入口
----------------------------------------------------------------
路径: AutoSpinmotorSystem/__init__.py

定义包级别的元数据：
  __version__    = "2.1.0"
  __author__     = "AutoSpinmotorSystem Team"
  __description__ = "自动旋涂电机控制系统，基于 YAML 解耦架构"

----------------------------------------------------------------
  2.2  maestro.py  —— 系统总控制器
----------------------------------------------------------------
路径: AutoSpinmotorSystem/maestro.py

类: Maestro
  系统的核心调度中心，负责初始化所有硬件设备并协调任务执行。

【构造函数】
  __init__(use_gantry=True, mock=False, logger=None)
    参数:
       use_gantry  -- 是否启用机械臂（True=自动模式，False=人工引导模式）
       mock        -- 是否启用软件仿真模式（True=拦截物理串口通信，仅运行软件逻辑）
       logger      -- 外部传入的 logging.Logger 实例，默认自建

【主要方法】

  _initialize_hardware()
    读取 `CONFIG['communication']` 中的端口配置（motor_port, pipette_port 等）。
    初始化：只需传入 port 和 mock 参数，硬件类会自动去 YAML 寻址自身所需的波特率、极对数等物理参数，并调用 `connect()`。
    当前会初始化 `spincoater`、`liquidhandler`、`relay`、可选 `xyz_stage/gantry`，并将 AI-516 加热台注册到 `self.hotplates["Hotplate1"]`。
    注意：如果 `gantry_port` 与 `heating_stage_port` 配置为同一串口，真实模式下二者不能同时独占该串口；共享 RS485 总线场景请使用专门的联动测试脚本或后续共享总线抽象。

  start_experiment()
    置 _is_running = True，记录实验开始时间戳。

  stop_experiment()
    置 _is_running = False，并依次触发 spincoater、relay、gantry 的安全/紧急停止，同时调用各 hotplate 的 `stop()`。
    加热台当前 `stop()` 不会隐式改写 SV，仅记录停止请求，避免停机时意外改变真实温控目标。

  idle_gantry()
    机械臂安全归位：先抬起 Z 轴至导轨属性 `safe_z_mm`，再移动到 YAML 定义的 `home` 坐标。

  idle_human()
    人工操作空闲占位（仅打日志）。

  transfer(from_position, to_position)
    样品转移占位方法，实际操作需在此扩展。

  run_task(task: dict)
    执行单个任务，task 字典需包含 'name' 键。

  update_experiment_time() / get_experiment_time() -> float
    维护实验已运行时长（秒）。

  shutdown()
    有序关闭：stop_experiment → 依次 close() 所有硬件 → shutdown threadpool。

----------------------------------------------------------------
  2.3  system.py  —— Worker 注册与系统构建
----------------------------------------------------------------
路径: AutoSpinmotorSystem/system.py

【函数: generate_workers(maestro=None)】
  实例化全部硬件 Worker 并以字典形式返回。
  热板和存储盘的容量动态读取自 `CONFIG['system']['default_worker_capacity']`。
  参数:
    maestro=None 时以 planning=True 模式实例化（用于任务规划，不连接硬件）
    maestro 非 None 时以 planning=False 模式实例化（连接真实硬件）

【全局变量: ALL_WORKERS / ALL_TASKS】
  收集和维护 Worker 字典以及执行所需的前置任务元数据。

【全局字典: TRANSITION_TASKS / transitions】
  定义设备间转移任务名称映射。

【函数: build(use_gantry: bool) -> dict】
  返回系统构建配置字典。

----------------------------------------------------------------
  2.4  workers.py  —— 硬件 Worker 类
----------------------------------------------------------------
路径: AutoSpinmotorSystem/workers.py

【基类: WorkerTemplate】
  所有硬件 Worker 的抽象基类，包含异步队列优先级调度逻辑 (PriorityQueue)。

【子类: Worker_GantryGripper(WorkerTemplate)】
  负责样品在各设备间的物理搬运。
  过渡耗时由 `CONFIG['scheduler']['transition_durations']` 动态提供。

  内部方法:
    _get_mechanical_pos(target_coord_name, tool, has_tip)
      适配 YAML 列表结构，提取 `[X, Y, Z]` 并减去工具偏移。
    _safe_move_to_target(target_name, tool)
      封装安全移动三步流程：
        1. 抬升 Z 轴到 safe_z_mm（安全高度，保持当前 XY 不变）
        2. 水平移动 XY 到目标点正上方
        3. 垂直下降 Z 到目标深度

【子类: Worker_SpincoaterLiquidHandler(WorkerTemplate)】
  集成旋涂仪与移液枪的协同操作。
  内部方法:
    _safe_pipette_move()
      移液枪专属的安全移动逻辑，强制遵循“抬升-平移-下降”。
  任务:
    spincoat(task, details)
      计算滴液位置补偿，安全悬停于滴液高度上方 10mm，下降滴液后迅速抬升避让。

【其他子类】
  Worker_Hotplate (退火任务), Worker_Storage (静置任务), Worker_Characterization, Worker_HumanOperator 保持标准接口调度。
  Worker_Hotplate.anneal() 已接入 `maestro.hotplates`：任务 details 可传入 `temperature` 或 `target_temp` 写入 SV，并在退火持续时间内周期读取 PV 记录日志。

----------------------------------------------------------------
  2.5  test_spin_motor_profile.py  —— 电机调试主入口
----------------------------------------------------------------
路径: AutoSpinmotorSystem/test_spin_motor_profile.py

用于单独调试旋涂电机，不依赖完整系统架构。
执行流程：加载 TEST_PROFILE，按 START, RAMP, HOLD, STOP 顺序插值执行并记录实际反馈转速。

----------------------------------------------------------------
  2.5.1  test_spin_heat_link.py  —— 旋涂电机与加热台共享总线联动测试
----------------------------------------------------------------
路径: AutoSpinmotorSystem/test_spin_heat_link.py

用于测试同一条 RS485 总线上两台 Modbus 设备的协同通讯：
  - AI-516 加热台：从站 ID = 3，标准 Modbus RTU 字节序，PV=74，SV=0。
  - DBLS400 旋涂电机：从站 ID = 2，沿用商家驱动器的寄存器低字节在前格式。

脚本只打开一次共享串口（默认 COM6），再按不同 slave_id 轮流读写，避免两个 controller 同时独占同一物理串口。
默认安全设置：
  - `WRITE_HEATER_SV = False`，只读加热台 PV，不写温度设定。
  - `WAIT_FOR_TEMPERATURE = False`，不等待升温条件，直接执行短旋涂 profile。
  - finally 中无论是否异常都会尝试给电机下发停止/刹车并关闭串口。

运行示例：
```powershell
D:\study\pythonlearning\venv\Scripts\python.exe D:\study\pythonlearning\study\AutoSpinmotorSystem\test_spin_heat_link.py
```

----------------------------------------------------------------
  2.6  example_experiment.py  —— 完整示例实验
----------------------------------------------------------------
路径: AutoSpinmotorSystem/example_experiment.py

演示完整实验流程：
  1. 初始化 Maestro(use_gantry=True)
  2. maestro.start_experiment()
  3. 遍历 DEFAULT_PEROVSKITE_PROFILE（钙钛矿标准旋涂工艺曲线）
  4. finally: shutdown()

================================================================
  三、config/ 配置模块详细说明
================================================================

----------------------------------------------------------------
  3.1  system_config.yaml  —— 物理世界全局配置文件
----------------------------------------------------------------
路径: AutoSpinmotorSystem/config/system_config.yaml

彻底解耦所有硬件参数。任何坐标、波特率修改直接改此文件，无需碰 Python 代码。

【主要模块】
  - system: 系统名称、use_gantry 开关、各 Worker 默认容量。
  - communication: 全局串口分配 (motor_port, pipette_port, relay_port, gantry_port, heating_stage_port)。
  - devices: 
    - xyz_stage: steps_per_mm, safe_z_height, limits(软限位 [X,Y,Z] 数组)。
    - relay: commands(0xA0 前缀等), channels 通道映射。
    - spin_motor / pipette: 波特率、从站 ID、超时时间、量程/最大转速等。
    - heating_stage: AI-516 加热台参数，包含 `slave_id=3`、`baudrate=9600`、`parity=N`、`read_func=holding`、`pv_addr=74`、`sv_addr=0`、`scale=10.0`。
      仪表面板参数 `AFC` 必须设置为 0 才是标准 Modbus RTU；若 `AFC=1` 则为宇电 AIBUS，`pymodbus` 标准 03H/06H 帧不会得到响应。
  - geometry:
    - lab_coordinates: 实验室关键工位绝对坐标 `[x, y, z]` 列表 (如 home, spin_center, storage_tray 等)。
    - tool_offsets: 各工具 (gripper, pipette) TCP 补偿参数，及 tip_length。
  - scheduler: 任务与路径过渡的预估耗时。

----------------------------------------------------------------
  3.2  hardware_config.py  —— YAML 配置解析器
----------------------------------------------------------------
路径: AutoSpinmotorSystem/config/hardware_config.py

保留动态目录管理逻辑 (BASE_DIR, LOG_DIR, DATA_DIR)。
加载并解析 `system_config.yaml`，向整个系统暴露唯一的 `CONFIG` 字典。

================================================================
  四、hardware/ 硬件驱动模块详细说明
================================================================

----------------------------------------------------------------
  4.1  spin_motor/driver_communication.py  —— 电机底层通信
----------------------------------------------------------------
类: DriverCommunication
职责: DBLS400 驱动器的 Modbus RTU 底层串口通信 (FC03, FC06 功能码及低位在前小端序转换)。

----------------------------------------------------------------
  4.2  spin_motor/motor_controller.py  —— 电机高层控制
----------------------------------------------------------------
针对商家版 DBLS400 驱动器的高层控制封装。

【核心特性】
  - 自动装配: `__init__` 中自动读取 `CONFIG['devices']['spin_motor']` 实例化底层通信。
  - 控制字修正: `_get_control_word()` 强制将极对数左移 8 位置于高位，控制位 Bit3 强制置 1。
  - 防甩飞校验: `start(wait_for_stop=True)` 启动前轮询电机，确保真实转速 < 5 RPM 时才下发启动指令。
  - 速度倍率补偿: 内部自带 2.5x 系数纠正驱动器实际转速反馈偏差。

----------------------------------------------------------------
  4.3  pipette/driver_communication.py  —— 移液枪底层通信
----------------------------------------------------------------
类: PipetteDriver
基于 pymodbus 3.x 的 Modbus RTU 客户端，负责底层保持寄存器、线圈的读写。

----------------------------------------------------------------
  4.4  pipette/pipette_controller.py  —— 移液枪高层控制
----------------------------------------------------------------
28 系列步进移液枪控制。
- 自动读取 `CONFIG['devices']['pipette']['max_volume_ul']`。
- 封装 `aspirate` (吸液) 与 `dispense` (吐液) 原子动作，自动拆分 32 位体积寄存器并处理状态机轮询。

----------------------------------------------------------------
  4.5  relay/relay_manager.py  —— 继电器通道管理
----------------------------------------------------------------
基于 LCUS-8 USB 的实体继电器控制。
- 协议实现: 自动生成符合 `[0xA0, 通道号, 0x01/0x00, 校验和]` 格式的十六进制控制报文。
- 提供 `turn_on()`, `turn_off()`, `emergency_stop()` 等方法控制外设，并维护内部状态字典。

----------------------------------------------------------------
  4.6  xyz_stage/xyz_stage.py  —— XYZ 三轴平台控制
----------------------------------------------------------------
基于 GRBL-Mega-5X 固件的 G-code 运动控制器。
- 智能换算: `move_to` 接收 mm 坐标，自动处理与 GRBL 的绝对坐标系协商。
- 软限位防撞: 在下发任何指令前，读取 YAML 中的 `limits` 进行数学判断，拒绝非法越界请求。
- 硬核急停: `emergency_stop()` 直接发送 `0x18` (Ctrl+X) 瞬间切断 GRBL 脉冲输出。

----------------------------------------------------------------
  4.7  heating_stage/heating_stage_controller.py  —— AI-516 加热台控制
----------------------------------------------------------------
基于 pymodbus 3.x 的 AI-516 标准 Modbus RTU 控制器。

【硬件与参数】
  - 当前型号：AI-516 D2 G S。
  - 仪表地址：Addr=3，对应 `slave_id=3`。
  - 通讯参数：9600 baud, 8N1。
  - 面板参数：`AFC=0` 表示标准 Modbus；`AFC=1` 是 AIBUS，不能用当前 pymodbus 控制器。
  - 寄存器：PV 使用 holding register 地址 0，SV 使用 holding register 地址 1，温度按 0.1℃ 放大。

【主要接口】
  - `connect()`：打开串口或 mock 连接。
  - `read_pv()`：读取当前温度 PV，返回摄氏度浮点值。
  - `write_sv(temp_c)`：写入目标温度 SV。
  - `get_status()`：返回连接状态、串口参数、最近 PV/SV 等信息。
  - `close()` / `shutdown()`：关闭串口。

【测试入口】
  - `test_heating_stage_smoke.py` 保留为单机 smoke test，但内部调用正式 `HeatingStageController`，避免测试路径和系统路径分叉。
  - 默认 `WRITE_AFTER_PV_READ=False`，即只读 PV，不写 SV。

================================================================
  五、坐标系与工具偏移说明
================================================================

坐标原点: 导轨 Z 轴可移动平台中心 = (0, 0, 0)

导轨物理坐标计算公式 (内置于 Workers，适配 YAML 列表结构):
  real_x = LAB_COORD[0] - TOOL_OFFSET["x"]
  real_y = LAB_COORD[1] - TOOL_OFFSET["y"]
  real_z = LAB_COORD[2] - TOOL_OFFSET["z"] - (TIP_LENGTH, 若使用移液枪并带吸头)

================================================================
  六、日志系统说明
================================================================

logger 名称统一为 "AutoSpinmotorSystem"（所有模块共用同一命名空间）。
日志输出:
  - 控制台 (StreamHandler): 格式 "%(asctime)s [%(levelname)s] %(message)s"
  - 文件 (FileHandler): 保存至 logs/spinmotor_YYYYMMDD_HHMMSS.log

日志级别: INFO（默认）
关键日志节点:
  硬件初始化成功/失败
  电机启动/停止/转速变化
  任务队列的入队与出队
  各 Worker 的启动与停止

================================================================
  七、扩展与接入说明
================================================================

1. 接入新硬件
   在 maestro.py 的 _initialize_hardware() 中取消注释对应代码块，
   并确保 system_config.yaml 中的对应 communication 端口参数正确。

2. 添加新 Worker
   继承 WorkerTemplate，在 __init__ 中定义 self.functions 字典，
   使用 task_tuple(function, estimated_duration, other_workers) 描述每个任务。
   然后在 system.py 的 generate_workers() 中注册新 Worker 实例。

3. 添加新坐标点
   在 system_config.yaml 的 lab_coordinates 字典中添加新坐标点 [x, y, z]，
   无需修改运动控制代码，Worker 直接通过名称引用。

4. 修改旋涂工艺
   直接修改实验脚本中的 PROFILE 列表传入执行循环。

================================================================
  八、Mock 模拟模式 (软件仿真) 说明
================================================================

Mock 模式是 v2.0+ 引入的重要特性，允许在不连接任何物理硬件（电机、导轨、移液枪等）的情况下，完整运行和测试系统的软件调度逻辑与状态机流程。

1. 核心实现原理
   系统的 Mock 标志位由 Maestro 中心下发至各底层硬件驱动。
   - 底层通信拦截：当 mock=True 时，DriverCommunication 和 PipetteDriver 等串口类不会尝试打开真实串口，而是直接返回成功。
   - 指令旁路与虚拟反馈：写寄存器或 G-code 操作会被直接拦截并记录 [MOCK] 日志；读寄存器操作会返回默认的合规虚拟值（如移液枪归位标志位强返 1），确保上层代码不会因为读取不到传感器数据而崩溃。
   - 运动时间压缩：XYZ 平台等硬件在 Mock 模式下执行 move_to 等动作时，会跳过物理延时，立即更新内部虚拟坐标，极大加快仿真运行速度。

2. 如何启用 Mock 模式
   - 完整系统级仿真：在实例化 Maestro 时传入 mock=True 参数。
     示例: maestro = Maestro(use_gantry=True, mock=True)

3. 日志表现特征
   启用 Mock 模式后，系统生成的日志具有以下特征：
   - 硬件初始化阶段会出现显式的 MOCK 标记，例如：
     [INFO] [MOCK] DriverCommunication 连接成功 (port=COM14, slave_id=2)
     [INFO] [MOCK] XYZStage 连接成功 (port=COM6)

4. 适用场景
   - 编写与验证新的旋涂工艺曲线（Profile）。
   - 调试多 Worker 并发时的任务队列、优先级逻辑与防撞安全拦截。
   - 在未携带硬件设备的办公环境下进行代码开发。

================================================================
  文档结束
================================================================
================================================================
  2026-05-20 更新：XYZ 导轨 grbl 后端与网页调试控制台
================================================================

本项目已将原 `智能旋涂仪-代码/智能旋涂仪` 中用于 XYZ 导轨控制的 grbl L3 后端并入主项目。
后续运行导轨控制不再依赖外部 `智能旋涂仪-代码` 文件夹；主项目自身已经包含导轨控制后端、软限位配置和本地网页调试入口。

一、主项目内置文件

- `hardware/xyz_stage/xyz_stage.py`
  - 继续向 `Maestro` 和 Worker 暴露原有 `XYZStage` 接口。
  - 真实模式下内部调用 grbl L3 后端。
  - `mock=True` 时不打开任何串口，可用于离线测试。
- `hardware/xyz_stage/l3_backend/`
  - 从旧子项目迁入的 grbl L3 后端。
  - 包含 `GantryBackend`、`RelayBackend`、`GripperBackend`、结构化错误、Pydantic 返回模型、事件总线、runlog 和 observable 幂等封装。
- `hardware/xyz_stage/constants.yaml`
  - grbl 机器坐标系下的软限位与运动默认参数。
  - 当前合法工作空间按归零后的机器坐标定义：X/Y 为 `-275.0` 到 `-5.0` mm，Z 为 `-90.0` 到 `-5.0` mm。
- `web_control/server.py`
  - 主项目本地网页控制服务。浏览器通过 HTTP API 调 Python 后端，不再直接使用 Web Serial API。
- `web_control/index.html`
  - XYZ 导轨调试页面，支持连接、状态刷新、HOME 归零、急停、alarm 恢复、dry-run、绝对移动、相对点动和位置可视化。
- `requirements.txt`
  - 主项目运行依赖清单，包含 `pyserial`、`pymodbus`、`PyYAML`、`pydantic`。

二、运行网页调试控制台

在主项目根目录运行：

```powershell
python web_control/server.py
```

然后在浏览器打开：

```text
http://127.0.0.1:8765/
```

网页默认串口：

- 导轨 grbl 控制板：`COM13`
- DSTUR-T80 继电器：`COM10`

如果当前电脑上的串口不同，请在网页连接栏中修改后再连接。

三、安全使用流程

真实硬件控制建议按以下顺序执行：

1. 确认导轨、继电器、Z 轴刹车和急停/断电方式可用。
2. 打开网页，先勾选 `mock` 测试页面和 API。
3. 取消 `mock`，连接导轨串口和继电器串口。
4. 查看状态。如果 grbl 处于 `alarm` 且 `is_homed=False`，这是上电/串口复位后的常见状态。
5. 点击 `归零 HOME`，等待完成。
6. 先用 `Dry Run` 验证目标坐标。
7. 先做小幅低速移动，例如 `X=-20, Y=-20, Z=-10, Feed=1000`。
8. 再做较大范围移动，例如 `X=-100, Y=-100, Z=-20`。

注意：很多 Arduino/grbl 板在重新打开串口时会因 DTR 复位重新进入 `Alarm`，因此真实移动通常应在同一个连接会话内先 `home()` 再 `move_to()`。

四、Python API 示例

```python
from hardware.xyz_stage.xyz_stage import XYZStage

stage = XYZStage(port="COM13", relay_port="COM10", mock=False)
stage.connect()
stage.home()
stage.move_to(-20.0, -20.0, -10.0, feed_mm_min=1000)
print(stage.get_position())
stage.close()
```

离线 dry-run 示例：

```python
from hardware.xyz_stage.xyz_stage import XYZStage

stage = XYZStage(port="COM13", relay_port="COM10", mock=False)
plan = stage.dry_run_move_to(-50.0, -50.0, -20.0, feed_mm_min=1000)
print(plan)
```

五、已验证内容

- 串口识别：`COM13` 为 CH340 grbl 控制板，`COM10` 为 DSTUR-T80 继电器。
- 真实归零成功，归零后位置约为 `X=-4.999, Y=-4.999, Z=-4.999`。
- 小范围移动成功：`X=-20, Y=-20, Z=-10`。
- 较大范围移动成功：`X=-100, Y=-100, Z=-20`。
- `test_maestro_mock.py` 的 mock 路径已通过。
- 网页控制服务的 mock HTTP API 已通过。

================================================================
  2026-05-25 更新：Z2 / A 轴单轴导轨并入正式导轨控制链
================================================================

本项目已将新增的二级 Z 轴滑台并入正式 `XYZStage` / `GantryBackend` / `Maestro` 控制链。
Z2 与 XYZ 共用同一块 Arduino Mega 和同一个 grbl 串口，底层 grbl 轴名为 `A`，项目 API 中暴露为 `Z2` / `z2_mm`。

一、硬件与固件约定

- 控制板：Arduino Mega 2560，运行 grbl-Mega-5X 四轴固件。
- Z2 底层轴名：`A`。
- Z2 STEP/DIR：Arduino Mega `D40` / `D41`。
- 计划限位脚：`D42` / `D43`，但当前尚未安装限位传感器。
- 当前 grbl 参数：
  - `$103=356`
  - `$113=500`
  - `$123=100`
  - `$133=125`
- 当前关闭限位/归零：
  - `$20=0`
  - `$21=0`
  - `$22=0`

二、正式代码入口

- `hardware/xyz_stage/l3_backend/hardware/types.py`
  - `Position` 新增 `z2_mm: float = 0.0`。
  - 旧的 `Position(x_mm, y_mm, z_mm)` 构造方式仍兼容。
- `hardware/xyz_stage/l3_backend/hardware/gantry_backend.py`
  - grbl 状态解析支持 `MPos/WPos:X,Y,Z,A`。
  - 新增 `initialize_z2_at_top()`、`move_z2_to()`、`move_z2_rel()`、`park_z2()`。
  - Z2 越界会在发送 G-code 前拒绝。
- `hardware/xyz_stage/xyz_stage.py`
  - `get_position()` 现在返回 `X / Y / Z / Z2`。
  - 保持原有 `move_to(x, y, z)` 和 `move_rel(dx, dy, dz)` 不变。
- `maestro.py`
  - `Maestro` 初始化 XYZ 后会自动调用 `initialize_z2_at_top()`。
  - `stop_experiment()` 会优先尝试 `park_z2()`，将 Z2 停回 `A0`。
  - `self.z2_stage = self.xyz_stage`，实验脚本可通过 `maestro.gantry` 或 `maestro.z2_stage` 调用 Z2 方法。

三、Z2 坐标与固定位置

当前约定：

- `A` 值增大 = Z2 向下运动。
- `A0 / Z2=0`：顶部安全位。
- `A60 / Z2=60`：吸液位。
- `A70 / Z2=70`：吐液位。
- `A80 / Z2=80`：Tip 安装位。

软件安全行程：

```text
0 <= Z2 <= 125 mm
```

以下请求会被拒绝，不会下发 `G0 A...`：

```python
maestro.gantry.move_z2_to(-1)
maestro.gantry.move_z2_to(126)
```

四、启动与停车策略

因为 Z2 当前没有 HOME 限位，系统不能自动确认真实零点。当前采用“停车后自动 A0”策略：

1. 系统启动前，Z2 应处于顶部安全位。
2. `Maestro` 连接 XYZ/grbl 后自动调用 `initialize_z2_at_top()`。
3. `initialize_z2_at_top()` 会发送：

```gcode
$X
$20=0
$21=0
$22=0
G92 A0
```

4. 系统结束或 `stop_experiment()` 时，会优先尝试 `park_z2()` 回到 `A0`。
5. 下次启动继续把当前位置声明为 `A0`。

重要注意事项：

- 如果中途断电、手动移动滑台、疑似丢步，必须人工把 Z2 放回顶部安全位，再调用 `initialize_z2_at_top()`。
- 当前不要执行 `$HA`。
- 当前不要把 A 轴加入全局 `$H`。
- 正式系统运行时不要同时运行 `test_z2_stage_smoke.py` 或单独 `Z2Stage` 调试脚本，否则会抢占同一个 grbl COM 口。

五、正式 API 示例

```python
from maestro import Maestro

maestro = Maestro(use_gantry=True, mock=False)

# 固定位置
maestro.gantry.move_z2_to(60)  # 吸液位
maestro.gantry.move_z2_to(70)  # 吐液位
maestro.gantry.move_z2_to(80)  # Tip 安装位

# 回顶部安全位
maestro.gantry.park_z2()

print(maestro.gantry.get_position())  # 包含 X/Y/Z/Z2
```

如果只使用 `XYZStage`：

```python
from hardware.xyz_stage.xyz_stage import XYZStage

stage = XYZStage(port="COM11", relay_port="COM5", mock=False)
stage.connect()
stage.initialize_z2_at_top()
stage.move_z2_to(5)
stage.park_z2()
stage.close()
```

六、保留的低层调试工具

`hardware/xyz_stage/z2_stage/Z2Stage` 保留用于单独调试 Z2/A 轴，例如烧录后验证：

```gcode
$X
G92 A0
G0 A1 F100
G0 A0 F100
```

它不是正式系统运行路径。正式实验流程应通过 `XYZStage` / `Maestro` 操作 Z2，避免重复打开 Arduino Mega 串口。
