# 自动化旋涂系统开源方案调研报告

> 调研日期：2026-04-13 | 基于联网搜索最新数据

---

## Layer 1：Arduino Mega 2560 运动控制固件

### 速览表格

| 项目 | License | 最近提交 | Star | 兼容 Mega 2560？ | 核心能力 | 不适合的原因 |
|---|---|---|---|---|---|---|
| **grbl-Mega (gnea)** | GPLv3 | 2023-01 | ~497 | ✅ **专为 Mega 设计** | 3轴 step/dir，梯形加减速，G-code，归零，限位，实时覆写 | 已停止维护，仅3轴 |
| **grbl-Mega-5X (fra589)** | GPLv3 | 2025 活跃讨论 | ~377 | ✅ **专为 Mega 设计** | 5/6轴，基于grbl 1.1f，完整加减速+前瞻，realtime override | 社区较小，维护节奏慢 |
| **Marlin** | GPLv3 | 2026-03-30 | **~17.3k** | ✅ 原生支持 RAMPS/Mega | CNC模式(M3/M4)，S曲线加减速，多轴，LCD/Web，温控 | 偏3D打印，CNC功能为副线；配置复杂度极高 |
| **Klipper** | GPLv3 | 2026 极活跃 | ~18k | ✅ MCU端支持 atmega2560 | **运动规划在RPi侧**，MCU仅执行step脉冲，Python配置，高精度 | 架构转变大：需RPi常驻运行；CNC用法非主流 |
| **FluidNC** | GPLv3 | 2026 活跃 | ~2.2k | ❌ **ESP32 only** | YAML配置、Web UI、Modbus spindle、多工具 | **硬件不符**：不支持 ATmega2560 |
| **Smoothieware** | GPLv3 | 维护中 | ~1.8k | ❌ **ARM (LPC1768/STM32)** | RTOS、多轴、Web界面 | **硬件不符** |
| **TinyG / g2core** | MIT / GPLv2 | g2core 2023 | ~670 | ❌ **ARM (SAM3X8E)** | 6轴、S曲线jerk、JSON协议 | **硬件不符** |
| **LinuxCNC** | GPLv2 | 活跃 | ~2k+ | ❌ **x86 PC + 并口/Mesa卡** | 工业级多轴、HAL、Python接口 | **硬件不符**：需专用PC和实时内核 |

### Top 推荐

#### 推荐1：grbl-Mega-5X（短期最佳 drop-in replacement）

**为什么推荐**：
- 专为 Mega 2560 设计，零硬件改动直接刷入
- 解决你的痛点：✅ 梯形加减速+前瞻（解决丢步）、✅ 标准G-code协议（解决串口简陋）、✅ 两阶段归零（$27 pull-off）、✅ 硬/软限位、✅ feed hold/alarm/door、✅ realtime override（运动中改速生效）、✅ jog模式
- 闭环驱动器(2HSS57-C)通过 step/dir 接口兼容，ALM 信号可接到 Abort/Door pin
- 辅助I/O（Spindle Enable/Coolant）可复用为Z刹车和夹爪继电器控制

**落地步骤**（约1-2天工时）：
1. Fork fra589/grbl-Mega-5X，修改 `cpu_map.h` 映射你的9个限位开关pin（Mega有充足中断引脚）
2. 配置 `$` 参数：steps/mm=682.67，max rate/accel 按你的57闭环电机调
3. 将 Spindle Enable(M3) 映射到Z刹车继电器pin，Coolant(M8) 映射到夹爪继电器pin
4. 用任意G-code sender测试归零和运动
5. 上位机通过串口发标准G-code，替换你现有的自定义协议

**已知坑**：
- grbl在Mega上最高脉冲频率~30kHz，你的51200脉冲/转×最高速度=需要验算是否超限
- 仅3轴XYZ，如果未来需要旋转轴需要改5X分支
- 辅助I/O只有Spindle和Coolant两路，你需要8路继电器→USB继电器仍需上位机独立控制
- 讨论区活跃度一般，深度问题可能需要自己看源码

**链接**：
- GitHub: https://github.com/fra589/grbl-Mega-5X
- Wiki: https://github.com/fra589/grbl-Mega-5X/wiki
- grbl 1.1 协议文档: https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface

