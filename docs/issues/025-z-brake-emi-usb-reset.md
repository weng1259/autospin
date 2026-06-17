# Issue #025 — Z 刹车继电器频繁切换导致 USB 子系统重置

**日期**：2026-04-23
**提出人**：Claude（Phase 3.2 Agent smoke 后续测试时复现）
**状态**：open
**优先级**：中
**类型**：架构 / 硬件 EMI
**涉及文件**：
- `src/hardware/gantry_backend.py:243-259`（home 的 brake release/lock）
- `src/hardware/gantry_backend.py:371-397`（move_to 的 brake release/lock）
- `src/hardware/dstur_relay.py`

## 问题描述

`move_to` 当前对**每一次**调用都做 `release_z_brake → 移动 → lock_z_brake`，无论目标 Z 是否变化。在 Z 高频振荡场景下（Agent smoke "Z 上下移动 10 次" 剧本），每次 cycle = 2 次 move = 4 次继电器切换。

实测：第 4 次切换（Cycle 2 上移）触发 `L3.BRAKE` 错误：

```
[L3 error] code=L3.BRAKE severity=alarm recoverable=True
DSTUR write failed: SerialException('write failed: [Errno 6] Device not configured')
```

事后 `ls /dev/cu.*` 证实**两个 USB 串口都消失**（DSTUR 的 `cu.usbmodem*` 和 grbl Mega 的 `cu.wchusbserial*`），需要物理拔插恢复。比 memory 里记录的"只丢 DSTUR"更严重——说明 EMI 累积已经触发 macOS 重置整个 USB hub，不只单端口故障。

## 根因分析

EMI 物理路径：
1. DSTUR-T80 内部 SRD-05VDC-SL-C 继电器线圈通断 → 反向 EMF 脉冲
2. 通过共地耦合到 USB 5V/D+/D- 信号线
3. macOS USB 子系统对 EMI 敏感度高，累积到阈值后触发整 hub reset

软件层放大因素：**`move_to` 把"释放刹车"和"单次 move"绑死**，没有 brake-skip 优化也没有批量 context manager。纯 XY 移动也会无意义地切换 Z 刹车。

换树莓派**不是解决方案**——EMI 是物理问题，Linux 只是不主动 reset hub，但 EMI 本身仍存在，且 Pi 的 USB 供电更弱反而更敏感。

### 第二 bug：backend 无 USB 重连路径（同次复现暴露）

物理拔插 USB 恢复 `/dev/cu.usbmodem6670E00119391` 后，**L3 backend 仍然报同样的 `L3.BRAKE` 错误**。原因：`serial.Serial` 对象持有拔 USB 之前的 stale fd，Python 不会自动感知 USB 重插，必须显式 `close() + open()`。

当前 `DSTURRelay` 和 `GantryBackend`：
- 没有 `reconnect()` 方法
- 没有暴露给 Agent 的 `mcp__gantry__reconnect` 工具
- 没有自动检测 `[Errno 6] Device not configured` 后重试 connect 的逻辑

**实际后果**：USB 一掉 → Agent session 必须整体重启 → `is_homed` 丢失（实例属性，不持久化）→ 必须重新归零 → 30 秒物理动作 + 刹车磨损。本次复现 = 1 次 EMI 事故被放大成 30 秒 home + session 全部 context 丢失。

## 建议方案

### A. 软件层（性价比最高，先做）

1. **`move_to` 的 brake-skip 优化**：调用前比较 `target.z` 与 `current.z`，相等则跳过 release/lock。
   - 收益：纯 XY 移动场景 100% 消除继电器切换
   - 风险点：`current.z` 来自 status snapshot，需考虑 stale 问题；安全起见，只有 `wait_for_idle=True` 且 snapshot 新鲜（<200ms）时才跳过
2. **批量 context manager**：`with backend.brake_released(): for ...: move_to(...)` 给真的需要 Z 序列动作的场景一次性释放、最后一次性锁回。
3. （可选）DSTUR 写入之间加最小间隔（当前 release+lock 是连续两次 9600 波特字节流，~5ms 内完成；若加 50ms 隔离，单次切换 EMI 应该降低）。
4. **USB 重连路径**（针对第二 bug）：
   - `DSTURRelay.reconnect()`：检测 `[Errno 6]` 时 `close() + 重 open()`，最多重试 N 次
   - `GantryBackend.reconnect()`：同上
   - 暴露 `mcp__gantry__reconnect` MCP 工具给 Agent，物理拔插后 Agent 自己调一次就能恢复，不必重启整个 session
   - （可选）在 `_brake.write()` 失败时自动调一次 reconnect 再重试，把 `L3.BRAKE` 从 `severity=alarm` 降为 `severity=warning`

