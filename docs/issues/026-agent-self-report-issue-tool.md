# Issue #026 — Agent 自主 `report_issue` MCP 工具

**日期**：2026-04-23
**提出人**：Claude + PM（agent_smoke X 轴压测复盘）
**状态**：open
**优先级**：中
**类型**：Agent 产品化 / L6 工具集
**涉及文件**：
- `tools/spikes/agent_smoke.py`（当前 spike，工具加这里）
- Phase 3.5 起迁 `src/agent/tools/report_issue.py`
- `docs/issues/`（写入目标）

## 问题描述

当前 Agent（`agent_smoke.py` 的 `ClaudeSDKClient`）遇到硬件异常时只能把错误翻译给人类读：
- 2026-04-23 X 轴压测：Agent 命令 `move_to(x=0)` 两次撞 + 限位进 alarm，但 PM 是事后看终端 stdout 才知道发生了什么
- 同日 Issue #025（Z 刹车 EMI）是**Claude（主会话）**手写的——不是 agent 自己写的；agent 遇到 `L3.BRAKE` 时只会报给当时那个 session 的用户

**根本问题**：agent 的"故障观察"是一次性的——session 结束就散了，PM 离线时错过的信息不会留存。

**理想行为**：agent 遇到"需要人来修"的错误时，自主调 `report_issue` 工具，把现场信息按 `docs/issues/` 模板落盘。PM 下次 `git pull` 就能看到新 issue，不需要守在终端前。

## 根因分析

Agent 目前只有 3 个工具（`gantry_get_status` / `gantry_home` / `gantry_move_to`），没有"反馈观察到的问题"的能力。错误发生后，agent 靠 `human_message` / `suggested_action_zh` 打到终端，这是**同步、易丢失**的渠道。

如果要异步、持久化，需要 agent 自己能写文件。SDK 默认不开放文件工具给 MCP server，得我们主动加一个限定作用域（仅写 `docs/issues/NNN-*.md`）的 tool。

## 建议方案

### A. 新增 MCP tool（Phase 3.5 Slice B 的一部分）

```python
@tool(
    "report_issue",
    "把硬件/运动异常写成 docs/issues/NNN-*.md。仅在需要人来修的情况调用——"
    "自错（SoftLimitExceeded / NotHomed）不要报，修完重试即可。"
    "触发条件：L3.BRAKE / 串口失联 / recover 后再 alarm / 未知 grbl 错误码 / "
    "反复同一 alarm ≥3 次。",
    {
        "title": str,              # 简短标题，用于 slug 生成
        "severity": str,           # low / medium / high
        "symptoms": str,           # 观察到的现象 + 错误码
        "trigger_sequence": str,   # 触发操作序列（agent 自己记的上下文）
        "suggested_fix": str,      # agent 的假设（可选）
    },
)
async def report_issue(args: dict[str, Any]) -> dict[str, Any]:
    # 1. 读 docs/issues/ 最大编号 → N+1
    # 2. title → slug（拼音/英文转 kebab-case）
    # 3. 按 025 模板渲染 md
    # 4. 返回 issue 路径 + 编号给 agent，让它告诉 PM "已记录为 #NNN"
```

### B. 触发策略（写进 SYSTEM_PROMPT）

Agent 系统提示加一段：

```
报 issue 规则：
- 仅当遇到「需要人类决策才能修」的错误才调 report_issue：
  * L3.BRAKE（刹车/继电器故障）
  * ConnectionError（串口失联）
  * 同一 recover_from_alarm 后 5 分钟内再次 alarm（反复发作）
  * grbl 返回未知错误码（error:XX 非已知列表）
- 不报的情况（自己修即可）：
  * SoftLimitExceededError → 调整坐标重试
  * MachineNotHomedError → 先调 gantry_home
  * OperationConflictError → 等待当前操作或 halt 后重试
- 每个 session 同一 title 只报一次（去重靠 agent 记忆 + tool 内部检查最新 issue）
```

### C. 去重机制

`report_issue` 工具内部实现：
1. 读 `docs/issues/` 最近 5 个 issue
2. 若近 1 小时内有 title 相似度 > 0.7 且 severity 相同的，**不创建新文件**，返回 `已存在 #NNN，skipped`
3. 相似度算法：简单 token 重叠（`set(title_new.split()) ∩ set(title_old.split()) / union ≥ 0.7`）

### D. 字段自动补全

Agent 调用时只需填 title / severity / symptoms / trigger_sequence / suggested_fix。工具内部自动补：
- `**日期**`：today
- `**提出人**`：`Agent（{model_name} via agent_smoke.py session {uuid4[:8]}）`
- `**状态**`：open
- `**涉及文件**`：留空或 TODO，让 PM 后补
- 附一段 `git log -1 --oneline` 作为"当前 HEAD 上下文"

## 验收标准

- [ ] `tools/spikes/agent_smoke.py` 新增 `report_issue` MCP tool
- [ ] SYSTEM_PROMPT 加触发规则 + 不报清单
- [ ] 去重机制单测（mock `docs/issues/` + 触发 2 次相同 title，断言第 2 次 skipped）
- [ ] 真机 smoke：故意制造 L3.BRAKE（拔 DSTUR USB）→ agent 调 `report_issue` → 检查 `docs/issues/NNN-*.md` 生成 + 字段齐全
- [ ] 反向 smoke：故意 `move_to(x=9999)` 触发 `SoftLimitExceededError` → agent **不**报 issue（调整坐标重试即可）
- [ ] Phase 3.5 产品化时迁到 `src/agent/tools/report_issue.py`

## 相关

- Issue #025（Z 刹车 EMI）是**人手写**的，本 issue 希望下次这类故障 agent 自己写
- `phase-3.5-plan.md` Slice B（扩展工具集）可容纳本 issue
- ADR-005 白名单：`report_issue` 写入路径限制在 `docs/issues/`，不能写别处
- 今天（2026-04-23）X 轴压测事故是 catalyst：agent 撞两次 alarm 但 PM 是事后看 stdout 才知——理想情况是 agent 自己在第 2 次撞时就写 issue 示警
