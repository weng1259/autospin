# Phase 0 — 硬件安全底座

> **给新会话的说明**：你被打开是因为用户要执行项目 [ADR-001](../decision-log/ADR-001-migrate-to-grbl-stack.md) 中定义的 **Phase 0**。这是开源栈迁移的第一步，也是后续 Phase 1（grbl-Mega-5X spike）的前置条件。本文档是 Phase 0 的自包含 runbook——按顺序做完就行。
>
> **不要先**改固件、不要先烧 grbl、不要先装 cncjs。Phase 0 只做两件事：**把 ALM 报警线接上 + 把 Z 刹车软件策略定稿**。
>
> **背景阅读**（可选，但新会话建议先扫一遍）：
> - [ADR-001 迁移到 grbl 栈](../decision-log/ADR-001-migrate-to-grbl-stack.md) — 为什么迁移
> - [migration-checklist.md](../decision-log/migration-checklist.md) — 全部 Phase 的行动清单
> - MEMORY.md — 项目硬件配置速查（已自动加载）

---

## 目标

Phase 0 完成后，项目具备以下能力：

1. **驱动器一旦报警，软件层立刻能知道**：X/Y/Z 三个 2HSS57-C 驱动器的 ALM 输出接到 Arduino D31/D32/D33，可通过读引脚检测缺相/过流/超差等故障
2. **Z 轴刹车有明确的软件联动策略文档**：后续 Phase 3 的 Python orchestrator 照此实现，Phase 1-2 手动释放

这为后续工作提供的价值：

- **Phase 1（grbl-Mega-5X spike）**：grbl 需要 alarm 输入才能在故障时 feedhold；cpu_map.h 里把 D31-33 之一映射到 `CONTROL_RESET` / `CONTROL_FEED_HOLD` 就直接形成硬件联锁
- **issue #006**（硬件级 ALM + ENA 急停联锁）：物理接线部分完成一半
- **Phase 3 Python orchestrator**：能基于明确的 Z 刹车策略写 `RelayBackend` 联动逻辑，不用临时拍板

**Phase 0 明确不做的事**：
- 不改 `firmware/motor_control/motor_control.ino`（固件本来就预留了 ALM 读取，不需要改）
- 不做 ENA 急停接线（issue #006 的另一半，属于 Phase 1 或更晚）
- 不实现 Z 刹车联动代码（属于 Phase 3）
- 不接夹爪（独立硬件问题，和 Phase 0 无关）

---

## 前置条件

开工前确认：

- [ ] 项目处于 "pre-migration baseline" 状态，自研固件 `firmware/motor_control/motor_control.ino` Phase 2b 已验证通过
- [ ] 三轴电机、9 路传感器、Z 轴刹车继电器物理接线全部完成（见 `docs/guides/02-电控接线.md` §14-§26）
- [ ] 24V 电源 + Arduino USB 线工作正常
- [ ] 备齐 6 根母对母杜邦线（20~30 cm 为宜）
- [ ] （强烈建议）项目已 `git init`，当前状态提交为 `v0.1-pre-grbl-migration` tag。这样 Phase 0 改崩了可以回滚