#### 推荐2：Klipper（中期最优架构，但改动大）

**为什么推荐**：
- 运动规划完全在 RPi 上用 Python 执行——这和你规划的「树莓派 MCP Server」架构天然契合
- MCU（Mega 2560）仅负责精确的step脉冲执行，所有高层逻辑在Python中
- 意味着：加减速算法、坐标转换、多模块协调 全在RPi侧用Python写，比在AVR C里改方便100倍
- 你可以在Klipper的Python层直接集成夹爪Modbus、USB继电器、旋涂模块控制
- 社区极度活跃，Discord非常responsive

**不推荐立即上的原因**：
- Klipper面向3D打印，CNC场景（归零逻辑、坐标系统）需要大量自定义
- 没有现成的CNC G-code sender生态配合
- 学习曲线陡峭：需要理解MCU/host分离架构

**链接**：
- GitHub: https://github.com/Klipper3d/klipper
- AVR文档: https://www.klipper3d.org/Bootloaders.html
- ATmega2560 pin alias: https://github.com/Klipper3d/klipper/blob/master/config/sample-aliases.cfg

---

## Layer 2：上位机 G-code 发送器

### 速览表格

| 项目 | License | 最近提交 | Star | Web UI | REST/Python API | 自定义 Macro | 树莓派友好 |
|---|---|---|---|---|---|---|---|
| **cncjs** | MIT | **2026-03-12** v1.11.0 | **~2.5k** | ✅ 完整Web界面 | ✅ Socket.IO API + npm controller lib | ✅ 宏 | ✅ 有官方RPi镜像 |
| **UGS (Universal Gcode Sender)** | GPLv3 | 2026-01 活跃 | ~2.1k | ✅ Platform版有Web模块 | ⚠️ Java API，集成较重 | ✅ | ⚠️ Java，RPi性能勉强 |
| **bCNC** | GPLv2 | 2025 活跃 | ~1.5k | ❌ Tkinter桌面 | ✅ **Python原生** | ✅ Python插件 | ✅ 但无Web界面 |
| **OpenBuilds Control** | GPLv3 | 2025 | ~200+ | ✅ Electron | ⚠️ 有限 | ✅ | ⚠️ Electron较重 |
| **Candle** | GPLv3 | 低活跃 | ~700 | ❌ Qt桌面 | ❌ | ⚠️ | ❌ |
| **gSender** | GPLv3 | 2025 活跃 | ~400 | ❌ Electron | ⚠️ | ✅ | ⚠️ |

### Top 推荐

#### 推荐1：cncjs（最佳选择）

**为什么推荐**：
- **Web UI + Socket.IO API** = 完美匹配你的「树莓派 MCP Server + Mac/手机 WiFi 接入」架构
- 2026年3月刚发布v1.11.0，极度活跃
- 支持 Grbl / Marlin / Smoothie / TinyG 全系，换固件无缝切换
- `cncjs-controller` npm包提供编程接口，可以从Node.js/Python发G-code
- 支持自定义widget和pendant扩展
- 有现成的RPi镜像 `cncjs-pi-raspbian`

**落地步骤**（约半天工时）：
1. `npm install -g cncjs` 装在树莓派上
2. 配置串口连接到 Arduino Mega（运行grbl-Mega-5X）
3. 浏览器访问 `http://rpi-ip:8000` 即可控制
4. 用 Socket.IO API 写 Python wrapper 发送G-code，集成到你的协调层

**已知坑**：
- Socket.IO API文档偏薄，需要看源码
- 自定义宏不支持条件跳转（复杂工序逻辑需要在上位机Python层做）
- Web UI在手机端偶有响应慢的报告

**链接**：
- GitHub: https://github.com/cncjs/cncjs
- 官网: https://cnc.js.org/
- RPi安装: https://github.com/cncjs/cncjs-pi-raspbian

#### 备选：bCNC（Python直接集成最方便）

如果你不需要Web UI，bCNC是纯Python写的GRBL控制器，可以直接 `import` 它的串口通信模块到你的Python协调层，省去Node.js中间层。

---

## Layer 3：多模块协调框架

### 速览表格

