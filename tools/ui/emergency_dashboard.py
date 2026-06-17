"""L3 应急控制台 — Phase 3.1 验收 UI（不是产品 UI）。

ADR-003 给 Streamlit 留 ~10% 预算，本页面是其中一小部分用于 PM 点按钮验收。
Phase 3.5 Agent demo 上线后，此页面将被聊天界面替代。

启动:
    cd /Users/kevin/Code/智能旋涂仪
    tools/spikes/.venv/bin/streamlit run tools/ui/emergency_dashboard.py \\
        --server.headless=true --server.port=8501 \\
        --server.runOnSave=true --browser.gatherUsageStats=false
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import streamlit as st

# Repo root in sys.path so `import src.*` works without a packaging step.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402

from src.config import get_config  # noqa: E402
from src.hardware.errors import (  # noqa: E402
    ConnectionError as L3ConnectionError,
    L3Error,
)
from src.hardware.gantry_backend import GantryBackend  # noqa: E402
from src.hardware.types import MachineState, Position  # noqa: E402
from src.runlog import RUNLOG  # noqa: E402
from tools.ui.error_actions import _action_recover, get_action  # noqa: E402


def _wrap_unexpected(e: Exception) -> L3Error:
    """把裸 Python 异常（例如 backend race 抛的 AttributeError / AssertionError）
    包成 L3Error，避免 Streamlit 默认错误 UI 泄漏完整栈追踪到页面上。
    完整 traceback 已由 @observable 打到 stderr + runlog agent_message。"""
    wrapped = L3Error(
        human_message=f"内部错误：{type(e).__name__}: {e}",
        agent_message=repr(e),
    )
    wrapped.error_code = "L3.UNEXPECTED"
    wrapped.severity = "alarm"
    wrapped.recoverable = True
    wrapped.suggested_action_zh = (
        "检测到未预期的后端错误（通常是 USB 重置 / 串口并发）。"
        "建议点 sidebar「断开并重连」后重试；若持续出现拔插 USB 线物理重连。"
    )
    wrapped.suggested_action = "Drop backend and reconnect; check stderr log for traceback."
    return wrapped

DEFAULT_PORT = "/dev/cu.wchusbserial110"

STATE_DISPLAY: dict[MachineState, tuple[str, str]] = {
    MachineState.IDLE: ("#1f7a1f", "✅ Idle"),
    MachineState.RUN: ("#1c5fb4", "🏃 Run"),
    MachineState.JOG: ("#1c5fb4", "🏃 Jog"),
    MachineState.HOME: ("#1c5fb4", "🏠 Home"),
    MachineState.HOLD: ("#b48a1c", "⏸ Hold"),
    MachineState.ALARM: ("#b41c1c", "⚠️ Alarm"),
    MachineState.DISCONNECTED: ("#b41c1c", "🔌 Disconnected"),
    MachineState.UNKNOWN: ("#666666", "❓ Unknown"),
}


def _drop_session_backend() -> None:
    """连接断开 / 主动重连 时统一清理：backend + 当前归零事务 key。"""
    old = st.session_state.pop("backend", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    st.session_state.pop("home_idem_key", None)


def _get_or_connect_backend(port_path: str) -> GantryBackend:
    backend = st.session_state.get("backend")
    if backend is None:
        backend = GantryBackend(port=port_path)
        backend.connect()  # may raise L3ConnectionError / BrakeError
        st.session_state["backend"] = backend
    return backend


def _render_recover_button(*, key: str, container=st) -> None:
    """独立于 error 框的「🔧 清除并恢复」按钮（Slice 5）。

    `state in (Alarm, Hold)` 时显示，PM 不必先看到错误框也能触发恢复
    （例如冷启 grbl 默认进 Alarm，无 API 调用产生 error）。两个入口位置
    （状态卡片 + 实时位置 fragment）共用本 helper，确保防抖和错误路径
    一致；`recovery_in_progress` session flag 由 `_action_recover` 管理。
    """
    busy = bool(st.session_state.get("recovery_in_progress"))
    clicked = container.button(
        "🔧 清除并恢复",
        type="primary",
        width="stretch",
        disabled=busy,
        help=(
            "上一次恢复仍在进行..." if busy else
            "soft-reset → unlock → 重新归零（约 30s，会动机器）"
        ),
        key=key,
    )
    if not clicked:
        return
    backend = st.session_state.get("backend")
    try:
        # st.spinner 在 handler block 期间给 PM 一个"恢复中"的视觉反馈
        # —— 按钮本身不能 disabled（Streamlit 同步 handler 限制），但 spinner
        # element 会在 script flow 里立刻渲染出来，覆盖按钮没变灰的观感。
        with st.spinner(
            "🔧 恢复中：soft-reset → unlock → 重新归零（约 30s，会动机器）..."
        ):
            note = _action_recover(backend)
        st.session_state.pop("last_error", None)
        try:
            b2 = st.session_state.get("backend")
            if b2 is not None and b2.is_connected():
                st.session_state["last_status"] = b2.get_status()
        except L3Error:
            pass
        st.toast(note, icon="✅")
        st.rerun(scope="app")
    except L3Error as e:
        st.session_state["last_error"] = e
        st.session_state["last_status"] = None
        st.rerun(scope="app")
    except Exception as e:
        st.session_state["last_error"] = _wrap_unexpected(e)
        st.session_state["last_status"] = None
        st.rerun(scope="app")


def _render_l3_error(err: L3Error, *, key_prefix: str) -> None:
    """统一 L3Error 渲染（Slice 4）：
    - severity=warning → 黄色 st.warning（用户可自行改输入纠正）
    - severity=alarm   → 红色 st.error（机器侧异常，需恢复动作）
    - 中文建议优先 `suggested_action_zh`，缺省回退英文 `suggested_action`
    - 若 error_actions 注册表有该 error_code，尾部渲染「按建议操作」快捷按钮
      （同一错误在不同位置渲染时用 key_prefix 保证 streamlit key 唯一）
    """
    box_fn = st.warning if err.severity == "warning" else st.error
    zh = err.suggested_action_zh or err.suggested_action or "（无）"
    box_fn(
        f"❗ **{err.human_message}**\n\n"
        f"建议：{zh}\n\n"
        f"错误代码 `{err.error_code}` · severity `{err.severity}`"
        + ("" if err.recoverable else " · 不可自动恢复")
    )

    # 左边：按建议操作（如果注册了）；右边：手动清除
    spec = get_action(err.error_code)
    c_act, c_clr, _ = st.columns([2, 1, 5])
    clr_key = f"err_clear__{key_prefix}__{err.error_code}"
    if c_clr.button("❌ 清除", help="关闭此提示（不执行任何动作）", key=clr_key):
        st.session_state.pop("last_error", None)
        st.rerun(scope="app")
    if spec is None:
        return
    btn_key = f"err_action__{key_prefix}__{err.error_code}"
    # 恢复进行中时所有 action 按钮 disabled —— 既防并发，也给 PM 视觉反馈
    recovering = bool(st.session_state.get("recovery_in_progress"))
    if c_act.button(
        spec.label,
        help=("恢复进行中..." if recovering else spec.help),
        type="primary",
        key=btn_key,
        disabled=recovering,
    ):
        try:
            backend = st.session_state.get("backend")
            note = spec.handler(backend)
            st.session_state.pop("last_error", None)
            # 刷新状态卡片 —— 让 PM 立刻看到"恢复后"的机器状态而不是空白
            try:
                b2 = st.session_state.get("backend")
                if b2 is not None and b2.is_connected():
                    st.session_state["last_status"] = b2.get_status()
            except L3Error:
                pass  # 刷新失败就算了，至少错误已经消了
            st.toast(note, icon="✅")
            st.rerun(scope="app")
        except L3Error as ee:
            # handler 本身抛新错 —— 挂到 last_error 让整页重渲染
            st.session_state["last_error"] = ee
            st.session_state["last_status"] = None
            st.rerun(scope="app")
        except Exception as ee:
            # 裸异常（例如 handler 里的 TOCTOU race）→ 包成 L3.UNEXPECTED
            st.session_state["last_error"] = _wrap_unexpected(ee)
            st.session_state["last_status"] = None
            st.rerun(scope="app")


def _render_status_card(status) -> None:
    color, label = STATE_DISPLAY.get(status.state, STATE_DISPLAY[MachineState.UNKNOWN])
    cols = st.columns([1, 1.3, 1, 1])
    with cols[0]:
        st.markdown(
            f'<div style="font-size:0.85rem;color:#888;">状态</div>'
            f'<div style="font-size:1.4rem;color:{color};font-weight:700;">{label}</div>',
            unsafe_allow_html=True,
        )
    with cols[1]:
        p = status.position
        st.markdown(
            f'<div style="font-size:0.85rem;color:#888;">位置 (mm)</div>'
            f'<div style="font-family:monospace;font-size:1.05rem;line-height:1.45;">'
            f"X = {p.x_mm:+9.3f}<br>"
            f"Y = {p.y_mm:+9.3f}<br>"
            f"Z = {p.z_mm:+9.3f}</div>",
            unsafe_allow_html=True,
        )
    with cols[2]:
        homed = "✅ 是" if status.is_homed else "❌ 否"
        st.markdown(
            f'<div style="font-size:0.85rem;color:#888;">归零过</div>'
            f'<div style="font-size:1.2rem;">{homed}</div>',
            unsafe_allow_html=True,
        )
    with cols[3]:
        st.markdown(
            f'<div style="font-size:0.85rem;color:#888;">最后更新</div>'
            f'<div style="font-size:1.05rem;">{status.last_update_ms_ago:.0f} ms 前</div>',
            unsafe_allow_html=True,
        )
    if status.alarm_code is not None:
        st.warning(f"alarm_code = {status.alarm_code}")
    bf_p = status.planner_buffer_free
    bf_r = status.rx_buffer_free
    if bf_p is not None or bf_r is not None:
        st.caption(f"grbl 缓冲：planner={bf_p}/35  rx={bf_r}/256")

    # Slice 5：state in (Alarm, Hold) 时在状态卡底部显示永久恢复按钮，
    # 不依赖先触发 error 框（PM 冷启 grbl / 手动撞限位也能一键恢复）
    if status.state in (MachineState.ALARM, MachineState.HOLD):
        rc1, _ = st.columns([2, 5])
        with rc1:
            _render_recover_button(key="status_card_recover")

    with st.expander("原始 grbl 状态行"):
        st.code(status.raw or "(无)", language="text")


# ────────────────────────────────────────────────────────────
st.set_page_config(page_title="L3 应急控制台", page_icon="🔧", layout="wide")
st.title("🔧 L3 应急控制台")
st.caption("Phase 3.1 · Slice 4（错误中文化 + 建议动作）· 仅供 PM 验收用")

CFG = get_config()

# ── sidebar ──
with st.sidebar:
    st.subheader("连接设置")
    port = st.text_input("Arduino 串口", value=DEFAULT_PORT)
    backend_alive = "backend" in st.session_state
    st.write(f"会话缓存：{'✅ 已连接' if backend_alive else '⚪ 未连接'}")
    if st.button("断开并重连"):
        _drop_session_backend()
        st.session_state.pop("last_status", None)
        st.session_state.pop("last_error", None)
        st.rerun()

    st.divider()
    st.subheader("归零事务 key")
    cur_key = st.session_state.get("home_idem_key")
    st.code(cur_key or "(未生成 — 下次归零会创建)", language="text")
    st.caption("同一 key 重复点 = 幂等命中，不会重复物理动作")
    if st.button("🆕 新建归零事务"):
        st.session_state.pop("home_idem_key", None)
        st.rerun()

    st.divider()
    st.subheader("软限位（constants.yaml）")
    sl = CFG.soft_limits
    st.code(
        f"X ∈ [{sl.x_min_mm}, {sl.x_max_mm}]\n"
        f"Y ∈ [{sl.y_min_mm}, {sl.y_max_mm}]\n"
        f"Z ∈ [{sl.z_min_mm}, {sl.z_max_mm}]",
        language="text",
    )

# ── 实时位置 fragment（100ms 刷新）──
st.subheader("🎯 实时位置")


@st.fragment(run_every="0.1s")
def live_position() -> None:
    backend = st.session_state.get("backend")
    if backend is None or not backend.is_connected():
        st.caption("（未连接 — 点下方 🔍 查状态 建立连接后再看实时位置）")
        return

    try:
        status = backend.get_status()
    except L3Error as e:
        # fragment 内抛错 → 挂 last_error 走 app 级 rerun 让状态卡片持续显示
        st.session_state["last_error"] = e
        st.session_state["last_status"] = None
        st.rerun(scope="app")
        return

    # 移动完成 / 失败时吸收结果
    #  - 成功 → 短 flash（5s 自消失）
    #  - 失败 → 挂 last_error（走状态卡片统一渲染，不会 5s 消失；Slice 4 起
    #    异步 move 的错误也要能走 _render_l3_error 的快捷按钮）
    if not backend.is_move_in_progress():
        r, err = backend.consume_last_move_result()
        if err is not None:
            st.session_state["last_error"] = err
            st.session_state["last_status"] = None
            st.session_state.pop("move_flash", None)
        elif r is not None:
            st.session_state["move_flash"] = (
                "success",
                (
                    f"✅ 到达 X={r.final_position.x_mm:+.2f} "
                    f"Y={r.final_position.y_mm:+.2f} Z={r.final_position.z_mm:+.2f} "
                    f"（{r.duration_ms / 1000:.1f}s · event {r.event_id[:8]}）"
                ),
                time.time(),
            )

    color, label = STATE_DISPLAY.get(status.state, STATE_DISPLAY[MachineState.UNKNOWN])
    p = status.position
    moving = backend.is_move_in_progress()

    c_big, c_ctrl = st.columns([3, 1])
    with c_big:
        st.markdown(
            f"""
