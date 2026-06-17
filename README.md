# 智能旋涂仪 — 项目文档

> **⚠️ 2026-04-13 重要变更**：项目已决定从自研固件栈迁移到 **grbl-Mega-5X + cncjs + 薄 Python orchestrator** 开源栈。**新会话开始工作前必读**：
>
> - [docs/decision-log/ADR-001-migrate-to-grbl-stack.md](docs/decision-log/ADR-001-migrate-to-grbl-stack.md) — 迁移决策全文 + 回滚条件
> - [docs/decision-log/migration-checklist.md](docs/decision-log/migration-checklist.md) — 每个 Phase 的进入条件 / 行动 / 验收 / Post-Phase cleanup
> - [docs/guides/phase0-硬件安全底座.md](docs/guides/phase0-硬件安全底座.md) — **当前 Phase 的 runbook**（新会话执行入口）
> - [docs/spin-coater-opensource-research.md](docs/spin-coater-opensource-research.md) + [docs/spin-chatgpt.md](docs/spin-chatgpt.md) — 支撑决策的开源方案调研
>
> 下文「当前进度」和「快速导航」中涉及 Phase 2c/2d/2e 自研固件、网页面板迁移等条目已被迁移取代，待 Phase 1 验收后统一清理。

## 项目概述

基于 GH40 龙门架的三轴运动平台，用于自动化旋涂工艺。

- **龙门架型号**：GH40-D75-X300S-Y300S-Z100S-57（港豪）
- **行程**：X轴 300mm / Y轴 300mm / Z轴 100mm
- **电机**：57J1880EC-1000-LS-SCG 闭环步进电机 × 3（Z轴带刹车）
- **驱动器**：杰美康 2HSS57-C × 3
- **传感器**：大深 DS-ES61 槽型光电传感器 × 9（5~24V DC）
- **控制器**：Arduino Mega 2560（CH340 国产版）
- **控制方式**：Arduino → 脉冲/方向信号 → 驱动器（GPIO 控制）
- **驱动器模式**：SW8=OFF 闭环模式（DB9 编码器已连接）
- **细分设置**：SW3=OFF SW4=OFF SW5=OFF SW6=ON → 51200 脉冲/转，682.67 步/mm（导程 75mm）

---

## 目录结构

```
智能旋涂仪/
├── README.md                              ← 你正在看的这个文件
│
├── docs/                                  ← 文档中心
│   ├── devlog/                            ← 开发日志（按日期记录）
│   ├── decisions/                         ← 关键决策记录
│   ├── issues/                            ← 问题追踪
│   ├── architecture.md                    ← 系统架构（接线速查、引脚分配）
│   └── troubleshooting.md                 ← 踩坑汇总
│
├── hardware/                              ← 硬件资料（按模块分）
│   ├── 龙门架/                            ← GH40 三轴龙门架系统
│   │   ├── GH40-D75-...STEP              ← 3D模型
│   │   ├── 产品画册.pdf                   ← 龙门架产品画册
│   │   ├── 驱动器/                        ← 2HSS57-C 步进驱动器资料
│   │   └── photos/                        ← 实物照片（电机/驱动器/传感器/电源/Arduino/安装）
│   ├── 夹爪/                              ← 电动夹爪模块
│   │   ├── 初始状态.docx / *.mp4 / *.exe
│   │   └── photos/
│   ├── 继电器/                            ← DSTUR-T80 USB继电器
│   │   ├── USB Relay - 丢石头百科.pdf
│   │   └── photos/
│   ├── 旋涂模块/                          ← （待采购）
│   ├── 移液模块/                          ← （待采购）
│   ├── 参考/                              ← 参考资料（写字机图片、测试记录等）
│   ├── tutorials/                         ← 零基础教程
│   └── 采购清单.md                        ← BOM + 接线参考
│
├── firmware/                              ← Arduino 固件
│   ├── blink_test/                        ← LED闪烁测试
│   ├── motor_test/                        ← 单轴电机测试
│   ├── motor_control/                     ← 三轴电机控制（当前使用）
│   └── gripper_test/                      ← 夹爪测试
│
├── web_control/                           ← 网页控制界面
│   ├── index.html                         ← 三轴电机控制面板
│   ├── sensor_test.html                   ← 传感器测试工具
│   ├── gripper.html                       ← 夹爪控制面板
│   └── homing_test.html                   ← 归零测试
│
└── tools/                                 ← 辅助脚本
```

### 快速导航