| 项目 | License | Star | 最近活跃 | 核心场景 | 适合你的程度 | 不适合的原因 |
|---|---|---|---|---|---|---|
| **PyLabRobot** | MIT | ~389 | 2026 极活跃 | 通用实验室机器人SDK | ⭐⭐⭐⭐ **最推荐** | 需自己写backend适配 |
| **Opentrons API** | Apache 2.0 | ~498 | 2026-04 (v9.0.0) | OT-2/Flex移液机器人 | ⭐⭐ 架构可参考 | 深度绑定Opentrons硬件 |
| **pymodbus** | BSD | ~2.2k | 2026 活跃 | Modbus RTU/TCP | ⭐⭐⭐ 底层必用 | 仅通信层，非编排框架 |
| **ROS 2 + MoveIt** | Apache 2.0 | 海量 | 极活跃 | 工业机器人 | ⭐ | **杀鸡用牛刀**，学习曲线极陡，单机用太重 |
| **Node-RED** | Apache 2.0 | ~20k | 极活跃 | 流程编排+可视化 | ⭐⭐⭐ 适合胶水层 | 复杂逻辑难调试 |
| **minimalmodbus** | Apache 2.0 | ~300+ | 维护中 | 轻量Modbus RTU | ⭐⭐ 更简单的modbus选择 | 功能少于pymodbus |

### Top 推荐

#### 推荐1：PyLabRobot（强烈推荐）

**为什么推荐**：
- **Connor Coley 是共同作者之一**——这个人恰好是你的目标PhD advisor！用他的框架做本科项目，对申请有直接加分
- 架构设计完美匹配你的需求：`interface层`（统一API）+ `backend层`（硬件驱动）= 你可以为你的龙门架、夹爪、旋涂模块各写一个backend
- 已有 LiquidHandler、Centrifuge、Pump 等抽象，你可以加 SpinCoater、GantryRobot
- MIT License，可商用
- 内置浏览器 Visualizer，调试友好
- 支持 RPi OS，Python原生 async/await

**落地架构设想**：
```
PyLabRobot
├── GantryBackend (via cncjs Socket.IO → grbl-Mega-5X)
├── GripperBackend (via pymodbus RTU → YJZK夹爪)
├── SpinCoaterBackend (via pyserial → 旋涂模块RS485)
├── PipetteBackend (via pyserial → 移液模块RS485)
└── RelayBackend (via pyserial → DSTUR-T80 USB继电器)
```

**落地步骤**（约2-3周工时）：
1. `pip install pylabrobot`，跑通Visualizer模拟
2. 参考现有的 OpentronsBackend 写一个 GantryBackend，封装cncjs的Socket.IO
3. 写 GripperBackend，用 pymodbus 发 Modbus RTU 到夹爪
4. 写 SpinCoaterBackend（等旋涂模块到位后）
5. 写一个 Protocol，串联：move_to → grip → move_to → spin_coat → release

**已知坑**：
- PyLabRobot 目前主要围绕液体处理（Hamilton、Tecan、Opentrons），机械臂/龙门架的现成backend需要自己从零写
- 文档还在完善中，部分功能需看源码
- 版本迭代较快，API可能有breaking change

**链接**：
- GitHub: https://github.com/PyLabRobot/pylabrobot
- 论文: Wierenga et al., Device (2023) https://doi.org/10.1016/j.device.2023.100111
- 文档: https://docs.pylabrobot.org

#### 备选：自建薄协调层（最务实路径）

如果 PyLabRobot 的抽象过重，你可以自建一个极简协调层：

```python
# experiment_runner.py
import asyncio
from pymodbus.client import AsyncModbusSerialClient  # 夹爪
import aiohttp  # → cncjs Socket.IO
import serial_asyncio  # → USB继电器

class ExperimentRunner:
    async def run_coating_job(self, params: dict):
        await self.gantry.home()
        await self.gripper.open()
        await self.gantry.move_to(params['pickup_pos'])
        await self.gripper.close(force=params['grip_force'])
        await self.gantry.move_to(params['coater_pos'])
        await self.gripper.open()
        await self.coater.run_recipe(params['spin_recipe'])
        # ... 记录到SQLite
```

底层用 `pymodbus` + `pyserial-asyncio`，全 async，一个文件搞定。

---

## Layer 4：旋涂工艺专用开源项目

### 速览表格