如果 git 还没做，先做 `git init` 再回来。见 [migration-checklist.md § 全局前置](../decision-log/migration-checklist.md#全局前置项目未纳入-git-issue-021)。

---

## 动作 1：ALM 报警接线

**详细步骤见** [`docs/guides/02-电控接线.md` 第二十七章 § ALM 报警接线](02-电控接线.md#二十七第二十二阶段alm-报警接线)。

该章节包含：

- 为什么接 ALM（§27.1）
- ALM 端口的电气特性（§27.2 集电极开路光耦输出）
- Arduino INPUT_PULLUP 读取逻辑（§27.3）
- **引脚分配表**（§27.4）：D31=X, D32=Y, D33=Z, 共用 GND
- 物理位置示意图（§27.5）
- 6 步接线流程（§27.6）
- 接线自检清单（§27.7）
- 通电测试步骤（§27.8，使用 `firmware/alm_test/alm_test.ino`）
- 故障排查（§27.9）

**使用的测试 sketch**：`firmware/alm_test/alm_test.ino`——一个只读 D31/D32/D33 的极简 sketch，每 500ms 输出一行 `ALM X=OK Y=OK Z=OK`。**这个 sketch 不会影响现有的 `motor_control.ino`，测完可以刷回主固件**。

### 动作 1 验收

- [ ] 上电后 `alm_test` 串口监视器持续输出 `X=OK Y=OK Z=OK`
- [ ] 手动短接 D31 ↔ GND：串口变 `X=ALARM`；松开恢复
- [ ] 手动短接 D32 ↔ GND：串口变 `Y=ALARM`；松开恢复
- [ ] 手动短接 D33 ↔ GND：串口变 `Z=ALARM`；松开恢复
- [ ] （可选真实故障测试）断电拔 X 轴一根相线，通电看到红灯闪 6 次 + 串口 `X=ALARM`；断电装回线，通电恢复 OK
- [ ] 刷回 `firmware/motor_control/motor_control.ino`（不是强制，但测完 Phase 0 可以回到主固件；或直接停在 alm_test 等 Phase 1）

---

## 动作 2：Z 刹车软件策略定稿

Z 轴刹车的**物理接线**已在 §26 完成（DSTUR-T80 继电器 CH2 NO/COM 控制 24V 通断）。Phase 0 这一步**只定策略文字**，不写代码——代码在 Phase 3 的 Python orchestrator 里实现。

### 策略文字（定稿版，直接落到 `docs/guides/02-电控接线.md` 末尾附录 or 本节作为权威来源）

**Z 刹车默认行为矩阵**：

| 系统状态 | CH2 继电器 | 刹车物理状态 | 原因 |
|---|---|---|---|
| 上电初始 | OFF | 抱闸 | fail-safe 默认，即使 orchestrator 没启动也不会掉 |
| Orchestrator 启动完成、进入 IDLE | OFF | 抱闸 | 静止时保持抱闸，省电机电流 |
| 准备执行 Z 轴运动（MOVE Z / HOME Z 指令入队） | ON | 释放 | 在发 G-code 给 grbl 之前触发 |
| Z 轴运动中 | ON | 释放 | 维持 |
| Z 轴运动完成（状态回 idle + 位置稳定） | OFF | 抱闸 | **延迟 100~200ms 后**再抱，给机械完全停稳留时间 |
| 收到任何 alarm（ALM / limit / ESTOP） | OFF | 抱闸 | 立即执行，保护重力掉落 |
| Orchestrator 进程退出（正常或崩溃） | OFF | 抱闸 | 由继电器 NO 端子的"断电自动断开"机制兜底 |
| USB 物理断开 | OFF | 抱闸 | 同上，DSTUR-T80 失去 USB 5V 供电后 NO 继电器自然跳开 |

**X/Y 轴运动时刹车怎么办**：
- Z 轴不动时 CH2 保持 OFF（抱闸）
- X/Y 轴运动**不影响 Z 刹车状态**——即使 orchestrator 正在执行 `G0 X100 Y100`，Z 的 CH2 继电器也保持 OFF
- 这是因为 Z 轴不在动时抱闸不会发热，电机也不需要保持电流

**为什么不用 grbl 的 Spindle Enable 信号联动 Z 刹车**：
- grbl-Mega-5X 只有两路 aux 输出（Spindle + Coolant），未来夹爪可能还要用
- Spindle Enable 在 grbl 里语义是"运行主轴"，映射到 Z 刹车语义不清，未来维护会混乱
- Python orchestrator 已经掌握完整状态机，在高层统一调度更干净
- 代价是有几十毫秒的网络/串口延迟，但对旋涂仪的运动节奏完全够用

**实现责任归属**：
- Phase 0：仅文档化本表格（已完成）
- Phase 1 grbl spike 期间：**手动** `python -c "... CH2 ON ..."` 释放刹车做测试（见 §26.7 的 bash 命令片段）
- Phase 2 cncjs 接入期间：同样手动释放
- Phase 3 Python orchestrator：`GantryBackend._move_z()` 包装器实现上表逻辑；`RelayBackend` 暴露 `release_z_brake()` / `engage_z_brake()` 两个方法
- Phase 3 之前**不允许**让 orchestrator 以外的代码（比如网页按钮）直接操作 CH2，避免状态冲突

### 动作 2 验收

- [ ] 本节「Z 刹车默认行为矩阵」已写入本文档并被 git 提交
- [ ] 任何涉及 Z 刹车的未来讨论都指向本矩阵作为 single source of truth

（没有硬件测试——软件策略定稿是纯文档动作。）

---

## 动作 3：修正资料路径

MEMORY.md（旧版本）里写驱动器手册路径是 `hardware/datasheets/2HSS57-C (V2.01-T) (1).pdf`。**这个路径不存在**。正确路径是：

```
hardware/龙门架/驱动器/2HSS57-C (V2.01-T) (1).pdf
```

Phase 0 执行过程中如果发现任何文档引用旧路径，顺手修正。新会话应已通过更新后的 MEMORY.md 直接看到正确路径。

---

## Phase 0 整体验收清单

全部打勾才算 Phase 0 完成：

- [ ] **动作 1**：三轴 ALM 物理接线 + `alm_test.ino` 软件模拟 + 任选一次真实缺相测试
- [ ] **动作 2**：Z 刹车策略矩阵已落到本文档
- [ ] **动作 3**：资料路径修正（如有必要）
- [ ] 刷回 `motor_control.ino` 或停在 `alm_test.ino`（两者都可，看你 Phase 1 什么时候开始）
- [ ] 本次改动已 git commit（建议 message: `Phase 0: ALM wiring + Z brake strategy`）
- [ ] 更新 `MEMORY.md`「迁移进度」节：勾掉 `[x] Phase 0`

---

## Post-Phase 0 cleanup（新会话完成 Phase 0 后必须执行）

严格按 [migration-checklist.md Phase 0 Post-Phase cleanup 节](../decision-log/migration-checklist.md#post-phase-cleanup) 执行。核心：

1. 更新 `MEMORY.md`:
   - 迁移进度节把 `[ ] Phase 0` 改为 `[x] Phase 0`
   - 如果在 Phase 0 发现了新的注意事项（比如某根杜邦线太短），加到「注意事项」节
2. 更新 `docs/issues/README.md`: ALM 物理接线已完成对应 #006 的一部分，在 #006 条目加备注 "ALM 物理接线已在 Phase 0 完成 2026-XX-XX"（不关闭，因为 ENA 急停还没做）
3. 本文档（phase0-硬件安全底座.md）**不删除**，保留作为 Phase 0 的历史档案

---

## 下一步：Phase 1 grbl-Mega-5X spike

Phase 0 验收完成后，打开 [migration-checklist.md](../decision-log/migration-checklist.md) 的 **Phase 1** 节。那是下一个动作的权威来源。

Phase 1 摘要（这里只写备忘，不是权威）：
- 克隆 fra589/grbl-Mega-5X 到 `firmware/grbl_spike/`
- 改 `cpu_map.h` 映射我们的引脚（注意 ALM 现在可以作为 control input 了！）
- 烧录 Mega 2560
- 手发 `$H` / `G0` 验收三轴归零和点动
- 2-3 天内跑不通就回滚（回滚条件见 ADR-001 § 回滚条件）

Phase 1 是**决策点**——通过则继续迁移，不通过则回头做自研固件 Phase 2c 加减速。

---

## 给新会话的交接提示

你作为新会话开始执行 Phase 0 时：

1. **先读**：本文档 + ADR-001 + MEMORY.md 自动加载的硬件配置
2. **验证**：用 `git status` 确认项目状态干净；用 `ls firmware/` 确认 `alm_test/` 目录已存在（已在本次文档编写中创建）
3. **执行**：按「动作 1 → 动作 2 → 动作 3 → 整体验收」顺序走
4. **遇到障碍**：不要擅自换方案，ADR-001 有完整的回滚条件；如果是硬件问题，拍照/描述让用户判断
5. **完成**：按 Post-Phase 0 cleanup 清单更新 MEMORY.md 和 issues/README.md，commit，然后建议用户打开新会话做 Phase 1

Phase 0 预计总耗时：**半天**（纯硬件接线 + 测试 + 文档勾选）。