| 你想做什么 | 看这个文件 |
|-----------|-----------|
| **新会话开始：了解当前处于哪个 Phase** | [docs/decision-log/migration-checklist.md](docs/decision-log/migration-checklist.md) |
| **新会话开始：执行 Phase 0 硬件安全底座** | [docs/guides/phase0-硬件安全底座.md](docs/guides/phase0-硬件安全底座.md) |
| 了解为什么迁移到 grbl + cncjs | [docs/decision-log/ADR-001-migrate-to-grbl-stack.md](docs/decision-log/ADR-001-migrate-to-grbl-stack.md) |
| 硬件参数速查（步距/速度/引脚） | 顶部 MEMORY.md 自动加载 + 下文「关键接线参考」 |
| 从零组装龙门架 | [docs/guides/01-机械组装.md](docs/guides/01-机械组装.md) |
| 接电控线路（驱动器/传感器/通电/ALM） | [docs/guides/02-电控接线.md](docs/guides/02-电控接线.md)（§27 是 Phase 0 ALM 接线） |
| 夹爪测试 | [docs/guides/03-夹爪测试.md](docs/guides/03-夹爪测试.md) |
| 自研固件设计（**即将归档**，保留作回滚参考） | [docs/guides/04-固件开发.md](docs/guides/04-固件开发.md) |
| 自研固件测试（**即将归档**，保留作回滚参考） | [docs/guides/05-固件测试.md](docs/guides/05-固件测试.md) |
| 查物料清单和接线速查 | [hardware/采购清单.md](hardware/采购清单.md) |
| 查看问题追踪 | [docs/issues/README.md](docs/issues/README.md) |
| 查看开发过程和历史记录 | [docs/devlog/](docs/devlog/) |

---

## 当前进度

### 已完成

- [x] 龙门架机械组装（Y轴×2 + 连接杆 + X轴 + Z轴）
- [x] L型板、T型板安装
- [x] 电机安装到电机笼（联轴器已连接）
- [x] 零件清点确认（9个传感器 + 3个感应片 + 9个安装座）
- [x] 确认所有设备参数（驱动器手册、电机铭牌、传感器型号）
- [x] 安装光电传感器和感应片到各轴模组上
- [x] 电机相线接到驱动器（红A+ 蓝A- 绿B+ 黑B-）
- [x] 24V电源输出接到3个驱动器（并联）
- [x] 220V电源输入线接好（三芯电源线 L/N/⏚）
- [x] 驱动器拨码开关设置（SW8=OFF 闭环模式，DB9 编码器已连接）
- [x] Arduino Mega 到货，CH340驱动已安装
- [x] Arduino IDE + arduino-cli 环境就绪
- [x] Arduino 信号线接到3个驱动器（共阳极接法）
- [x] 首次通电成功，三轴电机均可转动
- [x] 网页控制界面可用（三轴独立控制，支持点动/长按/速度调节）
- [x] 传感器接线到 Arduino（D22-D30，9个传感器全部接通）
- [x] 传感器测试通过（网页传感器测试工具验证，9/9 正常）
- [x] Z轴刹车继电器接线完成（DSTUR-T80 CH2，24V→COM→NO→刹车红线，刹车黑线→GND）
- [x] 网页控制面板已集成刹车控制（连接继电器后可释放/锁定刹车）

- [x] DB9 编码器接线完成，SW8 改 OFF 切闭环模式
- [x] 固件升级 v2：位置追踪、ALM 报警监控、梯形加减速
- [x] 网页控制面板重写：Apple 简约风格、XY 位置画布、Z 竖条、传感器/报警状态
- [x] Phase 2a 固件已重构并上传：新行文本协议、状态机骨架、非阻塞解析、JOG/STOP/ESTOP/RESET、固件层限位保护
- [x] Phase 2a 串口实机验证通过：Python 自动化测试（STATE/POS/SENS/SPEED/JOG/ESTOP/RESET）
- [x] Phase 2b 固件完成：HOME 三阶段归零（Z→X→Y）+ MOVE 绝对定位，实机验证通过
- [x] Z 轴方向校正：`invertDir=false`，原点在顶部（`homeDirectionPositive=true`）
- [x] Z 轴刹车联动验证：继电器 CH2 释放刹车 → 归零/运动 → 锁定刹车

### 进行中（按 [migration-checklist.md](docs/decision-log/migration-checklist.md) 执行）

