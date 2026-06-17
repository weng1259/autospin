# Phase 3.2 整体验收（稳定底座 + Agent smoke）

## 元信息

- **日期**：2026-04-23
- **Phase**：3.2 稳定底座——后端打磨 + Agent smoke 1/3
- **持续时长**：2026-04-23（单日拿下；前置硬件 smoke + 8 件事全部按 checklist 推进）
- **关联**：[migration-checklist §Phase 3.2](../decision-log/migration-checklist.md) ·
  [ADR-004](../decision-log/ADR-004-l3-api-design-principles.md) ·
  [ADR-005](../decision-log/ADR-005-claude-agent-sdk-for-l3.md) ·
  [architecture-roadmap](../decision-log/architecture-roadmap.md)
- **PM 签字**：✅ 2026-04-23，tag `v0.3.1-phase32-stable`（2026-04-23 打）

## 结论

**Phase 3.2 核心目标全部达成**：L3 API 在没有 Agent 搭车的情况下自己先跑通
ADR-004 七条原则的可验证落地，并在末尾用 Agent SDK 在真硬件上跑了 3 剧本
smoke（PM 签字），为 Phase 3.3/3.4/3.5 准备好了稳定底座。

## 任务完成明细

| # | 任务 | commit | 状态 |
|---|---|---|---|
| 前置 | 硬件 smoke（冷启 $5=1 10/10 + X jog ±20 × 10 Pn: 0/10） | `ccf7b95` | ✅ PM 2026-04-23 |
| 2.1 | Schema 导出 + `docs/api-v1.json` 合同 v1（5 models / 1 enum / 8 errors / 12 methods） | `6822e0c` | ✅ |
| 2.2 | `mypy --strict src/` 全绿（observable.py ParamSpec + @overload） | `82f866d` | ✅ |
| 2.3 | 错误覆盖 unit test（tests/test_errors.py 15/15） | `743ac47` | ✅ |
| 2.4 | 幂等 + TTL 分级 unit test（tests/test_idempotency.py 8/8） | `33857bc` | ✅ |
| 2.5 | dry_run v0（move_to / home）+ MovePlan / HomePlan pydantic | `429db2f` | ✅ |
| 2.6 | get_status 并发性能（tests/test_get_status_concurrency.py 4/4，max < 50ms） | `d840b1b` | ✅ |
| 2.7 | Agent SDK smoke 3 剧本（tools/spikes/agent_smoke.py） | `727692a` | ✅ PM 2026-04-23 |
| ~~2.8~~ | ~~backend ALM 轮询~~ | — | ⏭️ 延期 Phase 3.3+（路径 D，2026-04-23 PM 决策） |
| 3 | 整体验收（本文档） | TBD | ⏳ |

## ADR-004 七条原则逐条落地

| 原则 | 实现 | 验证 |
|---|---|---|
| 1. 严格类型签名 | pydantic BaseModel + Field bounds + `mypy --strict` 全绿 | `docs/api-v1.json` diff 空 |
| 2. 结构化错误 | `L3Error` 基类 + 7 子类（ConnectionError / MachineNotHomedError / AlarmStateError / SoftLimitExceededError / HomingTimeoutError / BrakeError / OperationConflictError），含 severity / recoverable / suggested_action_{en,zh} | tests/test_errors.py 15/15 |
| 3. 幂等 + TTL 分级 | `@observable(idempotency_ttl_s=...)` 装饰器，home/recover=24h，其它默认 5min | tests/test_idempotency.py 8/8 |
| 4. 状态可查询 | `get_status` 只读快照不走 `_lock`；后台 poller 刷新；返回 `last_update_ms_ago` | tests/test_get_status_concurrency.py 4/4，p95/p99 < 50ms |
| 5. 硬编码安全边界 | `constants.yaml` + `SoftLimits.assert_contains`；`feed_mm_min` 运行时校验；Agent 不可改 | test_errors.py `SoftLimitExceededError` + test_dry_run.py feed / soft_limit |
| 6. 每操作 observable | `@observable` 装饰器 pub/sub + runlog.db + JSON 日志 | test_idempotency.py runlog 事件断言 |
| 7. Dry-run 支持 | `move_to(dry_run=True)` / `home(dry_run=True)` 返回 MovePlan/HomePlan；soft_limit + feed 校验仍执行；**不发字节不动 Z 刹车** | tests/test_dry_run.py 7/7（MagicMock side_effect="不应被调用"） |

## 整体验收指标

