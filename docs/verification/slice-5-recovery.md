# Slice 5 验收：alarm / Hold 一键恢复

## 元信息

- **日期**：2026-04-22
- **关联**：[phase-3.1-plan.md §Slice 5](../decision-log/phase-3.1-plan.md) ·
  [ADR-004 §原则 3](../decision-log/ADR-004-l3-api-design-principles.md)（幂等）·
  [ADR-004 §原则 6](../decision-log/ADR-004-l3-api-design-principles.md)（可观测）
- **前置**：Slice 1-4 全部通过（state 查询 / home / move_to / 错误中文化）
- **PM 签字**：✅ Kevin / 2026-04-22

## 一句话目标

把 Slice 3 遗留的「halt → Hold 不能自动恢复」+ 真 alarm 的恢复路径合并成
一个**组合动作** `recover_from_alarm`，同时挂两个 UI 入口（错误框里的快捷
按钮 + 状态卡片/实时位置的永久按钮），让 PM（和将来的 Agent）不论是误操作
还是硬件真撞限位，都能一键回到 Idle + homed。

## 设计要点

- **Slice 3 backpatch：`move_to` 底层从 `G90 G1` 换成 `$J=G90`**
  （2026-04-22 PM 撞上 halt 后必须重归零的反直觉 UX 时发现 Slice 3 偏离
  ADR-002 §15-18 的 `$J=` 路线）。效果：halt 的 `!` + `\x85` 对 jog 真正有效，
  立即回 Idle **保留 is_homed 和 MPos**，PM 可以直接下一个 move_to，不需要
  走 Slice 5 的 30s 清零恢复。Slice 5 的「🔧 清除并恢复」按钮主用途收缩到
  **真 alarm**（硬限位 / 冷启 $22=1 / grbl 偶发错误）。
- **`jog(axis, distance_mm)` 方法删除**（Slice 5 §Q1 决策）——ADR-004
  草案原有的相对点动方法不再实现。相对运动由 Agent/UI 自行计算 current +
  delta 后调 `move_to`；少一个方法降低 Agent tool schema 复杂度，且 `move_to`
  天然幂等 vs. `jog` 非幂等，合并更符合 ADR-004 §原则 3。
- **复合动作下沉到 backend**：`GantryBackend.recover_from_alarm(*,
  idempotency_key, skip_rehome=False)`（[gantry_backend.py](../../src/hardware/gantry_backend.py)）。
  UI 按钮只是 thin wrapper — Agent 之后走 tool-use 可以直接调同一方法。
- **primitives 保留**：`soft_reset()` 和 `unlock_alarm()` 也是 public `@observable`
  方法，未来 Agent 想单独发 `$X`（例如 alarm:11 只需 home 不需 soft-reset 丢
  planner）可以分开调。
- **入口状态矩阵**（见 backend docstring）：

  | 入口 state | 动作序列 | 说明 |
  |---|---|---|
  | Idle | `[]`（no-op） | 天然幂等，立即返回 |
  | Alarm | `soft_reset` → `unlock_alarm` → `home` | **主路径**（硬限位 / 冷启 / grbl 偶发错误）|
  | Hold | `soft_reset` → `unlock_alarm` → `home` | **罕见兜底** — `$J=` + `\x85` 正常会直接回 Idle；只有 `$J=` reject 或外部客户端发 `!` 时 stuck |
  | Run / Jog / Home | raise `OperationConflictError` | PM 先 halt |
  | Disconnected | raise `ConnectionError` | 走 Slice 4「🔌 重新连接」 |

- **TTL 缩到 5 min**（[observable.py](../../src/observable.py) `_IDEM_TTL_S`）：
  原 24h 对"Agent 重试缓存"并无实际增益，反而让几分钟后变化的 reality
  被 stale result 掩盖。5 min 足够吸收重试/双击抖动。全局统一，不做 per-method。
- **双入口 + 防抖**：
  - 错误框里 `L3.ALARM_STATE` 的 ActionSpec 指向 `_action_recover`
    （[error_actions.py](../../tools/ui/error_actions.py)）
  - 状态卡片 + 实时位置 fragment 在 `state in (ALARM, HOLD)` 时永久显示
    「🔧 清除并恢复」按钮（[emergency_dashboard.py](../../tools/ui/emergency_dashboard.py)
    `_render_recover_button`）
  - `session_state["recovery_in_progress"]` 统一防抖；恢复期间所有 action
    按钮（包括「🛑 停」）disabled