| 项目 | 平台 | Star | 最近活跃 | 核心特色 | 适合你的程度 |
|---|---|---|---|---|---|
| **Maasi** (klotzsch-lab) | ESP32 + ESC + Nextion | ~40 | 2021（论文后低活跃） | 完整开源spin coater，HardwareX论文，转速曲线控制，ESC遥测 | ⭐⭐⭐ 转速曲线控制逻辑可直接参考 |
| **Spin-coater-v1** (BirdbrainEngineer) | RPi Pico | ~20 | 2023 | PID转速控制，JSON job文件定义多阶段转速曲线，SD卡存储 | ⭐⭐⭐⭐ **Job定义格式和PID控制逻辑最值得借鉴** |
| **cphnano/spincoater** | Arduino | <10 | 低活跃 | 简单固件+Python client+记录功能 | ⭐⭐ client/logging部分可参考 |
| **Arduino-Spin-Coater** (r-ym) | Arduino | <10 | 低活跃 | 太阳能电池旋涂，ramp speed/time参数化 | ⭐ 参数化思路可参考 |

### 关键借鉴点

**BirdbrainEngineer 的 JSON Job 格式**最值得抄：
```json
{
  "name": "PEDOT:PSS",
  "steps": [
    {"rpm": 500, "accel": 200, "duration_s": 5, "dispense": true},
    {"rpm": 3000, "accel": 1000, "duration_s": 30, "dispense": false},
    {"rpm": 5000, "accel": 500, "duration_s": 10, "dispense": false}
  ]
}
```

**Maasi 的 ESC 遥测方案**也值得参考——通过ESC内置遥测读取实际转速，做闭环PID控制，比编码器方案更适合高速旋转场景。

**总体评估**：旋涂控制逻辑本身不复杂（就是一个多阶段转速曲线 + PID），上面这些项目的核心价值在于「怎么定义和执行一个旋涂recipe」的数据结构，而不是代码本身。建议直接抄 Job JSON 格式，PID 自己写（或让 Claude 写）。

---

## Layer 5：实验数据记录与 UI

### 速览表格

| 方案 | License | Star | 适合场景 | 学习曲线 | 你的场景评分 |
|---|---|---|---|---|---|
| **Streamlit** | Apache 2.0 | ~38k | 快速Web UI + 数据展示 | ⭐ 极低 | ⭐⭐⭐⭐⭐ **最推荐** |
| **Gradio** | Apache 2.0 | ~36k | ML demo / 参数调节面板 | ⭐ 极低 | ⭐⭐⭐⭐ 适合BO参数面板 |
| **Node-RED** | Apache 2.0 | ~20k | 流程编排+面板 | ⭐⭐ | ⭐⭐⭐ 可做设备联动可视化 |
| **Grafana + InfluxDB** | AGPLv3 / MIT | 66k/30k | 时序数据监控 | ⭐⭐⭐ | ⭐⭐⭐ 适合实时监控，但部署较重 |
| **SQLite + Streamlit** | — | — | 实验记录 + 查询 | ⭐ | ⭐⭐⭐⭐⭐ |
| **Home Assistant** | Apache 2.0 | ~75k | 智能家居编排 | ⭐⭐⭐ | ⭐⭐ 太重，偏离场景 |

### Top 推荐：Streamlit + SQLite

**为什么**：
- 你软件不差但时间有限，Streamlit 从零到一个能用的面板只需要1小时
- SQLite 零部署、单文件、Python 内置，存实验参数和结果绰绰有余
- 你的 Bayesian Optimization 结果可以用 Streamlit 的 plotly 图表直接展示
- 树莓派上跑 Streamlit 完全没问题

**进阶路径**：如果实验数据量上来、需要实时转速曲线监控，再加 InfluxDB + Grafana dashboard，但初期完全没必要。

---

## 整体迁移建议：最小风险路径

### Phase 0：验证（1-2天）
**操作**：在 Mega 2560 上刷 grbl-Mega-5X，不改任何接线，仅用USB串口连电脑发G-code测试三轴运动
**验证**：加减速是否平滑、归零是否可靠、脉冲频率是否够用
**Rollback**：随时刷回你的 `motor_control.ino`，EEPROM里的参数不影响你的原固件

