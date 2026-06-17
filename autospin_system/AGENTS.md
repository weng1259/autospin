# AGENTS.md

## 项目定位

本项目是一个自动旋涂与实验室自动化平台，当前代码名为 `AutoSpinmotorSystem`。它的近期目标是把旋涂电机、XYZ 龙门平台、移液枪、继电器和后续夹爪/热板等硬件统一到 Python 编排层中；更长期的目标是把这些能力暴露为结构化工具，让 LLM Agent 能通过自然语言理解实验意图、组合硬件动作并完成实验流程。

项目优先级不是传统的“先做 UI”，而是：

1. 底层硬件稳定性
2. 清晰、严格、可恢复的 Python API
3. 可观测日志和实验记录
4. UI 或聊天入口

对后续 Agent 来说，最重要的是 API 返回值必须可信：硬件失败、ALARM、未归零、越界等状态不能伪装成成功。

## 当前目录与主要文件

- `maestro.py`：系统总控制器。负责初始化硬件、启动/停止实验、统一 shutdown，并持有 `spincoater`、`liquidhandler`、`relay`、`gantry/xyz_stage` 等设备实例。
- `system.py`：Worker 注册和系统构建。`generate_workers()` 根据配置生成 Worker，`build(use_gantry)` 返回规划系统所需的 workers、transitions、起点和终点。
- `workers.py`：PASCAL 风格的 Worker 抽象。包含异步优先级队列、任务元数据、机械臂转移、旋涂/移液协同、热板、存储、表征和人工操作 Worker。
- `config/hardware_config.py`：配置加载器。创建 `logs/`、`data/` 目录，并读取 `config/system_config.yaml` 暴露为全局 `CONFIG`。
- `config/system_config.yaml`：核心配置文件。包含串口、硬件参数、几何坐标、工具偏移、Worker 容量、任务耗时和转移耗时。
- `hardware/spin_motor/`：DBLS400 旋涂电机控制。高层 `MotorController` 封装启动、停止、锁定、设速、读实际转速；底层 `DriverCommunication` 负责 Modbus RTU 帧、CRC 和寄存器读写。
- `hardware/pipette/`：移液枪控制。高层 `PipetteController` 封装归位、吸液、吐液、液面检测、Tip 检测等；底层 `PipetteDriver` 使用 `pymodbus`。
- `hardware/relay/`：继电器通道管理。`RelayManager` 按 LCUS-8 协议生成 `[0xA0, channel, state, checksum]` 控制报文，并维护通道状态。
- `hardware/xyz_stage/`：XYZ 平台控制。`XYZStage` 通过 GRBL/G-code 控制三轴运动，支持绝对移动、相对移动、归零、软限位检查和急停。
- `test_spin_motor_profile.py`：旋涂电机调试脚本。按 `TEST_PROFILE` 执行 START/RAMP/HOLD/STOP，并记录日志到 `logs/`。
- `test_maestro_mock.py`：最小 Maestro mock 测试入口。用 `mock=True` 初始化系统、启动实验、执行 `idle_gantry()`、再 shutdown。
- `example_experiment.py`：示例实验脚本，展示完整实验流程思路；当前引用的 `DEFAULT_PEROVSKITE_PROFILE` 在现有 `config/__init__.py` 中没有导出，运行前需要补齐或改写。
- `AutoSpinmotorSystem_说明文档.md`：主说明文档；已吸收的旧根目录说明文档不再保留。

## 核心架构

系统大致分为四层：

1. 硬件通信层：串口、Modbus RTU、G-code、继电器十六进制协议。
2. 硬件控制层：`MotorController`、`PipetteController`、`RelayManager`、`XYZStage`。
3. 编排层：`Maestro` 和 `WorkerTemplate`/各 Worker。
4. Agent/API 层：文档中规划为 LLM tool use、结构化错误、幂等 key、dry-run、状态查询和 hooks；当前仓库主要保留了 AutoSpinmotorSystem 侧代码与设计文档。

`Maestro` 是运行时中心。创建 `Maestro(use_gantry=True, mock=False)` 会读取 `CONFIG['communication']` 中的端口，初始化旋涂电机、移液枪、继电器，并在 `use_gantry=True` 时初始化 XYZ 平台。`mock=True` 时底层驱动不会打开真实串口，适合离线调试软件逻辑。

`WorkerTemplate` 是任务调度抽象。每个 Worker 通过 `self.functions` 注册任务名、执行函数、预计耗时和协作 Worker。任务进入 `asyncio.PriorityQueue`，按 `task["start"]` 时间调度。

## 配置模型

主要配置集中在 `config/system_config.yaml`：

- `communication`：`motor_port=COM14`、`pipette_port=COM4`、`relay_port=COM5`、`gantry_port=COM6`。
- `system.default_worker_capacity`：热板默认容量 30，存储盘默认容量 45。
- `devices.xyz_stage`：GRBL 串口参数、`steps_per_mm=1280`、安全 Z 高度、软限位。
- `devices.relay`：LCUS-8 命令前缀、开关命令和通道映射，如 `z_brake`、`nitrogen`、`pump`、`spin_power`、`aux_light`。
- `devices.spin_motor`：DBLS400 从站 ID、波特率、超时、极对数和常用寄存器。
- `devices.pipette`：移液枪从站 ID、波特率、超时、最大体积。
- `geometry.lab_coordinates`：关键工位坐标，如 `home`、`spin_center`、`clean_station`、`waste_bin`。
- `geometry.tool_offsets`：夹爪和移液枪 TCP 偏移，移液枪还包含 `tip_length`。
- `scheduler`：任务预计耗时和设备间转移耗时。

