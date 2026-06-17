# 架构路线图：从现在到产品成型

- **日期**：2026-04-22
- **目的**：一页纸讲完整个项目的终态架构 + Phase 3.2 → 5 的完整 roadmap
- **读者**：新会话的 Claude / 组员 / 未来的 PM 自己
- **状态**：活文档，Phase 完成时更新"✅"标记
- **关联**：[ADR-003](ADR-003-agent-first-vision.md) 愿景 · [ADR-004](ADR-004-l3-api-design-principles.md) API 规范 · [ADR-005](ADR-005-claude-agent-sdk-for-l3.md) Agent SDK 选型 · [migration-checklist.md](migration-checklist.md) 详细任务清单

> **怎么用这份文档**：新会话开场读 MEMORY.md 之后读这一份，就能看到"整个项目要去哪里 + 现在在哪里 + 下一步要做什么"的全貌。不需要拼着读 5 份 ADR。

---

## 终态架构分层图

```
╔═════════════════════════════════════════════════════════════════════╗
║                      ┌─ Agent 聊天（主入口，85%）                   ║
║    你用中文指令 ────┤                                              ║
║                      └─ 应急按钮（辅入口，10%）                     ║
║                      └─ 命令行 CLI（调试入口，5%）                  ║
╚═════════════════════════════════════════════════════════════════════╝
           │                                 │                 │
           ▼                                 ▼                 ▼
┌──────────────────────┐   ┌──────────────────────────┐  ┌──────────────┐
│  L6 Agent            │   │  L5 UI                   │  │  调试脚本     │
│  ─────────────       │   │  ─────────────           │  │              │
│  ClaudeSDKClient     │   │  Streamlit emergency_     │  │ demos/*.py   │
│    ├─ @tool 注册表   │   │  dashboard.py             │  │ verify_*.py  │
│    ├─ Hooks (安全守卫)│   │    ├─ 状态卡片            │  │              │
│    ├─ 系统提示       │   │    ├─ 归零 / move 按钮    │  │              │
│    ├─ Memory         │   │    ├─ 历史记录表          │  │              │
│    └─ Cost 追踪       │   │    └─ 🤖 Agent 聊天嵌入   │  │              │
└──────────────────────┘   └──────────────────────────┘  └──────────────┘
           │                                 │                 │
           └─────────────────┬───────────────┴─────────────────┘
                             ▼
┌────────────────────────────────────────────────────────────────────┐
│  L3 Python Orchestrator（产品本体，ADR-003）                       │
│  ─────────────────────────────────────────                         │
│  src/hardware/                                                     │
│    ├─ gantry_backend.py   ← Phase 3.1 ✅                           │
│    ├─ gripper_backend.py  ← Phase 3.3                              │
│    ├─ relay_backend.py    ← Phase 3.3（Z 刹车是 gantry 内部调用）  │
│    └─ spincoater_backend.py ← Phase 4                              │
│                                                                    │
│  src/spincoater/                                                   │
│    ├─ recipe.py (SpinRecipe pydantic model) ← Phase 4              │
│    └─ planner.py (dry-run + 时间预估)       ← Phase 4              │
│                                                                    │
│  src/agent/                 ← Phase 3.2                            │
│    ├─ tools.py  (@tool 装饰器包装每个 backend 方法)                │
│    ├─ hooks.py  (PreToolUse 安全守卫)                              │
│    └─ client.py (build_client(), ClaudeSDKClient 工厂)             │
│                                                                    │
│  src/ 公共基建（Phase 3.1 ✅）                                      │
│    ├─ runlog.py    (SQLite 事件记录)                               │
│    ├─ event_bus.py (内存 pub/sub)                                  │
│    ├─ observable.py (@observable 装饰器)                           │
│    ├─ config.py    (constants.yaml 加载)                           │
│    └─ maestro.py   (跨 backend 协同，Phase 4)                      │
└────────────────────────────────────────────────────────────────────┘
           │                        │                     │
  USB 串口 ①                USB 串口 ②              USB 转 RS485
           │                        │                     │
           ▼                        ▼                     ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────────┐
│  L1 grbl-Mega-5X │     │  DSTUR-T80       │     │  RS485 总线          │
│  (Arduino Mega)  │     │  (USB 继电器)    │     │  ├─ 夹爪 pymodbus    │
│                  │     │  CH1: 夹爪预留   │     │  └─ 旋涂模块 (P4)    │
│  管三轴运动       │     │  CH2: Z 刹车     │     │                      │
└─────────────────┘     └─────────────────┘     └─────────────────────┘
           │                        │                     │
           ▼                        ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  L0 物理硬件                                                     │
│  GH40 龙门架（XYZ 电机 + 限位传感器）                            │
│  + YJZK Y1 夹爪 + 旋涂模块（Phase 4）                             │
└─────────────────────────────────────────────────────────────────┘
           │
  运行平台：Mac 开发 (现在) → 树莓派驻守 (Phase 5)
```