- [ ] **Phase 0**：ALM 报警接线（D31/D32/D33）+ Z 刹车软件策略定稿 ← **当前这里**
- [ ] **Phase 1**：grbl-Mega-5X spike（决策点，2-3 天）
- [ ] **Phase 2**：cncjs 接管网页面板
- [ ] **Phase 3**：Python orchestrator MVP（抄 pylabrobot 接口形状 + PASCAL 目录结构）

### 已废弃（被迁移决策取代）

下列条目是旧计划的一部分，迁移后不再执行，保留作为历史记录：

- ~~Phase 2c：加减速曲线~~ → grbl 内置梯形加减速 + 前瞻
- ~~Phase 2d：ALM + ENA 硬件急停~~ → ALM 物理接线属于 Phase 0；ENA 急停留在 issue #006 等 Phase 1
- ~~Phase 2e：闭环模式验证~~ → grbl 直接跑闭环驱动器
- ~~网页控制面板迁移到新行文本协议~~ → cncjs 接管，旧面板待 Phase 2 归档

### 待开始（迁移后阶段）

- [ ] **Phase 4**：旋涂模块 RS485 接入（参考 PASCAL `frgpascal/hardware/spincoater.py`）
- [ ] **Phase 5**：移液模块 RS485 接入 + 树莓派搬家
- [ ] **Phase 5+**：Python 实验流程编排 + Streamlit/SQLite 记录面板

---

## 采购状态

所有物料已采购到货，完整清单见 [hardware/采购清单.md](hardware/采购清单.md)。

---

## 关键接线参考（速查）

### 电机 → 驱动器

```
电机相线（粗线4根）→ 驱动器功率端口：
  红 → A+
  蓝 → A-
  绿 → B+
  黑 → B-

编码器线（DB9散线6根）→ 驱动器编码器端口（已接通）：
  黑 → GND
  红 → VCC(+5V)
  黄 → PA+
  绿 → PA-
  蓝 → PB+
  白 → PB-

Z轴刹车线（2根）→ DSTUR-T80 继电器 CH2（已接通）：
  24V +V → CH2 COM，CH2 NO → 刹车红线，刹车黑线 → 24V -V
```

### 电源 → 驱动器

```
24V电源(+V) → 3个驱动器功率端口 (+)（并联）
24V电源(-V) → 3个驱动器功率端口 (-)（并联）

220V输入（电源右侧端子）：
  L(火线)  ← 棕色线
  N(零线)  ← 蓝色线
  ⏚(地线)  ← 黄绿色线
```

### Arduino → 驱动器（共阳极接法）

```
5V正极线（驱动器之间跳接）：
  Arduino 5V引脚1 → X轴 PLS+ → 跳接 → Y轴 PLS+ → 跳接 → Z轴 PLS+
  Arduino 5V引脚2 → X轴 DIR+ → 跳接 → Y轴 DIR+ → 跳接 → Z轴 DIR+

信号线：
  Arduino D2 → X轴驱动器 PLS-
  Arduino D3 → X轴驱动器 DIR-
  Arduino D4 → Y轴驱动器 PLS-
  Arduino D5 → Y轴驱动器 DIR-
  Arduino D6 → Z轴驱动器 PLS-
  Arduino D7 → Z轴驱动器 DIR-
  ENA+/ENA- → 悬空不接
```

### 光电传感器 → Arduino（已接通，已测试）

```
传感器棕色线 → Arduino 5V（9个并联）
传感器蓝色线 → Arduino GND（9个并联）
传感器白色线 → Arduino 数字引脚（每个传感器占1个引脚）

X轴：D22(负限位) D23(原点) D24(正限位)
Y轴：D25(负限位) D26(原点) D27(正限位)
Z轴：D28(负限位) D29(原点) D30(正限位)
```

### Z轴刹车 → DSTUR-T80 USB继电器 CH2（已接通）

```
24V电源 +V     → 继电器 CH2 COM（中间端子）
继电器 CH2 NO  → Z轴电机刹车红线
Z轴电机刹车黑线 → 24V电源 -V

控制协议（9600 baud）：
  释放刹车（CH2 ON）：0xA0 0x02 0x01 0xA3
  锁定刹车（CH2 OFF）：0xA0 0x02 0x00 0xA2

原理：CH2 ON → COM-NO 导通 → 24V 通过刹车线圈 → 刹车释放
      CH2 OFF / 断电 → COM-NO 断开 → 刹车断电锁死（安全）
```

---

## 驱动器拨码开关设置

