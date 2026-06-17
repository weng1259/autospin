# 开源栈迁移检查清单

> 配合 [ADR-001](ADR-001-migrate-to-grbl-stack.md) / [ADR-002](ADR-002-replace-cncjs.md) / [ADR-003](ADR-003-agent-first-vision.md) / [ADR-004](ADR-004-l3-api-design-principles.md) 一起使用。ADR 讲「为什么」，本文档讲「每个 Phase 具体做什么 + 完成后要清理什么文档」。
>
> **重要**：Phase 之间可能间隔数周，任何一个 Phase 验收后都必须完成它的「Post-Phase cleanup」才能进下一个 Phase，否则文档和实际状态会永久漂移。
>
> **2026-04-20 重大修订**（见 ADR-003 / ADR-004）：
> - Agent-first 架构确立，优先级翻转为「底层稳定 > API 质量 > UI」
> - Phase 2（cncjs 接管 UI）**废弃**——L2 降级为应急备份
> - Phase 3 范围从"薄 orchestrator MVP"扩大为"Agent-ready API surface v1"，时间 2-3 天 → 4-6 天
> - 新增 Phase 3.5：首个 Agent demo 作为验证 API 设计的里程碑
> - Phase 5 部署方案调整（去掉 cncjs 作为默认应急 UI）

---

## 全局前置：项目未纳入 Git（issue #021）

**强烈建议在 Phase 1 之前完成**，否则 spike 失败时没有安全网。

- [ ] `git init` + 写 `.gitignore`（忽略 `/tmp`、`*.zip`、`/data-*`、`/报销`）
- [ ] 首次提交，把当前状态冻结为 "pre-migration baseline"
- [ ] 打 tag `v0.1-pre-grbl-migration`

---

## Phase 0：硬件安全底座（半天）

**权威 runbook**：[`docs/guides/phase0-硬件安全底座.md`](../guides/phase0-硬件安全底座.md)

本节是总览和验收条目，具体步骤看上面那个 runbook——新会话应该直接打开 runbook 执行。

### 进入条件
- 现在即可
- 强烈建议 `git init` 并打 `v0.1-pre-grbl-migration` tag

### 行动
- [ ] **动作 1**：ALM 报警线物理接线——X/Y/Z 三轴驱动器的 ALM+/ALM- 分别接到 Arduino D31/D32/D33 + 共用 GND
  - 详细步骤见 `docs/guides/02-电控接线.md` §27
  - 用 `firmware/alm_test/alm_test.ino`（本次迁移新增的极简验证 sketch）做通电测试
- [ ] **动作 2**：Z 刹车软件策略落稿——把「Z 刹车默认行为矩阵」写进 `phase0-硬件安全底座.md § 动作 2`（本次已预写，只需 review + commit）
- [ ] **动作 3**：修正 MEMORY.md 驱动器手册路径（旧：`hardware/datasheets/...`，正确：`hardware/龙门架/驱动器/...`，本次已修）

### 验收
全部打勾才算 Phase 0 完成（详细条目见 runbook 末尾「Phase 0 整体验收清单」）：
- [ ] `alm_test` 串口监视器正常输出 `X=OK Y=OK Z=OK`
- [ ] 手动短接 D31/D32/D33 至 GND，对应轴切换为 `ALARM`
- [ ] 至少做一次真实缺相测试（拔 X 轴 A+ 相线 → 驱动器红灯闪 6 次 → 串口 `X=ALARM`）
- [ ] Z 刹车策略矩阵已在 phase0 runbook 定稿

### Post-Phase cleanup
- [ ] 刷回 `firmware/motor_control/motor_control.ino`（可选，也可停在 alm_test 等 Phase 1）
- [ ] Commit: `Phase 0: ALM wiring + Z brake strategy`，tag 可选 `v0.1.1-phase0-done`
- [ ] 更新 `MEMORY.md` 「迁移进度」节：`[ ] Phase 0` → `[x] Phase 0`
- [ ] 更新 `docs/issues/README.md`：在 #006 条目加备注「ALM 物理接线已在 Phase 0 完成 YYYY-MM-DD」（**不关闭**，因为 ENA 急停硬件联锁还没做）
- [ ] 不归档任何现有文档（Phase 0 纯增量，无文档废弃）

