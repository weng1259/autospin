# Phase 3.3 整体验收（Gripper/Relay 纯后端 + Agent smoke 2/3）

## 元信息

- **日期**：2026-04-23 开工 / 2026-04-24 PM smoke 签字收尾
- **Phase**：3.3 Gripper/Relay 纯后端 + Agent smoke 2/3
- **持续时长**：约 2 天（Task 1-5 单日 `0090c93` 2026-04-23；Task 6-8 次日）
- **关联**：[migration-checklist §Phase 3.3](../decision-log/migration-checklist.md) ·
  [ADR-004](../decision-log/ADR-004-l3-api-design-principles.md) ·
  [ADR-005](../decision-log/ADR-005-claude-agent-sdk-for-l3.md) ·
  [architecture-roadmap](../decision-log/architecture-roadmap.md) ·
  前置：[Phase 3.2 summary](phase-3.2-summary.md)
- **PM 签字**：✅ 2026-04-24，可选 tag `v0.3.2-phase33-stable`

## 结论

**Phase 3.3 核心目标全部达成**：
1. Slice 2 的胶水层 `dstur_relay.py` 抬到 ADR-004 七原则的可验证 `RelayBackend`（31 tests / 94% cov）
2. 新增 `GripperBackend`（12 tests / 100% cov），与 GantryBackend Z 刹车**共享同一个 RelayBackend**（依赖注入）
3. GantryBackend Z 刹车路径切回新 backend（`_release_brake` / `_lock_brake` helper），`BrakeError` 包装保留
4. Agent SDK smoke 2/3 在真硬件跑通 3 剧本（5 工具：gantry×3 + gripper×2），**Issue #025 brake-skip 行为证据确凿**
5. 底座回归测试 124/124、`mypy --strict` 12 files 0 errors、schema 合同 diff 稳定

## 任务完成明细

| # | 任务 | commit | 状态 |
|---|---|---|---|
| 1 | `RelayBackend` —— 31 tests / 94% cov，含 USB 重连 + state-memo | `0090c93` | ✅ |
| 2 | `GripperBackend` —— 12 tests / 100% cov，依赖注入 RelayBackend | `0090c93` | ✅ |
| 3 | `GantryBackend` Z 刹车迁移到 `_release_brake`/`_lock_brake` helper（包 `RelayCommunicationError` → `BrakeError`） | `0090c93` | ✅ |
| 4 | 删 `src/hardware/dstur_relay.py` —— 被 `RelayBackend` 完全取代 | `0090c93` | ✅ |
| 5 | Issue #025 软件层修复（brake-skip + USB 重连）落地到 `RelayBackend` | `0090c93` | ✅（真硬件验证在 Task 7） |
| 6 | Schema 导出 + 回归验证（`docs/api-v1.json` 加 Relay/Gripper），pytest 124/124 + mypy 全绿 | `001663d` | ✅ |
| 7 | Agent smoke 2/3 代码 + 真硬件 3 剧本实测 | `6ef9ca1` + 本 commit | ✅ PM 2026-04-24 |
| 8 | 本验收文档 + migration-checklist + MEMORY 更新 + commit | 本 commit | ⏳ |

## 为什么这个 Phase 重要

**Phase 3.2 稳定了底座**（类型 / 错误 / 幂等 / observable / dry-run），但只覆盖了 gantry。Phase 3.3 把"同一套 ADR-004 七原则"推广到**第二个和第三个 backend**（Relay / Gripper），证明这套骨架不是 gantry-specific 的，而是通用 L3 backend 的模板。

另一关键点：Issue #025（2026-04-22 发现的"用 DSTUR-T80 大量切 CH2 会把 USB 打挂"）在 Phase 3.2 时只有临时诊断，Phase 3.3 把修复系统化到后端——`_set_channel` 的 state-memo idempotency 短路 + `_write_with_retry` 失败后 `_try_reconnect`。Task 7 的 10 次循环 smoke 给了 brake-skip 生效的**行为证据**（iter 2-10 move_to 稳定 0.30s 无刹车切换开销）。

## ADR-004 七条原则在 Relay/Gripper 上的落地

| 原则 | RelayBackend | GripperBackend |
|---|---|---|
| 1. 严格类型签名 | `ch_on`/`ch_off` 返回 `RelayActionResult \| RelayActionPlan`；pydantic BaseModel | `open`/`close` 返回 `GripperActionResult \| GripperActionPlan` |
| 2. 结构化错误 | `RelayCommunicationError`（含 suggested_action_zh） | 透传 RelayCommunicationError；GantryBackend Z 路径包成 `BrakeError` |
| 3. 幂等 + TTL | `@observable` 5 min；内部 state-memo 额外短路（目标 state==当前 → was_noop） | `@observable` 5 min；UNKNOWN 初始状态**不短路**（物理真值未知必须实发） |
| 4. 状态查询不阻塞 | `get_state` 只读 `_state_lock`，写串口抢 `_write_lock` 时读立即返回 | `get_state` 只读 `_state_lock` |
| 5. 硬编码安全边界 | `channel ∈ [1, 8]` 越界立即抛 `RelayCommunicationError` | channel 由 RelayBackend 校验；夹爪初始状态语义 = UNKNOWN（明示未知） |
| 6. Observable | `@observable` + runlog + event_bus | 同左 |
| 7. Dry-run | `ch_on(..., dry_run=True)` → `RelayActionPlan(would_write=...)` | `open(..., dry_run=True)` → `GripperActionPlan(would_activate_relay=...)` |

