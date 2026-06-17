# ADR-002：替换 cncjs 作为 L2 上位机

> **状态**：**部分修订**（2026-04-20 晚）——原推荐 OpenBuilds CONTROL 为首选，但 [ADR-003](ADR-003-agent-first-vision.md) 确立 Agent-first 框架后，L2 角色降级为"应急备份"，评估维度从"用户 UI 体验"翻转为"L3 API 构建成本"，最终选型延后到 ADR-004 实现时重估。见文末 **2026-04-20 晚修订** 章节。
> **关联**：[ADR-001](ADR-001-migrate-to-grbl-stack.md)（原决策：cncjs 作为 L2） · [ADR-003](ADR-003-agent-first-vision.md)（Agent-first 重排优先级） · [ADR-004](ADR-004-l3-api-design-principles.md)（L3 API 规范影响 L2 选型）· [migration-checklist.md](migration-checklist.md) Phase 2
> **触发**：Phase 2 安装后稳定性测试发现 cncjs 多个架构缺陷，重新评估 L2 选型

## 背景：ADR-001 选 cncjs 的假设

ADR-001 选 cncjs 的核心理由是"3000+ 机器在用的成熟工具，开箱即用"。Phase 2 验收条件（`migration-checklist.md` L129-131）：
- cncjs 浏览器面板完全替代现有 `web_control/index.html` 的手动操作能力
- 急停 / feedhold / 限位触发都能在 UI 上看到状态反馈

## 2026-04-20 实测结果（三条决定性证据）

**证据 1：cncjs 不使用 grbl 1.1+ 的 `$J=` 协议**
- 全量 grep `/opt/homebrew/lib/node_modules/cncjs/dist/cncjs/app/main.*.bundle.js` 中 `$J=`：**零真实匹配**（唯一命中是同名变量）
- 实际 jog 命令构造（`main.bundle.js` 中搜 `G91` 上下文）：`G91\nG0 X±n\nG90` 三条 gcode
- 副作用：失去 "新 `$J=` 自动 cancel 前一个" 的关键属性

**证据 2：cncjs Feeder 在 ALARM 时死锁**
- 位置：`/opt/homebrew/lib/node_modules/cncjs/dist/cncjs/server/controllers/Grbl/GrblController.js:654-666`
- `runner.on('alarm', ...)` **不调 `feeder.reset()`**
- 结果：`feeder.state.pending=true` 永久挂住；后续用户命令在 line 1581 `if (!feeder.isPending()) next()` 检查下静默沉积队列；UI 彻底冻结，只能靠用户手动点 Reset 按钮恢复

**证据 3：grbl 层无过错，硬件层无过错**
- 30s 静止 `?` 轮询 × 60 次：`Pn:` 字段 **0 次触发**（传感器线路洁净）
- 100 次激进 jog（±15mm × F3000）：`Pn:` **0 次触发**
- 对照实验：有流控 100/100 稳定，无流控 89/100 稳定触发 `ALARM:11`
- 结论：问题在 **L2 缺严格流控 + ALARM 恢复**，不在传感器 / 杜邦线 / EMI

## 替代品评估（2026-04-20 source-verified）

全部候选 `git clone` 到 `/tmp/cncjs-alternatives-research/`，**必须读源码验证**（不信 README）：

