# Issue #017 — 多模块协同时缺少统一编排与设备契约层

**日期**：2026-03-24
**提出人**：Codex（架构分析）
**状态**：open
**优先级**：高
**类型**：优化
**涉及文件**：
- `Code/perovskite_auto_system/combine_tests/spin_pipette_test.py`
- `Code/perovskite_auto_system/system/profile_runner/profile_runner.py`
- `Code/perovskite_auto_system/system/monitor/system_monitor.py`
- `Code/perovskite_auto_system/hardware/xyz_stage/relay_control.py`
- `Code/perovskite_auto_system/hardware/relay/relay_manager.py`
- `Code/perovskite_auto_system/config/hardware_config.py`

## 问题描述

当前代码已经具备单模块控制能力，并已验证旋涂电机、移液枪、XYZ 导轨、继电器等硬件的基本联通性。

但随着系统从“单机调试脚本”进入“多模块协同实验流程”，现有结构开始暴露出几个架构性问题：

1. 多模块协同逻辑主要写在测试脚本中，缺少统一的系统编排层
2. 各设备返回的状态结构和控制接口不统一，组合使用时需要大量适配
3. 资源占用、互斥关系、联锁规则、急停语义没有集中管理
4. 同类职责已有重复实现，后续继续扩展容易分叉

这意味着当前结构适合“把设备跑起来”，但不适合继续承载正式的自动实验流程。

## 现象

- `spin_pipette_test.py` 直接承担了初始化、设备发现、流程编排、人工确认、绘图收尾等多种职责
- `ProfileRunner` 已经在做流程执行，但它依赖的 `monitor` 数据契约偏单模块，和 `SystemMonitor` 的系统级返回结构并不一致
- 继电器相关逻辑存在两套入口，后续扩展夹爪、阀门、抱闸时容易继续重复
- XYZ、旋涂、移液三个模块目前都能单独控制，但系统层没有统一的状态机、资源锁、故障联锁入口
- 浏览器直连串口的调试方式与后续“树莓派统一管理多设备”的目标架构并不一致

## 风险

- 新增模块后，实验流程代码会继续堆积在脚本里，维护成本快速升高
- 同一动作的前置条件可能散落在多个文件中，容易出现安全联锁遗漏
- 串口/总线资源缺少单一所有者，未来引入网页、API、后台任务后容易发生连接冲突
- 设备替换或协议调整时，流程层和驱动层耦合过深，修改范围过大

## 建议方案

将系统收敛为“单上位机进程 + 明确分层 + 单一总控”的结构，至少拆清以下职责：

### 1. 设备层（Devices）

每个硬件模块只暴露统一接口，不承担实验流程编排。

建议统一最小接口：

- `connect()`
- `home()`
- `stop()`
- `emergency_stop()`
- `get_state()`
- `close()`

### 2. 协议层（Protocols / Transport）

将串口、Modbus、Arduino 命令、USB 继电器协议从业务逻辑中剥离，形成单独适配层。

目标：

- 业务层不直接拼接报文
- 协议变更时不影响实验流程层

### 3. 运行时层（Runtime / Coordination）

新增统一系统运行时，集中处理：

- 设备注册与生命周期管理
- 串口/总线资源独占
- 全局状态机（IDLE / READY / RUNNING / FAULT / ESTOP）
- 安全联锁
- 急停广播

建议至少包含：

- `DeviceRegistry`
- `SystemRuntime`
- `SafetyManager`
- `ResourceManager`
- `StateStore`

### 4. 流程层（Workflows）

将实验流程从测试脚本中抽离，改为显式的配方/步骤模型。

不建议继续扩展松散的 `{"type": "RAMP"}` 字典方案，建议逐步收敛为明确步骤类型，例如：

- `StageMoveStep`
- `SpinRampStep`
- `PipetteAspirateStep`
- `PipetteDispenseStep`
- `WaitUntilStep`

### 5. 接口层（Interfaces）

网页、CLI、树莓派 API 只调用系统运行时，不直接占用底层串口。

目标：

- 手动调试与正式运行共用同一套后端能力
- 前端不再直接持有串口句柄

## 讨论问题

在正式开始重构前，建议先明确以下问题：

1. 未来是否坚持“单 Python 进程统一管理所有设备”，而不是拆成多个后台服务
2. 设备状态是否统一为 dataclass / typed model，而不是自由 dict
3. 流程引擎是保留同步串行模型，还是要为监控/联锁引入事件驱动机制
4. 网页控制是否从 Web Serial 逐步迁移为调用树莓派后端 API / WebSocket
5. 现有 `ProfileRunner` 是继续演进，还是只保留思路并由新的 `RecipeExecutor` 接管

## 非目标

本 issue 的目标是先收敛架构边界，不要求一次性完成以下工作：

- 不要求立即重写全部设备驱动
- 不要求立即替换现有网页调试工具
- 不要求一次性引入数据库、消息队列或微服务

## 验收标准

- [ ] 形成一版明确的分层目录结构和模块边界说明
- [ ] 形成统一的设备接口与状态模型约定
- [ ] 明确系统运行时如何管理设备生命周期、资源占用和急停
- [ ] 明确实验流程层与设备驱动层的边界
- [ ] 明确网页/API/CLI 如何通过统一后端访问设备
- [ ] 在开始重构前，形成一份经讨论确认的实施方案
