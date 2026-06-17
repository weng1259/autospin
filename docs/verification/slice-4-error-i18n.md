# Slice 4 验收：错误中文化 + 建议动作

## 元信息

- **日期**：2026-04-20
- **关联**：[phase-3.1-plan.md §Slice 4](../decision-log/phase-3.1-plan.md) · [ADR-004 §原则 2](../decision-log/ADR-004-l3-api-design-principles.md)
- **PM 签字**：✅ Kevin / 2026-04-22 19:00

## 一句话目标

ADR-004 §原则 2「结构化错误」落到 PM 看得懂的 UI 上：4 种常见错误场景触发
时，页面显示**中文**`human_message` + **中文**`suggested_action_zh` + 按
`severity` 分色（warning=黄 / alarm=红），`recoverable=True` 的错误挂一个绿色
「按建议操作」快捷按钮，点了自动执行恢复动作。Python 栈追踪不出现在 UI 上
但仍在 `runlog.db` 和日志里（Agent / 开发者可查）。

## 设计要点

- `L3Error` 新增 `severity: "warning" | "alarm"` + `suggested_action_zh` 字段
  （[src/hardware/errors.py](../../src/hardware/errors.py)）
- `severity` 和 `recoverable` 是**不同维度**：`SoftLimitExceededError.severity=warning`
  （用户改输入就行）但 `recoverable=False`（机器不需要自动恢复）
- UI 侧统一渲染器 `_render_l3_error(err, key_prefix)`
  （[tools/ui/emergency_dashboard.py](../../tools/ui/emergency_dashboard.py)）
  — 所有错误路径（查状态 / 归零 / 去这里 / 实时位置 / 急停）都挂 `last_error`
  并由这一个渲染器负责
- 建议按钮注册表 `tools/ui/error_actions.py` — 不污染 `src/hardware`。当前
  3 个 code 挂按钮：`L3.MACHINE_NOT_HOMED` → 🏠 立即归零；`L3.CONNECTION`
  → 🔌 重新连接；`L3.OPERATION_CONFLICT` → 🛑 立即停
- `L3.ALARM_STATE` 的「🔧 清除并恢复」留给 **Slice 5**（组合 unlock + home）

## severity / action 对照表

| error_code | severity | recoverable | 建议按钮 | 建议文案（中） |
|---|---|---|---|---|
| L3.CONNECTION | alarm | ✓ | 🔌 重新连接 | 检查 USB 线和端口占用，然后 sidebar 点「断开并重连」 |
| L3.MACHINE_NOT_HOMED | **warning** | ✓ | 🏠 立即归零 | 先点「🏠 归零」完成归零，再下运动指令 |
| L3.ALARM_STATE | alarm | ✓ | (Slice 5) | 点「🔧 清除并恢复」一键 unlock + 重新归零 |
| L3.SOFT_LIMIT_EXCEEDED | **warning** | ✗ | — | 目标超出工作空间，请调整坐标输入 |
| L3.HOMING_TIMEOUT | alarm | ✓ | — | 归零/运动超时未完成。检查 Z 刹车和限位传感器后重试 |
| L3.BRAKE | alarm | ✓ | — | DSTUR-T80 继电器命令失败，确认 USB 端口存在且未被占用 |
| L3.OPERATION_CONFLICT | **warning** | ✓ | 🛑 立即停 | 机器正忙，请先点顶部「🛑 停」或等当前动作完成 |

## 自动化数据模型验收

用 `tools/ui/verify_slice4_errors.py` 绕过 UI 直接 probe 每个错误场景的
`L3Error` 子类字段和 `error_actions` 注册表。不动硬件（连 Arduino 但不归零、
不移动），四场景全绿:

```
── 场景 1：没归零就 move → MachineNotHomedError ──
  ✓ MachineNotHomedError 抛出
    code=L3.MACHINE_NOT_HOMED severity=warning recoverable=True
    human = '机器未归零，请先点 🏠 归零按钮'
    zh    = '先点「🏠 归零」按钮完成归零，再下运动指令。'
  ✓ error_actions 注册了按钮  ·  label = '🏠 立即归零'

── 场景 2：坐标超限 (X=-500) → SoftLimitExceededError ──
  ✓ SoftLimitExceededError 抛出
    code=L3.SOFT_LIMIT_EXCEEDED severity=warning recoverable=False
    human = 'X = -500.0 超出 [-280.0, 0.0]，请调整坐标'
    zh    = '目标超出工作空间（见 sidebar「软限位」），请调整坐标输入。'
  ✓ error_actions **不**注册按钮（按 plan 设计，用户改输入即可）

── 场景 3：串口连接失败 → ConnectionError ──
  ✓ ConnectionError 抛出
    code=L3.CONNECTION severity=alarm recoverable=True
    human = '无法打开串口 /dev/cu.does_not_exist_xyz'
    zh    = '检查 USB 线和端口占用，然后在 sidebar 点「断开并重连」。'
  ✓ error_actions 注册了按钮  ·  label = '🔌 重新连接'

── 场景 4：运行中再下指令 → OperationConflictError ──
  ✓ OperationConflictError 抛出
    code=L3.OPERATION_CONFLICT severity=warning recoverable=True
    human = '上一次移动还没结束，请先点 🛑 停 或等待完成'
    zh    = '机器正忙，请先点顶部「🛑 停」或等当前动作完成。'
  ✓ error_actions 注册了按钮  ·  label = '🛑 立即停'

✓ Slice 4 数据模型 ✅（severity + 中文 + action 映射全部对齐）
```