### B. 硬件层（治本，下次出门顺手）

1. **铁氧体磁环**夹在 DSTUR USB 线 + Mega USB 线两端（~20 元，立竿见影）
2. **DSTUR 继电器线圈并 TVS 二极管或 RC 吸收回路**（~10 元，从源头消除反向 EMF）
3. **带光耦的 USB 隔离器**（~80-150 元，物理隔离最彻底）

## 验收标准

- [ ] `move_to` 实现 brake-skip：`target.z == current.z` 时跳过继电器切换
- [ ] 加 unit test 验证 brake-skip 触发条件（snapshot 新鲜度、wait_for_idle 状态）
- [ ] Agent smoke 重跑"纯 XY 移动 10 次"剧本，确认 0 次继电器切换
- [ ] Agent smoke "Z 上下 10 次" 剧本：用 `brake_released()` context 包住，确认整个剧本只 1 次 release + 1 次 lock
- [ ] 硬件加铁氧体磁环后，重跑原暴力剧本（不用上述软件优化），看是否能跑完 10 cycles 不丢 USB
- [ ] `DSTURRelay.reconnect()` 实现 + 暴露 `mcp__gantry__reconnect` 工具
- [ ] 验收剧本：故意拔插 DSTUR USB → Agent 调 `mcp__gantry__reconnect` → 续跑成功，**不重启 session、不丢 is_homed**

## 相关

- memory：`project_usb_subsystem_reset_mode.md`（第二类卡死，本次比那条记录更严重）
- memory 提示："跑 grbl 串口的 Python 脚本最好和 DSTUR 继电器串口用独立的 serial 实例，中间隔 0.5s 空白" —— 当前 backend 已经是独立实例，但没有 0.5s 间隔；本 issue 软件方案 #3 即落实这一点

---

## 2026-04-24 更新：真根因是**刹车红线物理断线**；软件诊断全线误诊

Phase 3.3 Task 7 Agent smoke 后 PM 临时加了一场 pick-and-place 剧本（"把 -150,-100 的物块夹起来搬到零点"）。Step 5 运动中进 alarm，Step 6 home 90s 超时，软件层彻底死锁。

### ⚠️ 真根因（最终定位）

**Z 电机刹车线（红线）物理断线**——断线位置在 DSTUR 继电器 → 电机刹车线圈这段中间某处，呈**间歇接触不良**状态。所有症状用这一个根因就能闭环解释：

- **Step 5 走 3mm 进 alarm**：刹车接触不良让 Z 卡涩 → 电机带着部分刹车负载运动 → 闭环驱动器失步抖动 → 状态异常 → grbl 报 alarm
- **Step 6 home 90s 超时**：CH2 ON 但刹车线圈没真通电 → Z 电机堵转
- **"Z 急速降下来"那次**：断线瞬间接通 → 刹车短时释放 → $1=0（当时 EEPROM 腐蚀 $1）失励磁 → Z 重力下落
- **哒哒哒 + 发涩摩擦声**：刹车部分抱死 + 电机硬推 → 失步 + 摩擦
- **CH2 LED 亮但刹车没响应**：DSTUR 侧 OK，断点在 DSTUR 下游

### ❌ 排查过程中的错误诊断（记录供未来借鉴）

修复过程中依次扣了以下锅，**全错**（或至少不是导火索）：

1. **Z 刹车继电器 EMI 快速切换**（假设 lock → release 紧贴切换引 EMI 干扰限位）——错，没有 EMI 这回事
2. **RelayBackend state-memo 漂移**（假设 EMI 让物理 CH2 变灭但 memo 以为是 ON）——错，memo 没漂移，继电器物理响应了，问题在 DSTUR 下游
3. **`$5=0` EEPROM 腐蚀**（确实存在，诊断出来发现 $5/$21/$22/$27/$1/$24/$25/$26 全被抖坏）——这个是**真的次生问题**，修好参数后依然归零失败，说明它不是主线根因
4. **Z 电机驱动器进了 ALM 状态**（假设 EMI 让驱动器保护）——错，驱动器正常
5. **SW8 拨码被抖成开环模式**（假设闭环失效导致失步）——错，SW8 没动
6. **联轴器顶丝松了**（假设强行手转导致）——错，联轴器正常

