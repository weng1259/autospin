# 05 - 固件测试（已归档）

> ⚠️ **本文档对应已废弃的自研固件路线**，于 2026-04-13 决策迁移到 grbl-Mega-5X 后归档。
> 保留供回滚参考。当前固件测试 / 验收记录见 [`docs/guides/04-grbl-固件配置.md`](../guides/04-grbl-固件配置.md) §7 和 [ADR-001](../decision-log/ADR-001-migrate-to-grbl-stack.md)。
>
> ---
>
> 适用于 `firmware/motor_control/motor_control.ino` 的 Phase 2a/2b 版本。
>
> 覆盖串口协议、状态机、限位保护（2a）以及归零和绝对定位（2b）。

---

## 一、当前测试范围

本轮固件已经实现：

- 新行文本协议（每条命令以 `\n` 结束）
- 状态机骨架：`IDLE` / `MOVING` / `ERROR` / `ESTOP`
- `IDLE` 状态下允许 `JOG`
- 非阻塞串口解析
- 固件层限位保护
- 查询命令：`POS` / `SENS` / `STATE`
- 控制命令：`SPEED` / `JOG` / `STOP` / `ESTOP` / `RESET`

本轮固件尚未实现：

- `HOME`
- `MOVE`
- `READY` 状态的完整流程
- 固件内三阶段回零
- ALM 联锁停机
- ENA 硬件急停

## 二、先知道的限制

- 当前网页面板仍是旧两字符协议，**不能直接用于这版固件测试**。
- 本轮请先用串口工具测试，不要先开 `web_control/` 页面。
- 未回零时，位置值只是**相对步数**，不能当作绝对毫米坐标。

---

## 三、安全准备

开始测试前，先确认：

- 龙门架行程内没有工具、样品、杂物
- 手不要伸入运动范围
- Z 轴测试前，刹车已经释放
- 电机速度先设低档，推荐 `SPEED 2`
- 人站在电源或可快速断开的地方
- 第一次测试只做 `N50` / `N100` 这种小步数点动

建议测试顺序：

1. 先测 X 轴
2. 再测 Y 轴
3. 最后测 Z 轴

---

## 四、编译与上传

编译：

```bash
arduino-cli compile --fqbn arduino:avr:mega --build-path /tmp/arduino-motor-control-build firmware/motor_control
```

上传前先查端口：

```bash
arduino-cli board list
```

上传示例：

```bash
arduino-cli upload -p /dev/cu.wchusbserialXXXX --fqbn arduino:avr:mega firmware/motor_control
```

串口参数：

- 波特率：`115200`
- 行尾：`Newline`

---

## 五、上电后的正常输出

固件启动后，应先看到：

```text
OK READY
@STATE IDLE
@POS 0,0,0
@SENS 000000000
@ALM 000
```

说明：

- `OK READY` 表示串口协议就绪
- `@STATE IDLE` 表示系统尚未回零，只能手动 `JOG`
- `@SENS` 中 `1` 表示对应传感器触发
- `@ALM` 中 `1` 表示该轴驱动器报警

---

## 六、命令速查

### 查询命令

```text
STATE
POS
SENS
```

预期返回示例：

```text
OK STATE IDLE
OK POS 0,0,0
OK SENS 000000000
```

### 控制命令

```text
SPEED 2
JOG X+ N50
JOG X-
STOP
STOP X
ESTOP
RESET
```

说明：

- `JOG X-` 不带 `N` 时，默认走 `50` 步
- `SPEED` 只接受 `1` 到 `9`
- `ESTOP` 后只能 `RESET`

---

## 七、推荐测试流程

### 7.1 冒烟测试

依次发送：

```text
STATE
POS
SENS
SPEED 2
```

预期：

- 每条命令都有 `OK ...` 响应
- 状态应为 `IDLE`
- 不应出现卡死、长时间无响应

### 7.2 单轴小步点动

