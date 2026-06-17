# ADR-005：L3 API 暴露给 Agent 的方式 —— Claude Agent SDK

- **日期**：2026-04-22
- **状态**：已决定，**Phase 3.5 按此执行**（原定 Phase 3.2，2026-04-22 晚方向重排后回 3.5）
- **决策人**：Kevin（PM）+ Claude（实施）
- **影响范围**：Phase 3.5 施工内容、未来所有 backend（gripper/spincoater/relay）的 Agent 集成方式、Phase 3.5 Agent demo 的实现形态
- **关联**：[ADR-003](ADR-003-agent-first-vision.md) Agent-first 愿景 · [ADR-004](ADR-004-l3-api-design-principles.md) L3 API 设计规范
- **参考仓库**：[Claude Agent SDK (Python)](https://github.com/anthropics/claude-agent-sdk-python)，本地副本 `/Users/kevin/Code/BiliBili机器人/claude-agent-sdk-python`

---

## 背景

ADR-003 定下 "API 是产品，主要 consumer 是 LLM Agent"。ADR-004 规范了 API 设计标准
（严格类型 / 结构化错误 / 幂等 / 状态可查 / 安全边界 / 可观测 / dry-run）。
但**具体怎么把 L3 API 暴露给 Agent** 一直没拍：是手写 Anthropic SDK tool
use？跑独立 MCP server 进程？还是用更高层的封装？

2026-04-22 Phase 3.1 收尾后，PM 两次纠正实施方向：
1. "这不是才完成 3.1 吗，3.5 才是 agent 吗" —— 纠正跳步
2. "为 agent 架构做准备肯定是走 mcp/tools/skills 方向" —— 纠正当时 Phase 3.2 内容
   不应是传统后端工程（mypy / unit test / dry-run），而应是 Agent 集成

PM 指向 [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)
后定下本 ADR。

## 候选方案

### 方案 A：Anthropic SDK raw tool use

直接用 `anthropic` Python SDK，每次 API 调用时手动把 tool schema JSON 塞进
`tools=[...]` 参数，自己写 tool-dispatch 循环：

```python
client = anthropic.Anthropic()
tools = [
    {"name": "gantry_home", "description": "...", "input_schema": {...}},
    ...
]
while True:
    response = client.messages.create(model="claude-opus-4", tools=tools, messages=...)
    for block in response.content:
        if block.type == "tool_use":
            result = dispatch(block.name, block.input)  # 自己写
            messages.append({"role": "user", "content": [...tool_result...]})
```

**缺点**：
- tool schema JSON 要自己手写或自己从 pydantic 导出（dry-run 工作量）
- agent loop 要自己实现：多轮、上下文、错误处理、流式
- 没有"系统提示 + memory + subagents"等高级功能
- Claude Code 客户端（Claude Desktop / Cursor / Claude Code CLI）**连不上**，因为是自建协议

### 方案 B：独立 MCP server 进程

用 [mcp](https://pypi.org/project/mcp/) 官方 Python SDK 写一个 MCP server，
作为独立进程运行，暴露 JSON-RPC stdio 接口。任何 MCP client（Claude Desktop /
Cursor / VS Code / Claude Code CLI）都能连：

```python
# src/mcp_server.py（独立进程）
from mcp.server import Server
app = Server("gantry-l3")

@app.list_tools()
async def list_tools() -> list[Tool]: ...

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]: ...

if __name__ == "__main__":
    app.run_stdio()
```

然后 Claude Desktop 配置 `mcp_servers` 连接，或 Claude Code CLI 通过 `~/.config/claude-code/mcp.json` 接。

**优点**：
- 标准协议，跨客户端
- L3 进程和 Agent 进程解耦

**缺点**：
- 跨进程 IPC 开销（每次 tool 调用要序列化 + stdio）
- 共享 `GantryBackend` 实例困难（每个客户端连上都要新建？还是 L3 内部 singleton 再手动暴露？）
- 部署 3 个进程（L3 backend + MCP server + Claude client）管理复杂
- 单元测试不方便（跑 subprocess）

### 方案 C：Claude Agent SDK 的 in-process MCP server ⭐

Anthropic 官方 [`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python)
提供 `@tool` decorator + `create_sdk_mcp_server()` + `ClaudeSDKClient`：

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeSDKClient, ClaudeAgentOptions

@tool("gantry_home", "归零 XYZ 三轴（约 30s，会动机器）", {"idempotency_key": str})
async def gantry_home(args):
    result = backend.home(idempotency_key=args["idempotency_key"])
    return {"content": [{"type": "text", "text": f"归零完成，耗时 {result.duration_ms/1000:.1f}s"}]}

gantry_server = create_sdk_mcp_server(name="gantry", tools=[gantry_home, ...])

options = ClaudeAgentOptions(
    mcp_servers={"gantry": gantry_server},
    allowed_tools=["mcp__gantry__gantry_home", ...],
    system_prompt="你是旋涂仪控制 agent...",
)

async with ClaudeSDKClient(options=options) as client:
    await client.query("把头移到 X=-50")
    async for msg in client.receive_response():
        ...
```

**优点**：
- `@tool` decorator **自动从 Python 类型注解生成 JSON schema**，不用手写
- **进程内** MCP server —— 和 `GantryBackend` 共享同一进程 + 同一实例，零 IPC 开销
- `ClaudeSDKClient` 底层**打包了 Claude Code CLI**（bundled wheel），免费继承全 agent loop：系统提示、多轮对话、planning、subagents、memory、流式响应
- **Hooks**：`PreToolUse` / `PostToolUse` / `SessionStart` 等钩子可以在 agent loop 里拦截——例如"`move_to` 前强制检查 `is_homed`，否则 deny 并告诉 agent 先 `home`"——把 L3 安全边界（ADR-004 §原则 5）落到 agent 实际决策路径上
- 权限模式：`acceptEdits` / `plan` / 每次确认 / 显式 allowed_tools 白名单
- 未来如果我们还想让 Claude Desktop / Cursor 连：同一套 `@tool` 代码也能**导出**成独立 MCP server（方案 B 的形态），因为 SDK 的 server 内部就是标准 MCP

**缺点**：
- 依赖 `claude-agent-sdk` 包 + bundled Claude Code CLI（wheel 有几十 MB）
- Claude Code CLI 需要授权（ANTHROPIC_API_KEY 或订阅制登录）
- 未来要迁到其它 LLM 厂商时需要改动——但可以通过保留"纯 `@tool` 函数"层做到 SDK 无关（tools 本身不绑 Anthropic，只是 `ClaudeSDKClient` 绑）

## 决策

**方案 C（Claude Agent SDK）**，理由：

1. **一行装饰器解决 schema 导出**——ADR-004 §原则 1 的"严格类型签名"天然
   适配 `@tool(name, description, {"arg": type})` 参数，schema 自动生成
2. **in-process 解决架构**——L3 backend 是 serial/USB 驱动的有状态单例，跨
   进程 IPC 暴露是徒增复杂度，同进程最直接
3. **Agent loop 免费**——我们不自己重写 multi-turn / planning / 系统提示，用
   Claude Code 的成熟实现
4. **Hooks 是可用性优化，不是安全防线**——PreToolUse 可以在 LLM 生成前拦
   截"调 `move_to` 却未 `is_homed`"之类情况，deny 并附 `permissionDecisionReason`
   让 Agent 直接改决策，**省掉一轮"LLM 发命令 → backend 抛错 → Agent 读错 → 重决策"
   的 token/延迟成本**。但这是**优化**，不是**保障**：硬安全边界（ADR-004 §原则 5）
   永远在 `GantryBackend` 方法内部做（`move_to` 自己 raise `MachineNotHomedError`）。
   理由：hooks 是 `claude-agent-sdk` 特有机制，未来换 LLM 厂商 / 自托管模型 /
   直接 HTTP 调用 L3 时 hooks 全失效；而 backend 层的 error raise 对所有调用者
   都生效。**规则**：任何通过 hook 实现的拒绝，必须在 backend 层有对应的
   raise，确保 hook 被绕过时仍然安全。
5. **未来可切（有限）**——`@tool` decorator 本身是 `claude_agent_sdk` 引入，
   切到 OpenAI function calling / 其它 MCP 客户端需要改每个 tool 签名约定
   （args 格式、return 格式）。现实评估：我们接受**短期 2-3 年绑 Anthropic**，
   不花成本造"SDK 无关抽象层"（伪需求）。想兼容 Claude Desktop/Cursor
   的话，把 tools 导出成独立 MCP server（方案 B）的退路永远在。

## 副作用 / 要监控的事

- **Claude Code CLI 启动成本**：`ClaudeSDKClient` 是一个 subprocess，首次启动
  有 ~1-2s 开销。对交互式聊天无感知，但对"短命脚本调一次"不经济——Phase 3.5
  的 demo 要用 `async with` 保持连接而不是每条命令新起一个 client
- **API 费用可见性**：每条 `ResultMessage.total_cost_usd` 能读到本轮成本；PM
  验收时应该把 cost 累计展示在 Streamlit 上（实验室预算敏感）
- **subprocess 日志**：Claude Code CLI 的 stderr 要接进我们的日志管道，不然
  调试困难（SDK 提供 `stderr_callback` 选项）
- **权限模式选择**：默认 `allowed_tools` 白名单足够 L3 使用（`mcp__gantry__*`
  精确列出）；未来加 `mcp__gripper__*` / `mcp__spincoater__*` 要显式加白名单

## Tools 白名单规则（Agent 能用什么、不能用什么）

以下规则是 **hard rule**，任何 PR 添加新 tool 或改 `allowedTools` 都要对照：

### 白名单（Agent 允许调用）
- **设备语义级 tool**：`gantry_home` / `gantry_move_to` / `gantry_halt` /
  `gantry_get_status` / `gantry_recover_from_alarm` / `gripper_open` /
  `gripper_close` / `gripper_get_state` / `spincoater_run_recipe` 等
- 命名约定：`<device>_<动作>`，**动作是设备层面的**（"开夹爪"），**不是通道层面的**（"CH1 上电"）

### 永不进白名单（Agent 永远不能调）
- **`send_raw_gcode`**——绕过类型校验直发 grbl 字节。留给人类在 CLI
  / Streamlit 确认后使用。即便写成 `@tool`，也**不要**加进 `allowedTools`
- **`relay_ch_on(ch)` / `relay_ch_off(ch)`**——raw 通道操作。Agent 只能用
  `gripper_open`（内部调 `relay_ch_on(1)`）这样的高层封装。理由：Agent
  幻觉出"ch=2"会关 Z 刹车，"ch=1"会关夹爪——这些是单次错误就造成物理
  损失的动作
- **任何直接写 `/dev/cu.*` 的函数**
- **修改 `constants.yaml` 安全边界的任何函数**（ADR-004 §原则 5 明示）

### 灰区（Agent 可以调但 PreToolUse hook 必须拦截确认）
- **`gantry_move_to(x, y, z)` 目标超出"历史常用工作区"**（Slice B 加 `canUseTool`
  callback，让 PM 在 Streamlit 点确认；"常用工作区"初版硬编码，未来可以按
  runlog 统计）
- **首次执行新 recipe**（Phase 4 加）

### 检查点
- Slice A-D 每切的 PR 描述必须列出：该 Slice 新增的 tool 名 + 是否进白名单 + 理由
- 新加 `relay_backend` 或 `spincoater_backend` 的 Phase 3.3/4 PR 对照本节 review

---

## 实施路线（→ [phase-3.5-plan.md](phase-3.5-plan.md)）

Phase 3.5 按 Slice A/B/C/D 四切执行，每切 PM 亲自点聊天验收。细节见
`phase-3.5-plan.md`（原 phase-3.2-plan.md，2026-04-22 晚改名）。

本 ADR 落地后：

- [ ] `src/agent/` 目录新建，放 `tools.py` + `hooks.py` + `client.py`
- [ ] `demos/agent_chat.py` 命令行入口
- [ ] Streamlit 里加「🤖 Agent 聊天」板块（Slice D）
- [ ] `migration-checklist.md` 的 Phase 3.2 / 3.3 / 3.4 / 3.5 描述按 2026-04-22 晚二次重排更新（✅ 2026-04-22 晚已更新）

## 回滚 / 演进条件

**什么时候放弃方案 C 回方案 B**：
- Claude Code CLI 在树莓派上跑不稳（Phase 5 时验证）
- 需要让非 Anthropic 客户端（如自建 web 前端用 GPT）用到同一套 tools
- 成本 / 延迟无法接受

**当前不认为以上条件会出现**。Phase 5 之前 Mac 上 SDK 稳定即可。

## 参考

- [Claude Agent SDK 文档](https://docs.claude.com/en/api/agent-sdk)
- 本地仓库 `/Users/kevin/Code/BiliBili机器人/claude-agent-sdk-python`
- 关键样例：
  - `examples/mcp_calculator.py` —— in-process MCP server 模板
  - `examples/hooks.py` —— PreToolUse hook 模板
  - `examples/tool_permission_callback.py` —— 权限决策
  - `examples/streaming_mode.py` —— 流式响应处理