**核心洞察**：**L3 是产品本体**。上面两条路通向用户（Agent 主 + UI 辅），下面三条路通向硬件。每新增一个硬件模块（旋涂 / 移液 / 未来扩展）只是在 L3 加一个 `*_backend.py` + 对应 `@tool` 暴露，L6 的 Agent 自动能用新工具。这就是 ADR-003 愿景的落地形状——做一个仪器就是做一个 Agent 可用的后端，整套架构可以反复复用。

---

## Phase 3.2 → 5 每个 Phase 的新能力

### Phase 3.2（2-3 天，排队中）—— API 稳定底座 + Agent smoke 1/3

**2026-04-22 晚方向重排后的 3.2**：不做 Agent 产品化，做**后端打磨**为主
+ **末尾 1-2 小时 Agent smoke**（增量验证）。按 ADR-004 七条原则的可验证
落地：schema 导出到 `docs/api-v1.json` 合同文件、`mypy --strict` 全绿、
错误覆盖 unit test、幂等测试自动化、dry-run v0（move_to/home 不动硬件返
回计划）、状态查询不阻塞。**末尾 smoke（必须过）**：`tools/spikes/agent_smoke.py`
暴露 3 个工具（get_status / home / move_to），CLI 真跑三个剧本（状态翻译 /
避免盲目归零 / 组合指令"归零然后移到 X=-50"）。**前提**：硬件 smoke（Phase 0 B
拔相 + 冷启 `$5` + 激进 jog `Pn:`）全过。

### Phase 3.3（2-3 天，要夹爪装好）—— Gripper/Relay + Agent smoke 2/3

GripperBackend + RelayBackend 两个纯后端实现，照搬 Phase 3.2 打磨套路。
**末尾 smoke 扩展（必须过）**：在 `agent_smoke.py` 加 `gripper_open` /
`gripper_close` 2 个工具，PM 剧本：
- "归零后把夹爪夹紧" —— 跨 Gantry/Gripper 协同
- "移到 X=-100 Y=-100 并打开夹爪" —— 组合指令升级

Z 刹车继续是 GantryBackend 内部调 RelayBackend，不暴露。

### Phase 3.4（1-2 天）—— 端到端 smoke + maestro + Agent smoke 3/3

写 `src/maestro.py` 的 `run_pick_and_place_cycle()` + 故障注入测试。**末尾
smoke 升级（必须过）**：在 `agent_smoke.py` 加 `maestro_pick_and_place` 1
个工具，PM 剧本：
- "把样品从 (-80, -80, -10) 搬到 (-200, -200, -10)" —— **一条中文指令走
  完整 workflow**

这是 Phase 3.5 之前最重要的"质变体验"——PM 第一次感受"像实验助理"。

### Phase 3.5（2-2.5 天）—— 首个完整 Agent Demo ⭐

**3.2/3.3/3.4 已累积 6 个 tools 的 CLI smoke**（get_status / home / move_to /
gripper_open / gripper_close / maestro_pick_and_place），踩完 SDK 集成 / 类
型映射 / 授权 / async bridge 的坑。本 Phase 做的是**产品化**：

- 从 `tools/spikes/agent_smoke.py` 正式迁移到 `src/agent/{tools,client,hooks}.py`
- **扩展工具集**（smoke 刻意没加的）：halt / recover_from_alarm / set_force 等
- **Streamlit 聊天面板**：浏览器入口（smoke 只有 CLI）
- **PreToolUse hooks 实装**：A5 canUseTool 高风险确认 + ADR-005 白名单守卫
- **系统提示调优 + cost 追踪**
- Slice A/B/C/D 详细见 [phase-3.5-plan.md](phase-3.5-plan.md)（原 phase-3.2-plan.md）

完成后：你打开浏览器，底部聊天框输入"把头放到工作台左下角然后升起来
20mm"，Agent 真动机器并中文报告。**Agent-first 愿景第一次产品级落地**。