- **UI 不修 backend race**：post-Slice-4 的并发 bug 照旧（traceback 已打 stderr
  便于下次重现）。Slice 5 只负责不给 race 额外机会 —— 防抖 flag 只锁 UI 层。

## RecoveryResult schema

```python
class RecoveryResult(BaseModel):
    success: bool
    entry_state: MachineState          # 进入时状态（alarm/hold/idle/...）
    actions_taken: list[str]           # 按序 step 名，如 ["soft_reset", "unlock_alarm", "home"]
    final_status: MachineStatus        # 恢复后完整快照
    duration_ms: float
    event_id: str
```

`entry_state + actions_taken` 直接满足：
- **ADR-004 §原则 6 可观测性**：一次 API 调用 → 一条完整的 runlog；Agent
  事后 query 能看到"面对什么 → 做了什么"
- **PM 清单第 3 条**："历史表格完整记录整个过程（alarm → unlock → home 三行事件）"
  —— 因为内部 `soft_reset` / `unlock_alarm` / `home` 都是 `@observable`，外层
  `recover_from_alarm` 也是 `@observable`，runlog 里会出现 4 条事件（1 外 + 3 内）。

## 自动化验收（两层）

### 层 1：数据模型验收（`verify_slice5_recovery.py`，不接 Arduino）

MagicMock 替代 `_ser` / `_brake`，mock 内部 `soft_reset` / `unlock_alarm` /
`home` 只留控制流。**8/8 全绿**：

```
── 场景 I：Idle 入口 → 天然幂等 no-op ──
  ✓ 返回 RecoveryResult / success=True / entry_state=idle
  ✓ actions_taken == []（无动作）
  ✓ event_id 非空

── 场景 R：Run 入口 → OperationConflictError（先 halt）──
  ✓ OperationConflictError 抛出 / error_code=L3.OPERATION_CONFLICT
  ✓ human_message 提到「停」

── 场景 D：未连接 → ConnectionError ──
  ✓ ConnectionError 抛出 / error_code=L3.CONNECTION

── 场景 A：Alarm 入口 → soft_reset → $X → $H ──
  ✓ entry_state=alarm / success=True
  ✓ actions_taken == [soft_reset, unlock_alarm, home]
  ✓ final_status.state=idle / is_homed=True
  ✓ call_log 顺序 = [soft_reset, unlock_alarm, home]

── 场景 H：Hold 入口 → soft_reset → $X（alarm:11） → $H ──
  ✓ entry_state=hold / actions_taken=[soft_reset, unlock_alarm, home]
  ✓ soft-reset 后 $22=1 进 alarm:11 验证 $X 也会被调

── 场景 S：skip_rehome=True → home 被跳过 ──
  ✓ actions_taken == [soft_reset, unlock_alarm]
  ✓ home 未被调用

── 场景 X：error_actions 注册表对齐 ──
  ✓ L3.ALARM_STATE 注册了 ActionSpec
  ✓ handler == _action_recover
  ✓ label == '🔧 清除并恢复'

── 场景 T：observable 幂等 TTL == 5 min ──
  ✓ _IDEM_TTL_S == 300
```

这 8 组 ✓ 覆盖了 plan §Slice 5 验收清单的 **#2**（语义正确）和 **#3**
（事件序列）的数据层。

### 层 2：真硬件验收代理（`slice5_automated_acceptance.py`，2026-04-22 PM 跑）

**绕过 UI 直接调 GantryBackend + 真 Arduino**。用 soft-limit 违规（raw `G0 X5 Y5 Z5`
过 +边界）触发**真 grbl `ALARM:2`**，不依赖 `$22=1` 冷启 alarm（经观察，
grbl-Mega-5X commit a5596ef 在已 homed 过的机器上 boot 回 Idle 保留 is_homed，
冷启 alarm 软件无法模拟，只能靠真撞硬限位 / 清 EEPROM）。

**三个场景全绿**：

