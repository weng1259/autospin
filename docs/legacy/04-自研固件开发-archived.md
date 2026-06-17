# 04 - 固件开发（已归档）

> ⚠️ **本文档对应已废弃的自研固件路线**，于 2026-04-13 决策迁移到 grbl-Mega-5X 后归档。
> 保留供回滚参考。当前固件栈见 [`docs/guides/04-grbl-固件配置.md`](../guides/04-grbl-固件配置.md) 和 [ADR-001](../decision-log/ADR-001-migrate-to-grbl-stack.md)。
>
> ---
>
> Phase 2 设计文档：将 Arduino 从测试原型改造为可靠的运动控制器。
>
> 本文档先定义"Arduino 对外是什么样的"（协议、状态、安全规则），再动手写代码。

---

## 一、背景：为什么要重写

Phase 1 的固件是测试原型，完成了"证明硬件能工作"的使命。但它不能直接用于正式系统，原因：

| 现有问题 | 对应 Issue | 后果 |
|----------|-----------|------|
| 无限位保护 | #002 | 连续运动撞机，损坏导轨/传感器 |
| ALM 报警只显示不联动 | #006 | 驱动器过流/过压时系统继续运动 |
| 降速命令不生效 | #007 | 只能加速不能减速，安全风险 |
| 归零没有两阶段寻边 | #008 | 重复精度差，坐标系不可靠 |
| ENA 急停未接入 | #015 | 软件崩溃时无法停机 |
| 命令解析阻塞 100ms | #016 | 异常字节导致运动顿挫 |
| 脉冲时序被串口打断 | #001 | 闭环模式电机抖动 |

这些不是独立的 bug，而是同一个根因的不同症状：**固件没有架构**。逐个修补不如重写。

### 重写目标

Arduino 成为一个**黑盒子**——上位机（Python/网页）只需要发命令、收反馈，不需要关心内部实现：

```
上位机发命令 → [Arduino 黑盒] → 电机动 / 传感器报 / 错误停
                    │
                    ├── 内部保证：不会撞机
                    ├── 内部保证：报警时自动停
                    ├── 内部保证：归零精度 < 0.1mm
                    └── 内部保证：急停 < 1ms 响应
```

---

## 二、通信协议

### 设计原则

- 行文本协议（`\n` 结尾），串口监视器可直接调试
- 命令-应答模式：每条命令必有一个响应（`OK` 或 `ERR`）
- 异步事件以 `@` 开头，上位机可选择监听或忽略
- 固件只认**步数**，mm 换算由上位机负责（保持固件简单）

### 命令表（上位机 → Arduino）

| 命令 | 参数 | 说明 | 允许状态 |
|------|------|------|---------|
| `HOME` | 无，或 `X`/`Y`/`Z` | 归零（无参数=全轴顺序归零） | IDLE, READY |
| `MOVE` | `X__ Y__ Z__` | 绝对位置移动（步数） | READY |
| `JOG` | `X+`/`X-`/`Y+`/... `N__` | 相对点动，N=步数（默认50） | READY |
| `STOP` | 无，或 `X`/`Y`/`Z` | 停止运动（带减速） | 任意 |
| `ESTOP` | 无 | 急停：立即停脉冲 + ENA 断使能 | 任意 |
| `RESET` | 无 | 从 ERROR/ESTOP 恢复到 IDLE | ERROR, ESTOP |
| `SPEED` | `1`-`9` | 设置速度档位 | 任意 |
| `POS` | 无 | 查询当前位置 | 任意 |
| `SENS` | 无 | 查询传感器状态 | 任意 |
| `STATE` | 无 | 查询当前状态 | 任意 |

### 响应格式（Arduino → 上位机）

**同步响应**（紧跟命令之后）：

```
OK                    # 命令被接受
OK HOMING             # 命令被接受，归零开始（异步完成）
OK MOVING             # 命令被接受，运动开始（异步完成）
ERR:BUSY              # 正在执行其他操作
ERR:STATE             # 当前状态不允许此命令
ERR:PARAM             # 参数错误
ERR:LIMIT             # 目标位置超出行程
```

**异步事件**（随时可能发出，以 `@` 开头）：