先测 X 轴：

```text
JOG X+ N50
STATE
POS
JOG X- N50
POS
```

预期：

- `JOG` 返回 `OK MOVING`
- 随后出现 `@STATE MOVING`
- 走完后回到 `@STATE IDLE`
- `POS` 数值会加减变化

然后对 Y / Z 轴重复同样流程：

```text
JOG Y+ N50
JOG Y- N50
JOG Z+ N50
JOG Z- N50
```

### 7.3 速度档位测试

发送：

```text
SPEED 1
JOG X+ N100
SPEED 5
JOG X- N100
```

预期：

- `SPEED 1` 明显更慢
- `SPEED 5` 明显更快
- 返回分别为 `OK SPEED 1` 和 `OK SPEED 5`

### 7.4 限位保护测试

目标：确认固件层限位保护生效。

做法一：先把轴移到靠近某侧端点，再继续朝该方向 `JOG`

```text
JOG X+ N50
```

如果此时正限位已触发，预期返回：

```text
ERR:LIMIT
@LIMIT X+
@FAULT LIMIT_X+
@STATE ERROR
```

做法二：人工遮挡对应限位传感器，再朝该方向发 `JOG`。

注意：

- 触发限位后不能继续 `JOG`
- 需要先 `RESET`
- 然后朝反方向 `JOG` 退出来

恢复流程：

```text
RESET
STATE
JOG X- N50
```

预期：

```text
OK
@STATE IDLE
```

### 7.5 急停测试

发送一个较大的点动：

```text
SPEED 3
JOG Y+ N1000
```

运动过程中发送：

```text
ESTOP
```

预期：

```text
OK
@STATE ESTOP
```

然后确认：

- 再发 `JOG` 会返回 `ERR:STATE`
- 只有 `RESET` 可以恢复

恢复流程：

```text
RESET
STATE
```

### 7.6 非法命令测试

依次发送：

```text
JOG
JOG X*
JOG X+ NABC
SPEED 0
HELLO
MOVE X100
HOME
```

预期：

```text
ERR:PARAM
ERR:PARAM
ERR:PARAM
ERR:PARAM
ERR:CMD
ERR:TODO
ERR:TODO
```

重点看两件事：

- 固件不会卡住
- 发错命令后，后续正常命令仍可继续执行

---

## 八、状态机验收标准

### IDLE

- 允许：`JOG` / `SPEED` / `POS` / `SENS` / `STATE` / `STOP`
- 不允许：`MOVE` / `HOME`

### MOVING

- 单条 `JOG` 执行期间进入 `MOVING`
- 走完后自动回到 `IDLE`
- 再发新的 `JOG` 时，若上一条还没完成，应返回 `ERR:BUSY`

### ERROR

- 限位触发后进入 `ERROR`
- `JOG` 应返回 `ERR:STATE`
- `RESET` 后恢复 `IDLE`

### ESTOP

- `ESTOP` 后进入 `ESTOP`
- 除 `RESET` / 查询命令外，不应允许继续运动

---

## 九、记录模板

每次测完建议记录：

| 日期 | 固件版本 | 测试项 | 命令 | 实际现象 | 是否通过 | 备注 |
|------|----------|--------|------|----------|----------|------|
|      | 2a | X 轴点动 | `JOG X+ N50` | | | |
|      | 2a | X 正限位保护 | `JOG X+ N50` | | | |
|      | 2a | ESTOP | `ESTOP` | | | |

---

## 十、通过标准

进入 2b 之前，至少要满足：

- `JOG` 三轴都能稳定工作
- 限位触发能稳定进入 `ERROR`
- `RESET` 后能恢复手动点动
- `ESTOP` / `RESET` 流程正常
- 错误命令不会让串口解析卡住

如果这 5 条还没稳定，就不要进入 `HOME` 和 `MOVE` 的开发。

---

## 十一、Phase 2b 测试 — 归零与绝对定位

