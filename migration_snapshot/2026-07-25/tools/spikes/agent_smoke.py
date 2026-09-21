#!/usr/bin/env python3
"""Phase 3.2 task 2.7 + Phase 3.3 task 7 —— Claude Agent SDK spike（5 工具 CLI 交互）。

目标（ADR-004 + ADR-005 首次真·Agent 集成）：
- 踩 claude-agent-sdk 0.1.65 集成坑（@tool schema / ClaudeSDKClient /
  allowed_tools / async ↔ sync bridge / 错误透传）
- 给 Phase 3.5 留可复用模板 —— 产品化时从这里迁到 src/agent/

硬约束（Phase 3.5 未到，本 spike 刻意简化）：
- 不写 src/agent/（Phase 3.5）
- 不加 PreToolUse hooks / canUseTool 高风险确认（Phase 3.5）
- 不做 Streamlit 集成（只有 CLI）
- 最小可跑通即可，踩完 SDK 坑就算成功

使用：
    tools/spikes/.venv/bin/python tools/spikes/agent_smoke.py

PM 验 Phase 3.2 smoke 1/3（3 剧本，只 gantry）—— 已通过 2026-04-23。
PM 验 Phase 3.3 smoke 2/3（3 剧本，加 gripper）：
    剧本 1（单夹爪）：
        > 把夹爪打开
        Agent 调 gripper_open（DSTUR CH1=OFF），语义翻译到中文回报
    剧本 2（归零后夹紧）：
        > 归零后夹紧
        Agent 按序 home + gripper_close，Z 刹车在 home 内部自动释放/锁
    剧本 3（XY-only 联动，顺便验 Issue #025 brake-skip）：
        > 移到 X=-100 Y=-100 并打开夹爪
        Agent 调 get_status → move_to (Z 不变 → 不切 CH2) → gripper_open
        连做 10 次不应把 DSTUR USB 打挂（brake-skip state-memo 生效）

前置：
- claude login 已登录（PM 2026-04-23 决策：走订阅不拉 API key）
- /dev/cu.wchusbserial110（grbl）+ /dev/cu.usbmodem* (DSTUR-T80) 两个串口都在
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

# 本文件在 tools/spikes/，`from src...` 需要项目根在 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from src.hardware.errors import L3Error
from src.hardware.gantry_backend import GantryBackend
from src.hardware.gripper_backend import GripperBackend
from src.hardware.relay_backend import RelayBackend
from src.hardware.types import (
    GripperActionResult,
    GripperCommandedState,
    HomeResult,
    MoveResult,
    Position,
)


# ─────────────────── backend singletons ───────────────────
# @tool 函数是异步的 free functions；靠模块级变量访问同一个实例。
# Phase 3.5 时会改成 contextvar / 注入，但 spike 阶段简单直接。
# RelayBackend 是 **共享** 实例 —— 单一 DSTUR-T80 串口同时给 Z 刹车（CH2）和
# 夹爪（CH1）用，必须依赖注入。各自 `RelayBackend()` 会抢端口。
_RELAY: RelayBackend | None = None
_BACKEND: GantryBackend | None = None
_GRIPPER: GripperBackend | None = None


def _get_backend() -> GantryBackend:
    if _BACKEND is None:
        raise RuntimeError(
            "backend 未初始化：main() 应在 connect 成功后才注册 tool"
        )
    return _BACKEND


def _get_gripper() -> GripperBackend:
    if _GRIPPER is None:
        raise RuntimeError(
            "gripper 未初始化：main() 应在 connect 成功后才注册 tool"
        )
    return _GRIPPER


def _text(s: str, is_error: bool = False) -> dict[str, Any]:
    """SDK tool 返回格式 wrapper。"""
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if is_error:
        out["is_error"] = True
    return out


def _format_l3_error(e: L3Error) -> str:
    """错误透传：Agent 决策读 agent_message + suggested_action，
    用户显示读 human_message + suggested_action_zh。一次把两份都给 Agent，
    由它综合。"""
    return (
        f"[L3 error] code={e.error_code} severity={e.severity} "
        f"recoverable={e.recoverable}\n"
        f"human_message (zh): {e.human_message}\n"
        f"agent_message (en): {e.agent_message}\n"
        f"suggested_action (en): {e.suggested_action}\n"
        f"suggested_action_zh: {e.suggested_action_zh}"
    )


# ─────────────────── 3 个工具 ───────────────────

@tool(
    "gantry_get_status",
    "查询龙门架实时状态：state (idle/run/jog/home/alarm/hold/disconnected) + "
    "position (x/y/z in mm, 机器坐标系) + is_homed + alarm_code + "
    "last_update_ms_ago. 只读无副作用，亚毫秒级返回。任何决策前都应先调此工具。",
    {},
)
async def gantry_get_status(args: dict[str, Any]) -> dict[str, Any]:
    try:
        # backend 方法是同步 + 可能阻塞几 ms；用 to_thread 避免堵 event loop
        st = await asyncio.to_thread(_get_backend().get_status)
    except L3Error as e:
        return _text(_format_l3_error(e), is_error=True)
    return _text(
        f"state={st.state.value} "
        f"position=(x={st.position.x_mm:.3f}, y={st.position.y_mm:.3f}, "
        f"z={st.position.z_mm:.3f}) "
        f"is_homed={st.is_homed} alarm_code={st.alarm_code} "
        f"planner_buffer_free={st.planner_buffer_free} "
        f"rx_buffer_free={st.rx_buffer_free} "
        f"last_update_ms_ago={st.last_update_ms_ago:.0f}"
    )


@tool(
    "gantry_home",
    "XYZ 三轴归零（Z 先，然后 X+Y 并行）。约 30s，会动机器。自动处理 Z 刹车时序。"
    "幂等：同 idempotency_key 24h 内复用缓存不重复物理动作。"
    "注意：调前应先 gantry_get_status 看 is_homed —— 已归零且无明确重归需求就不要再调。",
    {"idempotency_key": str},
)
async def gantry_home(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("idempotency_key") or str(uuid.uuid4())
    try:
        result = await asyncio.to_thread(
            _get_backend().home, idempotency_key=key
        )
    except L3Error as e:
        return _text(_format_l3_error(e), is_error=True)
    if isinstance(result, HomeResult):
        return _text(
            f"归零完成 ✓ "
            f"position=(x={result.position_after_pulloff.x_mm:.3f}, "
            f"y={result.position_after_pulloff.y_mm:.3f}, "
            f"z={result.position_after_pulloff.z_mm:.3f}) "
            f"duration={result.duration_ms/1000:.1f}s "
            f"event_id={result.event_id} "
            f"idempotency_key={key}"
        )
    # dry-run 路径：spike 未开放 dry_run 参数，此分支理论不走
    return _text(f"HomePlan (dry_run): {result}")


@tool(
    "gantry_move_to",
    "绝对坐标运动（grbl $J= jog 命令）。机器坐标系：x/y ∈ [-275, -5] mm, "
    "z ∈ [-90, -5] mm（归零后原点在 +极限，两端各留 5mm 避让限位开关）。"
    "feed_mm_min 默认 2000，硬上限 3000。"
    "前置：必须 is_homed=True。超 soft_limit 抛 SoftLimitExceededError。",
    {
        "x_mm": float,
        "y_mm": float,
        "z_mm": float,
        "feed_mm_min": float,
    },
)
async def gantry_move_to(args: dict[str, Any]) -> dict[str, Any]:
    try:
        target = Position(
            x_mm=float(args["x_mm"]),
            y_mm=float(args["y_mm"]),
            z_mm=float(args["z_mm"]),
        )
        feed = float(args.get("feed_mm_min") or 2000.0)
        result = await asyncio.to_thread(
            _get_backend().move_to, target, feed_mm_min=feed
        )
    except L3Error as e:
        return _text(_format_l3_error(e), is_error=True)
    except (KeyError, TypeError, ValueError) as e:
        return _text(
            f"参数错误：{type(e).__name__}: {e}. "
            f"需要 x_mm/y_mm/z_mm (float)，feed_mm_min 可选",
            is_error=True,
        )
    if isinstance(result, MoveResult):
        return _text(
            f"移动完成 ✓ "
            f"final=(x={result.final_position.x_mm:.3f}, "
            f"y={result.final_position.y_mm:.3f}, "
            f"z={result.final_position.z_mm:.3f}) "
            f"duration={result.duration_ms/1000:.2f}s "
            f"event_id={result.event_id}"
        )
    return _text(f"MovePlan (dry_run): {result}")


@tool(
    "gripper_open",
    "松开夹爪（DSTUR-T80 CH1=OFF，24V 断开 → 夹爪松开）。幂等：同 "
    "idempotency_key 5 min 内复用；commanded_state 已是 OPEN 也会 was_noop 短路。"
    "和 Z 刹车共享同一个 DSTUR-T80 继电器板（CH2），backend 层已串行化写入，"
    "Agent 不需要考虑时序。约 0.3s 完成。",
    {"idempotency_key": str},
)
async def gripper_open(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("idempotency_key") or str(uuid.uuid4())
    try:
        result = await asyncio.to_thread(
            _get_gripper().open, idempotency_key=key
        )
    except L3Error as e:
        return _text(_format_l3_error(e), is_error=True)
    if isinstance(result, GripperActionResult):
        noop_tag = "（幂等命中，未发送继电器命令）" if result.was_noop else ""
        return _text(
            f"夹爪松开 ✓{noop_tag} "
            f"commanded_state_after={result.commanded_state_after.value} "
            f"duration={result.duration_ms:.1f}ms "
            f"event_id={result.event_id} "
            f"idempotency_key={key}"
        )
    return _text(f"GripperActionPlan (dry_run): {result}")


@tool(
    "gripper_close",
    "夹紧夹爪（DSTUR-T80 CH1=ON，24V 输出 → 夹爪夹合）。幂等：同 "
    "idempotency_key 5 min 内复用；commanded_state 已是 CLOSED 也会 was_noop 短路。"
    "力度目前固定（RS485 力控 Phase 3.5+ 再接），适合常规拾取样品。约 0.3s 完成。",
    {"idempotency_key": str},
)
async def gripper_close(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("idempotency_key") or str(uuid.uuid4())
    try:
        result = await asyncio.to_thread(
            _get_gripper().close, idempotency_key=key
        )
    except L3Error as e:
        return _text(_format_l3_error(e), is_error=True)
    if isinstance(result, GripperActionResult):
        noop_tag = "（幂等命中，未发送继电器命令）" if result.was_noop else ""
        return _text(
            f"夹爪夹紧 ✓{noop_tag} "
            f"commanded_state_after={result.commanded_state_after.value} "
            f"duration={result.duration_ms:.1f}ms "
            f"event_id={result.event_id} "
            f"idempotency_key={key}"
        )
    return _text(f"GripperActionPlan (dry_run): {result}")


# ─────────────────── 消息渲染 ───────────────────

def _display(msg: Any) -> None:
    """把 SDK 消息打到终端。Phase 3.5 Streamlit 会替换，此处只给 CLI。"""
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"\n🤖 {block.text}")
            elif isinstance(block, ToolUseBlock):
                print(f"  🔧 tool_use: {block.name}  input={block.input}")
    elif isinstance(msg, UserMessage):
        # tool_result 以 UserMessage 的 block 形式进来
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                content = block.content
                text = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text += c.get("text", "")
                else:
                    text = str(content)
                # 截断避免刷屏
                trimmed = text[:500] + ("…" if len(text) > 500 else "")
                print(f"  ↩  tool_result: {trimmed}")
    elif isinstance(msg, SystemMessage):
        pass
    elif isinstance(msg, ResultMessage):
        if msg.total_cost_usd is not None:
            print(f"  💰 cost=${msg.total_cost_usd:.6f}")


# ─────────────────── main ───────────────────

SYSTEM_PROMPT = """你是智能旋涂仪（XYZ 龙门架 + 电夹爪）的控制助理。通过下列工具操作机器：
- mcp__gantry__gantry_get_status: 查龙门架状态（位置 + is_homed + alarm）
- mcp__gantry__gantry_home: 归零（约 30s，物理动作）
- mcp__gantry__gantry_move_to: 移到绝对坐标
- mcp__gantry__gripper_open: 松开夹爪（DSTUR CH1=OFF）
- mcp__gantry__gripper_close: 夹紧夹爪（DSTUR CH1=ON）

