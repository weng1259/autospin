# Phase 3.3 Task 7 Agent smoke 验收（5 工具：gantry×3 + gripper×2）

## 元信息

- **日期**：2026-04-24
- **任务**：Phase 3.3 Task 7 —— Agent SDK smoke 2/3，新增夹爪工具
- **关联**：[migration-checklist §Phase 3.3](../decision-log/migration-checklist.md) ·
  [ADR-004](../decision-log/ADR-004-l3-api-design-principles.md) ·
  [ADR-005](../decision-log/ADR-005-claude-agent-sdk-for-l3.md) ·
  前置 smoke 1/3：[phase-3.2-agent-smoke.md](phase-3.2-agent-smoke.md)
- **脚本**：`tools/spikes/agent_smoke.py`（相对 Phase 3.2 新增 `gripper_open` / `gripper_close`，共享 `RelayBackend` 实例）
- **SDK**：`claude-agent-sdk==0.1.65`
- **硬件串口**：grbl `/dev/cu.wchusbserial110`，DSTUR `/dev/cu.usbmodem6670E00119391`
- **PM 签字**：✅ Kevin / 2026-04-24

## 结论

**三剧本全过**。夹爪依赖注入 / 继电器 state-memo / brake-skip 均在真硬件上拿到行为证据。

| 剧本 | 行为 | 结果 |
|---|---|---|
| 1. 单夹爪 open | Agent 调 `gripper_open` 1 次，`was_noop=False`（UNKNOWN → OPEN，4.2ms） | ✅ |
| 2. 单夹爪 close | Agent 调 `gripper_close` 1 次（3.1ms，CH1=ON 物理咔哒） | ✅ |
| 3. XY-only + Issue #025 brake-skip 验证 | Agent 自发现 alarm→自归零→10 次 move+open，后 9 次 move 稳定 0.30s，gripper 全部 `was_noop` | ✅（**行为证据确凿**） |

## 目标

Phase 3.2 smoke 1/3 只验了 gantry 三工具。Phase 3.3 Task 7 加两个夹爪工具后，验证：
1. **多 backend 共享 RelayBackend** —— 依赖注入可行，Z 刹车 + CH1 夹爪同串口不抢
2. **继电器幂等** —— 同 `idempotency_key` 5 min 内短路；`commanded_state` 已匹配时 `was_noop=True`
3. **Issue #025 brake-skip** —— XY-only move_to 不切 CH2（_release/_lock_brake 幂等命中），连跑 10 次不应让 DSTUR USB 掉出 `/dev/`
4. **Issue #025 USB 重连** —— 若 DSTUR 掉线，`_write_with_retry` 自动 `_try_reconnect`；此剧本若未重现断线，则留作未激活分支，等真断线时复测

## 前置

1. 两根 USB 都插上：`ls /dev/cu.usbmodem* /dev/cu.wchusbserial*` 应见两个端口
2. `claude login` 订阅态（`claude status` 确认）
3. Z 轴**物理上可移动**（无机械卡滞），夹爪 24V 已通（V+/V- 端子接 24V 电源）
4. 按 migration-checklist §Phase 3.3 的 Phase 1.5 待办：限位 5V 接线回顾一下（NPN `$5=1` 已持久化）

## 启动命令

```bash
cd /Users/kevin/Code/智能旋涂仪
tools/spikes/.venv/bin/python tools/spikes/agent_smoke.py
```

启动日志应类似：
```
连接继电器 /dev/cu.usbmodem6670E00119391 ...
✓ 继电器已连
连接龙门架 /dev/cu.wchusbserial110 ...
✓ 龙门架已连
✓ 夹爪已就绪 (channel=1, commanded_state=unknown)

启动 Agent（SDK: claude-agent-sdk 0.1.65）

=== Agent smoke 已就绪（5 工具：gantry×3 + gripper×2）===
```

## 3 剧本实测（PM 填 transcript）

### 剧本 1：单夹爪 open（最小 smoke —— 验依赖注入 + CH1 通断）

**输入**：`把夹爪打开`

**预期行为**：
- Agent 调 `gripper_open` 1 次
- 首次 `commanded_state=UNKNOWN`，不短路，**实发** DSTUR 命令（CH1=OFF）
- 返回 `was_noop=False`（因为 UNKNOWN ≠ OPEN）

**实测**：

```
你> 把夹爪打开
  🔧 tool_use: mcp__gantry__gripper_open  input={'idempotency_key': 'open-2026-04-24-001'}
  ↩  tool_result: 夹爪松开 ✓ commanded_state_after=open duration=4.2ms
                   event_id=721aa416-dbc8-44b6-bfa5-5da72c9d27a0

🤖 夹爪已松开 ✓
- 命令后状态：OPEN（松开）
- 耗时：4.2ms
  💰 cost=$0.357395
```

**验证**：
- [x] Agent 中文汇报"夹爪已松开"
- [x] duration=4.2ms（远 <500ms —— UNKNOWN 实发路径，但只写一个 4 字节帧）
- [x] 不抛 `RelayCommunicationError`
- [x] Agent 自发用 ToolSearch 拉取 gripper_open schema（deferred tool 首次激活）

---

### 剧本 2：单夹爪 close（PM 选变体，未要求归零 —— 归零已在剧本 3 覆盖）

**输入**：`把夹爪夹紧`

**预期行为**：
- Agent 调 `gripper_close` 1 次
- `commanded_state` 从 OPEN → CLOSED，`was_noop=False`，实发 DSTUR CH1=ON

**实测**：

