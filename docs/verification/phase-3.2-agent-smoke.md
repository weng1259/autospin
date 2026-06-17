# Phase 3.2 任务 2.7 Agent smoke 验收

## 元信息

- **日期**：2026-04-23
- **任务**：Phase 3.2 #7 —— Claude Agent SDK spike，3 工具 CLI 交互
- **关联**：[migration-checklist §Phase 3.2 (7)](../decision-log/migration-checklist.md) ·
  [ADR-004](../decision-log/ADR-004-l3-api-design-principles.md) ·
  [ADR-005](../decision-log/ADR-005-claude-agent-sdk-for-l3.md)
- **脚本**：`tools/spikes/agent_smoke.py`
- **SDK**：`claude-agent-sdk==0.1.65`（requirements.txt pin）
- **Claude CLI**：2.1.118（bundled Claude Code）—— `claude login` 订阅模式
- **硬件**：grbl @ `/dev/cu.wchusbserial110`，DSTUR @ `/dev/cu.usbmodem6670E00119391`
- **PM 签字**：✅ Kevin / 2026-04-23

## 结论

**三剧本全过**。Phase 3.5 首次产品级 Agent 集成有绿灯。

| 剧本 | 行为 | 结果 |
|---|---|---|
| 1. 状态翻译 | Agent 调 `gantry_get_status` 1 次，中文汇报含状态/位置/is_homed/alarm_code/buffer 全部字段，主动提醒 is_homed=False 并建议归零 | ✅ |
| 2. 避免盲目归零 | Agent 直接 `gantry_home`（未先查状态，但用了上下文里的 is_homed=False 推断——行为合理） | ✅ |
| 3. 组合指令 | Agent 先 `gantry_get_status` → 发现 is_homed=True 自动跳过 home → 直接 `gantry_move_to(-50,-50,当前z)`，并贴心保留当前 Z 坐标而非重置 0 | ✅✅（超预期的细腻） |

## 目标（回顾）

ADR-005 落地首次真·Agent 集成。踩完 SDK 集成坑：
1. `@tool` 装饰器从 Python 类型注解生成 schema —— ✅ 3 个工具成功注册
2. `ClaudeSDKClient` + `allowed_tools` 白名单 + `mcp_servers` —— ✅ `mcp__gantry__*` 白名单生效
3. 同进程 MCP server 共享 `GantryBackend` 单例 —— ✅ 模块级 `_BACKEND` 可行
4. `async` tool handler + `asyncio.to_thread(sync_backend_method)` —— ✅ 归零 30s 不堵 event loop
5. `L3Error` 透传 Agent —— ✅ 本次未触发 error path，但 `_format_l3_error` 双语合同已就绪
6. pydantic 返回值序列化 —— ✅ HomeResult / MoveResult 文本化输出正常

## 3 剧本实测 CLI 片段

### 剧本 1：状态翻译

**输入**：`机器什么状态？`

```
你> 机器什么状态？
  🔧 tool_use: ToolSearch  input={'query': 'select:mcp__gantry__gantry_get_status', ...}
  ↩  tool_result:
  🔧 tool_use: mcp__gantry__gantry_get_status  input={}
  ↩  tool_result: state=idle position=(x=-4.999, y=-4.999, z=-4.999) is_homed=False
                   alarm_code=None planner_buffer_free=35 rx_buffer_free=255 last_update_ms_ago=90

🤖 机器当前状态：
   - 状态：idle（空闲）
   - 位置：X=-4.999 mm, Y=-4.999 mm, Z=-4.999 mm（机器坐标系）
   - 是否归零：❌ 否
   - 报警码：无
   - 规划缓冲区空闲：35 / 接收缓冲区空闲：255 / 上次更新：90 ms 前

   ⚠️ 提醒：机器尚未归零，此时调用 move_to 会抛 MachineNotHomedError。要我帮你归零吗？
  💰 cost=$0.349614
```

### 剧本 2：避免盲目归零 → 实际：直接归零（未归过，行为合理）

**输入**：`帮我归零`

**预期分支**：因为剧本 1 已告知 is_homed=False，Agent 直接 home 也合理（不
需要每次都浪费一次 get_status）。原本 migration-checklist 设计剧本 2 是"已
归过不再归"——但实际测试场景**前置状态未归**，所以 Agent 走的是 home 路径。

**评估**：✅ 行为正确。真正的"避免盲目归零"由剧本 3 前半段覆盖（连续两条
指令中第二条发现已归零时不重归）。

```
你> 帮我归零
  🔧 tool_use: ToolSearch  input={'query': 'select:mcp__gantry__gantry_home', ...}
  ↩  tool_result:

🤖 机器未归零，开始归零（约 30 秒，Z 先 → X+Y 并行，请勿阻挡机器）：

  🔧 tool_use: mcp__gantry__gantry_home
          input={'idempotency_key': 'home-2026-04-23-user-request-001'}
  ↩  tool_result: 归零完成 ✓ position=(x=-4.999, y=-4.999, z=-4.999)
                  duration=30.7s event_id=0ae88d36-5b09-4398-8552-64c8b4d8e442

🤖 ✅ 归零完成
   - 耗时：30.7 秒
   - 当前位置：X=-4.999 mm, Y=-4.999 mm, Z=-4.999 mm
   - 事件 ID：0ae88d36-5b09-4398-8552-64c8b4d8e442

   机器已归零，现在可以安全执行 move_to 指令了。需要我把机器移到某个位置吗？
  💰 cost=$0.407959
```