---

## Phase 1：grbl-Mega-5X spike（2-3 天，决策点）

### 进入条件
- Phase 0 完成
- 已 git init 并冻结 baseline

### 行动
- [ ] `mkdir firmware/grbl_spike && cd firmware/grbl_spike`
- [ ] `git clone https://github.com/fra589/grbl-Mega-5X.git`（上次克隆在 `/tmp/research-clone/grbl-Mega-5X`，开机会丢；最后验证的 commit 是 `a5596ef`，2024-10-20）
- [ ] 改 `grbl/cpu_map.h` 映射我们的 Mega 引脚：
  - X: PLS=D2, DIR=D3 → AXIS_0
  - Y: PLS=D4, DIR=D5 → AXIS_1
  - Z: PLS=D6, DIR=D7 → AXIS_2
  - 6 个限位：D22/24/25/27/28/30（X-/X+, Y-/Y+, Z-/Z+）
  - 3 个原点：D23/26/29（作为 homing switch，grbl 只支持单原点方向，可能需把「正限位做原点」的 Z 轴逻辑改掉）
  - ALM 3 路：D31/32/33 → 接到 grbl 的 Abort/Door/Reset pin 之一
  - Spindle Enable 输出 → （可选）联动 Z 刹车；或保持由 Python 层管
- [ ] 改 `grbl/config.h`: `N_AXIS 3`, `N_AXIS_LINEAR 3`
- [ ] 用 Arduino IDE 或 PlatformIO 编译 + 烧录（**先关闭所有占用 /dev/cu.usbmodem* 的程序**）
- [ ] `screen /dev/cu.usbmodem* 115200` 手动发命令配置 `$` 参数：
  - `$100=682.67` `$101=682.67` `$102=682.67`（steps/mm）
  - `$110=2000` `$111=2000` `$112=2000`（max rate mm/min）
  - `$120=50` `$121=50` `$122=50`（accel mm/s²）
  - `$130=280` `$131=280` `$132=95`（max travel mm，依实测行程）
  - `$20=1` `$21=1`（soft limits, hard limits on）
  - `$22=1` `$23=3` `$24=25` `$25=500` `$26=250` `$27=1`（homing enable + 方向 + 速度）

### 验收（必须全部通过）
- [ ] `$H` 三轴归零：Z 先归 → X → Y，全部稳定可靠，坐标复位为 0
- [ ] `G0 X100` 点动无异响、无丢步
- [ ] `G1 X50 Y50 F500` 协同运动平滑
- [ ] 手动触发限位开关，grbl 进入 alarm 状态并停止
- [ ] 运行中按 `!` feedhold 暂停、`~` resume 继续、`Ctrl-X` soft reset
- [ ] 用尺子量 10 mm 行程误差 < 0.1 mm（682.67 步/mm 已于 2026-03-26 实测校准过，理论上应直接准）
- [ ] 连续 5 分钟来回运动无累积丢步

### 回滚条件（任一触发即回滚）
- 2-3 天内 cpu_map 映射无法跑通
- 脉冲频率撞上 AVR 30 kHz 上限导致最大速度 < 40 mm/s 且无法通过拨码降细分
- 归零流程在我们的「9 传感器 3 原点」布局下无法工作
- 遇到需要深度改 grbl C 代码才能解决的 bug

### 回滚动作
- 刷回 `firmware/motor_control/motor_control.ino`
- 保留 `firmware/grbl_spike/` 目录供后续尝试
- 在 ADR-001 末尾追加「Phase 1 失败复盘」章节，记录卡点
- 回到原计划 Phase 2c（加减速）

