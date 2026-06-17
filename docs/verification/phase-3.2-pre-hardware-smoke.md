# Phase 3.2 前置硬件 smoke 验收

## 元信息

- **日期**：2026-04-23
- **关联**：[migration-checklist.md §Phase 3.2 前置](../decision-log/migration-checklist.md) ·
  [phase-3.5-plan.md §P0 硬件稳定性 smoke](../decision-log/phase-3.5-plan.md)
- **前置**：Phase 3.1 全部 Slice ✅ / grbl 栈固定 / DSTUR-T80 继电器接线完好
- **PM 签字**：✅ Kevin / 2026-04-23 19:05

## 一句话目标

进 Phase 3.2 软件工作前，验证硬件底座足够稳——避免 backend 打磨完了才发
现传感器抖动 / EEPROM 腐蚀 / 限位误触发这些会污染 Agent 推理链的根因。

## 结果总览

| 项 | 预期 | 实测 | 结果 |
|---|---|---|---|
| **冷启 `$5=1` 验证（10 次）** | 每次断电/通电后 `$5=1` 保持 | 10/10 PASS | ✅ |
| **X 轴 jog `Pn:` smoke（±20mm × 10，f=3000）** | 0 次 Pn 误触发 / 0 ALARM / 0 断联 | 11 发 / 11 ok，0/0/0 | ✅ |
| ~~Phase 0 测试 B（拔 X 相线 → ALARM:1）~~ | ~~grbl 自动捕获~~ | **跳过**（见下） | ⏭️ |

## Phase 0 测试 B 跳过的原因

检查 `firmware/grbl_spike/our_config/config.h:376` 确认 `USE_DIGITAL_INPUT`
**被注释**——grbl-Mega-5X 当前不读 D31/D32/D33 的 ALM 信号。这是设计决定
（见 `docs/guides/04-grbl-固件配置.md:79`：*"ALM 报警引脚 D31/D32/D33：物
理已接，但未接入 grbl。Phase 3 由 Python orchestrator 轮询"*）。

但 `src/hardware/gantry_backend.py` 目前也**没有 ALM 轮询实现**。即 ALM
线已接，软件两侧（grbl / backend）都没读，拔相只能看驱动器红灯闪 6 次，
软件层无反应。跑「拔相 → 预期 ALARM:1」没有端到端意义。

**登记为 Phase 3.2 补做项**（任务 2.8）：`GantryBackend` 加后台 ALM 轮询线
程，读 D31-33 → 触发 L3Error。补完后再约 PM 做真·拔相验收，落
`docs/verification/phase-3.2-alm-polling.md`。

## 脚本和日志

- 冷启验证脚本：`tools/verify_eeprom_cold_boot.py`（自动检测 USB 拔插，
  PM 只做物理动作）。日志：`tools/_stability_logs/20260423_185907_cold_boot.log`
- jog smoke：沿用 `tools/grbl_stability_test.py jog --axis X --range 20
  --count 10 --feed 3000 --flow-control`（原 Phase 1 spike 工具）。日志：
  `tools/_stability_logs/20260423_190447_jog_X.log`

## 10 次冷启 `$5` 原始记录

```
#1  ✓ PASS  $5=1
#2  ✓ PASS  $5=1
#3  ✓ PASS  $5=1
#4  ✓ PASS  $5=1
#5  ✓ PASS  $5=1
#6  ✓ PASS  $5=1
#7  ✓ PASS  $5=1
#8  ✓ PASS  $5=1
#9  ✓ PASS  $5=1
#10 ✓ PASS  $5=1
PASS: 10/10
```

## X 轴 jog smoke 汇总

```
压测汇总（14.4s）
发出命令数      : 11
ok 响应数       : 11
error 总数      : 0
ALARM 总数      : 0
Pn 触发次数     : 0
?-status 采样   : 139
USB/serial 断联 : 0
```

## Y/Z 轴未跑的理由（PM 决定）

Y 和 X 硬件规格（电机 / 驱动器 / 传感器）完全相同，同一套固件。Z 压测要
额外开 CH2 释放刹车，有感性尖峰 / USB EMI 风险（MEMORY 记过的第二类卡
死）。X 单轴全绿已经足以证明三轴层面的"限位 + 流控 + 串口"组合没有结构
性毛病。后续 Phase 3.3 / 3.4 做 Gripper + maestro 时若暴露 Y/Z 特有问题
再补跑。

## 结论

**进 Phase 3.2 软件任务的前提成立。** 硬件底座在 2026-04-23 19:00 这个
时间点处于稳定状态，后续 backend 打磨 / mypy / schema / 幂等测试可以开
工。任务 2.8（ALM 轮询）作为 Phase 3.2 的第 8 件事补上。
