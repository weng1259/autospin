# Issue #017 — 多模块协同架构设计：整合师兄代码与现有系统

**日期**：2026-03-24
**提出人**：Claude（架构规划）
**状态**：open
**优先级**：高
**类型**：架构
**涉及文件**：`Code/perovskite_auto_system/`（整个师兄代码目录）、`firmware/`、`web_control/`

## 背景

项目现在有两套独立代码：

| 维度 | 我们的部分 | 师兄的部分 (`Code/perovskite_auto_system/`) |
|------|-----------|----------------------------------------------|
| 语言 | Arduino C + HTML/JS | Python |
| 覆盖 | 固件、传感器、网页调试面板 | 旋涂电机(RS485)、移液枪(RS485)、继电器、XYZ上位机、工艺流程 |
| 硬件 | Mac，DSTUR-T80继电器 | Windows，LCUS-8继电器 |
| 阶段 | 已接线测试通过 | 有完整代码但未在我们硬件上跑过 |

两套代码各自能运行，但还没整合。**旋涂模块和移液模块到货后，需要所有设备（XYZ导轨 + 旋涂电机 + 移液枪 + 继电器）协同工作。** 现在需要设计统一的代码架构。

## 师兄代码分析

### 代码结构

```
Code/perovskite_auto_system/
├── config/hardware_config.py    # 全局配置（端口、Modbus参数、实验坐标、旋涂工艺配方）
├── hardware/
│   ├── xyz_stage/               # XYZ三轴 — Arduino串口ASCII控制
│   │   ├── xyz_stage.py         # 高级API：safe_move_to_mm()、安全路径规划（先升Z→平移XY→降Z）
│   │   ├── motor_control.py     # 底层：ArduinoMotorBridge（串口收发）+ AxisController（单轴操作）
│   │   ├── relay_control.py     # Z轴刹车继电器联动（RelayManager + BrakeController适配器）
│   │   ├── data_monitor.py      # 驱动器状态监控（ALM报警、PEND到位信号解析）
│   │   ├── port_detector.py     # 自动识别Arduino vs 继电器（靠协议探测区分同芯片设备）
│   │   └── config.py            # XYZ参数：1280 steps/mm、软限位、速度档位
│   ├── spin_motor/              # 旋涂电机 — DBLS400无刷驱动器，RS485 Modbus RTU
│   │   ├── __init__.py          # SpinMotor 统一封装类（一键初始化通信+控制器）
│   │   ├── driver_communication.py  # Modbus RTU底层（CRC16、寄存器读写、字节序处理、自动ID对齐）
│   │   ├── motor_controller.py  # 电机控制逻辑（启停、调速、方向切换、紧急刹车）
│   │   └── data_monitor.py      # 转速/电压/故障实时监控
│   ├── pipette/                 # 电动移液枪 — 28系列，RS485 Modbus RTU (pymodbus库)
│   │   ├── driver_communication.py  # ModbusRTUClient（基于pymodbus 3.x）
│   │   └── pipette_controller.py    # 吸液/吐液/退Tip/液面探测/归位
│   └── relay/                   # USB继电器 — LCUS-8 (HEX协议，和我们的DSTUR-T80协议相同)
│       ├── usb_relay.py         # 底层驱动（on/off/查询状态）
│       ├── relay_manager.py     # 设备注册、通道冲突检测、全局急停
│       ├── gripper.py           # 夹爪封装
│       └── valve.py             # 电磁阀封装
├── system/
│   ├── profile_runner/profile_runner.py  # ★ 核心：工艺流程执行器
│   │   # 支持5种步骤：START → RAMP → HOLD → PIPETTE → STOP
│   │   # 内置故障检测、速度重试、数据记录
│   └── monitor/system_monitor.py  # 系统级监控（聚合电机+移液枪状态，安全判断）
├── utils/
│   ├── logger.py                # 日志系统（CSV数据 + 详细日志 + 报告JSON）
│   ├── plot_analyzer.py         # 数据可视化
│   └── hardware_utils.py        # 串口自动检测
└── combine_tests/
    └── spin_pipette_test.py     # 联合测试入口（初始化所有设备 → 执行工艺配方）
```

### 师兄代码的优点

1. **分层清晰**：driver_communication（底层通信）→ controller（设备逻辑）→ profile_runner（流程编排），职责分离做得好
2. **ProfileRunner 声明式配方**：用 JSON 列表定义工艺流程，灵活且易于修改
3. **日志完善**：自动记录通信报文、速度曲线CSV、故障日志，便于实验数据分析
4. **Modbus RTU 实现完整**：CRC16校验、重试机制、字节序处理、ID自动对齐，可以直接复用
5. **继电器协议与我们完全一致**：LCUS-8 和 DSTUR-T80 都是 `0xA0 + CH + ON/OFF + checksum`

### 师兄代码的问题