**排查最终突破点**：用户把电机从龙门架拆下检查时发现**一根刹车线从插头里脱出**，接线整改后所有症状立刻消失。

### 教训

**硬件故障次序应该物理层优先**：
- 有"机械异响 / 扭矩异常 / 卡涩"类症状时，先查**电源线 / 信号线 / 端子 / 插头是否物理完整**
- 软件层诊断（grbl 参数、state-memo、EMI 推理）是**最后**才上的手段
- 软件诊断之所以被误导走得这么远，是因为伴生的 EEPROM 腐蚀让参数错乱干扰判断，**让"真实硬件故障"的症状和"软件配置错"的症状混在一起**
- 下次遇到"grbl MPos 增加但物理没动"这种 pattern，**先上万用表量刹车线和相线的通断**，再碰软件

### 实施的代码修复（保留，不回滚）

虽然刹车线断才是今天的真根因，但修复过程中做的 4 个防御性代码改动**仍然正确**（防未来可能发生的 state-memo 漂移 + EMI 快速切换 + brake-skip 架构更清晰），不改 API 合同，133/133 tests 通过——**保留不回滚**：

- `RelayBackend._ch_on_force` / `_ch_off_force`（私有，bypass state-memo）
- `GantryBackend._release_brake` / `_lock_brake` 改调 force 版本
- `_lock_brake` 末尾 `time.sleep(0.2)` grace period
- `move_to` 入口 `z_unchanged` 判断，brake-skip 上移到 GantryBackend 层

### 仍未做

- `mcp__gantry__unlock_alarm` / `recover_from_alarm` 暴露给 Agent —— Phase 3.5 做
- 刹车线换成**带锁扣的工业连接器**（现在 JST/杜邦头太容易松）
- `agent_smoke.py` 启动时自动诊断 + 恢复关键 grbl 参数（$1/$5/$21/$22 等），应对 EEPROM 腐蚀
- 硬件层 B（铁氧体磁环 / TVS / USB 隔离器）

## 2026-04-24 补充：`$25=1500` 会稳定复现 `$H -> ALARM:9`

刹车线问题处理后，又复现了一类更单纯的归零失败：`$H` 约 8s 后返回 `ALARM:9`。本次验证与刹车线断线不同，传感器链路本身是通的：

- `$5=1`、`$3=6`、`$23=0`、`$27=5` 均正确
- 只读 `?` 轮询期间人工遮挡 Z+/X+/Y+，grbl 分别报告 `Pn:Z` / `Pn:X` / `Pn:Y`
- `$24=50`、`$25=1500` 时，`tools/grbl_stability_test.py home` 复现 `ALARM:9`
- 降回 `$24=25`、`$25=500` 后，同一脚本成功归零到 `MPos:-4.999,-4.999,-4.999`

处理结果：

- `tools/force_grbl_unlock.py` 改为写回 `$24=25.000` / `$25=500.000`
- `tools/diag_grbl_readonly.py` 改为校验 `$24=25.000` / `$25=500.000`
- 详细证据见 `docs/verification/phase-3.3-homing-alarm9-recovery.md`

## 2026-04-24 进一步补充：`$0=130` / `$4=1` 也会制造“假成功”

随后又确认到更基础的一层参数漂移：`$0` 漂到 `130`、`$4` 漂到 `1`。这会让 grbl
仍然返回 `ok`，甚至 `WPos` 也继续推进，但 step pulse 宽度和 enable 极性已经不对，
驱动器可能收不到有效脉冲，结果就是“软件看起来到位，机械实际没到位”。

这类问题和 Z 刹车 EMI 可以同时存在，也可以独立发生。为此 backend 已在
`GantryBackend.connect()` 加入关键 `$` 参数预检和自动修复，`agent_smoke.py` 启动时
也会显式做一次校验。