> 前置条件：2a 全部测试通过。Z 轴测试前必须先释放刹车（DSTUR-T80 CH2 ON）。

### 11.1 Z 轴刹车验证

测试前先释放刹车：

```python
import serial, time
relay = serial.Serial('/dev/cu.usbmodem6670E00119391', 9600, timeout=1)
time.sleep(0.5)
relay.write(bytes([0xA0, 0x02, 0x01, 0xA3]))  # CH2 ON = brake release
```

确认：继电器咔一声，Z 轴可用手自由推动。

### 11.2 HOME 命令测试

#### 全轴归零

```text
HOME
```

预期：

```text
OK HOMING
@STATE HOMING
...（Z轴向上移动，找到原点后退回再慢寻）
...（X轴向负方向移动，找到原点）
...（Y轴向负方向移动，找到原点）
@HOME_OK
@STATE READY
```

归零后验证：

```text
STATE  → OK STATE READY
POS    → OK POS 0,0,0
SENS   → OK SENS 010010010（三轴原点全部触发）
```

#### 单轴归零

```text
HOME Z
```

预期：只有 Z 轴执行三阶段归零，完成后 `@HOME_OK Z`。

#### 归零中取消

```text
HOME
```

运动过程中发送：

```text
STOP
```

预期：

```text
OK
@HOME_FAIL STOPPED
@STATE ERROR
```

恢复：`RESET` → `@STATE IDLE`

### 11.3 MOVE 绝对定位测试

前置：必须先 `HOME` 完成（状态为 READY）。

#### 未归零时 MOVE 应被拒绝

```text
MOVE X100
```

预期：`ERR:STATE`（IDLE 状态不允许 MOVE）

#### 单轴移动

```text
MOVE X1600
```

预期：

```text
OK MOVING
@STATE MOVING
@POS 200,0,0
...
@MOVE_OK
@STATE READY
```

验证：`POS` 返回 `OK POS 1600,0,0`

#### 多轴移动

```text
MOVE X1600 Y1600
```

预期：两轴同时运动，`@MOVE_OK` 在全部到达后上报。

#### 返回原点

```text
MOVE X0 Y0
```

预期：`@MOVE_OK` 后 `POS 0,0,0`。

#### 已到位

```text
MOVE X0
```

预期：立即返回 `OK MOVING` + `@MOVE_OK`（无实际运动）。

### 11.4 归零方向说明

| 轴 | invertDir | homeDirectionPositive | 归零方向 | 原点位置 |
|----|-----------|----------------------|---------|---------|
| X | true | false | 向负方向（左） | 左端 |
| Y | false | false | 向负方向（前） | 前端 |
| Z | false | true | 向正方向（上） | 顶部 |

### 11.5 归零速度参数

| 阶段 | 延迟 | 频率 | 用途 |
|------|------|------|------|
| 快寻 | 30μs | ~16.7kHz | 快速找到原点传感器 |
| 退回 | 100μs | ~5kHz | 退出传感器范围 |
| 慢寻 | 300μs | ~1.67kHz | 精确定位传感器边缘 |

超时：快寻 60s，退回/慢寻 30s。

### 11.6 2b 通过标准

进入 2c 之前，至少要满足：

- `HOME` 三轴依次归零成功，`@HOME_OK` + `STATE READY`
- 归零后 `POS 0,0,0`，传感器 `SENS 010010010`
- `MOVE` 能精确到达目标位置并返回 `@MOVE_OK`
- `STOP` 能取消归零，`ESTOP` 能中断任何操作
- Z 轴刹车正确联动（运动前释放，运动后锁定）

### 11.7 已知问题

- 运动停止时有明显振动（无减速急停），将在 2c 加减速阶段解决
- Z 轴与 X/Y 同规格：682.67 步/mm（同步带 HTD3M×25齿，导程 75mm/转），2026-03-26 实测确认
