# Issue #029 — 最小 CI 基建（pytest + mypy + schema diff）

**日期**：2026-04-24
**提出人**：Claude（项目整体评价后追加）
**状态**：open
**优先级**：中
**类型**：工程 / 基建
**涉及文件**：
- `.github/workflows/ci.yml`（新增）
- `pyproject.toml` 或 `pytest.ini`（与 #028 共用，约束 pytest 收集范围）
- `.pre-commit-config.yaml`（可选）
- `Makefile` 或 `scripts/`（可选）
- `README.md`（加一小节"本地开发 / CI 检查"）

## 问题描述

项目当前工程素养已经很高（`mypy --strict` 全绿、80+ unit test、`docs/api-v1.json` 作为 API 合同、requirements.txt 精确 pin），但**这些检查全部靠人工本地执行**：

- 没有 `.github/workflows/`，每次 push / PR 不会自动验证
- 没有 pre-commit hook，`git commit` 前不会自动 format / lint / 跑 mypy
- 没有 `pyproject.toml` / `pytest.ini`，pytest 收集范围靠"开发者记得只指到 `tests/`"
- 没有 `ruff` / `black` 等 lint/format 自动化，代码风格一致性完全靠人工保持

**结果**：#028 描述的所有漂移（schema drift、README 滞后、pytest 入口错误）、以及 2026-04-24 复验发现的 `_brake → _relay` 重构导致 44 个 test error 未被开发者感知——都是"本地没跑全 → 没有自动卡点 → 进入 main 分支"这条链路的典型症状。

单人 + 学生项目规模下，CI 的性价比更高而不是更低：它把"必须记得跑全套检查"的认知负担从 PM 脑子里卸下来，放到机器上。

## 复现记录

```bash
ls /Users/kevin/Code/智能旋涂仪/.github 2>/dev/null
# (empty)

ls pyproject.toml pytest.ini setup.cfg 2>/dev/null
# (empty)

ls .pre-commit-config.yaml 2>/dev/null
# (empty)
```

当前唯一的"自动化"是 `requirements.txt` 版本 pin 和 `mypy.ini`（若存在）——都是**静态配置**，没有**执行时机**的自动化。

## 建议方案

按"成本低 → 收益高"的顺序，分三档推进。每一档都可以独立验收，不必一次性做完。

### Tier 1（必做，半天内完成）：GitHub Actions 最小 workflow

新增 `.github/workflows/ci.yml`，对每次 push 和 PR 触发以下三个 gate：

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
      - run: pip install -r tools/spikes/requirements.txt
      - name: pytest (tests/ only)
        run: python -m pytest tests/ -q
      - name: mypy --strict
        run: python -m mypy --strict src/
      - name: schema freshness
        run: |
          python -m src.schema_export --out /tmp/api-v1.json
          diff -u docs/api-v1.json /tmp/api-v1.json
```

**约束**：
- **不跑硬件 smoke**（`tools/*test*.py`）——CI 环境没有串口 / grbl / DSTUR
- **不跑 `tools/spikes/agent_smoke.py`**——需要 Claude login，且成本 ~$0.5/剧本
- Python 版本和本地 venv 对齐（3.14）
- 依赖安装用 `tools/spikes/requirements.txt`，保持与本地 venv 同步

### Tier 2（推荐，1-2 小时）：约束 pytest 收集范围

与 #028 方案 1 合并。新增 `pyproject.toml`（或 `pytest.ini`），至少包含：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

效果：即使开发者在仓库根跑 `pytest`，也不会误扫 `tools/*test*.py` 和 `Code/` 下的参考项目。CI 脚本里的 `pytest tests/` 等价，但本地 DX 立刻变好。

### Tier 3（可选，半天内完成）：pre-commit hook 本地卡点

新增 `.pre-commit-config.yaml`：

```yaml
repos:
  - repo: local
    hooks:
      - id: schema-export
        name: Regenerate docs/api-v1.json
        entry: tools/spikes/.venv/bin/python -m src.schema_export --out docs/api-v1.json
        language: system
        files: ^src/hardware/(types|errors)\.py$
        pass_filenames: false
      - id: mypy
        name: mypy --strict src/
        entry: tools/spikes/.venv/bin/python -m mypy --strict src/
        language: system
        files: ^src/.*\.py$
        pass_filenames: false
```

好处：`git commit` 时自动重新导出 schema，commit 本身就带上最新 `api-v1.json`。CI 里的 schema diff gate 就几乎永远不会触发。

**代价**：引入 `pre-commit` 包依赖，所有贡献者要 `pre-commit install` 一次。单人项目这个代价极小；如果以后有组员贡献代码，文档里要提一笔。

### Tier 4（未来考虑，不在本 issue 内）：lint/format

ruff + black / ruff format。**暂不做**，因为：

- 当前代码风格一致性由人工维护已经足够好
- 引入 lint 会导致一次性大 diff（所有既有代码要过一遍 formatter），增加 review 负担
- 等 Phase 3.5 结束、团队扩大（组员真正要提交 Python 代码）时再做更合适

## 验收标准

### Tier 1（必做）
- [ ] `.github/workflows/ci.yml` 已提交
- [ ] 在 GitHub Actions 上能看到 CI run 成功（pytest + mypy + schema diff 三个 gate 全绿）
- [ ] 制造一次人为漂移（改 `src/hardware/types.py` 但不重新导出 schema），push 后 CI 应失败在 "schema freshness" gate
- [ ] `README.md` 加一小节 "CI / 本地检查"，说明三个 gate 和本地怎么跑

### Tier 2（推荐）
- [ ] 仓库根 `python -m pytest -q` 只收集 `tests/`，不再误扫
- [ ] 与 #028 第 1 条合并落地

### Tier 3（可选）
- [ ] `.pre-commit-config.yaml` 存在且被 `pre-commit install`
- [ ] 改 `src/hardware/types.py` 后 `git commit`，`docs/api-v1.json` 被自动重新生成并一同提交

## 相关

- **#028**：工程入口与 API 合同漂移收口——#028 是"让本地能跑正确的检查"，#029 是"每次改动都自动跑"。**两者最好在同一批 PR 里收口**，否则 Tier 1 CI 一上来就会红（因为 #028 的漂移没修）
- **ADR-004**：L3 API 设计规范要求 API 合同稳定、可审查——CI 的 schema diff gate 是这条原则的工程化保障
- **feedback_agent_first_scope_discipline.md**：规划"为 Agent 做准备"的工作时要走 MCP/tools/skills 方向——但本 issue 是传统后端工程基建，不是"为 Agent 准备"，而是"为项目长期可维护"。独立成立

## 不做的事（scope guard）

- ❌ **硬件 smoke 进 CI**：串口 + grbl + DSTUR 不可在 GitHub runner 上复现，永远保持手动
- ❌ **agent_smoke 进 CI**：需要订阅 / API key 且有真实成本，不能在每次 PR 触发
- ❌ **lint/format 全面铺开**：见 Tier 4 说明，等团队规模到位再做
- ❌ **覆盖率门禁**：当前 70% 的数字是 Phase 3.2 自然结果，不是目标；加 coverage gate 会诱导"为覆盖率而写测试"，违背"测试围绕 Agent 真实场景"的原则