<div style="padding:0.7rem 1rem;border-left:6px solid {color};background:#fafafa;border-radius:4px;">
  <div style="font-size:1.1rem;color:{color};font-weight:700;">
    {label}{" · 🏃 移动中" if moving else ""}
  </div>
  <div style="font-family:monospace;font-size:1.9rem;line-height:1.25;margin-top:0.3rem;">
    X = {p.x_mm:+9.3f}<br>
    Y = {p.y_mm:+9.3f}<br>
    Z = {p.z_mm:+9.3f}
  </div>
  <div style="font-size:0.8rem;color:#888;margin-top:0.3rem;">
    最后更新 {status.last_update_ms_ago:.0f} ms 前
    {" · buf P:" + str(status.planner_buffer_free) if status.planner_buffer_free is not None else ""}
    {" RX:" + str(status.rx_buffer_free) if status.rx_buffer_free is not None else ""}
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
    with c_ctrl:
        recovering = bool(st.session_state.get("recovery_in_progress"))
        halt_disabled = not backend.is_connected() or recovering
        if st.button(
            "🛑 停",
            type="primary",
            width="stretch",
            disabled=halt_disabled,
            help=(
                "恢复进行中，halt 暂时禁用" if recovering else
                "发 feedhold `!` + jog cancel `0x85`。停在 Hold/Idle，不做 soft-reset。"
            ),
        ):
            try:
                backend.halt()
                st.toast("已发急停", icon="🛑")
            except L3Error as e:
                st.session_state["last_error"] = e
                st.session_state["last_status"] = None
                st.rerun(scope="app")
            except Exception as e:
                # 兜底：halt 路径下的 TOCTOU race（_ser 被另一线程 dropped）或
                # 其他未预期异常 —— 不让栈追踪泄漏到 UI
                st.session_state["last_error"] = _wrap_unexpected(e)
                st.session_state["last_status"] = None
                st.rerun(scope="app")

        # Slice 5：state in (Alarm, Hold) 时永久恢复按钮；与状态卡片的按钮
        # 共用 handler + recovery_in_progress 防抖
        if status.state in (MachineState.ALARM, MachineState.HOLD):
            _render_recover_button(key="fragment_recover")

    flash = st.session_state.get("move_flash")
    if flash is not None:
        _, msg, ts = flash
        if time.time() - ts < 5.0:
            st.success(msg)
        else:
            st.session_state.pop("move_flash", None)


