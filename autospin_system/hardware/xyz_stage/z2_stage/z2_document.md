# Z2（二级Z轴）控制调试记录

## 一、系统结构

当前系统采用：

- Arduino Mega 2560
- grbl-Mega-5X
- DM320 步进驱动器
- 28 滑台（T6×2 丝杆）
- 2相4线步进电机

新增二级Z轴作为：

- 上位机逻辑名称：Z2
- GRBL底层轴名称：A轴

---

# 二、GRBL 四轴扩展

## config.h

修改：

```c
#define N_AXIS 4
#define N_AXIS_LINEAR 4
#define AXIS_4_NAME 'A'
```

---

## cpu_map.h

A轴引脚定义：

| 功能 | Arduino Mega |
|---|---|
| STEP | D40 |
| DIR | D41 |
| MIN LIMIT | D42 |
| HOME/MAX LIMIT | D43 |

当前未接限位。

---

# 三、DM320 接线

| DM320 | Arduino Mega |
|---|---|
| PUL | D40 |
| DIR | D41 |
| VCC | 5V |
| DC- | GND共地 |

电机：

| 电机线 | 驱动器 |
|---|---|
| 黑 | A+ |
| 绿 | A- |
| 红 | B+ |
| 蓝 | B- |

---

# 四、当前参数

## 运动参数

```text
$103=356
$113=500
$123=100
```

## 行程参数

实际总行程：

```text
135 mm
```

两侧预留：

```text
5 mm
```

建议安全行程：

```text
125 mm
```

对应：

```text
$133=125
```

---

# 五、当前关闭功能

由于尚未安装限位：

```text
$20=0
$21=0
$22=0
```

即：

- 关闭软限位
- 关闭硬限位
- 关闭自动归零

---

# 六、当前位置逻辑

当前采用：

```text
A值增大 = 向下运动
```

当前位置逻辑：

| 功能 | A值 |
|---|---|
| 顶部安全位 | A0 |
| Tip安装位 | A80 |
| 吸液位 | A60 |
| 吐液位 | A70 |

---

# 七、启动流程（无HOME限位）

每次启动：

## 1

手动移动到顶部安全位。

## 2

发送：

```gcode
G92 A0
```

建立坐标系。

---

# 八、测试成功内容

已验证：

- GRBL 4轴运行正常
- A轴G-code解析正常
- D40/D41 STEP/DIR输出正常
- DM320驱动正常
- 28滑台正常运动
- G0 Axx 指令正常执行

---

# 九、后续计划

## 1

增加顶部HOME微动限位。

## 2

开启：

```text
$22=1
```

实现自动归零。

## 3

开启软限位：

```text
$20=1
```

## 4

在 AutoSpinmotorSystem 中增加：

```python
z2_mm
```

支持。

## 5

实现：

- Tip安装
- 吸液
- 吐液
- 安全高度移动

完整逻辑。

---

# 十、已并入 AutoSpinmotorSystem 的正式控制方式

Z2 已经从临时单独测试模块并入正式 grbl 导轨控制链。

正式运行时不要再让 `Z2Stage` 单独打开 Arduino Mega 串口。Z2 与 XYZ 共用同一块 Arduino Mega / 同一个 grbl 串口，正式控制入口在：

```text
AutoSpinmotorSystem.hardware.xyz_stage.xyz_stage.XYZStage
```

底层实现位置：

```text
hardware/xyz_stage/l3_backend/hardware/gantry_backend.py
hardware/xyz_stage/xyz_stage.py
maestro.py
```

保留 `hardware/xyz_stage/z2_stage/Z2Stage` 只作为低层调试工具，用于烧录后单独验证 A 轴，不作为系统正式运行路径。

---

# 十一、正式 API

Z2 在项目 API 中使用 `Z2` / `z2_mm` 命名，底层发送给 grbl 的轴名仍然是 `A`。

可用方法：

```python
maestro.gantry.initialize_z2_at_top()
maestro.gantry.move_z2_to(z2_mm)
maestro.gantry.move_z2_rel(dz2_mm)
maestro.gantry.park_z2()
```

典型固定位置：

```python
maestro.gantry.move_z2_to(60)  # 吸液位
maestro.gantry.move_z2_to(70)  # 吐液位
maestro.gantry.move_z2_to(80)  # Tip 安装位
maestro.gantry.park_z2()       # 回到 A0 顶部安全位
```

`get_position()` 现在返回：

```python
{
    "X": ...,
    "Y": ...,
    "Z": ...,
    "Z2": ...
}
```

原有 `move_to(x, y, z)` 和 `move_rel(dx, dy, dz)` 保持不变。

---

# 十二、启动和停车策略

当前 Z2 没有 HOME 限位传感器，因此系统不能自动探测真实零点。

当前策略：

1. 系统启动并连接 XYZ/grbl 后，自动执行 `initialize_z2_at_top()`。
2. 该方法会发送：

```gcode
$X
$20=0
$21=0
$22=0
G92 A0
```

3. 系统假设启动时 Z2 已经在顶部安全位。
4. `Maestro.stop_experiment()` 会优先尝试执行 `park_z2()`，让 Z2 回到 `A0`。
5. 下次启动时继续把当前位置声明为 `A0`。

重要前提：

- 每次实验结束必须成功回到 `A0`。
- 中途断电、手动移动滑台、疑似丢步后，必须人工把 Z2 移回顶部安全位，再调用 `initialize_z2_at_top()`。
- 在没有限位前，不执行 `$HA`。
- 不把 A 轴加入全局 `$H`。

---

# 十三、安全约束

当前软件限制：

```text
0 <= Z2 <= 125 mm
```

禁止：

- `move_z2_to(-1)`
- `move_z2_to(126)`
- 在当前位置不确定时直接移动 Z2
- 正式系统运行时同时启动 `Z2Stage` 调试脚本占用同一个 COM 口

如果目标超出范围，正式后端会在发送 G-code 前拒绝，不会下发 `G0 A...`。

---

# 十四、实机测试建议

系统级测试顺序：

```python
from maestro import Maestro

maestro = Maestro(use_gantry=True, mock=False)

maestro.gantry.move_z2_to(5)
maestro.gantry.move_z2_to(0)

maestro.gantry.move_z2_to(60)
maestro.gantry.move_z2_to(70)
maestro.gantry.move_z2_to(80)
maestro.gantry.park_z2()
```

测试越界保护：

```python
assert maestro.gantry.move_z2_to(-1) is False
assert maestro.gantry.move_z2_to(126) is False
```

注意：如果 Arduino IDE、串口监视器、`test_z2_stage_smoke.py` 或 `Z2Stage` 调试脚本正在占用同一个 COM 口，正式系统会连接失败。