```
@STATE READY          # 状态变更
@POS 1000,2000,500    # 位置上报（定时）
@SENS 000000000       # 传感器上报（定时，9位，1=触发）
@ALM 000              # 报警上报（定时，3位，1=报警）
@LIMIT X+             # 限位触发（即时）
@HOME_OK              # 归零完成
@HOME_FAIL reason     # 归零失败
@MOVE_OK              # 移动完成
@FAULT reason         # 故障（自动进入 ERROR 状态）
```

### 协议示例

```
# 上电
→ (Arduino boots)
← OK READY
← @STATE IDLE

# 归零
→ HOME
← OK HOMING
← @STATE HOMING
...（等待）
← @HOME_OK
← @STATE READY

# 移动
→ MOVE X5000 Y5000
← OK MOVING
← @STATE MOVING
...
← @MOVE_OK
← @STATE READY

# 限位触发
→ JOG X+
← OK MOVING
← @LIMIT X+
← @FAULT LIMIT_X+
← @STATE ERROR

# 恢复
→ RESET
← OK
← @STATE IDLE

# 急停
→ ESTOP
← OK
← @STATE ESTOP
```

---

## 三、状态机

### 状态定义

```
IDLE        上电初始 / 复位后。未归零，不接受 MOVE 命令。
HOMING      归零进行中。只接受 STOP 和 ESTOP。
READY       已归零，空闲等待。可接受所有运动命令。
MOVING      运动进行中。只接受 STOP、ESTOP 和查询命令。
ERROR       故障（限位触发 / ALM 报警）。只接受 RESET 和 ESTOP。
ESTOP       急停。ENA 已断开，电机失力。只接受 RESET。
```

### 状态转换图

```
                    ┌──────────┐
          ┌─RESET──│  ESTOP   │←──── ESTOP 命令（任意状态可触发）
          │         └──────────┘
          ▼
     ┌──────────┐   HOME    ┌──────────┐  完成   ┌──────────┐
     │   IDLE   │─────────→│  HOMING  │───────→│  READY   │
     └──────────┘           └──────────┘        └──────────┘
          ▲                      │                │       ▲
          │                      │失败          MOVE/JOG  │
          │                      ▼                ▼       │完成
          │               ┌──────────┐        ┌──────────┐│
          └───── RESET ───│  ERROR   │←─限位──│  MOVING  │┘
                          └──────────┘  /ALM  └──────────┘
```

### 每个状态接受的命令

| 命令 | IDLE | HOMING | READY | MOVING | ERROR | ESTOP |
|------|------|--------|-------|--------|-------|-------|
| HOME | ✓ | | ✓ | | | |
| MOVE | | | ✓ | | | |
| JOG | | | ✓ | | | |
| STOP | ✓ | ✓ | ✓ | ✓ | | |
| ESTOP | ✓ | ✓ | ✓ | ✓ | ✓ | |
| RESET | | | | | ✓ | ✓ |
| SPEED | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| POS/SENS/STATE | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

---

## 四、安全系统

安全保护在固件层实现，不依赖上位机。任何上位机（网页 / Python / 串口监视器）接入后都自动受保护。

### 4.1 限位保护（Issue #002）

```
每批脉冲（50步）之前检查：
  运动方向为正 → 读正限位传感器（D24/D27/D30）
  运动方向为负 → 读负限位传感器（D22/D25/D28）

  触发（HIGH）→ 立即停止该轴
               → 上报 @LIMIT X+（示例）
               → 进入 ERROR 状态

  原点传感器（D23/D26/D29）不阻止运动，仅用于归零。
  限位触发后，反方向运动仍然允许（可以退出来）。
```

### 4.2 ALM 报警联锁（Issue #006）

```
每次 loop 迭代检查 ALM 引脚（D31/D32/D33）：
  LOW = 驱动器报警（过流/过压/缺相等）

  任意 ALM 触发 → 停止所有轴
                → 上报 @FAULT ALM_X（示例）
                → 进入 ERROR 状态
```

### 4.3 ENA 硬件急停（Issue #015）

```
引脚分配：D34 = ENA 总控输出

接线：
  D34 → 三台驱动器 ENA+ 串联 → 急停按钮（常闭）→ 回到 +5V
  三台驱动器 ENA- → +5V（共阳极）

逻辑：
  正常：D34 = HIGH → ENA 有效 → 电机有力矩
  急停：D34 = LOW  → ENA 无效 → 电机立即失力
  物理急停按钮按下 → 电路断开 → 同样效果（不依赖软件）

ESTOP 命令执行：
  1. digitalWrite(ENA_PIN, LOW)   // 硬件级断使能
  2. 停止所有脉冲输出
  3. 进入 ESTOP 状态

RESET 恢复：
  1. 确认所有 ALM 正常
  2. digitalWrite(ENA_PIN, HIGH)  // 恢复使能
  3. 进入 IDLE 状态（需重新归零）
```

