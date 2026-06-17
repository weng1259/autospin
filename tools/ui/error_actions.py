"""error_code → UI 快捷按钮 映射。

纯 UI 侧模块（依赖 streamlit），不进 src/hardware。
用意：把 ADR-004 §原则 2 的"结构化错误 + 建议动作"落到 PM 能点一下就自动
执行的按钮。Agent 看 `suggested_action`（英文）；PM 看 `suggested_action_zh`
并可以一键触发常见恢复。

每个 ActionSpec 的 handler 必须：
- 同步执行；阻塞期间 dashboard 走 streamlit 的 rerun 周期（pass）
- 成功时返回一个短中文字符串给 toast / flash 用
- 失败时抛 L3Error，由 dashboard 的统一错误渲染器接住
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import streamlit as st

from src.hardware.errors import ConnectionError as L3ConnectionError
from src.hardware.gantry_backend import GantryBackend


@dataclass(frozen=True)
class ActionSpec:
    label: str  # 按钮文案，如 "🏠 立即归零"
    help: str   # tooltip
    handler: Callable[[Optional[GantryBackend]], str]


# ── handlers ──────────────────────────────────────────────────────────

def _action_home(backend: Optional[GantryBackend]) -> str:
    if backend is None or not backend.is_connected():
        raise L3ConnectionError(
            human_message="没有活动连接，无法归零",
            agent_message="action_home: backend is None or disconnected.",
        )
    key = str(uuid.uuid4())
    st.session_state["home_idem_key"] = key
    result = backend.home(idempotency_key=key)
    return f"✅ 归零完成（{result.duration_ms / 1000:.1f}s · event {result.event_id[:8]}）"


def _action_reconnect(_: Optional[GantryBackend]) -> str:
    """清理旧 backend，让下一次操作触发 connect()。不立刻新建连接，
    避免在按钮点击线程里阻塞 2s（Arduino DTR reset）。"""
    old = st.session_state.pop("backend", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    st.session_state.pop("last_error", None)
    st.session_state.pop("last_status", None)
    st.session_state.pop("home_idem_key", None)
    return "🔌 已断开旧连接，下次点「🔍 查状态」会重新打开串口"


def _action_halt(backend: Optional[GantryBackend]) -> str:
    if backend is None or not backend.is_connected():
        raise L3ConnectionError(
            human_message="没有活动连接，无法急停",
            agent_message="action_halt: backend is None or disconnected.",
        )
    backend.halt()
    return "🛑 已发急停（feedhold + jog cancel）"


def _action_recover(backend: Optional[GantryBackend]) -> str:
    """一键恢复：soft_reset + (可能) unlock_alarm + home。

    复合动作下沉到 backend `recover_from_alarm()`，此 handler 只是 thin wrapper。
    参见 Slice 5 验收：docs/verification/slice-5-recovery.md。

    防抖（2026-04-22 改进）：
    - Streamlit 同步 handler 执行期间页面不重渲染，`disabled=True` 来不及显示
      —— 这是 Streamlit 固有限制，非 bug。
    - Streamlit 原生把 handler 运行期间的重复点击合并成单一 event，所以即使
      视觉上按钮没变灰，物理动作不会重复（runlog 可证）。
    - 本 handler 开头再查一次 flag：如果确实被重入（理论上不会），直接
      raise 避开 backend 并发。
    """
    if backend is None or not backend.is_connected():
        raise L3ConnectionError(
            human_message="没有活动连接，无法恢复",
            agent_message="action_recover: backend is None or disconnected.",
        )
    if st.session_state.get("recovery_in_progress"):
        # 理论上 Streamlit 不会走到这 —— double-insurance，防止未来 fragment
        # 或 background thread 重构引入并发
        raise L3ConnectionError(
            human_message="上一次恢复动作还在进行，请稍候",
            agent_message="action_recover: re-entered while in_progress flag set.",
        )
    st.session_state["recovery_in_progress"] = True
    try:
        result = backend.recover_from_alarm(idempotency_key=str(uuid.uuid4()))
        if not result.actions_taken:
            return f"✅ 已是 Idle，无需恢复（event {result.event_id[:8]}）"
        steps_zh = {
            "soft_reset": "soft-reset",
            "unlock_alarm": "$X",
            "home": "重新归零",
        }
        chain = " → ".join(steps_zh.get(a, a) for a in result.actions_taken)
        return (
            f"✅ 恢复完成（{result.entry_state.value} → {chain} → "
            f"{result.final_status.state.value}，"
            f"{result.duration_ms / 1000:.1f}s · event {result.event_id[:8]}）"
        )
    finally:
        st.session_state.pop("recovery_in_progress", None)


# ── registry ──────────────────────────────────────────────────────────

ACTIONS: dict[str, ActionSpec] = {
    "L3.MACHINE_NOT_HOMED": ActionSpec(
        label="🏠 立即归零",
        help="新 idem key 执行 home()，约 30s（机器会动）",
        handler=_action_home,
    ),
    "L3.CONNECTION": ActionSpec(
        label="🔌 重新连接",
        help="清理旧 backend，下次状态查询/操作会自动重开串口",
        handler=_action_reconnect,
    ),
    "L3.BRAKE": ActionSpec(
        # DSTUR 继电器 USB 子系统重置后，旧 serial 句柄失效。GantryBackend.close()
        # 会把 DSTUR 一起关掉，下一次 home/move 会走 DSTURRelay.connect() 重建。
        label="🔌 重新连接",
        help="USB EMI 后 DSTUR 句柄失效；清理 backend 让下次操作重开两个串口",
        handler=_action_reconnect,
    ),
    "L3.UNEXPECTED": ActionSpec(
        # 裸 Python 异常（AssertionError / AttributeError 等）通常是 backend race
        # 或串口突然失效。重连 backend 是最有概率让系统回到干净状态的动作。
        # 完整 traceback 已经在 @observable 打到 stderr，下一次触发能直接看堆栈。
        label="🔌 重新连接",
        help="未预期异常；清理 backend 让下次操作重开两个串口",
        handler=_action_reconnect,
    ),
    "L3.OPERATION_CONFLICT": ActionSpec(
        label="🛑 立即停",
        help="发 feedhold `!` + jog cancel `0x85`",
        handler=_action_halt,
    ),
    "L3.ALARM_STATE": ActionSpec(
        # Slice 5：组合恢复动作 — `\x18` + `$X` + `$H`。包含重新归零，~30s。
        # 由 backend.recover_from_alarm 负责顺序 + 幂等；UI 只做防双击。
        label="🔧 清除并恢复",
        help="soft-reset → unlock → 重新归零（约 30s，会动机器）",
        handler=_action_recover,
    ),
    # L3.SOFT_LIMIT_EXCEEDED / L3.HOMING_TIMEOUT 不配快捷按钮 —— 建议文案里
    # 已经指明用户该做什么（调坐标 / 看 DSTUR / 复位硬件）
}


def get_action(error_code: str) -> Optional[ActionSpec]:
    return ACTIONS.get(error_code)