1. **串口管理分散** — 每个模块自己打开串口，没有统一端口管理器。Arduino和继电器都是CH340芯片（hwid `1A86:7523`），`auto_detect_port()` 只返回第一个匹配的，多个同芯片设备时无法区分
2. **设备接口不统一** — xyz_stage 有 `emergency_all_stop()`，spin_motor 有 `emergency_stop()`，relay 有 `emergency_stop()` 签名不同，没有统一的设备基类
3. **硬件配置写死** — Windows COM端口、LCUS-8特定逻辑，无法直接在Mac + DSTUR-T80上运行
4. **ProfileRunner 只管旋涂+移液** — XYZ导轨移动不在工艺流程里，无法编排"移到吸液位→吸液→移到旋涂台→滴液→旋涂"的完整流程
5. **安全联锁不够** — `SystemMonitor.is_safe()` 只检查电机故障，没有和XYZ限位传感器、Z轴抱闸、旋涂台转速联动
6. **步距参数可能不匹配** — 师兄用 `1280 steps/mm`（6400细分÷5mm导程），我们闭环模式是 3200脉冲/转（80步/mm、导程5mm → 400步/mm？），需要确认

## 要讨论的问题

### 问题1：目录结构怎么组织？

**方案A — 在师兄目录基础上改造：**
```
Code/perovskite_auto_system/   # 直接在这里改
├── config/
├── hardware/
├── system/
└── utils/
```
优点：改动最小。缺点：和我们现有的 `firmware/`、`web_control/` 割裂，两套代码仓库感觉。

**方案B — 新建 `server/` 目录，迁移整合：**
```
智能旋涂仪/
├── firmware/          # Arduino固件（不动）
├── web_control/       # 网页面板（不动）
├── hardware/          # 硬件文档（不动）
├── docs/              # 文档（不动）
└── server/            # ← 新的Python上位机控制系统
    ├── config/
    ├── drivers/       # 从师兄的 hardware/ 迁移+重构
    ├── services/      # 新增业务逻辑层
    └── api/           # 未来对外接口
```
优点：项目结构统一、清晰。缺点：需要迁移代码，工作量大。

**方案C — 保持 `Code/` 为师兄原始参考，`server/` 为正式版：**
```
智能旋涂仪/
├── Code/              # 师兄原始代码（只读参考，不再修改）
├── firmware/
├── web_control/
└── server/            # 正式系统，从师兄代码重构而来
```
优点：原始代码有据可查，正式版干净。缺点：两份相似代码容易混淆。

**倾向方案B**，请讨论。

### 问题2：设备驱动层要不要定义统一基类？

师兄的四个设备驱动接口各不相同。如果定义统一基类：

```python
class DeviceDriver:
    def connect(self) -> bool: ...
    def disconnect(self): ...
    def is_connected(self) -> bool: ...
    def emergency_stop(self): ...
    @property
    def name(self) -> str: ...
```

**好处**：设备管理器可以统一遍历所有设备做急停、状态检查等。
**代价**：不是所有设备都自然fit这个接口（比如继电器没有"连接"概念——它是通过串口发指令，不需要handshake）。

要不要强制统一？还是用鸭子类型（只要有 `emergency_stop` 方法就行）？

### 问题3：串口端口管理怎么做？

核心难点：Arduino 和 DSTUR-T80 继电器都是 CH340 芯片，hwid 都是 `1A86:7523`。

**方案A — 协议探测（师兄的做法）：**
依次尝试每个CH340端口：发 `Q\n`(115200) 看回不回 `X...Y...Z...` → 是Arduino；发 `0xFF`(9600) 看回不回8字节 → 是继电器。
- 优点：全自动
- 缺点：每次启动要逐个试，如果设备没开机会很慢；探测可能误触发设备动作

**方案B — 配置文件指定端口：**
```python
# config/hardware.py
PORTS = {
    "arduino": "/dev/cu.usbmodemXXXX",
    "relay": "/dev/cu.usbmodemYYYY",
}
```
- 优点：简单可靠
- 缺点：换USB口就要改配置；不够"智能"

**方案C — 配置优先，探测兜底：**
先读配置文件，如果端口不可用再自动探测。配置文件在首次探测成功后自动生成。
- 优点：兼顾便捷和可靠
- 缺点：实现稍复杂

**倾向方案C**，请讨论。

### 问题4：ProfileRunner 怎么扩展支持 XYZ 导轨？

师兄的 ProfileRunner 只支持 5 种步骤类型（START/RAMP/HOLD/PIPETTE/STOP），都是关于旋涂电机和移液枪的。

完整的自动旋涂流程需要这样：