### 4.4 安全规则汇总

| # | 触发条件 | 固件动作 | 目标状态 | 恢复方式 |
|---|---------|---------|---------|---------|
| 1 | 限位传感器触发 | 停该轴，上报 @LIMIT | ERROR | RESET → IDLE |
| 2 | ALM 报警 | 停全轴，上报 @FAULT | ERROR | RESET → IDLE |
| 3 | ESTOP 命令 | ENA 断开，停全轴 | ESTOP | RESET → IDLE |
| 4 | 物理急停按钮 | 电路断开（硬件级） | ESTOP | 松开按钮 + RESET |
| 5 | 归零超时（30s） | 停该轴 | ERROR | RESET → IDLE |

### 4.5 Z 轴刹车说明

Z 轴刹车由 DSTUR-T80 USB 继电器（CH2）控制，**不经过 Arduino**。刹车联锁由上位机（Python SafetyManager）负责：

- Z 轴运动前：上位机发继电器命令释放刹车 → 等 0.3s → 再发 MOVE 给 Arduino
- Z 轴停止后：上位机收到 @MOVE_OK → 发继电器命令锁定刹车

固件不直接控制刹车，但上报 Z 轴运动状态供上位机决策。

> **已知风险**：如果上位机崩溃，刹车不会自动锁定。ENA 急停可断电机力矩，但不能锁刹车。物理急停按钮是最终安全保障。

---

## 五、归零流程（Issue #008）

### 三阶段归零

以单轴为例（三轴按 Z → X → Y 顺序依次归零，Z 先归零以确保安全高度）：

```
阶段 1 — 快速寻找
  速度：档位 5（stepDelay = 200μs）
  方向：向负方向
  终止：原点传感器触发（HOME pin = HIGH）
  超时：30 秒
  异常：碰到负限位 → 反弹退出 → 继续等原点

阶段 2 — 退回
  速度：档位 2（stepDelay = 1000μs）
  方向：向正方向
  终止：原点传感器释放（HOME pin = LOW）
  超时：10 秒

阶段 3 — 慢速精确寻边
  速度：档位 1（stepDelay = 2000μs）
  方向：向负方向
  终止：原点传感器再次触发（HOME pin = HIGH）
  超时：10 秒

  → 此位置为精确零点，position = 0
```

### 归零顺序

```
1. Z 轴归零（先归零 Z 以避免水平移动时碰撞工件）
2. X 轴归零
3. Y 轴归零
4. 全部完成 → 三轴 position = 0 → 上报 @HOME_OK → 进入 READY
```

### 失败处理

- 任意阶段超时 → 停止 → 上报 `@HOME_FAIL TIMEOUT_X` → ERROR
- ALM 报警 → 停止 → 上报 `@HOME_FAIL ALM` → ERROR

---

## 六、运动控制

### 6.1 加减速（Issue #001, #007）

对称梯形速度曲线，解决"只能加速不能减速"的问题：

```
currentDelay > targetDelay → 减小 currentDelay（加速）
currentDelay < targetDelay → 增大 currentDelay（减速）
currentDelay == targetDelay → 匀速

每批（50步）后调整一次：
  diff = targetDelay - currentDelay
  调整量 = constrain(diff, -ACCEL_STEP, +ACCEL_STEP)
  currentDelay += 调整量
```

停止时（STOP 命令）：
```
targetDelay 设为最慢速度（2000μs）
等 currentDelay 追上后再停止脉冲
→ 实现"减速停车"而非"急刹"
```

### 6.2 脉冲生成（Issue #001）

解决"串口检查打断脉冲时序"的问题：

```
原来：每步之后检查 Serial（2-4μs 抖动）
现在：每批（50步）之后才检查

loop() 结构：
  1. 非阻塞读串口（有数据就读进 buffer，没有就跳过）
  2. buffer 有完整命令（遇到 \n）→ 解析执行
  3. 对每个运动中的轴：
     a. 检查该方向的限位传感器
     b. 生成 50 步脉冲（纯脉冲，不掺杂其他操作）
     c. 更新 currentDelay（加减速）
  4. 检查 ALM 引脚
  5. 定时上报（500ms 间隔，仅在无运动时；运动中只报位置）
```