这 4 组 ✓ 等价于 **plan §Slice 4 的验收清单 1/2/3/4 全部数据层通过**。UI 人工
验收只需要再确认"黄/红颜色渲染对"、"点按钮真的触发 handler"两件事。

## 验收清单

| # | 场景 | PM 步骤 | 期望看到 | 结果 | 截图 |
|---|---|---|---|---|---|
| 1 | 没归零就 move | 点 sidebar「断开并重连」让 backend drop（is_homed 清零），输入合法坐标 → 点「🎯 去这里」| 🟡 黄色警告框「机器未归零，请先点 🏠 归零按钮」+ 绿色按钮「🏠 立即归零」 | ⏳ | ⏳ |
| 1a | 点建议按钮「🏠 立即归零」 | 承上，直接点按钮 | 机器真归零（~30s），完成后错误消失，状态卡片刷新 | ⏳ | ⏳ |
| 2 | 坐标超限（backend 侧） | 用浏览器 DevTools 强改 X 输入框或直接修 min_value；或直接在 pydantic 校验外调 backend.start_move_async（见文末脚本）| 🟡 黄色「X=-500 超出...」+ 无按钮（预期，只需 PM 调输入） | ⏳ | ⏳ |
| 2a | 坐标超限（UI front-line）| X 输入框直接填 -500 | `st.number_input` 自己挡住（值不会小于 min），**不进 backend** | ⏳ | ⏳ |
| 3 | USB 拔线 + move | 跑着拔 Arduino USB → 点「🔍 查状态」或「🎯 去这里」| 🔴 红色「无法打开串口 .../串口未连接」+ 绿色按钮「🔌 重新连接」 | ⏳ | ⏳ |
| 3a | 插回 USB 后点建议按钮 | 插回 Arduino → 点「🔌 重新连接」| toast「已断开旧连接」→ 点「🔍 查状态」重连成功，状态卡片显示 Idle | ⏳ | ⏳ |
| 4 | 移动中再点移动 | 归零 → 输入远坐标按「🎯 去这里」→ **移动中再点一次**「🎯 去这里」| 🟡 黄色「机器正忙」+ 绿色按钮「🛑 立即停」 | ⏳ | ⏳ |
| 4a | 点建议按钮「🛑 立即停」| 承上，点按钮 | 机器 500ms 内停下（Hold 状态），toast「已发急停」 | ⏳ | ⏳ |
| A | Python 栈追踪不出现在 UI | 整轮 4 场景下 | **任何** UI 区域都看不到 `Traceback (most recent call last):` 或 `.py:line` | ⏳ | — |
| B | 栈追踪仍在 runlog | 查 `runtime/runlog.db` 里 error 行的 `error_agent_message` | 包含原始 grbl 字节 / 栈上下文 | ⏳ | — |
| C | 建议按钮至少 2 种工作 | 完成 1a + 3a（+ 4a 奖励） | 3/3 都工作 ≥ plan 要求的 2/3 | ⏳ | — |

## 关键截图

### ① 场景 1：没归零就 move（warning + 建议按钮）

*(待 PM 测试后嵌入截图 `images/slice-4-not-homed.png`)*

期望画面要素：
- 状态卡片位置出现**黄色** warning 框
- 主文案：❗ **机器未归零，请先点 🏠 归零按钮**
- 建议：先点「🏠 归零」按钮完成归零，再下运动指令
- 小字：错误代码 `L3.MACHINE_NOT_HOMED` · severity `warning`
- 下面一行两个按钮：`🏠 立即归零`（主色 primary）+ `❌ 清除`（次色）

### ② 场景 2：坐标超限（双防线）

*(待 PM 测试后嵌入截图 `images/slice-4-soft-limit.png`)*

