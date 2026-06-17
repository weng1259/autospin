# Issue #028 — 工程入口与 API 合同漂移收口

**日期**：2026-04-24
**提出人**：Codex（本地代码/文档审查）
**状态**：open
**优先级**：中
**类型**：工程 / 文档 / API 合同
**涉及文件**：
- `pyproject.toml` 或 `pytest.ini`（新增）
- `docs/api-v1.json`
- `src/schema_export.py`
- `README.md`
- `src/runlog.py`

## 问题描述

当前项目的 L3 API 设计和测试骨架已经比较完整，但工程入口存在几处漂移，导致新会话或 CI 很容易得到错误信号：

- 直接运行 `python -m pytest` 会误收集 `Code/` 外部参考项目和 `tools/*test*.py` 硬件脚本，触发 `opentrons` / `serial` / 外部包缺失错误。
- 正确限定为 `tools/spikes/.venv/bin/python -m pytest tests -q` 后，结果是 `111 passed, 1 failed`；唯一失败为 `docs/api-v1.json` 没跟上当前代码里的 Relay / Gripper 类型。
- `src/schema_export.py` 目前只导出 `GantryBackend`，但 Phase 3.3 已新增 `RelayBackend` / `GripperBackend`，需要决定是否纳入公共 API 合同。
- `README.md` 仍混有旧自研固件 / 旧网页面板描述，与当前 grbl + L3 orchestrator 路线不完全一致。
- 测试中出现 `ResourceWarning: unclosed database`，指向 `RunLog` SQLite 连接生命周期仍有小问题。

这些问题不代表核心逻辑不可用，但会降低项目可维护性：新人、Agent 或 CI 不知道“正确的测试入口”和“真实的当前架构”。

## 复现记录

在仓库根目录直接运行：

```bash
python -m pytest -q
```

结果：pytest 收集到 `Code/PASCAL-main`、`Code/perovskite_auto_system`、`tools/gripper_test.py` 等非单元测试入口，出现多处导入错误。

使用项目 venv 且限定 `tests/`：

```bash
tools/spikes/.venv/bin/python -m pytest tests -q
```

结果：`111 passed, 1 failed`。失败项：

```text
tests/test_schema_export.py::test_schema_matches_checked_in_contract
api-v1.json drift detected
```

## 建议方案

1. 新增 `pyproject.toml` 或 `pytest.ini`，固定 pytest 只收集 `tests/`，避免误扫外部参考代码和硬件手动脚本。
2. 更新 `docs/api-v1.json`，并同步调整 `src/schema_export.py`：
   - 若 `RelayBackend` / `GripperBackend` 已是正式 L3 API，则加入 schema export。
   - 若仍是内部实现或 Phase 3.3 草稿，则明确排除，并避免相关类型污染公共合同。
3. 整理 `README.md` 顶部“当前真实状态”：
   - 明确当前主线是 grbl-Mega-5X + Python L3 orchestrator。
   - 标注 `firmware/motor_control`、`web_control/*.html` 为历史/调试入口，而非当前主路径。
   - 把“当前 Phase”更新到 Phase 3.3 或实际状态。
4. 修复 `RunLog` SQLite 连接警告，避免测试期间产生 `ResourceWarning: unclosed database`。

## 验收标准

- [ ] 仓库根目录运行 `tools/spikes/.venv/bin/python -m pytest -q` 不再误收集 `Code/` 或 `tools/` 下的硬件脚本。
- [ ] `tools/spikes/.venv/bin/python -m pytest tests -q` 全部通过。
- [ ] `tests/test_schema_export.py::test_schema_matches_checked_in_contract` 通过，`docs/api-v1.json` 与当前公共 API 一致。
- [ ] README 第一屏能准确说明当前架构、当前 Phase、废弃/历史入口。
- [ ] pytest 输出不再出现 `RunLog` SQLite `ResourceWarning`。

## 相关

- ADR-004：L3 API 设计规范要求 API 合同稳定、可审查。
- Phase 3.3 正在新增 Relay / Gripper backend，本 issue 应在该阶段合并前收口。
- `.gitignore` 已排除 `Code/`，但 pytest 仍会收集本地未跟踪目录，因此需要测试配置而不是只依赖 git ignore。
- **关联新 issue #029**：最小 CI 基建——028 收口的是"能跑、跑出正确结果"；#029 负责"每次改动都自动跑"，两者合起来才能根治本类漂移。

---

## 补充（Claude，2026-04-24）

在"项目整体评价"过程中复验了本 issue 的现状，确认问题依然存在，同时发现几处**范围已扩大**，需要追加记录。Codex 原描述仍然正确，以下是增量观察。