live_position()

# ── 机器状态（完整卡片，手动刷新）──
st.divider()
st.subheader("机器状态（完整卡片）")

if st.button("🔍 查状态", type="primary"):
    try:
        backend = _get_or_connect_backend(port)
        st.session_state["last_status"] = backend.get_status()
        st.session_state["last_error"] = None
    except L3ConnectionError as e:
        _drop_session_backend()
        st.session_state["last_status"] = None
        st.session_state["last_error"] = e
    except L3Error as e:
        # 其他 L3 错误（例如连着但状态机出问题）—— 不重建连接，只显示错误
        st.session_state["last_status"] = None
        st.session_state["last_error"] = e
    except Exception as e:
        # 裸异常兜底（Slice 4 验收项 A：栈追踪不上 UI）
        st.session_state["last_status"] = None
        st.session_state["last_error"] = _wrap_unexpected(e)

last_status = st.session_state.get("last_status")
last_error = st.session_state.get("last_error")

if last_error is not None:
    _render_l3_error(last_error, key_prefix="status")
elif last_status is not None:
    _render_status_card(last_status)
else:
    st.info("点上面 🔍 查状态 按钮建立连接。连接后顶部「实时位置」会自动刷新。")

# ── 归零 ──
st.divider()
st.subheader("🏠 归零（XYZ）")