| 项目 | 最近 commit | `$J=` | ALARM 恢复 | grbl-Mega-5X 3 轴 | L3 Python API | Mac+Pi 支持 | 结论 |
|---|---|---|---|---|---|---|---|
| cncjs 1.11.0 | — | ❌ 无 | ❌ 死锁 | ✅ | socket.io | ✅ | **现状，要换** |
| **gSender 1.6.1** | 2026-04-17 | ✅（`GrblController.js:2018`） | ❌ **完全复刻 cncjs bug**（`GrblController.js:841-889`） | ✅ | socket.io | ✅ | 🚫 **避坑**：换它等于换汤不换药 |
| **OpenBuilds CONTROL 1.0.390** | 2025-06-02 | ✅（`app/js/jog.js:471,520,571,621,671,721`） | ⚠️ 半自动：ALARM 不自动 drain（`index.js:1467-1487`），但 `clearAlarm` method 2 完整 drain（`index.js:2145-2200`）可被 L3 emit 触发 | ✅（通用 status parser） | socket.io，事件：`runCommand`/`jog`/`clearAlarm`/`stop`/`data`/`status` | ✅ mac-arm64 dmg + `pi-install.sh` | ⭐ **首选** |
| **bCNC 0.9.16** | 2026-04-15 | ✅（`controllers/GRBL1.py:36`） | ✅ **正确实现**（`_GenericController.py:261-273`：pop cline+sline + _stop=True） | ✅ | Python 原生 import + HTTP Pendant | ⚠️ macOS 上 Tk UI 糙 | 🥈 **备选（Phase 3 源码可抄）** |
| UGS v2.1.22 | 2026-04-12 | ✅ | ✅ | ✅ | **真 REST API（Swagger/jakarta）** | ✅ | 不推荐（JVM+OpenGL 对 Pi 太重） |
| spjs | 2020-09-14 | ❌ | N/A | N/A | — | — | 💀 死项目 |

**关键反直觉发现**：**所有 Node 栈 grbl sender 都把 ALARM 自动恢复当"设计妥协"推给用户**。这不是 bug，是"谁该决定怎么恢复"的权责分配。Phase 3 orchestrator 接管 ALARM 恢复是**必须**的，无论选哪个 L2。

## 决策建议：OpenBuilds CONTROL

### 理由
1. `$J=` + `0x85` 原生支持，jog 连点问题根治
2. `clearAlarm` socket handler 提供**可脚本化**的 ALARM 恢复通道（cncjs/gSender 完全没有）
3. socket.io API 和 cncjs 架构相近，L3 Python 用 `python-socketio` 直连
4. 官方 mac-arm64 发行 + `pi-install.sh`，覆盖我们的开发 + Phase 5 生命周期
5. 源码仍是 Node+Electron，学习曲线平

### 成本
- **迁移**：1-2 天（L2 安装 + L3 socket client 改 event 名）
- **L3 职责扩容**：Phase 3 必须自己实现「监听 ALARM → 自动 emit clearAlarm → 从已知位置重试或人工介入」。约 +1 天工作量
- **放弃**：现有 cncjs 装好的任何配置（我们没配 macro，所以无损失）

### 避坑：**不要选 gSender**
- line 841-889 的 `on('alarm')` 处理跟 cncjs 1.11.0 **一字不差**（只 emit 不 reset feeder）
- 我们逃离 cncjs 的首要原因在 gSender 完全复刻
- 除非 fork 并 patch，否则这个选择毫无意义

### 备选：bCNC 用于 Phase 3 参考
即使不选 bCNC 作为 L2，它的 **character-counting streaming** 实现（`Sender.py:40,645,851` + `_GenericController.py:261-281`，不到 50 行）是 grbl 1.1+ 官方推荐算法的干净 Python 参考。Phase 3 orchestrator 严格流控**直接抄**比自己写省一天。

## 对 ADR-001 的影响

ADR-001 的**迁移方向仍然正确**——不是推倒重来。需要修订的只有：

1. L2 选型：`cncjs` → `OpenBuilds CONTROL`
2. L3 职责描述：增加「ALARM 监听 + 自动恢复 + 严格流控」明示项
3. Phase 3 预算：从 2-3 天 → 4-5 天（吸收 L3 扩容）
4. migration-checklist.md 的 Phase 2 验收标准需要增加：
   - **ALARM 恢复测试**：故意撞硬限位 → L3 socket emit `clearAlarm` data=2 → 验证 queue 清空、下一条 jog 能执行
   - **快速点击 jog 测试**：50ms 间隔点击 10 次 → 第 11 次 `0x85` 干净中断、末位置无漂移
   - **Z 刹车集成**：L3 jog Z 前 DSTUR CH2 释放、emit gcode、完成后回锁

## 回滚条件

选 OpenBuilds 后，任一条触发则回滚评估 bCNC：
- 装 OpenBuilds 后 socket.io API 与 python-socketio 不能互通
- ALARM 恢复流程在我们硬件上不可靠（如 `clearAlarm` method 2 的 500ms `$X` 时序对 Z 刹车不友好）
- Raspberry Pi 上 Electron 跑不动或内存吃太多