### 6.3 DIR 方向设置时序（Issue #001）

```
切换方向时：
  digitalWrite(dirPin, newDir)
  delayMicroseconds(10)        // 驱动器要求 ≥ 6μs 建立时间
  // 然后才开始发脉冲
```

### 6.4 绝对定位

```
MOVE X5000 Y3000 命令处理：

1. 计算每轴需要的步数和方向：
   stepsNeeded_X = |5000 - position_X|
   direction_X = (5000 > position_X) ? POSITIVE : NEGATIVE

2. 检查目标是否在行程范围内（可选，上位机也会检查）

3. 各轴独立运动（不做插补，依次或同时均可）

4. 到达目标步数 → 停止 → 上报 @MOVE_OK
```

---

## 七、非阻塞命令解析（Issue #016）

替换原来的 `while (!Serial.available())` 阻塞等待：

```c
// 全局
char cmdBuffer[64];
uint8_t cmdLen = 0;

// 每次 loop 调用
void readSerial() {
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (cmdLen > 0) {
                cmdBuffer[cmdLen] = '\0';
                parseCommand(cmdBuffer);
                cmdLen = 0;
            }
        } else if (cmdLen < 63) {
            cmdBuffer[cmdLen++] = c;
        } else {
            // 缓冲区溢出，丢弃
            cmdLen = 0;
        }
    }
}
```

特点：
- 永远不阻塞：没数据就跳过，有数据就读完
- 逐字符积累，遇到换行符才解析
- 不完整命令不影响运动循环
- 溢出保护（>63 字节直接丢弃）

---

## 八、引脚分配汇总

| 引脚 | 功能 | 方向 | 备注 |
|------|------|------|------|
| D2 | X_PLS（脉冲） | OUTPUT | |
| D3 | X_DIR（方向） | OUTPUT | 固件取反 |
| D4 | Y_PLS | OUTPUT | |
| D5 | Y_DIR | OUTPUT | |
| D6 | Z_PLS | OUTPUT | |
| D7 | Z_DIR | OUTPUT | 固件取反 |
| D22 | X 负限位 | INPUT_PULLUP | HIGH=触发 |
| D23 | X 原点 | INPUT_PULLUP | HIGH=触发 |
| D24 | X 正限位 | INPUT_PULLUP | HIGH=触发 |
| D25 | Y 负限位 | INPUT_PULLUP | HIGH=触发 |
| D26 | Y 原点 | INPUT_PULLUP | HIGH=触发 |
| D27 | Y 正限位 | INPUT_PULLUP | HIGH=触发 |
| D28 | Z 负限位 | INPUT_PULLUP | HIGH=触发 |
| D29 | Z 原点 | INPUT_PULLUP | HIGH=触发 |
| D30 | Z 正限位 | INPUT_PULLUP | HIGH=触发 |
| D31 | X ALM 报警 | INPUT_PULLUP | LOW=报警 |
| D32 | Y ALM 报警 | INPUT_PULLUP | LOW=报警 |
| D33 | Z ALM 报警 | INPUT_PULLUP | LOW=报警 |
| D34 | ENA 总控 | OUTPUT | HIGH=使能，LOW=断力（**待接线**） |

---

## 九、与上位机的分工

| 职责 | 固件（Arduino） | 上位机（Python） |
|------|----------------|-----------------|
| 单位 | 步数 | mm（682.67步/mm 换算，见 docs/hardware-specs.md） |
| 限位保护 | ✓ 硬件级，不可绕过 | 冗余检查 |
| ALM 报警 | ✓ 自动停机 | 显示 / 日志 |
| 急停 | ✓ ENA 断使能 | 发 ESTOP 命令 |
| 归零流程 | ✓ 内部三阶段完成 | 发 HOME 命令，等 @HOME_OK |
| 加减速 | ✓ 内部梯形曲线 | 只选速度档位 |
| Z 轴刹车 | ✗ | ✓ 通过继电器控制 |
| 多轴安全顺序 | ✗ | ✓ 先升Z→移XY→降Z |
| 坐标系管理 | 步数计数 | mm↔步数换算、工位坐标 |
| 实验流程 | ✗ | ✓ RecipeExecutor |

---

## 十、实现步骤

### 步骤 2a：最小可用固件

**目标**：状态机 + 非阻塞解析 + 限位保护 + 单轴 JOG