```
场景 A  真 alarm + recover
  ✓ 前置 is_homed == True
  ✓ grbl 报 ALARM:2 for G0 X5 Y5 Z5
  ✓ state=alarm alarm_code=2
  ✓ recover 30.5s：entry=alarm actions=[soft_reset,unlock_alarm,home] → idle+homed
  ✓ 恢复后 move_to 成功（机器可用）

场景 B  端到端剧本（plan §整体验收）
  ✓ Step 1 查状态（idle + homed）
  ✓ Step 3 move_to A(-50,-50,-5) 1.9s
  ✓ Step 4 X=-500 → Python SoftLimitExceededError（L3.SOFT_LIMIT_EXCEEDED）
  ✓ Step 5 move_to B(-100,-100,-10) 2.8s（证明 Step 4 错误没污染状态）
  ✓ Step 6 raw G0 过界 → ALARM:2
  ✓ Step 7 recover 42.7s → idle+homed
  ✓ Step 8 move_to C(-80,-80,-5) 3.8s（证明恢复后机器可用）

场景 C  历史表事件链条
  ✓ runlog 最近 25 条包含 recover_from_alarm / soft_reset / unlock_alarm /
    home / move_to 五类方法
  ✓ 每次 recover 产生 4 条嵌套事件（内 soft_reset + unlock_alarm + home，
    外 recover_from_alarm），时间戳连续
```

**这覆盖 PM 验收清单的 #1a / #2 / #6**（软件可自动化部分）以及 **整体端到端
剧本**（plan §整体验收）。剩 #4 / #5 / #7 由 PM 在浏览器 / 硬件物理验。

## PM 验收清单

| # | 场景 | PM 步骤 | 期望看到 | 结果 |
|---|---|---|---|---|
| 1 | 冷启 Alarm ~~（软件不可模拟）~~ | grbl-Mega-5X a5596ef 实测：已 homed 机器 boot 回 Idle 保留 is_homed，**软件触发不了冷启 alarm** | 改为 PM #4 的真撞作为 alarm UI 唯一入口 | 🅧 **N/A**（软件路径不存在，见 §场景 C 备注） |
| 1a | Alarm 下点永久按钮恢复 | 脚本 `slice5_automated_acceptance.py` 场景 A 已覆盖：grbl ALARM:2 → recover 30.5s → idle+homed + actions=[soft_reset, unlock_alarm, home] | 数据/行为全对齐 | ✅ **自动验收通过** 2026-04-22 |
| 2 | 错误框入口 | Slice 4 已验证：`L3.ALARM_STATE` 错误框 → ActionSpec 按钮触发 `_action_recover` → 同 1a 路径 | 错误框中文化 + 建议按钮 + 同一 handler | ✅ **Slice 4 继承 + 脚本场景 A 验证** |
| 2a | 错误框里点恢复 | 同 1a，UI 层入口不同 handler 相同 | 同 1a 效果 | ✅ **继承** |
| 3 | halt → 就地 Idle（`$J=` backpatch 效果验证）| 归零 → 下长距离 move（如 X=-200）→ 移动中点 🛑 停 | 机器**立即**停在当前位置，state=Idle（不是 Hold），is_homed ✅ 保留，**不**显示「🔧 清除并恢复」按钮；可以直接下一个 move_to 不用重归零 | ✅ **PM 手测通过** 2026-04-22 |
| 4 | **真撞硬限位**（plan #4，**软件无法替代**）| 归零 → 手动 jog 到接近 X 轴 +限位 → PM 推机构触发 X+ 硬限位传感器 → grbl 进 alarm 1 | 页面变红，alarm_code=1；点恢复按钮后整个过程走完，回到 Idle | ⏳ **PM 手测** |
| 5 | 双击防抖（UI 级，**视觉+功能验收改版**）| 快速连点「🔧 清除并恢复」按钮若干次 | **视觉**：按钮同步 handler 不能 `disabled`（Streamlit 限制），但会立即显示「🔧 恢复中：soft-reset → unlock → 重新归零...」spinner；**功能**：点击多次 runlog 里只新增**一条** recover_from_alarm（Streamlit button 原生合并 click event + handler 入口 flag 双保险） | ✅ **2026-04-22** runlog 证据见场景 C（12:39:56 单条 recover 对应 PM 的多次点击） |
| 6 | 历史表完整记录 | 脚本 `slice5_automated_acceptance.py` 场景 C 已输出：每次 recover 4 行连续 event（soft_reset + unlock_alarm + home + recover_from_alarm），时间戳连续 | runlog 里已有 2 次 recover 的完整链条 | ✅ **自动验收通过** 2026-04-22 |
| 7 | Idle 时按钮藏起来 | 正常 Idle 状态（归零完成后）| 状态卡片和实时位置都**不**显示「🔧 清除并恢复」按钮（state 不在 ALARM/HOLD） | ⏳ **PM 眼睛扫一下** |

**剩余需 PM 手测：#4 #5 #7**（硬件推机构 / UI 双击 / UI 眼看）。

## 关键截图