## 产物（已归档）

- 调研工具：`tools/grbl_stability_test.py`（复现工具 + health/jog/home/duplex 四模式）
- cncjs Feeder 补丁（未应用）：`tools/patches/cncjs_feeder_alarm_fix.patch`
- 诊断日志：`tools/_stability_logs/`
- 项目记忆（3 条）：
  - `project_cncjs_stability_diagnosis.md`
  - `project_cncjs_jog_architecture_finding.md`
  - `project_usb_subsystem_reset_mode.md`
- 本文档：`docs/decision-log/ADR-002-replace-cncjs.md`

## 原"待决定"（2026-04-20 中）

1. ~~批准 OpenBuilds CONTROL 作为新 L2？~~ → **延后**，见下节
2. ~~Phase 2 状态：回退为未通过？~~ → **确认回退**，见 migration-checklist 更新
3. ~~Phase 3 预算扩到 4-5 天是否接受？~~ → **确认，且进一步扩大**（API 规范工作量，见 ADR-004）

---

## 2026-04-20 晚修订：Agent-first 框架下的 L2 重估

### 为什么需要修订

本 ADR 中段推荐 OpenBuilds 的理由是：
1. 用 `$J=`，jog 问题根治
2. `clearAlarm` socket handler 提供可脚本化恢复
3. UI 接近 cncjs，用户熟悉
4. Mac+Pi 发行齐全

但 [ADR-003](ADR-003-agent-first-vision.md) 确立了 Agent-first 愿景，UI 角色从"主要交互"降级到"~5% 场景的应急备份"。这推翻了原评估矩阵中"用户 UI 体验"占的主导权重。**新评估维度**：

| 旧排序维度（UI-first） | 新排序维度（Agent-first） |
|---|---|
| UI 体验 | L3 API 构建成本 |
| 手动操作便捷度 | 调用链路短、无中间层翻译 |
| 3D 预览漂亮 | 状态可观测、延迟低 |
| 归零/jog 按钮直观 | 能被 Python 直接 import / 调用 |

### 重新评估（同样的候选，翻转权重）

| 候选 | Agent-first 视角 | 新排名 |
|---|---|---|
| **OpenBuilds CONTROL** | 功能齐全但**多一层 socket.io 翻译**；Pi 上要跑 Electron（headless 需 fork 或 Xvfb）；clearAlarm 等半自动恢复逻辑需要 L3 理解 OpenBuilds 特有语义 | 🥈 备选 |
| **bCNC（作为库）** | **Python 原生**，`Sender.py` 可直接 import 进 L3；streaming / ALARM 处理 / char-counting 流控全是教科书实现；无 RPC 翻译，L3 方法直接映射到 grbl 行为；缺 UI（与 Agent-first 一致，UI 不是重点） | ⭐ **新首选（库模式）** |
| **DIY（抄 bCNC 流控算法）** | 50 行 char-counting 拷进 L3 + 自己封 pyserial；无任何第三方运行时依赖；调试、追溯、演进最自由；**前提是**团队接受"自己维护 gcode streaming"的长期责任 | ⭐⭐ **候补首选（自研模式）** |
| gSender | 复刻 cncjs bug（已验证），不考虑 | 🚫 |
| cncjs | 原问题不变，不考虑 | 🚫 |
| UGS | Java/Netbeans 对 Pi 太重 | 不考虑 |

### 延后到 ADR-004 实现时做出最终选择

**为什么延后**：L2 选型现在取决于 L3 API 的实现路径，而 L3 API 路径由 ADR-004 定义。在 ADR-004 的规范下开始写 L3 时，会立刻感知到：

- 如果选 **bCNC-as-library** / DIY：L3 `GantryBackend` 直接 pyserial + char-counting，每一个 `move_to` 都是透明的 grbl 调用
- 如果选 **OpenBuilds**：L3 `GantryBackend` 变成 socket.io 客户端，要处理连接管理、事件反序列化、clearAlarm method 2 语义等。可能带来"API 假返回 success 但底层还在队列"的风险，违反 ADR-004 原则 4（状态可查询）