### Phase 4（时间未定，要旋涂硬件到位）—— 完整旋涂流水线

这是项目的**真功能**：Agent 能读 recipe（"2000 rpm 30s，800 rpm 60s 烘干"），自动编排运动 → 旋涂 → 烘干 → 下一个样品。拆成：

- **4.1 SpincoaterBackend**：封装旋涂模块硬件通信（RS485 / USB 看具体型号）
- **4.2 Recipe 格式**：抄 PASCAL `frgpascal/hardware/spincoater.py` 的 recipe JSON 结构（rpm / 时长 / 加速度曲线）
- **4.3 Agent 理解 recipe**：系统提示嵌入 recipe 知识，Agent 能把"涂 PCBM 膜 2000 rpm 60s"翻译成具体 `spin_start(2000, 60)` 调用
- **4.4 dry-run 启用**：ADR-004 §原则 7 在这里真用上——recipe 是不可逆物理动作，dry-run 模式先把计划展示给人审批再执行
- **4.5 实验数据面板**：Streamlit 加"实验历史"板块，每次 recipe 跑完落 SQLite，可回溯"上周三那片膜的条件是什么"

**这时才有"产品"**——一个能自动重复旋涂的机器人。

### Phase 5（2-3 天，要树莓派到位）—— 搬家到树莓派驻守

从"每次你开 Mac 才能跑"变成"树莓派 7×24 在线，随时 SSH 或浏览器访问"：

- **5.1**：`claude-agent-sdk` + 所有依赖在 Pi 上装起来（arm64 wheel 兼容性验证）
- **5.2**：`systemd` 守护进程，开机自启、崩溃自重启
- **5.3**：局域网内浏览器访问 Streamlit、外网 SSH 调试
- **5.4**：日志轮转 + SQLite 备份策略（runlog.db 不能无限增大）
- **5.5**：断电恢复测试（Pi 重启后 Agent 能读 runlog 理解"上次做到哪了"）

### Phase 6 及以后（未定）—— 扩展其它设备

按 L3 架构模板加新设备：移液模块 / 热板 / 膜厚测量仪 / 显微镜 / …… 每加一个就是 `*_backend.py` + 对应 `@tool`，Agent 自动能用。这时真正验证了 ADR-003 的"整套架构可复用到其它仪器"——我们已经拥有一个"通用实验室 Agent 基座"。

---

## 三类交互场景的最终形状

按 ADR-003 的预算（Agent 85% / UI 10% / 应急 5%），Phase 5 之后具体落地：

**Agent（85%）** —— 你打开浏览器用中文对聊天框说话：
- 简单：查状态 / 归零 / 移动
- 组合：取样 + 旋涂 + 放回 + 换下一片
- 模糊："按昨天那个 recipe 再做一片"
- 意外："卡 alarm 了帮我恢复" / "最近三次实验的成膜厚度对比"

Agent 会：看系统提示了解仪器能力 → 查 runlog 看历史上下文 → 调 L3 tool 做事 → 遇错自恢复 → 用中文回报。

**UI（10%）** —— Streamlit 页面分三块：
- 顶部：**实时状态 + 应急按钮 + 历史表**（Phase 3.1 的 emergency_dashboard）
- 底部：**🤖 Agent 聊天 + cost 累计**（Phase 3.2 加）
- 右侧：**📊 实验数据**（Phase 4 加）

除了应急按钮外，UI 主要是**展示和监控**（看 Agent 在干啥）而不是操作。

**应急（5%）** —— 浏览器顶部的"🛑 停"、"🔧 清除并恢复"、"断开并重连"按钮永远工作。这些按钮存在就是告诉你"Agent 不是神，你永远有紧急制动权"。

---

## 关键边界 —— 明确不做的事

避免 scope creep。这些是**已经讨论过并拍板不做**的：