### Post-Phase cleanup（验收通过后）
- [ ] **新建** `docs/guides/04-grbl-固件配置.md`：cpu_map 映射表 + 完整 `$` 参数表 + 烧录步骤 + 常见故障排查
- [ ] **归档** `docs/guides/04-固件开发.md` → `docs/legacy/04-自研固件开发-archived.md`（顶部加一段「本文档对应已废弃的自研固件路线，保留供回滚参考。当前固件栈见 04-grbl-固件配置.md 和 ADR-001」）
- [ ] **归档** `docs/guides/05-固件测试.md` → `docs/legacy/05-自研固件测试-archived.md`（同上）
- [ ] **保留** `firmware/motor_control/motor_control.ino` 原样，在同目录加一个 `README.md` 说明「已退役，仅作 rollback snapshot」
- [ ] **更新** `docs/issues/README.md`：把 #001/#002/#007/#008/#016/#018/#023 的状态从 `open` 改为 `resolved-by-migration-phase1`（保留原 issue 文件不删）
- [ ] **更新** `MEMORY.md`：
  - 移除「固件 2a/2b 完成」「固件 2c/2d/2e 待做」
  - 新增「固件栈 = grbl-Mega-5X (commit XXX)，`$` 参数在 docs/guides/04-grbl-固件配置.md」
  - 更新「关键文件」节指向新文档
- [ ] Commit: `Phase 1: migrate to grbl-Mega-5X`，tag `v0.2-grbl-spike-passed`

---

## Phase 2：~~cncjs 接管 Web UI~~（**已废弃，详见 ADR-002/ADR-003**）

**2026-04-20 回退决策**：
- cncjs 1.11.0 已本地安装但**验收未通过**（Feeder ALARM 死锁 + 不用 `$J=`，详见 ADR-002）
- [ADR-003](ADR-003-agent-first-vision.md) 进一步把 L2 降级为应急备份，原 Phase 2 的"UI 接管"目标不再成立
- 原 Phase 2 整个章节**废弃**，不再作为独立 Phase
- 历史记录：该 Phase 曾计划 `npm install -g cncjs` + 配置 Grbl controller + 5 项验收。`~/.cncrc` 仍保留（不卸载），作为应急手动工具

**替代方案**：
- L2 最终形态由 Phase 3 第一天 spike 决定（bCNC-as-library / DIY / OpenBuilds 三选一，见 ADR-002 修订）
- 应急 UI 三种兜底：cncjs 原封不动保留 / `tools/grbl_stability_test.py` CLI / Phase 3 自带的最小 Streamlit jog 页
- 原 Phase 2 的 cleanup 项（归档 web_control/legacy 等）**取消**，等 Phase 3 结束再统一清理

---

## Phase 3：L3 Python Orchestrator API Surface v1（**4-6 天**，原 2-3 天已扩容）

> **Agent-first 重构的核心 Phase**。见 [ADR-003](ADR-003-agent-first-vision.md)（愿景）+ [ADR-004](ADR-004-l3-api-design-principles.md)（API 规范）。

### 进入条件

- Phase 1 验收通过（✅）
- ~~Phase 2 验收通过~~（废弃）
- 夹爪已接到 DSTUR-T80 CH1 并通电测试能开合（issue #010/#011 物理验证）
- ADR-003、ADR-004 已接受（✅ 2026-04-20）

### Phase 3.0：L2 选型 Spike（第 1 天）✅ 2026-04-20 完成

**目标**：在正式写 L3 前，决定 GantryBackend 的底层实现走哪条路径。

- [x] 实现两个 1 小时 spike：
  - **Spike A（直连 pyserial）**：`GrblDirectGantryBackend.home()` + `move_to(Position)`，用 bCNC `Sender.py` 的 char-counting 算法（`tools/spikes/spike_a_pyserial/`）——真连 Arduino，dry-run + 状态轮询 + 幂等通过
  - **Spike B（socket.io 到 OpenBuilds）**：同样两个方法，通过 OpenBuilds 的 `runCommand` / `status` 事件（`tools/spikes/spike_b_openbuilds/`）——代码完整，dry-run 通过；真连未跑（OpenBuilds 未装）