```
当前设置（3个驱动器相同）：
SW1 = OFF  （功率角模式）
SW2 = OFF  （方向默认 CCW）
SW3 = OFF  （细分设置）
SW4 = OFF  （细分设置）→ SW3-SW6 组合 = 51200 脉冲/转，682.67 步/mm（导程 75mm）
SW5 = OFF  （细分设置）
SW6 = ON   （细分设置）
SW7 = OFF  （指令平滑关闭）
SW8 = OFF  （闭环模式，DB9 编码器已连接）
```

---

## 固件说明

### motor_control（当前使用）

位置：`firmware/motor_control/motor_control.ino`

串口命令协议（115200 波特率，行文本协议，Phase 2b）：

```
命令格式：命令 + 参数（\n 结尾）

归零：
  HOME           → 全轴三阶段归零（Z→X→Y）
  HOME X/Y/Z     → 单轴归零

绝对定位（需先 HOME）：
  MOVE X5000 Y3000 Z2000  → 多轴绝对定位（步数，任意组合）

手动点动：
  JOG X+ N200    → X轴正方向走200步（N可省略，默认50步）
  JOG Z- N100    → Z轴负方向走100步

停止：
  STOP           → 全轴停止（归零中则取消归零）
  STOP X         → 单轴停止
  ESTOP          → 急停（需 RESET 恢复）

速度（1最慢，9最快）：
  SPEED 3        → 设置速度档位

查询：
  STATE          → 查询状态（IDLE/READY/HOMING/MOVING/ERROR/ESTOP）
  POS            → 查询位置
  SENS           → 查询传感器

恢复：
  RESET          → 从 ERROR/ESTOP 恢复到 IDLE

异步事件（Arduino 主动上报，@ 开头）：
  @STATE READY   → 状态变更
  @POS x,y,z     → 位置上报（定时）
  @SENS 010010010 → 传感器上报（9位，1=触发）
  @ALM 000       → 报警上报（3位，1=报警）
  @HOME_OK       → 归零完成
  @HOME_FAIL reason → 归零失败
  @MOVE_OK       → 定位完成
  @LIMIT X+      → 限位触发
  @FAULT reason  → 故障

注：X轴方向取反（invertDir=true），Z轴不取反。Z轴原点在顶部。
```

### 网页控制面板

位置：`web_control/index.html`

启动方式：
```bash
cd web_control && python3 -m http.server 8080
# 浏览器打开 http://localhost:8080
# 点击"连接 Arduino"选择串口
```

功能（v2 重写，Apple 简约风格）：
- 三轴独立控制（点动、长按持续、速度、步距）
- XY 位置画布（300×300mm 网格，蓝色位置点实时跟踪）
- Z 轴高度竖条（0~100mm）
- 位置读数（mm + 步数）
- 9 路传感器状态指示灯
- 3 轴驱动器报警监控
- Z 轴刹车控制（DSTUR-T80 继电器 CH2）
- 键盘快捷键：A/D=X轴、←/→=Y轴、W/S=Z轴、空格=全部急停

**注意：上传新固件前必须先关闭网页，否则串口占用会导致上传失败（需重启电脑恢复）**

---

## 控制架构

> 详细架构图和引脚分配见 [docs/architecture.md](docs/architecture.md)

```
电脑 (Mac)
  ├── USB串口① → Arduino Mega 2560
  │                 ├── D2-D7 脉冲/方向 → 驱动器×3 → 步进电机×3（闭环）
  │                 ├── D22-D30 数字输入 ← 光电传感器×9（已通）
  │                 └── D31-D33 数字输入 ← 驱动器 ALM 报警（待接线）
  │
  ├── USB串口② → DSTUR-T80 USB继电器（8路）
  │                 ├── CH1 → 夹爪（24V通断）
  │                 ├── CH2 → Z轴刹车（24V通断，已接通）
  │                 └── CH3~CH8 → 备用
  │
  └── USB转RS485 → RS485 总线（待接入）
                     ├── 旋涂模块
                     └── 移液模块
```

---

## 下一步行动

1. **Phase 2c 加减速** → 解决运动停止时的振动问题（梯形速度曲线）
2. **ALM 报警接线** → 3 根线（D31/D32/D33 ← 驱动器 ALM+），固件已支持
3. **Phase 2d ALM + ENA** → 报警联锁停机 + 硬件急停
4. **网页面板迁移** → 适配新行文本协议
5. **部署树莓派** → 上位机远程控制
6. **接入旋涂模块 + 移液模块** → 上位机 USB转RS485 直连，完成整机联调
