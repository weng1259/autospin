# 任务卡 W0-R：Issue #028 残余收尾（工程入口与文档现状化）

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：无

## 背景（一段话）

本仓库是旋涂仪 L3 orchestrator（grbl-Mega-5X 龙门架 + USB 继电器 + RS485 外设）。
测试/类型/schema 合同基线已绿（154 passed / mypy strict 0 / schema diff 空）。
本卡收掉 Issue #028 的残余项：README 与现状脱节、RunLog SQLite ResourceWarning、
pytest 收集范围防御。

## 硬边界（违反即任务失败）

1. **禁止连接任何真实硬件**：不打开任何串口设备，不运行 tools/ 下的硬件脚本。
2. 只许改：`README.md`、`src/runlog.py`、`pytest.ini`（或新增 `pyproject.toml`）、
   对应测试文件。**禁改** `src/hardware/`、`src/config.py`、`docs/api-v1.json`。
3. 会话开始先跑 `scripts/check.sh`，必须全绿才开工；结束时必须全绿。

## 分步

1. `scripts/check.sh` 确认基线绿。
2. **README 现状化**：顶部"当前架构"改为 grbl-Mega-5X + L3 Python orchestrator
   (DIY pyserial) + Streamlit 应急面板；删除 cncjs / 自研固件 / 旧网页面板的"当前"表述
   （历史内容移到"历史沿革"小节保留）；"新会话必读"清单指向
   `docs/decision-log/architecture-roadmap.md` → ADR-003 → ADR-004。
3. **RunLog ResourceWarning**：pytest 运行时出现 `ResourceWarning: unclosed database`
   则修复 `src/runlog.py` 连接生命周期（context manager 或显式 close），并加回归测试
   （`pytest -W error::ResourceWarning` 下相关测试不报警）。若当前已无该警告，在本卡
   验收记录里写明"复验无警告"即可，不做无意义改动。
4. **pytest 收集防御**：确认根 `pytest.ini` 限定只收集 `tests/`（`testpaths = tests`），
   防止误收 `tools/`、`autospin_system/` 顶层 `test_*.py` 硬件脚本。已限定则跳过。
5. `scripts/check.sh` 全绿 → 小步 commit（每步一个 commit，中文一行消息）。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] `.venv/bin/python -m pytest tests -q -W error::ResourceWarning` 无 RunLog 相关报警
- [ ] README 第一屏无 cncjs/自研固件的"当前"表述
- [ ] 提交历史为多个小步 commit，无 `git add -A`

## 回滚

单文件级 `git checkout` 即可，无跨文件耦合。