- **不自己造 GUI** —— Streamlit 丑但能用，美化是无底洞，Phase 5 后就锁定不改
- **不自己造 agent loop** —— Claude Agent SDK 的 `ClaudeSDKClient` 带 Claude Code 成熟 loop（系统提示 / 多轮 / planning / subagents），我们只提供工具和守卫。自己写 agent loop 等于跟 Anthropic 赛跑
- **不做多租户 / 多用户并发** —— 这是单机实验仪器，一个人用。加认证 / 权限 / 审计是浪费精力
- **不做 "generic lab robot framework"** —— PyLabRobot / PASCAL 那种目标是"抽象一切实验仪器"，我们目标是"这台旋涂仪好用"。借它们的代码思路，不追他们的抽象层次
- **不把 recipe 做成拖拽可视化编辑器** —— recipe 就是 YAML 或 JSON，Agent 会读会写，PM 手动改文件也行。图形化 recipe 编辑器是 Phase 6+ 的事（如果真有需要）
- **不追求 mypy --strict 100%** —— ADR-004 §原则 1 要求类型注解，但"100% mypy strict"是收尾打磨（Phase 3.4），不是主线
- **不写 unit test 到 100% 覆盖率** —— 有 `verify_slice*_*.py` 这种"真硬件数据层验收脚本" + PM 亲自点聊天 / 按钮的"纵向 Slice 验收"就够了。传统 unit test 占 20% 精力得 80% 安全感即可
- **Phase 3.2 里 dry-run 做 v0，不做高级版**——ADR-004 §原则 7 要求"破坏性
  操作支持 dry-run"。Phase 3.2 做 `move_to(dry_run=True)` / `home(dry_run=True)`
  的最小版（返回 MovePlan/HomePlan，不发任何字节），为 Phase 4 旋涂 recipe
  做模板。轨迹可视化、时间估算精度等高级版推到 Phase 4 真涂胶时再做

---

## Phase 4 之前 PM 要拍的决定

Phase 3.2/3.3 都是软件工作，PM 几乎不动。但 **Phase 4 之前**要决策几件：

**一、旋涂模块选型**：采购清单里写的"旋涂模块"具体什么型号？RS485 协议还是别的？有没有文档？写 SpincoaterBackend 前要先看协议。

**二、recipe 来源**：PM 提供的 recipe（"2000 rpm 60s 然后 800 rpm 120s"）是从文献抄 / 组员讨论 / Agent 自己生成？如果 Agent 生成，系统提示里要嵌入"材料学 recipe 常识"，工作量不小。

**三、样品容器 / 托盘**：旋涂之前机器要"取样品"，需要定义"样品位置"。是用 `constants.yaml` 硬编码几个坐标，还是用摄像头视觉识别？后者是大工程。初版建议硬编码。视觉/测距自动对准的三档方案（激光测距 / ArUco / 完整 CV）见 [Issue #027](../issues/027-vision-alignment-backend.md)。

**四、实验数据出口**：每次旋涂结果（膜厚 / 均匀度等）是人工测量后录入，还是接个膜厚仪自动采集？前者简单，后者是 Phase 5+ 的扩展。

这四个不是现在决定，但 **Phase 3.4 收尾时要跟 PM 对齐**，作为进入 Phase 4 的前提。

---

## 一句话总结

**我们正在从"能动机器的代码"过渡到"能理解中文、自动做实验的机器人"**，架构上每一层都想清楚了，每个 Phase 交付一个 PM 能直接验收的新能力，没有隐藏的"全部做完才能看到结果"阶段。

Phase 3.2 Slice A 是下一步，到 Phase 5 差不多全项目完成。

---

## 修订历史

- **2026-04-22**：初版。Phase 3.1 ✅ 画句号后写。
- **2026-04-22 晚**：架构审阅 + 方向二次重排。Phase 3.2 从"Agent SDK 集成"
  拆回"后端打磨稳定底座"；Agent demo 从"合并进 3.2"**恢复**为独立 Phase 3.5。
  phase-3.2-plan.md 改名为 phase-3.5-plan.md。同步架构审阅 A-D 组 7 条改动
  （ADR-005 hooks 重定位 + Tools 白名单规则、observable.py TTL 分级、
  requirements.txt pin 依赖、phase-3.5-plan 前置硬件 smoke 和 Slice B 高
  风险确认和 Slice A 附加）。
- **2026-04-23**：采纳"折中方案"——Phase 3.2/3.3/3.4 主线保持后端打磨，
  但每个 Phase 末尾加 1-2 小时 Agent smoke spike（累积 3/2/1 个工具到
  `tools/spikes/agent_smoke.py`）。smoke 是必须过的验收项。目的：(1) 每
  个 Phase 有 PM 可聊天验收的"有用感" demo；(2) Phase 3.5 开工时 SDK 集成
  坑已踩完，避免一次性爆炸。Phase 3.5 定位从"首次上场"变为"完整产品化"。
- *（下次重大结构变化时加日期）*