if st.button("🏠 归零（会动机器）", type="secondary"):
    st.session_state["confirm_home"] = True

if st.session_state.get("confirm_home"):
    st.warning(
        "⚠️ 这会让机器**真的运动**。grbl `$H` 是阻塞命令：Z 先归（向 +），"
        "然后 X+Y 并行。整个过程约 10-15 秒。"
    )
    st.caption(
        "Z 刹车自动时序：归零开始前释放 DSTUR-T80 CH2，归零完成（或失败）"
        "后立即锁回。"
    )
    cc1, cc2, _ = st.columns([1, 1, 6])
    if cc1.button("✅ 确认执行", type="primary"):
        st.session_state.pop("confirm_home", None)
        key = st.session_state.get("home_idem_key")
        if key is None:
            key = str(uuid.uuid4())
            st.session_state["home_idem_key"] = key
        try:
            backend = _get_or_connect_backend(port)
            with st.status("归零进行中...", expanded=True) as status:
                st.write(f"事务 key：`{key[:8]}...`")
                st.write("步骤：释放 Z 刹车 → 发 `$H` → 等 ok → 锁回 Z 刹车")
                result = backend.home(idempotency_key=key)
                status.update(
                    label=(
                        f"✅ 归零完成（{result.duration_ms / 1000:.1f}s）— "
                        f"event_id {result.event_id[:8]}"
                    ),
                    state="complete",
                )
            st.session_state["last_status"] = backend.get_status()
            st.session_state["last_error"] = None
        except L3ConnectionError as e:
            _drop_session_backend()
            st.session_state["last_status"] = None
            st.session_state["last_error"] = e
            st.rerun()
        except L3Error as e:
            st.session_state["last_status"] = None
            st.session_state["last_error"] = e
            st.rerun()
        except Exception as e:
            # 裸 Python 异常兜底（Slice 4 验收项 A：栈追踪不上 UI）
            st.session_state["last_status"] = None
            st.session_state["last_error"] = _wrap_unexpected(e)
            st.rerun()
    if cc2.button("❌ 取消"):
        st.session_state.pop("confirm_home", None)
        st.rerun()

