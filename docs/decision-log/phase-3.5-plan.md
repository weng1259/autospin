# Phase 3.5 施工计划：Agent-ready L3 API + 第一次 Agent 真跑

- **日期**：2026-04-22（原为 phase-3.2-plan，2026-04-22 晚方向重排后改名）
- **状态**：待 Phase 3.2/3.3/3.4 稳定底座完成后才开工
- **关联**：[ADR-003](ADR-003-agent-first-vision.md) Agent-first 愿景 · [ADR-004](ADR-004-l3-api-design-principles.md) L3 API 规范 · [ADR-005](ADR-005-claude-agent-sdk-for-l3.md) 选 Claude Agent SDK · [phase-3.1-plan](phase-3.1-plan.md) 前一 Phase 的纵向切范本
- **适用记忆**：
  - `feedback_vertical_slices_ui_validation.md` — 每个里程碑 PM 点按钮/聊天验收
  - `feedback_streamlit_module_cache.md` — 改 src/* 必须重启 Streamlit
  - `feedback_verification_vs_tutorial.md` — 验收日志 ≠ 教程文档
  - `feedback_agent_first_scope_discipline.md` — 为 Agent 铺路 ≠ 传统后端工程

---

## 一句话目标

在 2 天内，让 PM **能用中文指挥机器** ——从命令行到 Streamlit 浏览器，Claude
看懂中文，自己调 L3 API 真动机器，全程不需要 PM 写任何代码或点按钮。

## 背景：两次方向重排

**第一次重排（2026-04-22 上午）**：原 Phase 3.2 是"传统后端打磨（mypy / unit
test / dry-run / schema / 幂等 / 状态查询不阻塞）"，当时 PM 纠正我"为 agent
架构做准备肯定是走 mcp/tools/skills 方向"，于是 Phase 3.2 被改成 Agent SDK
集成，原 Phase 3.5 的 Agent demo 被合并进 3.2。

**第二次重排（2026-04-22 晚）**：架构审阅后 PM 决定**拆回去**——Phase 3.2
回归"后端打磨"（让 API 稳定），Phase 3.5 重新立起来做 Agent demo。理由：
- Agent 上场前 API 的类型/错误/幂等/schema 应该先固定，否则 Agent 会在
  不稳定底座上连环决策
- Phase 3.1 5 Slice 刚收尾，紧接着开 Agent 节奏过满，不如先打磨稳再上 Agent
- 架构审阅发现的多条问题（dry-run、canUseTool 确认兜底、白名单规则）之所
  以在"Agent 在 3.2 上场"的假设下是刚性问题，推回 3.5 后转为"届时再解决"

**本文档（phase-3.5-plan）是第一次重排时写的 Agent 施工计划全文**，拆回
去后仍然有效——把 Phase 编号从 3.2 改成 3.5 即可。Slice A/B/C/D 内容、
ADR-005 选型、Claude Agent SDK 集成方式都不变。

**进入 Phase 3.5 的前提**：Phase 3.2（后端打磨）+ Phase 3.3（Gripper/Relay
backend）+ Phase 3.4（端到端 smoke）全部完成。

### 2026-04-23 增量 Agent smoke 方案（对本 Phase 的重要影响）

Phase 3.2/3.3/3.4 各自**末尾加 1-2 小时 Agent smoke**，累积到
`tools/spikes/agent_smoke.py`：

| Phase | smoke 加的工具 | 演示维度 |
|---|---|---|
| 3.2 | `gantry_get_status` / `gantry_home` / `gantry_move_to` | 状态翻译 / 避免盲目 / 组合指令 |
| 3.3 | `gripper_open` / `gripper_close` | 跨 backend 协同 |
| 3.4 | `maestro_pick_and_place` | 工作流级别"像实验助理" |

到本 Phase 开工时，SDK 集成 / `@tool` 从 pydantic 导 schema / 授权 / async
bridge / 错误透传格式都**已经踩过坑**，本 Phase 不再"首次上场"。

**对本文档内容的影响**：
- **Slice A 前置 P2** "跑 `examples/mcp_calculator.py` 验证 SDK"**已经在 3.2
  smoke 里做过**，可以跳过
- **Slice A** "第一个 @tool" 的新鲜感降低，改为"从 agent_smoke.py 正式迁
  移到 src/agent/"——代码层面是 refactor 而非 greenfield
- **Slice B** "加 home / move_to / halt / recover_from_alarm 四个 tool"
  其中 home / move_to 已在 smoke 有了，本 Slice 主要是加**刻意没加的**
  `halt` / `recover_from_alarm`
- **Slice C 故障自恢复**不变（需要 recover_from_alarm tool 加完才能演）
- **Slice D Streamlit 集成**不变（smoke 只有 CLI，Streamlit 集成是本 Phase
  的独有价值）

## 前置准备（~半天，扩容自原 30 分钟——2026-04-22 架构审阅追加）

### P0. 硬件稳定性 smoke（~30 分钟，D 组）

**为什么现在做**：ADR-003 把底层稳定性放最高优先级，但 Phase 0 测试 B 和
EEPROM 冷启验证一直"延期"。Slice B/C 让 Agent 真动机器，一旦传感器 / ALM
/ `$5` 腐蚀之类的问题现场冒出来，Agent 会在错误状态上连环决策。**先把硬件
底座坐实再让 Agent 上场**。

- [ ] **Phase 0 测试 B**：拔 X 轴 A+ 相线 → grbl 应该通过 D31 读到 ALM 并
      进 `ALARM:1`（驱动器面板红灯闪 6 次）。接回相线后 `$X` 清 alarm。
      详见 `docs/guides/02-电控接线.md §27`
- [ ] **冷启 10 次 `$5` 验证**：断电 → 通电 → USB 连 → `$$` 查 `$5`，
      确认值仍为 1（NPN 反相）。任一次为 0 就是 EEPROM 腐蚀，**Slice A 暂
      停**，先修 EEPROM 问题（改 grbl defaults.h 把 `$5=1` 硬编译进去，
      或走 Mega EEPROM 更换流程）
- [ ] 10 次激进 jog（`$J=G91 X5 F3000` 往复 10 次）验证 `Pn:` 仍然 0 次触
      发。命中一次就停下排查传感器 5V 线

### P1. SDK 依赖 pin（~5 分钟，A4）

- [ ] 确认 `tools/spikes/.venv/bin/pip install -r requirements.txt` 一次性
      装好（2026-04-22 已写 `requirements.txt` pin `claude-agent-sdk==0.1.65`）
- [ ] 跑 `pip freeze | diff - requirements.txt`，无额外 ≠ 差异
- [ ] **任何 SDK 升级**都必须同步跑 Phase 3.1 Slice 5 的 `slice5_automated_acceptance.py` 回归

### P2. 授权 + 骨架

- [ ] 确认 ANTHROPIC_API_KEY 环境变量可用，或者 `claude` CLI 已 `claude login`
      订阅授权（两种方式任选其一；PM 告诉我用哪个）
- [ ] `src/agent/` 目录新建骨架：`tools.py` / `hooks.py` / `client.py` / `__init__.py`
- [ ] `demos/agent_chat.py` 空白骨架
- [ ] 跑 `examples/mcp_calculator.py`（bilibili 仓库里的）确认 SDK 能在
      本机跑起来（Anthropic key 可用）

**PM 验收前置**：我跑通 `mcp_calculator.py` 的截图发你，你看到 Claude 用
自定义 add/subtract 工具算 15+27=42，即可。

---

## Slice A：查状态按钮 → Agent 第一次真跑（~半天）

### PM 能做什么

命令行跑：`tools/spikes/.venv/bin/python demos/agent_chat.py`

进入交互：
```
You: 帮我查一下机器现在是什么状态
Claude: 让我调用 get_status 工具查看...
        [tool_use: mcp__gantry__gantry_get_status]
        [tool_result: state=idle, position=(-80, -80, -5), homed=True]
        机器当前处于 Idle 状态，位置 X=-80 Y=-80 Z=-5（mm），已归零。
You: /quit
```

### 背后的技术内容

- `src/agent/tools.py`：第一个 `@tool` —— `gantry_get_status`
  - args schema：空（get_status 不需要参数）
  - wrapper：调 `backend.get_status()` → 转成 JSON-safe dict → return
    `{"content": [{"type": "text", "text": "..."}]}` 格式
- `src/agent/client.py`：一个 `build_client()` 辅助函数，包含：
  - `create_sdk_mcp_server(name="gantry", tools=[gantry_get_status])`
  - `ClaudeAgentOptions(mcp_servers={...}, allowed_tools=["mcp__gantry__gantry_get_status"], system_prompt=...)`
  - 系统提示初稿：中文 + 说明"你是旋涂仪控制 agent，可调工具见 MCP server，
    用户可能用中文指令"
- `demos/agent_chat.py`：简单 REPL，接收用户输入 → `client.query(prompt)` →
  流式打印 `AssistantMessage` 的 TextBlock + ToolUseBlock + ToolResultBlock
- **backend 生命周期**：`build_client()` 内部 lazy 连接 `GantryBackend`
  （第一次工具调用时 connect），chat 结束时 close

### Slice A 附加（B 组——2026-04-22 架构审阅追加）

- [ ] **runlog 加 `tool_use_id` 字段**：`src/runlog.py` Event schema + 迁移
      sqlite（alter table 加一列，可空）。tool wrapper 从 `ClaudeSDKClient`
      的 `ToolUseBlock` 取 `.id` 通过 contextvar 传进 `@observable`，让
      agent 对话和 runlog 事件能一对一回溯
- [ ] **错误语言约定写进 `src/hardware/errors.py` docstring**：
      - `human_message` = **中文**（Streamlit 卡片、PM 看）
      - `agent_message` = **英文**（LLM 原生训练语言，推理更稳）
      - `suggested_action_zh` = **中文**（UI 展示建议动作）
      新写的 tool wrapper 返回 error 时按此区分

### PM 验收清单

- [ ] `demos/agent_chat.py` 能启动（Claude CLI 正常，无 auth 错）
- [ ] 输 "查状态"，Claude 调 `gantry_get_status` 工具，中文回复状态摘要
- [ ] 故意拔 USB 再输 "查状态"，Claude 收到 ConnectionError，用中文告诉
      PM "USB 未连接" + 建议"检查线/重连"（verify L3Error 的中文 message
      能透传给 Agent）
- [ ] 查 `runtime/runlog.db`，`gantry_get_status` 的 event 有完整记录

### 失败回滚

- 纯新增代码（`src/agent/*` + `demos/agent_chat.py`），回滚 `git reset HEAD^`
- 不影响 Streamlit 应急面板和任何现有 backend 方法

### PM 签字位

```
Slice A 验收通过：□  签字人：_______  日期：_______
```

详细验收记录：`docs/verification/phase-3.5-slice-a.md`（开工时创建）

---

## Slice B：指挥机器动（~半天）

### PM 能做什么

命令行聊天，可以下**组合指令**：
```
You: 归零然后移到 X=-50 Y=-50 Z=-5
Claude: 我先调 home 工具归零...
        [gantry_home(idempotency_key="xxx")] → 成功，30.5s
        然后调 move_to 移到指定坐标...
        [gantry_move_to(target=...)] → 成功，2.1s
        已归零并到达目标位置。
```

或：
```
You: 把头放到工作台左上角
Claude: 工作台的工作空间是 X ∈ [-280, 0] Y ∈ [-280, 0]，左上角
        我理解为 X=-280 Y=0 Z=0，先归零...
        [tool_use chain...]
```

### 背后的技术内容

- `src/agent/tools.py` 追加 4 个 `@tool`：
  - `gantry_home(idempotency_key)` → 调 `backend.home(...)`
  - `gantry_move_to(x_mm, y_mm, z_mm, feed_mm_min=2000)` → 构造
    `Position(...)` 调 `backend.move_to(...)`
  - `gantry_halt()` → 调 `backend.halt()`
  - `gantry_recover_from_alarm(idempotency_key)` → 调 `backend.recover_from_alarm(...)`
- **L3Error 转换**：每个 tool wrapper 统一 catch `L3Error`，返回
  `{"content": [{"type": "text", "text": err.human_message + "\n建议：" + err.suggested_action}], "is_error": True}` —— Agent 收到 `is_error` 会自己决定是否重试
- `src/agent/hooks.py`：PreToolUse hook —— `gantry_move_to` 之前检查
  `backend.is_homed()`；未归零时 deny 并附
  `"permissionDecisionReason": "Machine not homed; call gantry_home first."`
  **注意（2026-04-22 架构审阅 A1）**：这是**可用性优化**（省一轮 LLM），
  **不是安全防线**。backend 层 `move_to()` 内部**仍然必须** raise
  `MachineNotHomedError`，hook 只是提前告知
- `build_client()` 的 `allowed_tools` 白名单扩充到 5 个工具。**对照 ADR-005
  §白名单规则**：`send_raw_gcode` 不在白名单
- **高风险移动确认（A5——2026-04-22 架构审阅追加）**：`canUseTool` callback
  判断 `gantry_move_to` 目标是否在"历史常用工作区"外——初版硬编码：
  - Z 跨度 > 当前位置 ±10mm → 弹确认
  - X/Y 跨度 > 当前位置 ±100mm → 弹确认
  - 坐标绝对值 > `constants.yaml.workspace_safe_*` → 弹确认
  命中任一条时 `canUseTool` return `deny_with_confirm`，UI 弹"Claude 要移到
  X/Y/Z，确认？[确认] [取消]"（Slice D 在 Streamlit 里接；Slice B CLI 模
  式下直接 raw input Y/N）
- `system_prompt` 补上：工作空间范围、Z 刹车自动时序、recover 什么时候用

### PM 验收清单

- [ ] "归零" → 机器真归零
- [ ] "移到 X=-50 Y=-50" → 机器真移动
- [ ] **"归零然后移到 X=-50 Y=-50"** 单条指令 → Claude 自动按序调 2 工具
- [ ] **移动中**输入 "停" → Claude 调 halt，机器停在当前位置保留归零
- [ ] **未归零**时输入"移到 X=-50" → PreToolUse hook deny，Agent 收到拒绝
      理由"先 home"，自己改策略先 home 再 move_to（不需要 PM 手动提示）
- [ ] **高风险移动确认**："移到 X=-250 Z=-80"（大跨度）→ `canUseTool`
      弹确认，PM 不点无法继续；PM 点"取消"后 Agent 收到 deny，改用中文
      询问 PM 确认意图
- [ ] **Hook 绕过等价性测试**（A1 防线验证）：直接用 Python REPL 调
      `backend.move_to(...)` 时未归零，**backend 本身** raise
      `MachineNotHomedError`（证明 hook 不是唯一防线）
- [ ] 全程 runlog 完整，每条事件有 `tool_use_id`

### PM 签字位

```
Slice B 验收通过：□  签字人：_______  日期：_______
```

---

## Slice C：故障自恢复（~半天）

### PM 能做什么

PM 故意把机器整进 alarm 状态（推硬限位 / bCNC 发 `\x18`），然后下指令：
```
You: 把头移到 X=-100
Claude: 我先查状态...
        [gantry_get_status] → state=alarm, alarm_code=1
        机器处于 Alarm 状态（硬限位触发），需要先恢复。
        [gantry_recover_from_alarm(...)] → 成功，42s
        机器已恢复到 Idle。现在移到目标位置...
        [gantry_move_to(...)] → 成功
        已到达 X=-100。
```

### 背后的技术内容

- 无新 backend 代码（recover_from_alarm 已在 Phase 3.1 完成）
- `system_prompt` 强化 recover 的使用时机：
  - 看到 AlarmStateError → 自己调 recover_from_alarm，**不要问 PM**
  - 看到 ConnectionError → 不能 recover，报告 PM 手动重连（区分"机器侧可恢复"和"链路侧需人工"）
  - 看到 SoftLimitExceededError → 不要 recover（机器没错，是目标错），直接报告 PM 并建议调整坐标
- `src/agent/hooks.py` 可能加：`PostToolUse` hook 记录 Agent 触发过的 recover
  次数（给 PM 一个"今天 Agent 自救几次"的摘要）
- 细节：Anthropic Claude 通常能从 `is_error=True` 的 tool result 里的
  `human_message + suggested_action` 正确决策，系统提示只是加强

### PM 验收清单

- [ ] 机器在 Alarm 状态下输 "移到 X=-50"，Agent 自动：
      ① 查状态 → ② recover → ③ move → ④ 告诉 PM 已完成
- [ ] 机器未连接时输 "移到 X=-50"，Agent 不试 recover，直接报告 PM
      "USB 未连接，请检查并重连"（不会无限重试）
- [ ] 输 "移到 X=-500"（超限），Agent 不试 recover，直接告诉 PM 范围并
      请求调整（这是 Slice 4 `L3.SOFT_LIMIT_EXCEEDED` 的 Agent-facing
      行为验证）
- [ ] 查 runlog：看到 Agent 触发的 recover_from_alarm 事件 + 各步骤

### PM 签字位

```
Slice C 验收通过：□  签字人：_______  日期：_______
```

---

## Slice D：Streamlit 里嵌入 Agent 聊天（~半天）

### PM 能做什么

浏览器打开 `localhost:8501`，除了之前的应急按钮板块，**底部新增一个 "🤖 Agent
聊天" 板块**：
- 一个聊天输入框（`st.chat_input`）
- 历史聊天记录（`st.chat_message`）
- 每次 Agent 调工具时显示展开式 expander（"Claude 正在调 gantry_move_to..."）
- 右上显示本次对话累计 cost（美元）

PM 可以：
- 在聊天框里用中文指挥机器，同时看到下方的状态卡片 / 实时位置 fragment
  反映机器真动
- 如果 Agent 卡住，右上角永久按钮「🛑 停」依然工作（应急路径和 Agent 路径并行）

### 背后的技术内容

- `tools/ui/agent_chat_panel.py` 新建，包含：
  - `render_agent_chat()` 函数，render 到 `emergency_dashboard.py` 底部
  - `st.session_state["agent_messages"]` 持久化对话历史
  - `st.session_state["agent_client"]` 持久化 `ClaudeSDKClient` 连接（`async with` 改成 lifetime-managed）
  - 异步 bridge：Streamlit 的同步模型下跑 async SDK，用 `anyio.run` 或 `asyncio.run_in_executor` 桥接
- `emergency_dashboard.py` 底部加 `render_agent_chat()` 调用
- cost 追踪：读 `ResultMessage.total_cost_usd`，累加显示

### PM 验收清单

- [ ] 浏览器里能看到「🤖 Agent 聊天」板块
- [ ] 输入 "查状态"，返回正确，不影响上方状态卡片的实时刷新
- [ ] 输入 "归零然后移到 X=-50 Y=-50"，机器真动，同时实时位置 fragment
      同步显示移动过程
- [ ] **并发测试**：Agent 在跑移动命令时，PM 手动点应急 「🛑 停」 按钮，
      机器能停下（不冲突）
- [ ] 关闭浏览器再打开，聊天历史保留或清除（二选一都可，PM 定）
- [ ] cost 累计显示正确

### PM 签字位

```
Slice D 验收通过：□  签字人：_______  日期：_______
```

---

## 整体验收（Phase 3.5 结束标志）

Slice A-D 都签字后，**端到端剧本**：

PM 在 Streamlit 聊天框输入一条**模糊指令**：
> "把头放到工作台左下角然后升起来 20mm"

期望 Agent：
1. 查当前状态
2. 理解"左下角"是 X=-280 Y=-280（或按系统提示的约定），Z 升起来 = Z=-10 之类
3. 判断是否需要归零
4. 先 home（如果未归零）
5. move_to(-280, -280, -10)
6. 中文报告"已到达左下角，Z 抬升 20mm"

**Phase 3.5 只有这一条路径通过才算结束**。

## 时间预算 + 风险

| 项 | 耗时 | 风险 |
|---|---|---|
| 前置准备 | 30 min | SDK auth 问题（API key 或 claude login） |
| Slice A | 半天 | `@tool` decorator 类型映射可能有坑（Position 是 pydantic，不是基础类型） |
| Slice B | 半天 | PreToolUse hook 的 is_homed 查询时序（hook 里能不能拿到 backend 实例） |
| Slice C | 半天 | 系统提示调参——Agent 可能一开始过度 recover 或不肯 recover |
| Slice D | 半天 | Streamlit 的 sync 模型和 SDK 的 async 模型 bridge |
| 整体剧本 | 半天（含反馈打磨） | Agent 对"左下角"这种模糊语义的解析 |
| **合计** | **2-2.5 天** |  |

**已知坑**：
- Streamlit 的 `session_state` 跨 rerun 持有 `ClaudeSDKClient` —— 需要小心
  生命周期（不能每次 rerun 都重开 client）
- Claude Code CLI 首次启动 ~1-2s 延迟，Slice A 第一次对话会有等待（PM 要
  有预期）
- Anthropic API 费用——每条对话约 $0.01-0.03（取决于工具调用次数和模型），
  2 天 demo 总成本估计 < $5

## 原则约束

- 每个 Slice 一个独立 git commit
- Slice 签字前不进下一个
- **验收文档轻量**（沿用 `feedback_verification_vs_tutorial.md`）——
  Slice A-D 只留 `docs/verification/phase-3.5-slice-*.md` 的 6 段验收记录，
  完整教程留 Phase 3.4 末尾
- **backend 代码不要为了 Agent 大改**——如果某个 Agent 行为不好，优先改
  tool wrapper / system prompt / hooks，不改 `GantryBackend`
- **Agent 犯错的每一条都记进 `docs/decision-log/agent-fails-log.md`**——这
  是未来 Phase 4+ 迭代系统提示和 hooks 的训练数据

## PM 最终签字

我（PM）批准按本计划执行 Phase 3.5：

```
签字：_______  日期：_______
```

---

## 计划外 / 延期到 3.3 / 3.4 的

- **Gripper/Relay 的 `@tool` 封装**——等硬件 + Phase 3.3
- **mypy --strict / unit test 覆盖率**——Phase 3.4 顺带跑
- **dry-run 支持**——Phase 4 旋涂 recipe 时再加（ADR-004 §原则 7）
- **系统提示的"中文 + 英文 + 多轮任务"调优**——Phase 3.5 跑完第一版，Phase 4+
  持续迭代
- **Subagents（`ClaudeAgentOptions.subagents`）**——Phase 4+ 如果单 agent
  搞不定复杂工作流再启用
- **用 Claude Desktop / Cursor 作为 client 连 MCP server**——目前 in-process
  够用；如果未来要多端使用，用 ADR-005 §方案 B 的"把 `@tool` 函数导出成
  独立 MCP server"的退路
