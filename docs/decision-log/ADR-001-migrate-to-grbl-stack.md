# ADR-001：从自研固件栈迁移到 grbl-Mega-5X + cncjs + 薄 Python orchestrator

- **日期**：2026-04-13
- **状态**：已决定，待 Phase 1 spike 验证
- **决策人**：Kevin（+ 组员同步）
- **影响范围**：固件层、上位机控制面板、Python 协调层、docs/guides、docs/issues

---

## 背景

截至 2026-03-26，项目自研固件 `firmware/motor_control/motor_control.ino`（~1000 行）已完成 Phase 2b（三阶段归零 + 绝对定位），但仍有大量开放 issue：

- #001 闭环脉冲时序不稳
- #002 限位保护
- #007 运动中降速不生效
- #008 归零流程简陋
- #016 命令解析阻塞
- #018 未回零坐标不安全
- #023 串口协议过简
- #006 硬件级急停联锁未做

继续按原计划写 Phase 2c（加减速）、2d（ALM+ENA 急停）、2e（闭环验证）意味着在 AVR C 里重新实现 GRBL 二十年前就解决的问题。

同时项目需要搭建多模块协调层（运动 + 夹爪 + 旋涂 + 移液 + 实验记录），这部分在 issue #017 里规划但未动工。

## 调研过程

2026-04-13 做了两份独立的开源方案调研（分别用 Claude 和 ChatGPT 联网搜索），产出：
- `docs/spin-coater-opensource-research.md`（Claude）
- `docs/spin-chatgpt.md`（ChatGPT）

两份报告核心结论高度一致。进一步克隆了 4 个关键项目读源码验证：
- `fra589/grbl-Mega-5X`（最近提交 2024-10-20，维护偏慢但功能完整）
- `cncjs/cncjs`（最近提交 2026-03-30 v1.11.0，有完整 REST API，不只 Socket.IO）
- `PyLabRobot/pylabrobot`（最近提交 2026-04-10，`arms/backend.py` 的 `SCARABackend` 抽象和我们龙门架+夹爪需求完全匹配）
- `fenning-research-group/PASCAL`（最近提交 2026-02-24，`frgpascal/` 是真正的生产级参考，目录结构值得全盘借鉴）

关键发现：
- **PASCAL 的龙门架固件用的是 Marlin**（STM32），Python 上位机直接 `pyserial` 发 G-code，没走 cncjs
- **PyLabRobot 的 `SCARABackend`** 提供了 `move_to / home / halt / open_gripper / close_gripper / pick_up_resource / drop_resource / approach` 全部原语，以及 `VerticalAccess / HorizontalAccess` 数据类
- **cncjs 有 `/api/gcode`、`/api/commands`、`/api/state` 等 REST 接口**，集成比两份报告说的简单
- **grbl-Mega-5X 在 AVR 上脉冲频率上限 ~30 kHz**，对应我们 682.67 步/mm 最大速度约 44 mm/s（2640 mm/min），够用但无富余

## 决策

### 采用的栈

| 层 | 方案 | 角色 |
|---|---|---|
| L1 固件 | **grbl-Mega-5X** | Mega 2560 运动控制内核 |
| L2 上位机 | **cncjs** | G-code sender + Web UI + REST API |
| L3 协调层 | **自研 Python orchestrator**，接口形状抄 PyLabRobot `SCARABackend`，目录结构抄 PASCAL `frgpascal/` | 统一调度运动、夹爪、继电器、旋涂、实验记录 |
| L4 旋涂 | 等模块到货后，参考 PASCAL `frgpascal/hardware/spincoater.py` | 旋涂 recipe 执行 |
| L5 UI | **Streamlit + SQLite** | 实验面板 + 数据记录 |

### 明确排除的方案

- **FluidNC / Smoothieware / g2core / LinuxCNC**：硬件不匹配 Mega 2560
- **直接 `pip install pylabrobot` 全量依赖**：主体模块围绕液体处理，拖入过多无关依赖
- **ROS 2 / MoveIt 2**：杀鸡用牛刀，单机场景学习成本不值
- **ChemOS**：6 年未更新
- **Klipper**：MCU+host 架构理论上最优，但面向 3D 打印生态，CNC 用法非主流，学习曲线陡

### Layer 3 的「第三条路」

两份报告在 Layer 3 分歧明显：Claude 推 PyLabRobot 全吃，ChatGPT 推自研薄层。本决策选第三条路：

- **代码完全自研**（无 pylabrobot 运行时依赖）
- **接口签名复制 `pylabrobot/arms/backend.py` 的 `SCARABackend`**（方法名、参数类型一致）
- **目录结构复制 PASCAL 的 `frgpascal/hardware/` 分层**（每个设备一个模块 + 一个 `hardwareconstants.yaml`）
- **未来升级路径**：如果论文投稿需要，`GantryBackend` 可一行改成继承 `pylabrobot.arms.backend.SCARABackend`，平滑接入 PyLabRobot 生态