**判据**（Phase 3 第一天做个 spike 决定）：
- 写一个 `GantryBackend.home()` 实现
- 分别用"直接 pyserial"和"socket.io 到 OpenBuilds"两条路径各写 1 小时
- 对比：哪个实现对 ADR-004 七条原则更自然满足？
- 定结果，写进 ADR-004 的实施记录

### 应急 UI 怎么办？

Agent-first 下 L2 UI 的唯一作用是"~5% 场景：Agent 挂了人要手动接管"。三种兜底：

1. **保留 cncjs（不打补丁）** 作为应急工具。它卡死的缺点在应急场景里**反而可接受**——人类手动操作不会像 Agent 那样快速连发 jog 触发死锁
2. 用 `tools/grbl_stability_test.py` 的 CLI 接口作为终极兜底（命令行 jog，永远最可靠）
3. L5 Streamlit 里嵌入一个最小 jog 页（直连 L3 API）——这是和 L3 一起开发的副产品，几乎零成本

建议 3 + 1 组合：默认走 3；3 也挂了走 1。

## 执行计划（修订后）

1. ✅ 决策：延后 L2 最终选型到 Phase 3 第一天 spike 定
2. ✅ Phase 2 状态回退为未通过（见 migration-checklist）
3. ⏸ cncjs 不再推荐安装/卸载——保留现状作为应急工具，也不打补丁（除非 Phase 3 spike 发现离不开它）
4. ⏭ 进 Phase 3：先实现 `GantryBackend.home()` 两个路径的 spike，决定最终 L2 形态

---

## 实施记录：Phase 3.0 spike 结果（2026-04-20）

两条路径各实现一个 `GantryBackend.home() + move_to(Position)`，都在 `tools/spikes/`。Spike A（pyserial + bCNC char-counting）真连 Arduino 跑通 dry-run、状态轮询、幂等缓存；Spike B（socket.io 到 OpenBuilds）连接失败符合预期（未装 Electron 应用），但代码、错误翻译、dry-run 路径验证通过。

按 ADR-004 七原则打分（✅ 原生满足 / ⚠️ 能实现但别扭 / ❌ 结构性损失）：

| 原则 | Spike A（pyserial） | Spike B（OpenBuilds） |
|---|---|---|
| 1. 类型签名 | ✅ Pydantic 直接校验用户输入 | ✅ 同样能包一层 |
| 2. 结构化错误 | ✅ 直接抓 grbl `ALARM:N` 附 code | ❌ OpenBuilds 只给 `comms.alarm` 字符串，丢 alarm code |
| 3. 幂等性 | ✅ L3 缓存 | ✅ 同 |
| 4. 状态可查询 | ✅ `?` 随时拿实时 `<...>` | ⚠️ 只能信 100ms 轮询的 `status` 帧，有 stale 风险 |
| 5. 安全边界 | ✅ 发 gcode 前校验，完全可控 | ⚠️ `jog` 事件吃 CSV 字符串、`clearAlarm` 用 magic number 1/2，语义泄漏 |
| 6. 可观测 | ✅ 每一字节我们写我们记 | ⚠️ OpenBuilds 在中间吞吐，runlog 拿不到 grbl 原始字节 |
| 7. Dry-run | ✅ 不开 serial 即可 | ✅ 同 |

**决定：选 Spike A（DIY + bCNC 流控）**。Spike B 在原则 2/4/6 有结构性损失——把 grbl 的 alarm code、实时响应、原始字节全托管给 OpenBuilds，违反 Agent-first 要求的"API 返回值不能比人眼能看到的少"。Spike A 代码量小（~280 行带注释）、无第三方运行时、无 Electron + Xvfb 开销、Phase 5 搬 Pi 零改动。bCNC `Sender.py` L644-860 的 char-counting 算法直接移植 50 行。

**产物**：`tools/spikes/common.py`（共享 pydantic 类型 + L3 错误层级）、`tools/spikes/spike_a_pyserial/gantry_backend.py`（保留，Phase 3.1/3.2 扩展为 `src/hardware/gantry_backend.py`）、`tools/spikes/spike_b_openbuilds/gantry_backend.py`（**归档保留**作为回滚参考，不删除）。