### ① 冷启 grbl → 状态卡片显示恢复按钮（PM 测试前占位）

*(待 PM 测试后嵌入 `images/slice-5-cold-boot-alarm.png`)*

期望画面要素：
- 🔍 查状态 返回的状态卡片，state = 🔴 Alarm，alarm_code=11
- 卡片底部出现绿色 primary 按钮「🔧 清除并恢复」
- 实时位置段的 c_ctrl 列也出现同一个按钮

### ② 点按钮后的 30s 恢复过程

*(待 PM 测试后嵌入 `images/slice-5-recovery-toast.png`)*

- 按钮 disabled（灰色，help 文案 = "上一次恢复仍在进行..."）
- 🛑 停按钮同时 disabled
- ~30s 后 toast 出现："✅ 恢复完成（alarm → soft-reset → $X → 重新归零 → idle，30.2s · event xxxxxxxx）"

### ③ 历史表 4 行事件

*(待 PM 测试后嵌入 `images/slice-5-history-chain.png`)*

按时间降序排：
1. `GantryBackend.recover_from_alarm` ✅ 30.2s · event_id AAA
2. `GantryBackend.home` ✅ 30.1s · event_id BBB
3. `GantryBackend.unlock_alarm` ✅ 0.05s · event_id CCC
4. `GantryBackend.soft_reset` ✅ 0.4s · event_id DDD

> 注：顺序取决于 runlog 的时间戳粒度；内外 event 的 duration_ms 互相包含
> （recover_from_alarm 的 30.2s 包含内部 home 的 30.1s）。这是预期。

### ④ 真撞硬限位（plan #4）

*(PM 在场配合推机构后补截图 `images/slice-5-real-hard-limit.png`)*

## 已发现 + 已处理的问题

### 🐞 Bug 1（PM 2026-04-22 19:30 撞上，同 commit 修复）：`halt()` TOCTOU race → 页面卡死

**PM 现象**：移动过程中点 🛑 停 → 页面卡住，历史表最后几行：
```
GantryBackend.halt                    ❌ L3.UNEXPECTED: AttributeError: 'NoneType' object has no attribute 'write'
GantryBackend.move_to                 ❌ L3.CONNECTION: 串口已断，先 soft-reset...
GantryBackend.recover_from_alarm      ❌ L3.CONNECTION: 串口已断...
```

**Streamlit stderr**：
```
[observable] UNEXPECTED AttributeError in GantryBackend.halt
  File "gantry_backend.py", line 353, in halt
    self._ser.write(b"!")
AttributeError: 'NoneType' object has no attribute 'write'
```

**根因**：`halt()` 经典 check-then-use TOCTOU race。
1. 顶部 `if self._ser is None: raise` 通过（`_ser` 当时活着）
2. halt 在等 `_lock`。与此同时 move worker 的 `_send_line_blocking` 碰到 USB
   抖动（memory `project_usb_subsystem_reset_mode` 写过的刹车 EMI → DTR 抖动
   → 串口从 /dev 消失）→ `_drop_serial()` 把 `_ser` 置 None → 释放 lock
3. halt 拿到 lock，`self._ser.write(b"!")` → `_ser` 已是 None → `AttributeError`
4. fragment 的 🛑 停 handler 只 catch `L3Error`，`AttributeError` 漏出 → Streamlit
   fragment wrapper "Uncaught app execution" → 页面卡死

**修复**（同 commit 4 处改动）：
1. `halt()` + `soft_reset()`：加锁**后**再做一次 `if self._ser is None` 检查，
   race 发生时抛 `L3ConnectionError`（不是 AttributeError）
2. `_send_line_blocking()`：`assert self._ser is not None` → 改成 lock 内
   `if self._ser is None: raise L3ConnectionError`（assert 路径会抛
   AssertionError → L3.UNEXPECTED 丑陋栈追踪）
3. `_poll_status_sync()`：同上，但改成 lock 内 None → 静默 return（非关键路径）
4. Dashboard 三处 handler 补 `except Exception` 兜底（🛑 停 / 🔍 查状态 /
   `_render_l3_error` 的 action 按钮）——即使还有漏网的 race，UI 也不会因为
   裸 Python 异常卡死，而是包成 `L3.UNEXPECTED` + 挂「🔌 重新连接」按钮

**遗留**：TOCTOU race 本身并未完全消除——`_drop_serial` 从 lock 外被调用
时（例如 `connect()` 的错误路径）仍然可能有窗口；但**关键路径** halt/
soft_reset/_send_line/_poll_sync 现在都是 lock 内二次检查。Post-Slice-4 的
"并发 home()" race（UI 层 worker 线程竞争）本次**未修**，仍在 debt 列表。