## 已接受的代价与风险

1. **grbl-Mega-5X 维护偏慢**（2024-10 最后一次提交）：遇 bug 可能需自行 fork 修复
2. **AVR 脉冲频率上限 ~30 kHz**：最大速度 44 mm/s，对旋涂仪够但无富余；将来可通过驱动器拨码降细分到 10000 步/转把速度翻 5 倍
3. **grbl 只有 2 路 aux 输出**（Spindle + Coolant）：Z 刹车 + 夹爪正好占满；USB 继电器（DSTUR-T80）继续由上位机 Python 直管，不走 grbl
4. **Z 刹车与 grbl 无原生联动**：需在上位机 Python 层做「MOVE Z 前释放 CH2、完成后抱回」的时序，或自己改 grbl stepper.c
5. **夹爪 Modbus RTU 和 cncjs 完全解耦**：cncjs 只管 Arduino 那一条串口，夹爪 RS485 必须 Python 直管
6. **丢掉自研行文本协议**：换 G-code，网页面板和任何已有客户端都要改

## 回滚条件

如果 **Phase 1 spike 在 2-3 天内无法跑通以下全部验收项**，回滚到自研固件继续 Phase 2c：

- `$H` 三轴归零稳定可靠
- `G0` 点动无异响、无丢步
- 软限位 / 硬限位触发正常
- Feedhold `!` / Resume `~` / Soft-reset `Ctrl-X` 工作
- 脉冲频率满足 44 mm/s 以上（或降细分后仍满足使用需求）

回滚成本：刷回 `motor_control/motor_control.ino`，保留 `grbl_spike/` 目录供后续尝试，两份报告 + 本 ADR 归档为未完成方案。

## 迁移计划（最小风险路径）

| Phase | 内容 | 预计工时 | 可回滚 |
|---|---|---|---|
| **0** | ALM 硬件接线（D31-33）+ Z 刹车上电策略文档化 | 半天 | 不涉及软件 |
| **1** | grbl-Mega-5X spike：改 `cpu_map.h` → 刷写 → `screen` 手发 G-code 验收 | 2-3 天 | ✅ 刷回旧固件 |
| **2** | cncjs 装在 Mac 上（暂不上 Pi），替代 `index.html` 控制面板 | 1 天 | ✅ 保留旧面板 |
| **3** | 最小 Python orchestrator：`hardware/{backend,gantry,gripper,relay}.py` + `maestro.py` + `runlog.py`（SQLite） | 2-3 天 | ✅ 旁路服务 |
| **4** | 旋涂模块到货后接入 `SpincoaterBackend` + recipe 格式 | TBD | ✅ |
| **5** | 搬家到树莓派（触发条件：需独立运行 / 写 BO 闭环） | 1 天 | ✅ |

## 对现有文档/issue 的连锁影响

### 将被迁移解决的 issue（待 Phase 1 验收后统一标注 resolved-by-migration）

- #001 闭环脉冲时序 → grbl 硬件定时器
- #002 限位保护 → grbl hard/soft limits
- #007 降速不生效 → grbl realtime override
- #008 归零流程 → grbl 两阶段 homing
- #016 命令解析阻塞 → grbl 非阻塞 parser
- #018 未回零坐标 → grbl alarm 状态
- #023 串口协议简陋 → G-code
- #009 网页面板统一 → cncjs 接管
- #020 网页断开按钮 → cncjs 接管

### 仍然有效的 issue

- #003 Z 刹车联动（责任上移到 Python orchestrator）
- #006 硬件级 ALM/ENA 急停（硬件接线）
- #010 / #011 夹爪相关（与 grbl 无关）
- #021 Git 版本管理（越来越紧迫，建议 Phase 1 前先做）
- #022 24V 接线可靠性（硬件）
- #024 实验记录持久化（迁移后在 runlog.py 实现）

### 将归档的文档

- `docs/guides/04-固件开发.md` → Phase 1 验收后归档到 `docs/legacy/`
- `docs/guides/05-固件测试.md` → 同上
- `docs/issues/017-multi-module-architecture.md` → Phase 3 完成后由新架构文档取代

### 将新建的文档

- `docs/guides/04-grbl-固件配置.md`（Phase 1 验收后）
- `docs/guides/06-cncjs-部署.md`（Phase 2 后）
- `docs/guides/07-orchestrator-架构.md`（Phase 3 后）

## 参考资料

- `docs/spin-coater-opensource-research.md`（Claude 调研报告）
- `docs/spin-chatgpt.md`（ChatGPT 调研报告）
- `docs/开源方案调研-prompt.md`（调研提示词）
- https://github.com/fra589/grbl-Mega-5X
- https://github.com/cncjs/cncjs
- https://github.com/PyLabRobot/pylabrobot（参考 `pylabrobot/arms/backend.py`）
- https://github.com/fenning-research-group/PASCAL（参考 `frgpascal/hardware/` 结构）