坐标换算约定：

```text
real_x = lab_x - tool_offset_x
real_y = lab_y - tool_offset_y
real_z = lab_z - tool_offset_z - tip_length_if_needed
```

Worker 中的安全移动流程通常是：先把 Z 抬到 `safe_z_mm`，再水平移动 XY，最后下降到目标 Z。

## 硬件与安全注意事项

- 真实硬件运行前优先使用 `mock=True` 验证代码路径。
- `XYZStage.move_to()` 会在发送 G-code 前检查 YAML 软限位，越界会拒绝移动。
- `XYZStage.emergency_stop()` 在真实模式下发送 `Ctrl+X` 给 GRBL，触发软复位。
- `Maestro.stop_experiment()` 会停止旋涂电机、关闭继电器并急停机械臂。
- `RelayManager.emergency_stop()` 会关闭全部继电器通道。
- 旋涂电机启动前 `MotorController.start(wait_for_stop=True)` 会尝试确认实际转速接近 0，避免未停稳时再次启动。
- DBLS400 控制字中 Bit 3 被视为必要运行位；极对数会左移 8 位写入控制字。
- 电机实际转速读取中有 `2.5` 倍反馈补偿。
- 移液枪吸液/吐液前会检查初始化状态、体积范围和 Tip 是否在位。

## 运行与测试建议

常用入口：

```powershell
python test_maestro_mock.py
python test_spin_motor_profile.py
```

建议优先运行 mock 测试，尤其是在没有连接硬件或串口号不确定时。`test_spin_motor_profile.py` 当前创建 `Maestro(use_gantry=False, mock=True)`，用于旋涂电机调试流程演示。

项目没有发现 `requirements.txt`、`pyproject.toml` 或其他依赖清单。根据源码，至少需要：

- `pyyaml`
- `pyserial`
- `pymodbus`

如果从仓库新环境启动，先安装这些依赖，再运行 mock 脚本。

## 当前已知问题与接手提醒

- 源码里的部分中文注释在终端输出中会乱码，但 Markdown 文档按 UTF-8 读取正常。编辑时请保持 UTF-8。
- `__init__.py` 中版本为 `2.0.0`，说明文档写的是 `2.1.0`，版本信息不一致。
- `example_experiment.py` 引用 `AutoSpinmotorSystem.config.DEFAULT_PEROVSKITE_PROFILE`，但当前 `config/__init__.py` 没有导出该变量。
- `system.py` 使用相对导入 `.workers`，当从包外正确导入 `AutoSpinmotorSystem.system` 时更合理；直接在项目根目录运行某些脚本可能遇到包路径问题。
- `workers.py` 中 `WorkerTemplate` 只有在 `planning=False` 时才设置 `logger`，但部分 Worker 构造函数无条件调用 `self.logger.info()`；`generate_workers(maestro=None)` 的 planning 模式可能因此出错，需要修正。
- `system.py` 中 `transitions` 当前仍是空列表，设备间 transition 任务映射虽然定义了，但还没有真正构造成可执行 transition 对象。
- `RelayManager.connect()` 当前没有实际打开串口，`_serial` 仍为空；真实继电器控制前需要补齐串口打开逻辑。
- `XYZStage.disconnect()` 目前没有实际关闭 `_serial`，只更新状态；真实运行建议补齐。
- `XYZStage._initialize_stage()` 仍是 TODO，没有实际归零或配置 GRBL。
- `PipetteDriver` 使用 `pymodbus` 3.x 风格，但实际调用是否需要 `slave=`/`unit=` 参数取决于安装版本，联机前要实测。
- `.gitignore` 已忽略 `logs/`、`data/`、`__pycache__/`，实验日志和数据默认不会进 Git。

## 后续开发方向

从文档看，项目计划从单机旋涂控制走向 Agent-first 实验平台：

- 先把底层硬件动作稳定封装为原子 API，例如归零、移动、夹爪开合、吸液、吐液、旋涂 ramp、热板退火。
- 再把原子动作组合为工艺级 Unit Tasks，例如滴加反溶剂、转移样品、旋涂并退火。
- API 需要结构化错误、幂等 key、dry-run、状态查询和可观测事件，方便 LLM Agent 安全决策。
- 近期可采用人在回路：机器人完成制备，人工完成表征并把结果输入算法。
- 后续可引入贝叶斯优化，用旋涂转速、反溶剂滴加延迟、退火温度/时间等参数做黑盒优化。

## 给后续 Agent 的工作原则

- 不要默认连接真实硬件；除非用户明确要求并确认串口、设备、电源和安全边界。
- 修改坐标、端口、容量、耗时时，优先改 `config/system_config.yaml`，不要把硬件参数重新硬编码进 Python。
- 新增硬件时，优先按“底层通信类 + 高层控制器 + Worker/任务注册”的结构扩展。
- 新增任务时，在 Worker 的 `self.functions` 中注册 `task_tuple(function, estimated_duration, other_workers)`，并在 `system.py` 中确认调度侧能发现它。
- 涉及运动控制时，一律保留“抬 Z 到安全高度 -> 平移 XY -> 下降 Z”的安全节奏。
- 对可能造成硬件动作的脚本，先提供 mock 路径或 dry-run 路径。
- 不要提交 `logs/`、`data/`、`__pycache__/`。
- 如果要让 LLM Agent 调用硬件，工具返回值必须包含明确状态、错误码、是否可恢复、建议动作和最终位置/状态。