### 1. `api-v1.json` 漂移已不只是字段级，而是结构级

Codex 审查时 drift 体现在 `RelayActionResult` 等少量类型。**2026-04-24 复验**，`tests/test_schema_export.py` 输出已经包含新增的枚举/模型：

```
+     "GripperCommandedState": { "names": ["OPEN","CLOSED", ... ] }
```

说明 Phase 3.3 的 `GripperBackend` 类型正在进入公共 API 面。**收口时要做的判定**（原方案第 2 条的延伸）：

- [ ] 明确决定 Phase 3.3 的 `GripperBackend` / `RelayBackend` **是否**作为 L3 API v1 的一部分对外暴露（即被 Agent 直接调用），还是仅作为内部实现
- [ ] 若纳入：`src/schema_export.py` 要把它们的 BaseModel 注册到 `build_schema()`，并在 `docs/api-v1.json` 里正式固化
- [ ] 若不纳入：要在 `schema_export.py` 明确排除，避免 pydantic 通过类型引用把内部类型顺带带入

### 2. 测试入口已从"误收集"升级为"真失败"

Codex 审查时报告 `111 passed, 1 failed`。**2026-04-24 复验**（在 `tools/spikes/.venv/` 下、只收集 `tests/`）结果是：

```
1 failed, 79 passed, 44 errors in 14.54s
```

44 个 `error` 全部是 `AttributeError: 'GantryBackend' object has no attribute '_brake'`——Phase 3.3 的 `_brake: DSTURRelay → _relay: RelayBackend` 重构已落地 `src/hardware/gantry_backend.py`，但 `tests/test_gantry_backend_unit.py::fake_serial_backend` fixture 还在引用 `b._brake._ser`。

这**不是**本 issue 的原始范围，但它印证了本 issue 的核心论点：**没有 CI 卡点时，重构和 schema 变更很容易穿过开发者的本地验证**。建议收口方案增加一条：

- [ ] 5. 修复 `tests/test_gantry_backend_unit.py` 里 `_brake` → `_relay` 的 fixture 引用，让该文件所有 test 在 Phase 3.3 合并前恢复绿色（或明确 skip 带 TODO）

### 3. schema 导出应该**自动化**，不是靠"手动记得跑"

原方案第 2 条只说"更新 `docs/api-v1.json`"，一次性修复；但 drift 之所以反复发生，根因是没有卡点。建议至少二选一：

- **方案 A（推荐）**：在项目根加 `scripts/check_schema.sh` 或 Makefile target `make schema-check`，内容等价于：
  ```bash
  tools/spikes/.venv/bin/python -m src.schema_export --out /tmp/api-v1.json
  diff -u docs/api-v1.json /tmp/api-v1.json
  ```
  并在 `CONTRIBUTING.md` / `README.md` 明示"改任何 types.py / errors.py 后要跑它"
- **方案 B（更稳）**：pre-commit hook（需要 `pre-commit` 包），在 commit 时自动重新导出 `docs/api-v1.json` 并把它 stage 进去。成本是引入 pre-commit 依赖

**落地哪个方案最好在 #029（最小 CI 基建）里一起想清楚**——CI workflow 和 pre-commit 是同一个"自动卡点"主题的两端，分开做会重复。

### 4. `README.md` 修订的最小增量

原方案第 3 条写得很对，我补充一点**具体的**当前漂移，便于收口时 checklist：

- 顶部警告条还在说 "grbl-Mega-5X + **cncjs** + 薄 Python orchestrator"，但 cncjs 已在 ADR-002/003 正式废弃，应改为 "grbl-Mega-5X + **L3 Python orchestrator (DIY pyserial)** + Streamlit/Agent 前端"
- "当前 Phase" 里 "下文当前进度⋯涉及 Phase 2c/2d/2e 自研固件、网页面板迁移等条目已被迁移取代，待 Phase 1 验收后统一清理" 这段话应删除——Phase 1 早已验收，现在是 **Phase 3.2 完成 / Phase 3.3 进行中**
- "新会话开始工作前必读" 清单应把 `ADR-003` 和 `architecture-roadmap.md` 放在最前（当前只列了 ADR-001），和 MEMORY.md 里的阅读顺序对齐

### 5. 优先级建议

Codex 标了"中"。考虑到：

- Phase 3.3 合并在即（gripper + relay backend 已存在磁盘上待提交）
- 合并后 `api-v1.json` 漂移会成为 Phase 3.3 PR 的一部分，不再是独立工程债

建议**在 Phase 3.3 合并前的同一批提交里收口本 issue**，不要拖到 3.3 之后。严格说这仍是"中"优先级，但**时间窗很窄**。