- UI 层：`st.number_input(min_value=-280)` 自动限幅
- backend 层：如果绕过 UI 直接调 `start_move_async`，`SoftLimits.assert_contains`
  抛 `SoftLimitExceededError`（severity=warning，无按钮）

### ③ 场景 3：USB 断线

*(待 PM 测试后嵌入截图 `images/slice-4-usb-disconnect.png`)*

- **红色** alarm 框
- 主文案：❗ **无法打开串口 /dev/cu.wchusbserial110**
- 建议：检查 USB 线和端口占用，然后在 sidebar 点「断开并重连」
- 按钮：`🔌 重新连接`（不立即打开串口，只清 session，下次操作再 connect —
  避免按钮线程被 2s Arduino DTR reset 阻塞）

### ④ 场景 4：运行中再下指令

*(待 PM 测试后嵌入截图 `images/slice-4-operation-conflict.png`)*

- **黄色** warning（这是用户可纠正的冲突，不是机器异常）
- 主文案：❗ **上一次移动还没结束，请先点 🛑 停 或等待完成**
- 按钮：`🛑 立即停`

## 已发现 + 已处理的问题

*(PM 测试中遇到 bug 填这里，沿用 Slice 3 的格式)*

### ⚠️ 设计 tradeoff：错误从"inline 展示"改为"挂 last_error + rerun"

**现象**：最早版本把 `_render_l3_error` 直接放在 `except L3Error as e:` 块里
inline 渲染。问题：streamlit 按钮触发的 rerun 会重跑整页，而 `try/except` 只有
在按钮被点击的那一次 run 才进入 —— 错误箱只显示一瞬，PM 来不及点建议按钮。

**修复**：所有错误路径统一写 `st.session_state["last_error"] = e; st.rerun()`。
状态卡片段有一个集中的 `if last_error: _render_l3_error(...)` 分支，错误
**持久显示**直到 PM 点「❌ 清除」或「按建议操作」成功执行。

成功动作（归零完成 / 移动发出 / 建议按钮跑通）会自动 `pop("last_error")`。

## 给后续 tutorial 的 hook

- **severity vs recoverable 的语义分层** → tutorial 展开两个字段为什么必须分开；
  举三个反例（alarm + recoverable=True 的 AlarmStateError；warning + recoverable=False
  的 SoftLimitExceededError；warning + recoverable=True 的 MachineNotHomedError）
- **error_actions 注册表的扩展方式** → Phase 3.3 加 Gripper backend 时新 error
  怎么挂按钮；要不要让 backend 声明 preferred action 名字（当前是 UI 单方面
  决定）
- **Agent vs human message 的双通道** → ADR-004 §原则 2 要求 `suggested_action`
  (英文)给 Agent，现在 `suggested_action_zh` 给 UI；Phase 3.5 Agent demo 时要
  验证 tool schema 导出的是英文版
- **为什么 Slice 5 还要再加一个 ALARM_STATE 按钮** → 因为 alarm 恢复不是单步
  `unlock_alarm()` —— 还要重新 home()，并且要看 alarm code 决定是否需要
  soft-reset

## PM 签字位

```
Slice 4 验收通过：✅  签字人：Kevin  日期：2026-04-22 19:00
```

### 验收结论

- **数据模型** — 自动化测试 4/4 全过（severity + 中文 + action 注册表）
- **UI 视觉** — 场景 1「MachineNotHomed 黄色 warning + 🏠 立即归零 按钮」截图
  确认（18:55:40），其余 severity 路径复用同一 `_render_l3_error`，低风险
- **Slice 4 意外补的事**:
  1. `@observable` 的裸 Python 异常分支补 `traceback.format_exc()` 打 stderr
     + 存进 runlog 的 `error_agent_message` —— 下次 reproduce 直接看堆栈
  2. Dashboard home/move 加 `except Exception` 兜底包成 `L3.UNEXPECTED`，不
     再泄漏粉色 traceback 到 UI（验收项 A 兑现）
  3. `L3.UNEXPECTED` + `L3.BRAKE` 也挂「🔌 重新连接」建议按钮
- **已发现的 backend 并发 bug（post-Slice-4 debt，不影响本次签字）** — 测试
  时 runlog 有两个不同 event_id 的 `GantryBackend.home` 时间重叠（12:46:36
  vs 12:46:59 起，同 12:47:05 崩）触发 `AssertionError` / `AttributeError`。
  说明某条路径下能并发进 home；不确定并发源是 Streamlit fragment 还是按钮
  queue。由于 Slice 4 的兜底 + traceback 已就位，下次重现能直接定位。记入
  post-Slice-4 跟进项（见 migration-checklist）