- [x] 按 ADR-004 七原则打分（详情见 [ADR-002 末尾"实施记录"](ADR-002-replace-cncjs.md#实施记录phase-30-spike-结果2026-04-20)）
- [x] **决定：选 Spike A**（DIY + bCNC char-counting）。Spike B 在原则 2/4/6 结构性丢信息（OpenBuilds 吞掉 alarm code + 原始字节）
- [x] Spike B 归档在 `tools/spikes/spike_b_openbuilds/` 保留回滚参考；Spike A 下一步扩展为 `src/hardware/gantry_backend.py`

### Phase 3.1：基础设施（第 2-3 天）

按 ADR-004 规范建立 `src/` 目录结构：

```
src/
├── __init__.py
├── hardware/
│   ├── __init__.py
│   ├── backend.py          # 抽象基类（抄 pylabrobot SCARABackend 签名 + ADR-004 扩展）
│   ├── types.py            # Position / MachineStatus / MoveResult / ... 的 pydantic 模型
│   ├── errors.py           # L3Error 基类 + MachineNotHomedError / SoftLimitExceededError / ...
│   ├── gantry_backend.py   # GantryBackend 实现（Phase 3.0 spike 决定）
│   ├── gripper_backend.py  # GripperBackend: pymodbus RTU 打 YJZK Y1
│   ├── relay_backend.py    # RelayBackend: 保留现有 DSTUR-T80 pyserial 代码
│   └── constants.yaml      # 硬件常量：端口、工作空间、速度、限位、**安全边界硬上限**
├── runlog.py               # SQLite 事件记录（ADR-004 原则 6）
├── event_bus.py            # 内存 pub/sub
├── logging.py              # 结构化 JSON 日志
├── observable.py           # @observable 装饰器
└── maestro.py              # Run orchestration（抄 PASCAL maestro.py，Phase 3 末尾）
```

- [ ] 写 `types.py` 所有数据模型 + schema 导出测试
- [ ] 写 `errors.py` 完整错误层级
- [ ] 写 `runlog.py` + `event_bus.py` + `@observable`
- [ ] 写 `backend.py` 抽象基类（纯接口，按 ADR-004 GantryBackend 完整样例）
- [ ] 写 `constants.yaml` 硬编码所有安全边界

### Phase 3.2：**稳定底座——后端打磨**（2-3 天，**2026-04-22 晚拆回原版**）

> **两次方向重排**：
> - 2026-04-22 上午：原版"后端打磨"被改成 Agent SDK 集成（合并原 Phase 3.5 进来）
> - 2026-04-22 晚：架构审阅后 PM 决定**拆回原版**——Agent 上场前先让 API 稳定
>
> 理由（详见 [phase-3.5-plan.md §背景](phase-3.5-plan.md) 的"两次方向重排"节）：
> Phase 3.1 5 Slice 刚收尾，API 还没经历大量调用磨合。直接上 Agent 会让
> Agent 在不稳定底座上连环决策，污染整条推理链（ADR-003 §1 "存在性前提"）。
> 先打磨稳再上 Agent。

按 ADR-004 七条原则的**可验证落地**推进：

- [x] **前置**：硬件 smoke — 2026-04-23 PM ✅。冷启 `$5=1` 10/10 PASS；X jog ±20mm × 10 f=3000：11 发 11 ok / 0 ALARM / 0 Pn / 0 断联。验收：`docs/verification/phase-3.2-pre-hardware-smoke.md`。**Phase 0 B 拔相跳过**——grbl 不读 D31-33 ALM（`USE_DIGITAL_INPUT` 未启用，设计决定），backend 也未实现 ALM 轮询；登记为下方 (8) 补做。Y/Z jog 未做（X 绿 + 硬件同规格 → PM 判足够）
- [x] **(1) Schema 导出**：commit `6822e0c`。`src/schema_export.py` + `docs/api-v1.json`（5 models / 1 enum / 8 errors / 12 methods）。`tests/test_schema_export.py` 5/5 PASS（确定性 + 与 commit 一致 + 错误字段合同 + home/recover 必传 idempotency_key）
- [x] **(2) mypy --strict 全绿**：commit `82f866d`。核心修复：`observable.py` 用 ParamSpec + @overload 保留被装饰方法签名；清掉 3 处 unused `type: ignore`。`Success: no issues found in 11 source files`
- [x] **(3) 错误覆盖 unit test**：commit `743ac47`。`tests/test_errors.py` 15/15 PASS。两层覆盖：静态字段合同（7 子类 parametrize，error_code 以 "L3." 开头 / severity ∈ {warning,alarm} / suggested_action* 非空 / docstring）+ 触发路径（SoftLimit 走真实 `SoftLimits.assert_contains()`，其它 5 类 raise 后 _assert_well_formed 断 6 个字段）
- [x] **(4) 幂等测试自动化**：commit `33857bc`。`tests/test_idempotency.py` 8/8 PASS。FakeBackend + clock fixture 替 `observable.time` 模拟 24h 过期；GantryBackend introspect 确认 `home` / `recover_from_alarm` TTL=24h，其它默认 5min。`@observable` 加 `__observable_ttl_s__` 元属性供测试读取
- [x] **(5) dry-run 支持 v0**：commit `429db2f`。加 `MovePlan` / `HomePlan` 两个 pydantic 模型。`home(*, idempotency_key, dry_run=False)` / `move_to(..., dry_run=False)` 返回 `*Result | *Plan`。dry-run 时 soft_limits + feed 校验仍执行（提前暴露错误），但不发字节不动 Z 刹车不依赖 connect。`tests/test_dry_run.py` 7/7 PASS（MagicMock side_effect 断"没被调用"）
- [x] **(6) 状态查询不阻塞**：commit `d840b1b`。`tests/test_get_status_concurrency.py` 4/4 PASS。StubBackend + mock `_send_line_blocking` 持 `_lock` 2s 模拟真实 `$H` 阶段；另线程跑 `home`，主线程 100 次 `get_status` 记 p50/p95/p99，断言 max < 50ms。前置断言"锁确实被持住"防虚假通过。另 3 个测试：baseline p99 < 5ms / 锁持续下仍报 last_update_ms_ago / disconnect 路径 < 5ms
- [x] **(7) Agent smoke spike**：commit `727692a`。`tools/spikes/agent_smoke.py` 用 claude-agent-sdk 0.1.65 暴露 3 工具。**PM ✅ 2026-04-23 三剧本全过**（docs/verification/phase-3.2-agent-smoke.md），累计 cost ~$1.25。超预期亮点：剧本 3 Agent 主动调 `get_status` → 发现已归零跳过 `home` → `move_to` 保留当前 Z 不偷懒置 0。SDK 集成坑（ToolSearch 多一轮 / `mcp__X__Y` 白名单格式 / async input + to_thread bridge）已归档给 Phase 3.5 复用
- [~] ~~**(8) backend ALM 轮询**~~ —— **延期到 Phase 3.3+**（2026-04-23 PM 决策，路径 D）。调研结论：grbl-Mega-5X 不改固件的前提下 backend 无法读 D31-33（串口上只跑 grbl 协议）；要读就得 `USE_DIGITAL_INPUT` + 改 `cpu_map.h` D29→D32 + 重编译重烧。Phase 3.2 的 thesis 是"稳定 API 底座"，ALARM:1/2（grbl hard/soft limit）已能报 90% 异常；**驱动器级 ALM**（拔相红闪）在 Phase 4 涂胶前才真正重要，且 Phase 3.3 做 gripper 时可能引入新 Arduino 通信方案，那时统一设计 ALM 路径更经济。Phase 3.2 整体验收文档写明此缺口

### 验收（Phase 3.2 结束标志）

- [ ] `docs/api-v1.json` 存在且 re-export 时 diff 为空（合同稳定）
- [ ] `mypy --strict src/` 本地 + CI 都绿
- [ ] `pytest tests/` 全绿，覆盖率 > 70%（骨干路径即可，非 100%）
- [ ] dry-run 模式验证：`move_to(dry_run=True)` 100 次无任何硬件交互
- [ ] **Agent smoke 三剧本全过**，验收留 `docs/verification/phase-3.2-agent-smoke.md`（PM 签字 + 每个剧本的 CLI 输出片段）

### Post-Phase cleanup

- [ ] 更新 `MEMORY.md` 迁移进度
- [ ] Commit：`Phase 3.2: 稳定底座 —— schema export + mypy strict + unit test + dry-run`

---

### Phase 3.3：GripperBackend + RelayBackend ✅ **完成 2026-04-24**（tag `v0.3.2-phase33-stable` 可选）

> **设计决定（2026-04-24 开工前拍板）**：
> - `set_force()` **不进** Phase 3.3，RS485 留给 Phase 3.5+（单独 spike）
> - `get_state()` 返回 `commanded_state` + `position_known=False`
>   （继电器只能反馈命令状态，真实位置需 RS485）
> - GripperBackend 依赖注入 RelayBackend 共享同一个 pyserial 实例
>   （不抢 DSTUR-T80 端口）
> - Issue #025 brake-skip 在 Task 5 落地（RelayBackend 幂等天然带来），
>   USB 重连在 Task 2 落地

> **Phase 3.2 遗留**：backend ALM 轮询（原任务 2.8，路径 D 延期）。若 Phase 3.3
> 装 gripper 用的是 RS485 / 独立 Arduino sketch 路径，和 ALM 轮询一起设计硬件
> side 方案（一块独立 Arduino 跑 RS485 + 读 D31-33 ALM + 独立串口回报），
> 比任由 grbl-Mega-5X 固件改动更干净。具体路径 Phase 3.3 开工时再拍。

硬件前提 ✅（2026-04-24 PM 确认）：夹爪接 DSTUR-T80 CH1；RS485 adapter 到位；夹爪通电能动。

- [x] **Task 1**：types.py + errors.py —— 加 RelayState / GripperState / RelayCommunicationError，BrakeError 降为子类。commit `0090c93`
- [x] **Task 2**：`src/hardware/relay_backend.py` —— 完整 8 通道 + 幂等（state-memo + @observable）+ USB 拔插自动重连。commit `0090c93`
- [x] **Task 3**：`tests/test_relay_backend.py` 31 tests / 94% 覆盖。commit `0090c93`
- [x] **Task 4**：`src/hardware/gripper_backend.py` 依赖注入 RelayBackend + `tests/test_gripper_backend.py` 12 tests / 100% 覆盖。commit `0090c93`
- [x] **Task 5**：GantryBackend Z 刹车迁移 —— `_release_brake` / `_lock_brake` helper 包 `RelayCommunicationError` → `BrakeError`；删除 `dstur_relay.py`（无人再引用）；brake-skip 免费副产品（纯 XY move 时 `ch_on(CH2)` 命中 state-memo 不切继电器）。commit `0090c93`
- [x] **Task 6**：schema 导出 regen `docs/api-v1.json`（加 RelayBackend / GripperBackend 方法 + 5 pydantic models + GripperCommandedState enum + 2 errors）+ mypy strict + pytest 124/124 绿 + 覆盖率 75%。commit `001663d`
- [x] **Task 7**：Agent smoke 2/3 —— `tools/spikes/agent_smoke.py` +2 工具 `gripper_open` / `gripper_close`（按 Phase 3.2 pattern：依赖注入共享 RelayBackend；不加 `set_force`）。PM 剧本 3 个全过（2026-04-24）：
  - 「把夹爪打开」→ `gripper_open` UNKNOWN→OPEN 4.2ms ✅
  - 「把夹爪夹紧」→ `gripper_close` OPEN→CLOSED 3.1ms ✅（归零协同在剧本 3）
  - 「移到 X=-100 Y=-100 并打开夹爪（×10 验 brake-skip）」→ Agent 自发现 alarm+归零+10 次循环，iter 2-10 move 稳定 0.30s（无刹车切换开销）+ gripper 全 was_noop 0.0ms ✅
  累计 cost ~$1.85（Opus）。代码 commit `6ef9ca1`，transcript `docs/verification/phase-3.3-agent-smoke.md`
- [x] **Task 8**：整体验收 `docs/verification/phase-3.3-summary.md` + MEMORY.md + migration-checklist 全打勾 + commit（本 commit）

**Issue #025 状态**：
- ✅ Brake-skip 真硬件行为证据（Task 7 剧本 3 iter 2-10 `move_to` 稳定 0.30s）
- ⏸️ USB 重连等自然掉线触发（单测已覆盖，真硬件未激活）

### Phase 3.4：端到端 smoke + maestro 最小实现（1-2 天）

跨 backend 协同 —— **从 Python 代码直接调 backend** 走通手工编排，**末尾加 Agent smoke 升级（质变体验）**：

- [ ] `src/maestro.py`：`run_pick_and_place_cycle(p1, p2)` = `home → move_to(p1) → gripper.close → move_to(p2) → gripper.open → runlog 导出`
- [ ] 故障注入测试：
  - 运动中拔 USB，API 应在 5s 内返回超时错误
  - 故意撞软限位，API 抛 `SoftLimitExceededError`（不是卡死）
  - 故意触发 ALARM:1/2，API 抛 `AlarmStateError`；手动调 `recover_from_alarm` 能恢复
- [ ] runlog.db 完整记录整个 cycle
- [ ] **Agent smoke 升级（维度 5 工作流级别 — 必须过）**：在 `agent_smoke.py` 加 **1 个工具** `maestro_pick_and_place(from_pos, to_pos)`。PM 剧本：
  - "把样品从 (-80, -80, -10) 搬到 (-200, -200, -10)" —— **一条中文指令走完整 workflow**
  这一条是 Phase 3.5 正式 Agent 之前最重要的"质变体验"——PM 第一次感受"像实验助理"而不是"按钮代理"。验收留 `docs/verification/phase-3.4-agent-smoke.md`
- [ ] Commit：`Phase 3.4: maestro 最小实现 + 端到端 smoke + Agent 工作流剧本`

---

## Phase 3.5：首个完整 Agent Demo（2-2.5 天，**详细计划见 [phase-3.5-plan.md](phase-3.5-plan.md)**）

> 2026-04-22 第二次方向重排后，Agent demo 从"合并进 3.2"**恢复**为独立
> Phase 3.5。**Phase 3.2/3.3/3.4 末尾已经做过 3 轮 Agent smoke**（见各
> Phase 的 "Agent smoke" 验收项），`tools/spikes/agent_smoke.py` 里已累积
> 6 个工具（get_status / home / move_to / gripper_open / gripper_close /
> maestro_pick_and_place）。本 Phase 不是"Agent 首次上场"，是"Agent 完整
> 产品化"。

本 Phase 做的事（细节见 phase-3.5-plan.md）：

- 把 `tools/spikes/agent_smoke.py` 正式化进 `src/agent/{tools,client,hooks}.py`
- **扩展工具集**：加 smoke 刻意没加的（`gantry_halt` / `gantry_recover_from_alarm` /
  `gripper_set_force` / `gripper_get_state` 等）
- **Streamlit 聊天面板集成**（smoke 阶段只有 CLI）：`tools/ui/agent_chat_panel.py`
- **PreToolUse hooks 实装**：A5 canUseTool 高风险确认 + ADR-005 白名单守卫
- **系统提示调优**：中文 / 英文 / 多轮任务 / 错误处理策略
- **cost 追踪**：`ResultMessage.total_cost_usd` 累加展示
- 按 Slice A/B/C/D 纵向切，每切 PM 聊天验收
- 架构审阅遗留给本 Phase 落地的项：
  - **A5** canUseTool 高风险移动确认兜底（Slice B）
  - **B1** runlog 加 `tool_use_id` 字段 + @observable 读 contextvar（Slice A）
  - **B2** errors.py 中英语言约定 docstring 约束（Slice A）
  - ADR-005 §白名单规则严格执行（不放 `send_raw_gcode` / `relay_ch_*` 进 allowedTools）

### 验收（Phase 3.5 结束标志）

见 phase-3.5-plan.md §整体验收：PM 在 Streamlit 聊天框输入"把头放到工作台
左下角然后升起来 20mm"，Agent 完成查状态 → 归零（如需）→ move_to → 中文
报告的完整链路。

---

## Phase 4：旋涂模块接入（时间 TBD，等硬件到货）

### 进入条件
- 旋涂模块购入
- Phase 3 + Phase 3.5 验收通过，L3 API 稳定

### 行动
- [ ] 按 [ADR-004](ADR-004-l3-api-design-principles.md) 规范新建 `src/hardware/spincoater_backend.py`
- [ ] 参考 PASCAL `frgpascal/hardware/spincoater.py`（`/tmp/research-clone/PASCAL/` 577 行可读）
- [ ] 参考 BirdbrainEngineer `Spin-coater-v1` 的 JSON recipe 格式
- [ ] 新建 `src/recipes/` 目录 + recipe pydantic 模型
- [ ] 定义 `SpinRecipe` schema（多阶段 RPM 曲线 + 点胶时序 + 持续时间 + 硬上限）
- [ ] 实现 `SpincoaterBackend`: `get_status` / `set_rpm` / `run_recipe(recipe, dry_run, idempotency_key)` / `halt` / `get_current_run`
- [ ] Agent demo：自然语言 "做一个 PEDOT:PSS 薄膜" → Agent 生成 recipe → 提交 → 执行

### 验收
- [ ] 所有 SpincoaterBackend 方法满足 ADR-004 七条原则
- [ ] `run_recipe(dry_run=True)` 返回完整 plan 可供 Agent/人类审批
- [ ] 端到端 Agent demo：从自然语言到完整实验记录

### Post-Phase cleanup
- [ ] 新建 `docs/guides/09-旋涂-recipe格式.md`
- [ ] 更新 `MEMORY.md`

---

## Phase 5：搬家到树莓派（时间 TBD）

### 触发条件
任一成立即触发：
- 需要在实验室独立运行，不想每次带 Mac
- 开始写 Bayesian Optimization 闭环，需要 24/7 跑实验
- 组员需要远程访问
- Agent 需要长期驻守一台机器 24/7 响应

### 行动
- [ ] 树莓派装 Raspbian + Python 3（Node.js 仅在选 OpenBuilds 作为应急 UI 时才装）
- [ ] `git pull` orchestrator 代码（git sync，不用 scp）
- [ ] 装 systemd service：`orchestrator.service` + `streamlit.service`
- [ ] 改 `constants.yaml`：`/dev/cu.usbmodem*` → `/dev/ttyACM*`、`/dev/cu.wchusbserial*` → `/dev/ttyUSB*`
- [ ] 应急 UI 策略（三选一）：
  - (a) cncjs systemd service（应急手动，不推荐新依赖，但成熟）
  - (b) Streamlit 里的最小 jog 页（已在 Phase 3 存在，零新成本）
  - (c) OpenBuilds headless + Xvfb（重，仅当 Phase 3.0 选 OpenBuilds 路径时才需要）
- [ ] Mac/手机通过 WiFi 连 Pi 的 Streamlit 面板 + Agent 聊天界面

### Post-Phase cleanup
- [ ] 新建 `docs/guides/10-树莓派部署.md`
- [ ] 更新 `MEMORY.md` 目标架构节

---

## 通用文档健康检查（每个 Phase 完成后都跑一遍）

- [ ] `MEMORY.md` 里有没有和现状冲突的过时陈述？
- [ ] `docs/guides/` 里有没有还指向已废弃路线的交叉引用？
- [ ] `docs/issues/README.md` 的表格状态是否和 `docs/issues/*.md` 单个文件一致？
- [ ] ADR-001 的回滚条件是否仍适用？如果迁移已完成大半，应该在 ADR-001 末尾加一段「迁移实际结果」