```python
FULL_EXPERIMENT = [
    # 阶段1：准备
    {"type": "XYZ_HOME"},                                    # 三轴归零
    {"type": "XYZ_MOVE", "target": "PIPETTE_RACK"},          # 移到吸液位
    {"type": "PIPETTE", "action": "aspirate", "volume": 500}, # 吸500μL前驱体溶液
    {"type": "XYZ_MOVE", "target": "SPIN_CENTER"},           # 移到旋涂台正上方

    # 阶段2：旋涂
    {"type": "SPIN_START", "direction": "forward"},
    {"type": "SPIN_RAMP", "from": 0, "to": 1000, "duration": 2},
    {"type": "SPIN_HOLD", "speed": 1000, "duration": 3},
    {"type": "PIPETTE", "action": "dispense", "volume": 500}, # 在旋转中滴加
    {"type": "SPIN_RAMP", "from": 1000, "to": 3000, "duration": 3},
    {"type": "SPIN_HOLD", "speed": 3000, "duration": 30},
    {"type": "SPIN_RAMP", "from": 3000, "to": 0, "duration": 3},
    {"type": "SPIN_STOP"},

    # 阶段3：清理
    {"type": "XYZ_MOVE", "target": "WASTE_BIN"},
    {"type": "PIPETTE", "action": "drop_tip"},
    {"type": "XYZ_MOVE", "target": "HOME"},
]
```

**问题**：
- 师兄现有的 `step_handlers` 字典模式很好扩展，直接加新的 handler 就行。但 XYZ_MOVE 是阻塞式等待（等 `_DONE` 信号），而旋涂 HOLD 也是阻塞式等待。如果需要"一边旋涂一边移动导轨"（虽然目前不需要），现有的顺序执行模型就不够了。
- 要不要现在就考虑并行步骤？还是先保持顺序执行，够用再说？

**倾向先保持顺序执行**，YAGNI 原则。请讨论。

### 问题5：安全联锁层怎么设计？

当前缺失的安全逻辑：

| 场景 | 期望行为 | 当前状态 |
|------|----------|----------|
| Z轴移动前 | 自动松刹车，等0.3s再动 | 师兄代码有（xyz_stage.py `_z_prepare`），但固件层没有 |
| Z轴停止后 | 等0.2s锁刹车 | 师兄代码有（`_z_finish`） |
| XY移动前 | Z轴先抬到安全高度 | 师兄代码有（`safe_move_to_mm`） |
| 限位传感器触发 | 立即停止对应轴 | 固件层已有（Issue #002 待实现），上位机层没有 |
| 旋涂电机高速旋转中 | 禁止XYZ移动（防震动干扰薄膜） | 完全没有 |
| 任何设备通信超时 | 全局急停 | 没有 |
| 用户按急停 | 所有电机停 + 刹车锁 + 继电器全断 | 没有统一的急停链路 |

**问题**：安全逻辑放在哪一层？
- **方案A — 放在设备驱动层**：每个驱动自己检查前置条件。分散，难以实现跨设备联锁。
- **方案B — 独立安全服务层**：所有操作先经过 SafetyManager 检查。集中，但多一层调用。
- **方案C — 流程编排层检查**：ProfileRunner 在每个步骤前后做安全检查。只保护自动流程，手动操作不受保护。

**倾向方案B**，请讨论。

### 问题6：步距参数是否匹配？

师兄代码假设 `1280 steps/mm`（基于 6400 细分 ÷ 5mm 导程）。

我们当前闭环模式下驱动器设置是 3200 脉冲/转。丝杠导程 5mm，所以：
- 3200 脉冲/转 ÷ 5mm/转 = **640 脉冲/mm**

这和师兄的 1280 步/mm 不一致。如果直接用师兄的代码，所有坐标会偏一倍。

**需要确认**：当前闭环模式下的实际步距关系，然后统一 `config` 中的参数。

### 问题7：是否需要现在就考虑树莓派部署？

目标架构是最终部署到树莓派，通过 WiFi 远程控制。但现在还在 Mac 上调试。

**问题**：Python 上位机代码是直接按树莓派目标写（比如用 FastAPI + WebSocket），还是先写最简单的命令行版本跑通再说？

**倾向先跑通命令行版本**，树莓派迁移主要是网络层（加个 API 服务器），底层驱动不需要改。请讨论。

## 建议的实施路径

```
第一步：确认步距参数，在现有硬件上跑通 师兄的 xyz_stage + relay 驱动
        → 验证 Python → Arduino 完整链路

第二步：创建 server/ 目录，搭建统一框架
        → DeviceDriver 基类 + PortManager + SafetyManager

第三步：迁移 xyz_stage 和 relay 驱动到 server/drivers/，适配 Mac 环境

第四步：等旋涂/移液模块到货，迁移 spin_motor 和 pipette 驱动

第五步：扩展 ProfileRunner 支持 XYZ 步骤，编写完整工艺配方

第六步：加 API 层，部署到树莓派
```

## 讨论要点汇总

请对以上 7 个问题给出意见：

1. 目录结构选 A/B/C？
2. 设备驱动要不要统一基类？
3. 串口管理选 A/B/C？
4. ProfileRunner 并行步骤现在做还是以后做？
5. 安全联锁放在哪一层？
6. 步距参数 1280 vs 640，怎么统一？
7. 是否现在就考虑树莓派部署？

另外欢迎提出我没想到的问题。