# ── 去这里 ──
st.divider()
st.subheader("🎯 去这里（绝对坐标）")
st.caption(
    "发送 `G90 G1 X… Y… Z… F…`。Z 刹车自动释放/锁回，移动中点顶部 🛑 停 可中止。"
    "软限位校验在 backend 入口做（双重防线）。"
)

sl = CFG.soft_limits
mc = CFG.motion

cx, cy, cz, cf = st.columns(4)
x_val = cx.number_input(
    "X (mm)",
    min_value=float(sl.x_min_mm),
    max_value=float(sl.x_max_mm),
    value=st.session_state.get("move_x", 0.0),
    step=1.0,
    format="%.3f",
    key="move_x_input",
)
y_val = cy.number_input(
    "Y (mm)",
    min_value=float(sl.y_min_mm),
    max_value=float(sl.y_max_mm),
    value=st.session_state.get("move_y", 0.0),
    step=1.0,
    format="%.3f",
    key="move_y_input",
)
z_val = cz.number_input(
    "Z (mm)",
    min_value=float(sl.z_min_mm),
    max_value=float(sl.z_max_mm),
    value=st.session_state.get("move_z", 0.0),
    step=1.0,
    format="%.3f",
    key="move_z_input",
)
f_val = cf.number_input(
    "进给 F (mm/min)",
    min_value=1.0,
    max_value=float(mc.max_feed_mm_min),
    value=float(mc.default_feed_mm_min),
    step=100.0,
    format="%.0f",
    key="move_feed_input",
)

