# DBLS400 旋涂电机 bring-up 验收记录

> 任务卡：`docs/decision-log/task-spinmotor-bringup.md` · 契约：`device-bringup-smoke-gates.md` §4
> 宿主：Pi（`ssh pi-spin`，`~/autospin`）· 执行：2026-06-20
> 设备：DBLS400 无刷驱动器（RS485 Modbus RTU），slave=2，9600，pole_pairs=4

## 状态总览

| Gate | 内容 | 结果 |
|---|---|---|
| Gate-1 | 读母线电压（总线通+设备活） | ✅ **通过**（23.25V 稳定） |
| Gate-2 | 最低速真转 + 方向 + 无异常振动 | ✅ **通过**（转盘真转、平稳、方向 CCW；根因=联轴器顶丝松，紧固后解决） |
| speed_factor=2.5 核对 | 实际转速换算系数 | ✅ **成立**（raw×20/极数8 = raw×2.5，实测吻合） |

> **结论：DBLS400 旋涂电机 bring-up 全部 smoke-gate 通过（2026-06-20）。** 可派 Chunk 5 的 `SpincoaterBackend` ADR-004 壳。

---

## 关键前置：串口拓扑变更（2026-06-20）

PM 把 RS485 转接头从 Exar（04E2:1411）换成 **CH340 USB-RS485 模块**，并改了 USB 接口映射。
结果：Pi 上出现**两个 CH340（1a86:7523）**——新 RS485 桥与 grbl 同芯片同 VID:PID，
仅靠 hwid 无法区分。

**用协议探测确定性识别**（`tools/serial_id_probe.py`，全只读）：

| 设备节点 | 物理 USB 口 | 探测结果 | 判定 |
|---|---|---|---|
| `/dev/ttyUSB1` (`/dev/autospin_rs485`) | 1-2 | DBLS400 slave=2 电压响应 + 加热台 slave=3 响应 | **RS485 总线（DBLS400 在此）** |
| `/dev/ttyUSB0` (`/dev/autospin_xyz`) | 1-1 | grbl 无响应、Modbus 无响应 | grbl 口（此刻无响应，疑未上电；不影响旋涂任务） |

> ⚠️ 软链接在 14:27 一度错位（`autospin_xyz→ttyUSB1`），14:37 自我纠正。udev 规则
> （`/etc/udev/rules.d/*autospin*`，KERNELS 锁物理口）触发不稳定，建议后续 RS485 串口
> 归一时复核 reload/trigger 的确定性。本次 bring-up 直接锁定**协议探测确认过的 ttyUSB1**。

---

## Gate-1：读母线电压 ✅

工具：`tools/spinmotor_bringup.py voltage`（直接驱动 MotorController，串口全程开）
量纲（DBLS400 手册行230）：**母线电压(V) = raw / 4**

```
第1次: raw=93 (0x005D) -> 23.25 V  PASS
第2次: raw=93 (0x005D) -> 23.25 V  PASS
第3次: raw=93 (0x005D) -> 23.25 V  PASS
```

3 次连读稳定一致 **23.25V**（额定 24–48V 范围内，与 24V 电源实测 23.92V 吻合）。
→ **RS485 总线通 + DBLS400 设备活 + 母线电压合理**。

---

## Gate-2：低速真转 100 RPM ✅ 通过

工具：`tools/spinmotor_bringup.py spin 100 6`（`SPIN_CONFIRM=1`，锁 ttyUSB1）
序列：读电压 → unlock → start(forward, 控制字 0x0409) → set_speed(100) → 观察 → stop+lock
量纲（DBLS400 手册行195-228）：**实际转速(RPM) = raw × 20 ÷ 极数 = raw × 2.5**（极数=2×4=8）

```
设定100 | 实际 50→67.5→90→90→90 RPM (raw 稳态=36) | 0x801B 故障位=无 (全程)
```

**验收结果**（紧固机械后重测，15:00）：
- 控制字下发成功（0x0409 正转），驱动器接受。
- 霍尔反馈稳定爬升到 **raw=36 → 90 RPM**（设定100，闭环差约10%）。
- **故障寄存器 0x801B 全程「故障位=无」**——无堵转/过流/霍尔异常/缺相/欠压报警。
- **PM 目视：转盘真转、运转平稳、无异常振动/异响，方向逆时针（CCW）**。
- → **真转 ✅ + 方向记录（forward 控制字 0x0409 = CCW，从上往下看）✅ + 无异常振动 ✅**。

### 故障诊断小记（物理层优先的范例）
首次真转时 PM 目视「转盘没动、无声音」，但软件读到 actual 爬升 + 故障寄存器零报警。
加读 **0x801B 故障状态**确认电气侧无任何报警（无堵转/缺相/霍尔异常）+ 霍尔稳定反馈，
判定**电机轴在转、动力没传到转盘**。PM 断电检查后**紧固联轴器/卡盘顶丝**，重测即真转。
→ 印证 memory「硬件故障诊断物理层优先」：电气数据正常时，矛盾常在机械连接，
不该盲目软件重试。故障寄存器 0x801B 是区分「电机没转」vs「转了没传动」的关键探针。

### speed_factor=2.5 核对结论 ✅
手册公式 raw×20/极数：5对极例子用×2，本电机 4 对极（极数8）应为 ×2.5——
driver 硬编码 `speed_factor=2.5` **正确**（仅对 pole_pairs=4 成立，换电机需改）。
实测设定100、稳态实际90，换算趋势吻合。

---

## 工具与可复现

- `tools/spinmotor_bringup.py`：分级 bring-up（`voltage` 只读 / `spin [RPM] [HOLD]` 真转）。
  安全门闩：真转需 `SPIN_CONFIRM=1`，RPM>300 硬拒绝，`SPIN_PORT` 可覆盖端口。
- `tools/serial_id_probe.py`：串口身份协议探测（grbl vs DBLS400 Modbus，全只读）。
- `autospin_system/test_spin_motor_profile.py`：已按任务卡改 mock=False + 低速 profile
  （原冲3000的多段升速降级为 `LEGACY_RAMP_PROFILE`，平衡确认后再用）。

## 后续（smoke-gate 已全过，以下为可选/移交）

1. ✅ 机械连接已修（联轴器/卡盘顶丝紧固）+ Gate-2 重测通过。
2. **（可选）升速验证**：确认平衡后可用 `LEGACY_RAMP_PROFILE` 升到 1000/3000 RPM 验斜坡，
   人始终在急停旁。本次首测只验低速（100 RPM），未升速。
3. **Chunk 5 `SpincoaterBackend` ADR-004 壳**：把 driver 的 `(bool,str)` tuple 统一成 pydantic
   `SpinResult` + 幂等 + dry_run + RuntimeError→L3Error 中文（落点 `src/hardware/`，照 GantryBackend 样式）。
4. udev 规则触发稳定性（两个 CH340 靠物理口区分）随 RS485 串口归一复核。
5. ⚠️ grbl 口（ttyUSB0/物理口1-1）本次探测无响应——需单独确认 grbl 控制器供电/接线（不影响旋涂）。