**关键观察**：
- Agent 自己生成了有意义的 `idempotency_key`（`home-2026-04-23-user-request-001`）
  而不是 UUID —— 这是 Claude 倾向可读性的表现。对项目无害（幂等仍生效）
- `duration=30.7s` 和 EEPROM 的 `$22=1` + `$27` 参数行为一致

### 剧本 3：组合指令

**输入**：`归零然后移到 X=-50 Y=-50`

```
你> 归零然后移到 X=-50 Y=-50
  🔧 tool_use: mcp__gantry__gantry_get_status  input={}
  ↩  tool_result: state=idle position=(x=-4.999, y=-4.999, z=-4.999)
                   is_homed=True alarm_code=None ... last_update_ms_ago=52

🤖 机器已归零（刚完成），无需重复归零。直接移动到 X=-50 Y=-50（Z 保持当前 -4.999）：

  🔧 tool_use: ToolSearch  input={'query': 'select:mcp__gantry__gantry_move_to', ...}
  ↩  tool_result:
  🔧 tool_use: mcp__gantry__gantry_move_to
          input={'x_mm': -50, 'y_mm': -50, 'z_mm': -4.999, 'feed_mm_min': 2000}
  ↩  tool_result: 移动完成 ✓ final=(x=-50.001, y=-50.001, z=-4.999)
                  duration=2.11s event_id=e81c103b-4d8e-4c0e-b552-937b0e474557

🤖 ✅ 移动完成
   - 最终位置：X=-50.001 mm, Y=-50.001 mm, Z=-4.999 mm
   - 耗时：2.11 秒
   - 进给速度：2000 mm/min
   - 事件 ID：e81c103b-4d8e-4c0e-b552-937b0e474557

   机器现位于 (-50, -50, -4.999)，状态空闲，可继续下一步指令。
  💰 cost=$0.495345
```

**超预期亮点**：
1. Agent **先**调 `gantry_get_status`（剧本 2 刚归完，但连续指令里再查是对的）
2. 发现 `is_homed=True` 主动**跳过** home —— 正是 ADR-003 "Agent 做判断" 的期望
3. **保留当前 Z 坐标**（-4.999）而不是用 0 或默认值 —— `move_to` 要求 x/y/z 三个坐标都给，Agent
   没有"偷懒"用 0 而是用当前 Z。这是 ADR-004 §原则 4 "状态可查询"真正发挥作用的样子
4. `final=(-50.001, -50.001, -4.999)` 说明 grbl 到达目标误差 ~1μm —— 682.67 steps/mm 精度正常

## 成本

3 剧本累计：$0.349614 + $0.407959 + $0.495345 ≈ **$1.25**（Opus）。

ADR-005 §副作用要监控的"API 费用可见性"在此验证：`ResultMessage.total_cost_usd`
能读到，Phase 3.5 Streamlit 上累加展示可行。

## 工具白名单（ADR-005 §白名单规则）

3 个工具全部进白名单，均为"设备语义级"：
- `mcp__gantry__gantry_get_status`（只读，无副作用）
- `mcp__gantry__gantry_home`（物理动作，幂等 24h TTL）
- `mcp__gantry__gantry_move_to`（物理动作，天然幂等）

未来不放白名单的（Phase 3.3+）：`send_raw_gcode`、`relay_ch_*`（见 ADR-005）。

## SDK 集成踩坑记录（供 Phase 3.5 复用）

1. **Claude Code bundled `ToolSearch` 会被 Agent 调到**：剧本 1/2/3 都看到
   `tool_use: ToolSearch  input={'query': 'select:mcp__gantry__*'}`。这是
   Claude Code 的 deferred tool 机制——Agent 先用 ToolSearch 查工具 schema
   再调用。
   **影响**：多一次 tool round，多 ~100-200 tokens。**应对**：Phase 3.5 可
   通过 `disallowed_tools=["ToolSearch"]` 关掉（或系统提示里说清不要用）。
   Spike 阶段不管。
2. **`input()` 在 async 里**：用 `loop.run_in_executor(None, lambda: input(prompt))`
   安全，不堵 event loop。
3. **pydantic 返回值**：直接返回格式化文本比 `model.model_dump()` 更省 token，
   Agent 读得更顺。Phase 3.5 保留此做法。
4. **idempotency_key 让 Agent 生成**：Agent 给出的 key（`home-2026-04-23-user-request-001`）
   比 uuid 可读性好，runlog 查起来清晰。Phase 3.5 保持此策略。
5. **30s 长 home 期间 event loop 不阻塞**：`asyncio.to_thread` 工作正常。
6. **`allowed_tools` 白名单格式**：`mcp__<server_name>__<tool_name>` 三段，
   双下划线分隔（与 SDK example 一致，别写错）。
7. **`create_sdk_mcp_server` 返回的 dict**（而非对象）用于 `mcp_servers` 字典值
   时直接传，不用解包。

## 后续

- ✅ Phase 3.2 任务 2.7 验收通过
- Phase 3.3 装 gripper 后：`agent_smoke.py` 增量加 `gripper_open`/`gripper_close`，
  跑新剧本"归零后夹紧"（见 migration-checklist §Phase 3.3）
- Phase 3.5 正式化：把 spike 迁到 `src/agent/{tools,client,hooks}.py`（
  phase-3.5-plan.md）