## 整体验收指标

```bash
$ tools/spikes/.venv/bin/python -m pytest tests/ --cov=src --cov-report=term
================================ 124 passed in 16.90s =============================

Name                              Stmts   Miss  Cover
-----------------------------------------------------
src/__init__.py                       0      0   100%
src/config.py                        40      8    80%
src/event_bus.py                     58      3    95%
src/hardware/__init__.py              0      0   100%
src/hardware/errors.py               61      0   100%
src/hardware/gantry_backend.py      404    219    46%
src/hardware/gripper_backend.py      48      0   100%
src/hardware/relay_backend.py       114      7    94%
src/hardware/types.py                86      0   100%
src/observable.py                    83      1    99%
src/runlog.py                        40      1    98%
src/schema_export.py                 96     16    83%
-----------------------------------------------------
TOTAL                              1030    255    75%

$ tools/spikes/.venv/bin/mypy --strict src/
Success: no issues found in 12 source files

$ tools/spikes/.venv/bin/python -m src.schema_export --out /tmp/api-v1-regen.json
$ diff -q docs/api-v1.json /tmp/api-v1-regen.json
(空 — schema 合同 diff 稳定)
```

相对 Phase 3.2（80 tests / 70% cov / 11 files）：
- tests：80 → 124（+43 = 31 relay + 12 gripper，新 backend 100% 自覆盖）
- coverage：70% → 75%
- mypy files：11 → 12（+1 relay_backend；gripper 被 relay 带入同批检查）
- schema：+3 backends 中 2 个新（RelayBackend / GripperBackend）+ 5 个新 pydantic models（Relay/Gripper Action Result/Plan + GripperState）+ 1 新 enum（GripperCommandedState）+ 2 新 errors（RelayCommunicationError, BrakeError）

`gantry_backend.py` 46% 覆盖率不变——Phase 3.3 没改公共 API，只把 Z 刹车内部 helper 换了底层实现；串口协议胶水仍由 Phase 3.1 Slice 1-5 + Phase 3.2/3.3 Agent smoke 实机覆盖（见 architecture-roadmap §"明确不做的事"）。

## Agent smoke 2/3 实测亮点

3 剧本累计 cost ~$1.85（Opus 订阅态）。详见 `docs/verification/phase-3.3-agent-smoke.md`。

**剧本 3 超预期**——PM 输入 `移到 X=-100 Y=-100 并打开夹爪（重复 10 次验 brake-skip）`，Agent：
1. 自检 `gantry_get_status` 发现在 alarm 状态
2. 自建 TodoWrite 拆步骤：归零 → 循环 → 总结
3. 自动 `gantry_home` 恢复
4. 10 次循环里 iter 2-10 的 `move_to` 稳定 0.30s，`gripper_open` 全部 `was_noop=True` 0.0ms
5. **Agent 自己生成 brake-skip 行为数据表**（见 smoke 文档末尾），完全没引导

这是 ADR-003 "Agent 做决策 + 做诊断"愿景第二次真实体现（第一次是 Phase 3.2 剧本 3 自动跳过已归零的 home）。Phase 3.4 maestro 其实已经被这个剧本的 Agent 行为提前 demo 掉大半了。

## Issue #025 修复状态

| 子 bug | 实现 | 验证 |
|---|---|---|
| Brake-skip（Z 不变时不切 CH2） | `RelayBackend._set_channel` state-memo：目标 state == 当前时 `was_noop=True` 不发字节 | ✅ Task 7 剧本 3 iter 2-10 行为证据 |
| USB 重连（EMI 导致 fd stale 时自愈） | `RelayBackend._write_with_retry` 首次写失败 → `_try_reconnect` 关旧 fd + 重开 → 再写 | ✅ 单元测试（tests/test_relay_backend.py），⏸️ 真硬件未自然触发 |

## Phase 3.3 留下给 Phase 3.4+ 的

1. **USB 重连路径真硬件验**——等自然掉线触发，不阻塞 Phase 3.3
2. **Phase 1.5 待办（限位 5V 接线）**——migration-checklist 标的"降级"项，若 Phase 3.4 再次遇到静态 `Pn:` 再查
3. **Phase 3.4 maestro**：`src/maestro.py` + 故障注入 + Agent smoke 3/3。Task 7 剧本 3 的 Agent 行为已接近 maestro 雏形，Phase 3.4 把它沉淀为代码级的 workflow abstraction
4. **Phase 3.5 Agent 正式化**：从 `tools/spikes/agent_smoke.py` 搬到 `src/agent/`，加 PreToolUse hooks + canUseTool 高风险确认 + Streamlit 集成（见 `docs/decision-log/phase-3.5-plan.md`）
5. **Phase 3.2 延期项（backend ALM 轮询，路径 D）**：Phase 3.3 未触及 Arduino 固件侧；Phase 3.4 maestro 设计时再决定要不要统一到"独立 Arduino 跑 RS485 + 读 ALM"的硬件 side 方案

## Post-Phase cleanup（本 Phase 做的）

- [x] 更新 `migration-checklist.md §Phase 3.3`（Task 6/7/8 打勾 + 进入条件）
- [x] 更新 `MEMORY.md` 迁移进度节：Phase 3.3 `[~]` → `[x]`
- [x] `docs/verification/` 新增 2 篇：`phase-3.3-agent-smoke.md` / 本文档
- [x] Commit Phase 3.3 剩余变更；可选 tag `v0.3.2-phase33-stable`