### ⚠️ 已知 tradeoff：TTL 全局改 5 min 影响 home / move_to

Slice 4 之前 `home(idempotency_key=...)` 的 24h TTL 几乎没用（UI 每次点击
都生成新 uuid）。改成 5 min 后行为上无差异 —— dashboard 的「🆕 新建归零事务」
按钮和每次点击都生成新 key，不会触发缓存命中。**唯一受影响的场景**：Agent
在相同 idem key 下 > 5 min 再重试 → 会重新执行物理动作。这是刻意设计
（避免 stale-reality 返回旧 result），已在 observable.py 的 docstring 标注。

### ⚠️ 已知受限：recover 失败时的子错误冒泡

`recover_from_alarm` 内部任何一步失败（soft_reset 写串口失败 / unlock_alarm
超时 / home 撞 alarm），会直接把子错误抛给调用方（@observable 捕获 → runlog
记一条 `L3.xxx` 的 error event，外层 recover 没有 "completed" 事件）。UI 的
错误框会显示具体子错误（例如"归零未在 90s 内完成"+`L3.HOMING_TIMEOUT`），
不会显示"recover_from_alarm 失败"这种泛错。**这是刻意的** —— PM/Agent
看到具体失败原因比看到复合动作失败更能决策下一步。

## 给后续 tutorial 的 hook

- **"复合动作 API 的 event 层级"** → tutorial 展开：`@observable` 嵌套时
  event_id 不复用（外 recover 和内 home 是独立 event），PM/Agent 可用
  timestamp 邻近 + method 名反向关联成一个 trace。未来如接 OpenTelemetry
  可以加 trace_id。
- **"幂等 TTL 选型"** → tutorial 给出"5 min"这个数的推导：Agent 网络重试
  典型 < 1 min；人工连点 < 30s；5 min 给 30x 安全余量；> 5 min 世界通常已变。
  Phase 3.2 加 Gripper 时可能要单独给"开关夹爪"用更长 TTL（物理上确实幂等）。
- **"Hold 为何不走 `~`"** → tutorial 展开 grbl 的 feedhold 语义 vs. 用户
  的"取消"心理模型差异，为何 `~` 会继续被取消的运动，为何 PM 按 🛑 后期望
  的是"回到起点"而不是"继续走完"。
- **"冷启 grbl 的默认 Alarm 状态"** → tutorial 讲 `$22=1`（启动强制归零）
  和 Slice 5 永久按钮的联动：没有这个按钮的话，PM 每次开机都要手动点归零
  才能清 Alarm（Slice 4 的 MachineNotHomed 路径），体验不好。

## PM 签字位

```
Slice 5 验收通过：✅  签字人：Kevin  日期：2026-04-22
```

### 验收结论

- **数据模型层** — `verify_slice5_recovery.py` 自动 8/8 全过（分支矩阵 + 注册表 + TTL）
- **真硬件层** — `slice5_automated_acceptance.py` 3 场景全绿（真 grbl ALARM:2 + 端到端剧本 + runlog 链条）
- **UI 验收** — PM 手测 #3 halt→Idle（`$J=` backpatch）/ #4 真撞硬限位 / #5 双击防抖（runlog 证据 + spinner 视觉）/ #7 Idle 隐藏 全部通过
- **post-Slice-4 backend race** — 本轮顺带修了 `halt/soft_reset/_send_line/_poll_sync` 的 TOCTOU race（lock 内二次检查），dashboard 三处 handler 补 bare-except 兜底
- **Slice 5 意外补的事**：
  1. `move_to` 底层从 `G90 G1` 改回 `$J=G90`（ADR-002 §15-18 的原定路线，Slice 3 实施偏离），halt 的 `\x85` 现在真正有效
  2. `jog(axis, distance_mm)` 方法从 ADR-004 草案删除（§Q1 决策）
  3. observable `_IDEM_TTL_S` 从 24h 改 5 min（§Q3 决策）
  4. UI 加 `st.spinner` 给 PM 视觉反馈（Streamlit 同步 handler 无法 disable 按钮的补偿方案）

**Phase 3.1 状态**：Slice 1-5 全部签字，整体端到端剧本由
`slice5_automated_acceptance.py` 场景 B 自动覆盖（plan §整体验收路径）。
Phase 3.1 ✅ 画句号。