**实现**：
- 状态机框架（IDLE / READY / MOVING / ERROR / ESTOP）
- 非阻塞命令解析器（cmdBuffer 方式）
- 限位传感器检查（每批脉冲前）
- JOG 命令：相对点动
- STOP / ESTOP / RESET
- POS / SENS / STATE 查询

**验证**：串口监视器发 `JOG X+ N200`，电机动 200 步；手动触发限位传感器，电机停，上报 `@LIMIT`。

### 步骤 2b：归零 + 绝对定位

**目标**：三阶段归零 + MOVE 绝对定位

**实现**：
- HOME 命令：三阶段归零（快寻→退回→慢寻边）
- MOVE 命令：绝对步数定位
- 归零顺序：Z → X → Y
- 归零完成后进入 READY，才允许 MOVE

**验证**：发 `HOME`，三轴依次归零，`@HOME_OK` 后发 `MOVE X3200`（=5mm），量尺子对得上。

### 步骤 2c：加减速 + 速度控制

**目标**：对称梯形曲线，SPEED 命令即时生效

**实现**：
- 双向加减速逻辑
- STOP 减速停车
- SPEED 命令在运动中也能改速度
- DIR 切换加 10μs 建立时间

**验证**：运动中发 `SPEED 2` 能减速；发 `STOP` 能减速停车而非急刹。

### 步骤 2d：ALM + ENA 急停

**前置**：需要先完成 ALM 接线（D31-D33）和 ENA 接线（D34）

**实现**：
- ALM 检测 + 自动停机
- ENA 总控输出
- ESTOP 命令触发 ENA 断使能
- RESET 恢复流程

**验证**：模拟 ALM 信号（D31 接地），系统停机进 ERROR；发 ESTOP，ENA 断开，电机失力。

### 步骤 2e：闭环切换

**前置**：需要先完成 DB9 编码器接线，驱动器 SW8 = OFF

**实现**：
- 验证闭环模式下脉冲时序正常
- 调整速度档位映射（闭环可能支持更高速度）
- 验证归零精度

**验证**：高速运动 100 次，检查是否丢步（位置漂移）；归零 5 次，重复精度 < 0.1mm。

---

## 十一、验证记录

> 每完成一步在此记录实际测试结果。

### 2a 最小可用固件

- 日期：2026-03-24
- 结果：`firmware/motor_control/motor_control.ino` 已完成 2a 代码重构，实现新行文本协议、状态机骨架（IDLE / MOVING / ERROR / ESTOP）、非阻塞命令解析、`JOG` / `STOP` / `ESTOP` / `RESET` / `POS` / `SENS` / `STATE` / `SPEED`，并加入固件层限位保护。已通过编译并上传到 Arduino。
- 验证：Python 串口自动化测试通过（STATE/POS/SENS/SPEED/JOG/ESTOP/RESET/无效命令），X 轴 JOG 电机实际转动，位置追踪正确。
- 问题：现有 `web_control/` 页面仍是旧两字符协议，当前不能直接用于这版固件联调。

### 2b 归零 + 绝对定位

- 日期：2026-03-24
- 结果：实现 HOME（三阶段归零）和 MOVE（绝对定位）。三轴依次归零（Z→X→Y）成功，`@HOME_OK` + `STATE READY`，归零后 `POS 0,0,0`，传感器 `SENS 010010010`（三轴原点全部触发）。MOVE X1600 + MOVE X0 往返定位正确，`@MOVE_OK` 正常上报。
- 调试过程中修正的问题：
  - Z 轴 `invertDir` 应为 `false`（非 `true`），物理正方向与 X 轴相反
  - Z 轴原点传感器在中间（顶部=正限位，底部=负限位），归零自动检测方向
  - Z 轴与 X/Y 同规格 682.67 步/mm（同步带 HTD3M×25齿，非丝杆），原归零速度太慢，快寻延迟从 200μs 降至 30μs
  - Z 轴运动前必须通过 DSTUR-T80 CH2 释放刹车，否则闭环驱动器会因位置偏差报警（红灯闪 5 次）
- 待解决：运动停止时有明显振动（无减速急停），需 2c 加减速解决

### 2c 加减速

- 日期：
- 结果：
- 问题：

### 2d ALM + ENA

- 日期：
- 结果：
- 问题：

### 2e 闭环

- 日期：
- 结果：
- 问题：