### Phase 1：替换Layer 1 + Layer 2（3-5天）
**操作**：
1. 正式配置 grbl-Mega-5X 参数（steps/mm、max speed、accel、homing cycle）
2. 映射 Spindle/Coolant pin 到 Z刹车/夹爪继电器
3. 树莓派装 cncjs，配置串口连 Mega
4. 通过 cncjs Web UI 验证全部功能
**Rollback**：grbl-Mega-5X 可以随时刷回你自己的固件

### Phase 2：增加 Python 协调层（1-2周）
**操作**：
1. 用 pymodbus 封装夹爪Modbus RTU控制
2. 用 pyserial 封装USB继电器控制
3. 写一个 thin Python layer 调 cncjs API + 夹爪 + 继电器
4. 跑通一个完整的「取片→放置→夹紧」工作流
**Rollback**：协调层是纯上层代码，不影响底层任何东西

### Phase 3：集成实验记录 + UI（1周）
**操作**：
1. SQLite 建表存实验参数和结果
2. Streamlit 写一个面板：提交实验→执行→记录→展示
3. 接入 Bayesian Optimization 循环
**Rollback**：纯应用层，随时改

### Phase 4（可选）：迁移到 PyLabRobot 架构
**条件**：Phase 2-3 稳定运行后，如果你决定发 Digital Discovery 论文时需要一个更「学术体面」的软件架构
**操作**：把 Phase 2 的 thin layer 重构为 PyLabRobot backend
**价值**：论文里可以引用 PyLabRobot（Connor Coley 的框架），增加学术认可度

---

## 反向意见：什么情况下继续自研更合理？

### 1. USB继电器（DSTUR-T80）控制：继续自研
你的 USB 继电器用的是 0xA0 字节协议、9600波特率，这种非标协议用任何框架都得自己写驱动。直接保留你现有的 pyserial 代码即可，封装成一个 `RelayController` 类就够了。没有任何开源项目专门解决这个。

### 2. 夹爪 Modbus RTU：用 pymodbus 但逻辑自写
pymodbus 只解决通信层，你的 YJZK 力控夹爪的寄存器映射（力度/速度/位置）需要自己看手册封装。这部分没有替代品。

### 3. 旋涂模块控制：必须自研
没有任何开源项目能直接驱动你的旋涂模块。上面 Layer 4 的项目都是**自带旋涂硬件的完整方案**，你只能借鉴它们的参数定义和控制逻辑，不能直接用它们的代码。

### 4. 如果你的龙门架需要非标运动模式
grbl 的运动模型是标准 CNC（直线/圆弧插补）。如果你未来需要：
- 异步多轴（X动的时候Z也在动但速度不同）→ grbl 支持
- 连续路径（不停顿地经过多个点）→ grbl 的前瞻规划支持
- 非笛卡尔运动（如果加了旋转轴）→ 需要 grbl-Mega-5X

但如果你需要**完全自由的运动时序**（比如"X移动到50mm的同时，第3秒时触发夹爪"），grbl 的 G-code 模型做不到——这种场景你的自研固件反而更灵活。不过这种需求可以通过上位机Python层的时序编排解决，不一定要在固件层做。

### 5. 学术价值考量
如果你的论文主要贡献是「全自动旋涂系统」而非「运动控制固件」，那么**用成熟开源方案做底层 + 自研上层协调逻辑**既省时间又不影响创新性。你的创新点应该在 Bayesian Optimization 闭环、实验自动化 pipeline、旋涂参数空间探索——这些和底层用不用 grbl 无关。

---

## 关键链接汇总

| 层级 | 推荐方案 | GitHub |
|---|---|---|
| Layer 1 固件 | grbl-Mega-5X | https://github.com/fra589/grbl-Mega-5X |
| Layer 2 上位机 | cncjs | https://github.com/cncjs/cncjs |
| Layer 3 协调 | PyLabRobot | https://github.com/PyLabRobot/pylabrobot |
| Layer 3 底层 | pymodbus | https://github.com/pymodbus-dev/pymodbus |
| Layer 4 参考 | Spin-coater-v1 | https://github.com/BirdbrainEngineer/Spin-coater-v1 |
| Layer 4 参考 | Maasi | https://github.com/klotzsch-lab/Maasi |
| Layer 5 UI | Streamlit | https://github.com/streamlit/streamlit |