backend_for_move = st.session_state.get("backend")
move_busy = backend_for_move is not None and backend_for_move.is_move_in_progress()

go_disabled = move_busy
go_help = "有移动正在执行中，请先点 🛑 停或等完成" if move_busy else None

if st.button(
    "🎯 去这里",
    type="primary",
    disabled=go_disabled,
    help=go_help,
):
    try:
        backend = _get_or_connect_backend(port)
        target = Position(x_mm=float(x_val), y_mm=float(y_val), z_mm=float(z_val))
        backend.start_move_async(target, feed_mm_min=float(f_val))
        # 同步预览保存，刷新不丢
        st.session_state["move_x"] = float(x_val)
        st.session_state["move_y"] = float(y_val)
        st.session_state["move_z"] = float(z_val)
        st.session_state.pop("last_error", None)  # 成功出招 → 清掉前一条错误
        st.toast(
            f"已发出移动：X={x_val:.2f} Y={y_val:.2f} Z={z_val:.2f} F={f_val:.0f}",
            icon="🎯",
        )
    except L3ConnectionError as e:
        _drop_session_backend()
        st.session_state["last_status"] = None
        st.session_state["last_error"] = e
        st.rerun()
    except L3Error as e:
        st.session_state["last_status"] = None
        st.session_state["last_error"] = e
        st.rerun()
    except Exception as e:
        # 裸 Python 异常兜底（Slice 4 验收项 A：栈追踪不上 UI）
        st.session_state["last_status"] = None
        st.session_state["last_error"] = _wrap_unexpected(e)
        st.rerun()

# ── 历史记录 ──
st.divider()
st.subheader("📜 历史记录（最近 50 条）")

events = RUNLOG.query_recent(limit=50)
if not events:
    st.caption("（暂无记录 — 还没有完成或失败的 API 调用）")
else:
    rows = []
    for e in events:
        ts = e["timestamp"][:19].replace("T", " ")
        result_icon = "✅ 成功" if e["phase"] == "completed" else "❌ 失败"
        dur = f"{e['duration_ms'] / 1000:.2f}s" if e["duration_ms"] else "-"
        if e["phase"] == "error":
            note = (
                f"{e['error_code']}: {e['error_message']}"
                if e["error_code"]
                else (e["error_message"] or "")
            )
        else:
            note = f"event_id: {e['event_id'][:8]}"
            # 对 move_to 补一句坐标摘要
            params = e.get("params") or {}
            if "target" in params and isinstance(params["target"], dict):
                t = params["target"]
                note += (
                    f" · X={t.get('x_mm', 0):+.1f} "
                    f"Y={t.get('y_mm', 0):+.1f} Z={t.get('z_mm', 0):+.1f}"
                )
        rows.append({
            "时间 (UTC)": ts,
            "动作": e["method"],
            "结果": result_icon,
            "耗时": dur,
            "备注": note,
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)