```bash
$ tools/spikes/.venv/bin/python -m pytest tests/ --cov=src --cov-report=term
========================== 80 passed in 2.60s ==========================

Name                             Stmts   Miss  Cover
----------------------------------------------------
src/__init__.py                      0      0   100%
src/config.py                       40      8    80%
src/event_bus.py                    58      3    95%
src/hardware/errors.py              55      0   100%
src/hardware/gantry_backend.py     395    212    46%
src/hardware/types.py               52      0   100%
src/observable.py                   83      1    99%
src/runlog.py                       40      1    98%
src/schema_export.py                92     16    83%
----------------------------------------------------
TOTAL                              815    241    70%

$ tools/spikes/.venv/bin/mypy --strict src/
Success: no issues found in 11 source files

$ tools/spikes/.venv/bin/python -m src.schema_export --out /tmp/api-v1-regen.json
$ diff -q docs/api-v1.json /tmp/api-v1-regen.json
(空 — schema 合同稳定)
```

`.coveragerc` 把 `src/hardware/dstur_relay.py` 从覆盖率统计中排除（纯 USB
继电器协议，Slice 2 真硬件实测覆盖）。`gantry_backend.py` 46% 是因为
`_send_line_blocking` / `_poll_status_sync` / `_poller_loop` / `_drain_once`
/ `connect` / `close` 等串口协议胶水实机覆盖（Phase 3.1 Slice 1-5 + Phase
3.2 Agent smoke），不算"骨干 unit test 范畴"——这是 architecture-roadmap
§明确不做的事 的既定方针。

### 测试文件一览（80 tests）

- `test_schema_export.py`（5）—— schema 合同确定性
- `test_errors.py`（15）—— 错误层级字段合同 + 真实触发路径
- `test_idempotency.py`（8）—— 幂等缓存 + TTL 分级 + GantryBackend 实际 TTL 挂载
- `test_dry_run.py`（7）—— dry_run 返回 plan + 校验仍执行 + no-op 硬件
- `test_get_status_concurrency.py`（4）—— 并发性能 + baseline + 陈旧度 + disconnect 快路径
- `test_gantry_backend_unit.py`（33）—— 公共 API 早退路径 + 状态分支 + 解析器 + 访问器
- `test_event_bus.py`（7）—— pub/sub + history bounded + 慢订阅丢最旧
- `test_observable_edges.py`（1）—— 非 L3Error 裸 Exception → L3.UNEXPECTED runlog + stderr traceback

## Agent smoke 实测亮点（PM ✅ 2026-04-23）

三剧本累计 cost ~$1.25（Opus 4.7）。关键的"超预期亮点"：

剧本 3「归零然后移到 X=-50 Y=-50」—— Agent 主动调 `gantry_get_status`
发现 `is_homed=True`，**跳过**归零直接 `move_to`；且给 `z_mm` 传当前值
`-4.999` 而不是偷懒置 0。这是 ADR-003 "Agent 做判断"愿景首次产品级体现。

完整 CLI 片段 + SDK 集成坑清单：`docs/verification/phase-3.2-agent-smoke.md`。

## Phase 3.2 留下给 Phase 3.3+ 的

1. **任务 2.8（backend ALM 轮询）延期**——路径 D。Phase 3.3 装 gripper 时
   若用独立 Arduino sketch 走 RS485，可统一设计一块"独立 Arduino 跑 RS485 +
   读 D31-33 ALM + 独立串口回报"的硬件 side 方案，比改 grbl-Mega-5X 固件
   干净。具体开工时再拍。
2. **Agent smoke 扩展计划**——`tools/spikes/agent_smoke.py` 当前 3 工具
   （get_status/home/move_to），Phase 3.3 末尾 + 2（gripper_open/close），
   Phase 3.4 末尾 + 1（maestro_pick_and_place），累计 6 工具后 Phase 3.5
   正式化进 `src/agent/`。
3. **Claude Code bundled ToolSearch 干扰**——smoke 里观察到 Agent 会先
   `ToolSearch` 查我们的工具 schema 再调。Phase 3.5 系统提示 / `disallowed_tools`
   层面压掉（省 ~100-200 tokens/轮）。

## Post-Phase cleanup（本 Phase 做的）

- [x] 更新 `migration-checklist.md §Phase 3.2`（2.6/2.7 打勾 + 2.8 延期说明）
- [x] 在 Phase 3.3 节顶部加"Phase 3.2 遗留：backend ALM 轮询"提示
- [x] `docs/verification/` 新增 3 篇：pre-hardware-smoke / agent-smoke / summary（本文档）
- [x] 更新 `MEMORY.md` 迁移进度节：Phase 3.2 标 [x]
- [x] Commit 所有 Phase 3.2 剩余变更，tag `v0.3.1-phase32-stable`（tag 打于 2026-04-23；项目介绍 / 压测 transcript / Issue #025 的 cleanup commit 在 2026-04-24）