```
你> 把夹爪夹紧
  🔧 tool_use: mcp__gantry__gripper_close  input={'idempotency_key': 'close-2026-04-24-001'}
  ↩  tool_result: 夹爪夹紧 ✓ commanded_state_after=closed duration=3.1ms
                   event_id=660c1aa2-02cd-46b1-8a0e-afdf86b83082

🤖 夹爪已夹紧 ✓
- 命令后状态：CLOSED（夹合）
- 耗时：3.1ms
  💰 cost=$0.409278
```

**验证**：
- [x] 实发继电器（CH1=ON 有咔哒声 + 夹爪真夹合）
- [x] duration=3.1ms
- [x] 无错误
- ~~[ ] 归零~~ → 归零验证由剧本 3 覆盖（Agent 从 alarm 状态主动恢复）

---

### 剧本 3：XY-only 联动 + Issue #025 brake-skip 验证（Agent 自循环 10 次）

**输入**：`移到 X=-100 Y=-100 并打开夹爪（重复 10 次验 brake-skip）`

**Agent 实际路径**（比剧本更丰富）：
1. 先 `gantry_get_status` → 发现 `state=alarm is_homed=False`
2. 自建 TodoWrite 拆步骤：归零 → 10 次循环 → 总结
3. `gantry_home`（30.1s）→ 位置 (-4.999, -4.999, -4.999)
4. 循环 10 次 `move_to(-100, -100, -5) + gripper_open`
5. 自总结 brake-skip + state-memo 行为

**实测（关键段）**：

```
你> 移到 X=-100 Y=-100 并打开夹爪（重复 10 次验 brake-skip）

  🔧 gantry_get_status → state=alarm is_homed=False
  🔧 gantry_home → 归零完成 ✓ duration=30.1s → pos=(-5,-5,-5)

**Iteration 1/10:**
  🔧 gantry_move_to(x=-100, y=-100, z=-5, feed=2000)
     → 移动完成 ✓ duration=4.39s  (首次从 -5 大幅位移)
  🔧 gripper_open(key='brake-skip-test-open-01')
     → 夹爪松开 ✓  duration=294.2ms  (CLOSED → OPEN，实发 CH1=OFF)

**Iteration 2-10/10**（均同结构）：
  🔧 gantry_move_to(-100,-100,-5)
     → 移动完成 ✓ duration=0.30-0.31s  ← 稳定无刹车切换开销
  🔧 gripper_open(key='brake-skip-test-open-NN')
     → 夹爪松开 ✓（幂等命中，未发送继电器命令）duration=0.0ms
                                           ↑ state-memo: commanded_state 已 OPEN
```

**Agent 自总结的数据表**：

| Iter | move_to | gripper_open 行为 | gripper duration |
|------|---------|------------------|------------------|
| 1    | 4.39s   | 实发（CLOSED→OPEN）| 294.2ms |
| 2-10 | 0.30-0.31s | state-memo 幂等命中 | 0.0ms |

**验证**：
- [x] 10 轮跑完 DSTUR 串口仍存在（后续剧本复用同 session，未再断）
- [x] Iter 2-10 move_to 稳定 0.30s（**无 2×0.3s 刹车切换** —— 若刹车每次都切应额外 +600ms；这是 brake-skip 生效的行为证据）
- [x] 后 9 次 `gripper_open` 全部 `was_noop=True` 0.0ms —— state-memo 短路
- [x] Agent 没因上下文过长报错，反而主动用 TodoWrite 跟踪进度
- [x] 10 个不同 `idempotency_key` 都跑了，但全部走 state-memo 路径（与 TTL idempotency 不同机制，行为正确）

---

## Issue #025 修复验证

### Brake-skip ✅ **行为证据确凿**

核心证据：剧本 3 Iter 2-10 的 `move_to` 耗时稳定在 0.30-0.31s。若 `_release_brake`/`_lock_brake` 每次都切 CH2，应至少多出 2×`_SETTLE_S`=0.6s。实测无此开销 → **Z 不变时 brake-skip 生效**。

Iter 1 为 4.39s 是首次长距离位移（从 -5 到 -100 mm，XY 各约 95mm @ 2000 mm/min）的正常时间，与 brake 无关。

### USB 重连 ⏸️ **路径未激活**

本剧本 10 次循环 DSTUR 未自然断线。修复路径 `_write_with_retry` + `_try_reconnect` 只有单元测试覆盖（见 `tests/test_relay_backend.py`），真硬件层留待下次自然掉线时验。不阻塞 Phase 3.3 验收。

## 成本

| 剧本 | cost_usd |
|---|---|
| 1. 夹爪 open | $0.357 |
| 2. 夹爪 close | $0.409 |
| 3. 10 次循环（含 home + 自总结） | $1.079 |
| **合计** | **$1.845** |

略高于预估 $1.2 —— 剧本 3 Agent 自主把 10 次循环装进单个 turn，产生 10 轮 tool_use + 最后一次自总结，token 用得比简单剧本多。对 Agent 能力是好事。

## 结论与下一步

- ✅ Task 7 验收通过，Phase 3.3 代码全绿
- ✅ Issue #025 brake-skip 在真硬件拿到行为证据
- ⏸️ USB 重连路径等自然触发
- ➡️ Task 8：写 phase-3.3-summary.md + 更新 migration-checklist + MEMORY → commit + 可选 tag `v0.3.2-phase33-stable`
- ➡️ Phase 3.4 maestro smoke 3/3：工作流级别演 "把样品从 A 搬到 B"（当前剧本 3 的 Agent 行为已非常接近 maestro 级别，Phase 3.4 把它沉淀到 `src/maestro.py`）