硬件背景：
- 机器坐标系：归零后原点在 +极限方向；合法位置 x/y ∈ [-275, -5] mm, z ∈ [-90, -5] mm（两端各留 5mm 避让限位）
- 未 is_homed 时 move_to 会抛 MachineNotHomedError
- 归零 30s 物理动作 + 电机磨损 + Z 刹车磨损，不要随便重归
- 夹爪初始 commanded_state=UNKNOWN：从未下过命令，物理真值未知，首次 open/close 会实发继电器
- 夹爪和 Z 刹车共享同一块 DSTUR-T80 继电器板（不同通道），backend 层已串行化，Agent 不用关心时序

行为要求：
1. 涉及 home / move_to / 夹爪 的任务前，可先 gantry_get_status 看机器状态
2. 已 is_homed=True 时用户说"归零"——告诉用户已归过并问"是否要强制重归"，默认不再调 home
3. 工具返回的英文字段 / grbl 状态值要翻译成中文回报
4. 遇 L3 error：把 suggested_action_zh 展示给用户；如果错误是 MachineNotHomedError，主动建议先归零
5. 夹爪操作不需要先 get_status —— 夹爪状态是自己的 commanded_state，get_status 只管龙门架
6. 用户说中文，你就用中文回复；说英文就用英文

示例好行为：
- 用户："机器什么状态？" → 调 get_status → "机器在 idle 状态，位置 X=-100 Y=-100 Z=-5，已归零"
- 用户："把夹爪打开" → 调 gripper_open → "夹爪已松开"
- 用户："归零后夹紧" → get_status → (若未归) home → gripper_close → 回报
- 用户："移到 X=-100 Y=-100 并打开夹爪" → get_status（确认 is_homed）→ move_to (x=-100, y=-100, z=<保持当前>) → gripper_open
"""


async def chat_loop(client: ClaudeSDKClient) -> None:
    print(
        "\n=== Agent smoke 已就绪（5 工具：gantry×3 + gripper×2）===\n"
        "Phase 3.3 剧本参考（见文件头 docstring）：\n"
        "  1. 把夹爪打开\n"
        "  2. 归零后夹紧\n"
        "  3. 移到 X=-100 Y=-100 并打开夹爪（重复 10 次验 brake-skip）\n"
        "`exit` / `quit` / Ctrl-D 退出。\n"
    )
    loop = asyncio.get_event_loop()
    while True:
        try:
            user_input = await loop.run_in_executor(
                None, lambda: input("你> ")
            )
        except (EOFError, KeyboardInterrupt):
            print("\n退出。")
            return
        user_input = user_input.strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", ":q"}:
            return
        try:
            await client.query(user_input)
            async for msg in client.receive_response():
                _display(msg)
        except Exception as e:
            print(f"[agent 错误] {type(e).__name__}: {e}")


async def main() -> None:
    global _RELAY, _BACKEND, _GRIPPER

    # 1) DSTUR-T80 继电器串口先起来（Z 刹车 + 夹爪 都要用它）
    _RELAY = RelayBackend()  # /dev/cu.usbmodem6670E00119391（默认）
    print(f"连接继电器 {_RELAY.port} ...")
    try:
        _RELAY.connect()
    except L3Error as e:
        print(f"✗ 继电器连接失败：{e.human_message}")
        print(f"  detail: {e.agent_message}")
        sys.exit(1)
    print(f"✓ 继电器已连")

    # 2) grbl 龙门架，复用同一个 RelayBackend 实例（dep injection）
    _BACKEND = GantryBackend(relay=_RELAY)
    print(f"连接龙门架 {_BACKEND.port} ...")
    try:
        _BACKEND.connect()
    except L3Error as e:
        print(f"✗ 龙门架连接失败：{e.human_message}")
        print(f"  detail: {e.agent_message}")
        _RELAY.close()
        sys.exit(1)
    print(f"✓ 龙门架已连")
    try:
        _BACKEND.validate_grbl_settings()
    except L3Error as e:
        print(f"✗ grbl 参数校验失败：{e.human_message}")
        print(f"  detail: {e.agent_message}")
        _BACKEND.close()
        _RELAY.close()
        sys.exit(1)
    print("✓ grbl 参数已校验")

    # 3) 夹爪 backend 也复用 _RELAY（CH1）
    _GRIPPER = GripperBackend(_RELAY)
    print(f"✓ 夹爪已就绪 (channel={_GRIPPER._channel}, "
          f"commanded_state={GripperCommandedState.UNKNOWN.value})")
    print(f"\n启动 Agent（SDK: claude-agent-sdk 0.1.65）\n")

    server = create_sdk_mcp_server(
        name="gantry",
        version="0.1.0",
        tools=[
            gantry_get_status,
            gantry_home,
            gantry_move_to,
            gripper_open,
            gripper_close,
        ],
    )

    options = ClaudeAgentOptions(
        mcp_servers={"gantry": server},
        allowed_tools=[
            "mcp__gantry__gantry_get_status",
            "mcp__gantry__gantry_home",
            "mcp__gantry__gantry_move_to",
            "mcp__gantry__gripper_open",
            "mcp__gantry__gripper_close",
        ],
        system_prompt=SYSTEM_PROMPT,
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            await chat_loop(client)
    finally:
        print("\n关闭 backends ...")
        try:
            if _BACKEND is not None:
                _BACKEND.close()
        finally:
            _BACKEND = None
        try:
            if _RELAY is not None:
                _RELAY.close()
        finally:
            _RELAY = None
            _GRIPPER = None
        print("✓ 已关闭")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
