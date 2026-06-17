# ADR-006：Agent 工具颗粒度 —— 两层工具面（原子设备工具 + 协议编排工具）

- **日期**：2026-06-17
- **状态**：已决定，**Phase 3.5 / 迁移 Step 5 接 Agent 时按此建工具**
- **决策人**：Kevin（PM）+ Claude
- **影响范围**：`src/agent/tools.py`（未来在 Pi 上）的工具设计、所有后续 MCP tool 的增删、首个 Agent demo 的形态
- **关联**：[ADR-003](ADR-003-agent-first-vision.md) Agent-first 愿景 · [ADR-004](ADR-004-l3-api-design-principles.md) L3 API 规范 · [ADR-005](ADR-005-claude-agent-sdk-for-l3.md) §"Tools 白名单规则"（本 ADR 是它的展开）· [Issue #027](../issues/027-vision-alignment-backend.md)（粒度讨论起源）· 师兄 `protocol/` 编排层

---

## 背景

"Agent 的 tools / MCP 颗粒度要到什么程度" 是反复出现的核心设计问题。两个极端：

- **高层**：工具 = 已编排好的整套实验流程，Agent 只传参（`run_perovskite_experiment(rpm=4000, 反溶剂延迟=22s)`）。
- **低层**：工具 = 更底层、更多样的原子动作，Agent 自己组合（`move_to` / `dispense` / `spin` / …）。

ADR-005 §"Tools 白名单规则" 已定了**中间档（设备语义级）**并划了硬底线（通道/字节级永不暴露）。Issue #027 把问题正式表述为"低层原子 vs 高层复合"。

**新情况**（2026-06-17）：师兄全栈仓库已经把"高层"那档造出来了——`protocol/`（schema→validator→simulator→compiler）能把一份结构化协议编译成 maestro 任务序列。所以现在不再是"二选一"，而是"两层都有了，怎么暴露给 Agent"。本 ADR 把完整答案定下来。

## 三个层级

| 层级 | 长什么样 | 结论 |
|---|---|---|
| ① **整实验级**（最高） | `run_perovskite_experiment(params)` 一个工具跑完整套 | 不作为唯一入口（见"为什么"） |
| ② **设备语义级**（中间）⭐ | `gantry_move_to` / `gripper_open` / `dispense` / `spin` / `set_temp` / `get_status` | **工具的主体**：`<设备>_<动作>`，动作是设备层面的（"开夹爪"），不是通道层面的（"CH1 上电"） |
| ③ **通道/字节级**（最低） | `relay_ch_on(1)` / `send_raw_gcode("$J=...")` | **永不进白名单**（ADR-005 hard rule）：一次幻觉 = 物理损坏 |

## 决策：两层工具面（Two-Tier Tool Surface）

给 Agent **两层工具，让它自己选**，二者都建立在重构后合规的 L3 backend 之上：

### Tier 1 — 原子设备工具（地基）
设备语义级原子动作，~8-10 个。Agent 把它们**自己组合**成流程。

> `get_status` · `home` · `move_to` · `gripper_open` / `gripper_close` · `dispense` · `spin`(按 recipe 曲线) · `set_temp` · `recover_from_alarm` · （未来）`measure_z_offset` / `vision_locate_sample`（见 [Issue #027](../issues/027-vision-alignment-backend.md)）

**用途**：探索、出错恢复、临时步骤、"看一眼台面再决定"。**这是 Agent 秀自主推理的地方。**

### Tier 2 — 协议编排工具（高阶 skill）
两个工具，内部走师兄 `protocol/` 链路：

> `dry_run_protocol(protocol)` → 走 validator + simulator，返回逐步预览，不动硬件
> `run_protocol(protocol)` → validator → compiler → maestro 执行

**用途**：可靠、便宜地跑成套实验（一次调用跑完 18 轮），自带校验 + dry-run 安全。

**关键约束**：传给 Tier 2 的 `protocol` **必须由 Agent 自己组合产出**（把"做钙钛矿薄膜"翻译成结构化协议），**不是**填一个固定模板的空。**这是"花哨表单"和"真 Agent"的分界线。**

Agent 的工作方式：跑日常成套实验 → 自己写协议走 Tier 2（高效、可复现）；遇到新情况/异常/探索 → 用 Tier 1 原子工具（灵活、可推理）。这正是 "mcp/tools/**skills**" 里 tools=原子、skill=protocol 封装的落地。

## 为什么

1. **不能只给高层（整实验级）**：项目卖点是"AI 自己跑实验"。只给 `run_perovskite_experiment(params)`，AI 就退化成一个花哨表单——把 LLM 换成 input 框效果一样，智能化的 wow 没了。自主拆解大白话→动作序列，只有原子工具给得出。
2. **不能给低层（通道/字节级）**：Agent 幻觉出 `ch=2` 会关 Z 刹车、乱发 gcode 会撞机，一次错就物理损坏。ADR-005 已把这层划进"永不暴露"，本 ADR 重申。
3. **要两层并存**：纯原子让 Agent 一步步发几百个调用跑 18 轮，又贵又脆、易漏安全步骤；纯高层没自主度。两层并存让 Agent 按场景选最合适的颗粒度。

## 颗粒度尺子（判断每个工具该多粗）

> **暴露到"一个熟练实验员会思考、会下口令"的层级；凡是实验员根本不会去想的东西（Z 刹车时序、gcode、继电器通道号、Modbus CRC），全部藏进工具内部。**

**典型反例 = Z 刹车**：绝不能把"开刹车/关刹车"做成两个工具让 Agent 自己记得配对——一旦忘了顺序或时序贴太近，就是 [Issue #025](../issues/025-z-brake-emi-usb-reset.md) 的 EMI 死锁。正确做法：刹车释放/锁定**焊死在 `move_to`/`move_z` 内部**，Agent 根本看不到刹车存在。

**推论**：任何"安全关键的复合时序"（刹车、归零的 Z-先-XY-后、急停清场）都必须封装进单个设备语义工具内部，**不暴露成需要 Agent 正确排序的多个步骤**。

## 边界（hard rules，复述 + 强化 ADR-005）

- **永不暴露给 Agent**：`send_raw_gcode`、`relay_ch_on/off`（raw 通道）、任何直写 `/dev/*`、任何改 `constants.yaml` 安全边界的函数。
- **灰区（PreToolUse hook 拦截确认）**：`move_to` 目标超出历史常用工作区、首次执行新 recipe（见 ADR-005 §灰区）。
- **安全边界仍在 backend 层 raise**（ADR-004 §原则 5）：hook/白名单是优化，不是唯一防线。

## 与现有架构的接法

- Tier 1 工具 = 薄 `@tool` 包一层（重构后合规的）`GantryBackend`/`GripperBackend`/`SpincoaterBackend`/… 的设备语义方法。
- Tier 2 工具 = 薄 `@tool` 包师兄 `protocol/` 的 `validate_protocol`/`simulate_protocol`/`compile_protocol` + maestro。
- 两层都**不需要改 L3/protocol 内部**，只是注册到 ADR-005 的 in-process MCP server。印证 ADR-003"加硬件 = 加 backend + tool"的复用预期。
- **前置依赖**：Tier 1 要先把师兄 `xyz_stage.py` wrapper 塌缩成 bool 的咽喉修掉（见交接文档 §8 / 迁移文档 Step 4），否则工具拿不到结构化结果/错误。

## 实施（→ 迁移 Step 5 / phase-3.5-plan）

1. 先建 **Tier 1** 那 8-10 个原子工具 → 接重构后的 L3 backend → demo："大白话 → AI 自拆解 → 真机跑"。
2. 再加 **Tier 2** `dry_run_protocol` / `run_protocol` 包师兄 compiler → demo："跑完整钙钛矿配方"。
3. 落在 Pi 上 `src/agent/tools.py`（按 ADR-005 用 Claude Agent SDK `@tool` + `create_sdk_mcp_server`）。

## 回滚 / 演进条件

- 若实测发现 Agent 用原子工具组合长流程**太贵/太不稳**，把更多常用复合动作下沉成 Tier 1.5 的"小 skill"工具（如 `pick_sample(from)` = home+move+gripper 复合），但仍由 backend 封装、不暴露内部时序。
- 若将来 Agent 模型强到能可靠直接编排几百步，可弱化 Tier 2——但**当前不认为会出现**。

## 参考

- ADR-005 §"Tools 白名单规则"（本 ADR 的母条款）
- Issue #027（粒度讨论起源 + 视觉工具如何按本 ADR 接入）
- 师兄 `protocol/README.md`（Tier 2 内部链路）
- 用户 `agent_smoke.py`（已验证的 Tier 1 工具雏形：home/move/gripper 真机 tool-use）
