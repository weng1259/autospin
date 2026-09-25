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
from src.routine import (  # noqa: E402
    PlayerOptions,
    RoutinePlayer,
    RoutineRecorder,
    list_routines,
    load_routine,
    save_routine,
)
from tools.ui.error_actions import _action_recover, get_action  # noqa: E402

# ── 全设备集成新增依赖（task-panel-full-device-integration）──
import logging  # noqa: E402
import threading  # noqa: E402

from src.hardware.gripper_backend import GripperBackend  # noqa: E402
from src.hardware.relay_backend import RelayBackend  # noqa: E402
from src.hardware.types import GripperCommandedState  # noqa: E402
from autospin_system.maestro import SharedRs485DeviceProxy  # noqa: E402
from autospin_system.hardware.heating_stage.heating_stage_controller import (  # noqa: E402
    HeatingStageController,
)
from autospin_system.hardware.spin_motor.motor_controller import (  # noqa: E402
    MotorController,
)


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

DEFAULT_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"

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


# ── 全设备集成常量（task-panel-full-device-integration §1/§3）──────────────
# Pi udev 稳定名（system_config.yaml / /etc/udev/rules.d/*autospin*）。两个 CH340
# 靠物理 USB 口区分，别用 by-id（会和 RS485 模块撞）。
FULL_GANTRY_PORT = "/dev/autospin_xyz"    # grbl-Mega-5X CH340（龙门独占）
FULL_RELAY_PORT = "/dev/autospin_relay"   # DSTUR-T80 STM32（夹爪 CH1 + Z刹车 CH2 + 真空泵 CH3 + 工艺 CH4-8）
FULL_RS485_PORT = "/dev/autospin_rs485"   # CH340 RS485（加热 slave3 + 旋涂 slave2 共享，一把 lock）

GRIPPER_CHANNEL = 1                       # CH1=夹爪（固定，gripper_hardware.md）
VACUUM_CHANNEL = 3                        # CH3=真空泵（PM 2026-06-29 确认）：通电 ON=泵开=吸附薄片于旋涂台，断电 OFF=释放
PROCESS_CHANNELS = (4, 5, 6, 7, 8)        # CH1=夹爪 / CH2=Z刹车 / CH3=真空泵 固定；其余工艺通道在 4-8 分配
# best-guess 标签，PM 现场听吸合声 / 看执行器逐个敲定后在面板里回填。
PROCESS_CHANNEL_DEFAULT_LABELS = {
    4: "spin_power?",
    5: "aux_light?",
    6: "备用 CH6",
    7: "备用 CH7",
    8: "备用 CH8",
}
SPIN_DEFAULT_RPM = 100                     # bring-up 验证过的低速起点
SPIN_MAX_RPM_PANEL = 500                   # 面板硬上限（保守）：bring-up 仅在 100 RPM
                                           # 确认过卡盘平衡；确认更高速平衡后再调大此常量


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


# ── 全设备集成 helper（task-panel-full-device-integration §1）─────────────


def _idem() -> str:
    """每次点按钮一个新幂等 key —— 面板手动操作不复用，确保每次都真执行。"""
    return str(uuid.uuid4())


def _connect_full_devices(gantry_port: str) -> None:
    """构造并连接 全部设备（除移液枪），存进 session_state 跨 rerun 存活。

    架构（task §1，已查实）：
    - relay：**单实例**，注入给 gantry（CH2 Z刹车），自己也开夹爪 CH1 + 工艺 CH3-8。
    - gantry：``GantryBackend(relay=relay)`` 复用同一继电器实例，不开第二个口撞
      autospin_relay。
    - rs485：**一把 lock**，加热 + 旋涂两个 ``SharedRs485DeviceProxy`` 共用，
      天然串行化共享口（open-use-close）。
    - **不构造 PipetteController**（移液枪排除）。

    relay/gantry 同步连（真开串口）；heater/spin 懒连（首次方法调用才开 RS485 口）。
    """
    # 先彻底清场（含旧的纯 gantry backend），避免双 relay / 双串口
    _drop_full_devices()

    relay = RelayBackend(port=FULL_RELAY_PORT)
    relay.connect()

    gantry = GantryBackend(port=gantry_port, relay=relay)
    gantry.connect()  # 冷启 grbl 可能进 Alarm（$22=1 归零锁）—— 由现有恢复/归零覆盖

    gripper = GripperBackend(relay=relay, channel=GRIPPER_CHANNEL)

    rs485_lock = threading.Lock()  # session 内唯一一把，两个 proxy 共用
    rs485_logger = logging.getLogger("panel.rs485")
    heater = SharedRs485DeviceProxy(
        HeatingStageController(port=FULL_RS485_PORT), rs485_lock, rs485_logger, "heater"
    )
    spin = SharedRs485DeviceProxy(
        MotorController(port=FULL_RS485_PORT), rs485_lock, rs485_logger, "spin"
    )

    st.session_state["relay"] = relay
    st.session_state["backend"] = gantry  # 复用现有龙门 UI（jog/move/home/急停/恢复）
    st.session_state["gripper"] = gripper
    st.session_state["rs485_lock"] = rs485_lock
    st.session_state["heater"] = heater
    st.session_state["spin"] = spin
    st.session_state.pop("spin_running", None)


def _drop_full_devices() -> None:
    """关闭并清除所有 full-device session 实例。先关龙门（可能做 Z 刹车收尾），
    再关 RS485 proxy，最后关 relay（让龙门收尾时 relay 仍在）。GripperBackend
    无 close（只是 relay 包装层），跳过。"""
    for key in ("backend", "heater", "spin", "relay"):
        obj = st.session_state.pop(key, None)
        if obj is not None and hasattr(obj, "close"):
            try:
                obj.close()
            except Exception:
                pass
    for key in (
        "gripper", "rs485_lock", "spin_running", "spin_last_rpm",
        "heater_last_pv", "home_idem_key", "last_status", "last_error",
    ):
        st.session_state.pop(key, None)


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

# ── 全设备连接（夹爪/工艺/加热/旋涂；除移液枪外全部）──
st.divider()
st.subheader("🔗 全设备连接")
st.caption(
    "一键连：龙门（注入共享 relay）+ 夹爪/工艺继电器 + 加热/旋涂（共享 RS485，一把 lock）。"
    "**整套手动旋涂流程从这里连** —— 别用 sidebar 的「断开并重连」（那只管龙门）。"
    "不构造移液枪（已排除）。"
)

_full_gantry_port = st.text_input(
    "龙门串口（grbl）",
    value=FULL_GANTRY_PORT,
    key="full_gantry_port",
    help="Pi udev 稳定名；两个 CH340 靠物理口区分，别用 by-id。",
)

_full_have = {
    k: (k in st.session_state)
    for k in ("relay", "backend", "gripper", "heater", "spin")
}
_fc1, _fc2, _ = st.columns([1.2, 1, 4])
if _fc1.button("🔗 连接全部设备", type="primary", key="full_connect_btn"):
    try:
        _connect_full_devices(_full_gantry_port)
        st.toast("全设备已连接（RS485 懒连：首次读/写时开口）", icon="✅")
        st.rerun()
    except L3Error as e:
        st.session_state["last_error"] = e
        st.session_state["last_status"] = None
        st.rerun(scope="app")
    except Exception as e:
        st.session_state["last_error"] = _wrap_unexpected(e)
        st.session_state["last_status"] = None
        st.rerun(scope="app")
if _fc2.button("⛔ 断开全部设备", key="full_disconnect_btn"):
    _drop_full_devices()
    st.toast("已断开全部设备", icon="🔌")
    st.rerun()

st.caption(
    "会话设备： "
    + "  ·  ".join(
        f"{'✅' if _full_have[k] else '⚪'} {name}"
        for k, name in [
            ("relay", "继电器"),
            ("backend", "龙门"),
            ("gripper", "夹爪"),
            ("heater", "加热"),
            ("spin", "旋涂"),
        ]
    )
)

# ── 实时位置 fragment（100ms 刷新）──
st.divider()
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
    {"" if not status.limit_pins else " · <span style='color:#b41c1c;font-weight:700;'>⚡ Pn:" + ",".join(status.limit_pins) + "</span>"}
  </div>
  {"" if not status.limit_pins else "<div style='color:#b41c1c;font-size:0.9rem;font-weight:600;margin-top:0.2rem;'>⚠️ 限位触发: " + ", ".join(c + "轴" for c in status.limit_pins) + " — 该轴 jog 已禁用</div>"}
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

# ── 🎬 示教 / 录制 & 自动运行（src/routine.py）──────────────────────────────
st.divider()
st.subheader("🎬 示教 / 录制 & 自动运行")

# 录制器跨 rerun 存活；首次创建即 arm（全手动：每步显式「记录」才进程序）
if "rec" not in st.session_state:
    _r0 = RoutineRecorder()
    _r0.arm()
    st.session_state["rec"] = _r0
rec: RoutineRecorder = st.session_state["rec"]

_teach_backend = st.session_state.get("backend")
_play_prog = st.session_state.get("play_progress")
_play_running = bool(_play_prog and _play_prog.get("running"))

st.caption(
    "全手动示教：用上面各区操作机器（jog 对准、设温…），到位后回这里点对应「📌 记录」"
    "把**当前这一步**快照进程序。运动只记**绝对到位点**（怎么对准的过程不记），回放时才真执行。"
)

with st.expander(f"📌 记录步骤（当前 {rec.step_count} 步）", expanded=True):
    # 1) 到位点（抓当前绝对坐标 → move_to）
    _wp_name, _wp_feed, _wp_btn = st.columns([2, 1, 1])
    _wp_label = _wp_name.text_input(
        "点名", key="teach_wp_name", label_visibility="collapsed",
        placeholder="点名（可选，如 取样点A）",
    )
    _wp_feed_v = _wp_feed.number_input(
        "进给", min_value=1.0, max_value=float(CFG.motion.max_feed_mm_min),
        value=float(CFG.motion.default_feed_mm_min), step=100.0,
        key="teach_wp_feed", label_visibility="collapsed",
    )
    if _wp_btn.button(
        "📍 记录到位点", use_container_width=True, key="teach_rec_wp",
        disabled=_teach_backend is None or not _teach_backend.is_connected(),
        help="抓取当前 XYZ 绝对坐标为一个 move_to 步（先用上面 Jog/去这里 对准）",
    ):
        try:
            _pos = _teach_backend.get_position()
            _lbl = f"去「{_wp_label}」" if _wp_label else ""
            rec.record("gantry", "move_to", args=(_pos,),
                       kwargs={"feed_mm_min": float(_wp_feed_v)}, label=_lbl)
            st.toast(f"📍 到位点 X={_pos.x_mm:+.2f} Y={_pos.y_mm:+.2f} Z={_pos.z_mm:+.2f}", icon="📍")
            st.rerun()
        except Exception as _e:
            st.error(f"记录到位点失败：{type(_e).__name__}: {_e}")

    # 2) 夹爪
    _gw1, _gw2 = st.columns(2)
    if _gw1.button("📌 记录：夹爪夹紧", use_container_width=True, key="teach_grip_close"):
        rec.record("gripper", "close"); st.rerun()
    if _gw2.button("📌 记录：夹爪松开", use_container_width=True, key="teach_grip_open"):
        rec.record("gripper", "open"); st.rerun()

    # 2.5) 真空泵（CH3 专属）
    _vw1, _vw2 = st.columns(2)
    if _vw1.button("📌 记录：真空吸附", use_container_width=True, key="teach_vac_on"):
        rec.record("relay", "ch_on", kwargs={"channel": VACUUM_CHANNEL}, label="真空吸附（CH3 ON）"); st.rerun()
    if _vw2.button("📌 记录：真空释放", use_container_width=True, key="teach_vac_off"):
        rec.record("relay", "ch_off", kwargs={"channel": VACUUM_CHANNEL}, label="真空释放（CH3 OFF）"); st.rerun()

    # 3) 其它工艺继电器 CH4-8
    _rc_ch, _rc_act, _rc_btn = st.columns([1, 1, 1])
    _rc_channel = _rc_ch.number_input("通道", min_value=4, max_value=8, value=4, step=1, key="teach_relay_ch")
    _rc_on = _rc_act.radio("动作", ["开", "关"], horizontal=True, key="teach_relay_act", label_visibility="collapsed")
    if _rc_btn.button("📌 记录继电器", use_container_width=True, key="teach_rec_relay"):
        rec.record("relay", "ch_on" if _rc_on == "开" else "ch_off",
                   kwargs={"channel": int(_rc_channel)}); st.rerun()

    # 4) 加热设温
    _ht_v, _ht_btn = st.columns([2, 1])
    _ht_temp = _ht_v.number_input("加热设定 (℃)", min_value=0.0, max_value=120.0, value=40.0, step=5.0, key="teach_heat_t")
    if _ht_btn.button("📌 记录加热设温", use_container_width=True, key="teach_rec_heat"):
        rec.record("heater", "write_sv", kwargs={"temp_c": float(_ht_temp)}); st.rerun()

    # 5) 旋涂（多步序列：解锁→启动→设速→停止→锁定）
    _sp_act, _sp_rpm, _sp_btn = st.columns([1.6, 1, 1])
    _spin_choice = _sp_act.selectbox(
        "旋涂动作",
        ["解锁 unlock", "启动 start(forward)", "设速 set_speed", "停止 stop", "锁定 lock", "急停 emergency_stop"],
        key="teach_spin_act", label_visibility="collapsed",
    )
    _spin_rpm_v = _sp_rpm.number_input("RPM", min_value=0, max_value=SPIN_MAX_RPM_PANEL, value=SPIN_DEFAULT_RPM, step=50, key="teach_spin_rpm")
    if _sp_btn.button("📌 记录旋涂步", use_container_width=True, key="teach_rec_spin"):
        _spin_map = {
            "解锁 unlock": ("unlock", {}),
            "启动 start(forward)": ("start", {"direction": "forward"}),
            "设速 set_speed": ("set_speed", {"rpm": float(_spin_rpm_v)}),
            "停止 stop": ("stop", {}),
            "锁定 lock": ("lock", {}),
            "急停 emergency_stop": ("emergency_stop", {}),
        }
        _sa, _skw = _spin_map[_spin_choice]
        rec.record("spin", _sa, kwargs=_skw); st.rerun()

    # 6) 等待（保温 / 旋涂时长）
    _wt_v, _wt_btn = st.columns([2, 1])
    _wt_sec = _wt_v.number_input("等待 (秒)", min_value=0.0, max_value=3600.0, value=5.0, step=1.0, key="teach_wait_s")
    if _wt_btn.button("⏱ 插入等待", use_container_width=True, key="teach_rec_wait"):
        rec.add_wait(float(_wt_sec)); st.rerun()

# 步骤列表 + 编辑
if rec.step_count == 0:
    st.info("程序为空。用上面的「📌 记录」按钮逐步搭建。")
else:
    _steps_df = pd.DataFrame(
        [{"#": s.seq + 1, "设备": s.device, "动作": s.action, "说明": s.label} for s in rec.steps]
    )
    st.dataframe(_steps_df, use_container_width=True, hide_index=True)
    _ed1, _ed2 = st.columns(2)
    if _ed1.button("↩ 删除最后一步", use_container_width=True, key="teach_del_last", disabled=_play_running):
        rec.remove_last(); st.rerun()
    if _ed2.button("🗑 清空程序", use_container_width=True, key="teach_clear", disabled=_play_running):
        rec.clear(); st.rerun()

# 保存 / 程序库
_sv_name, _sv_btn = st.columns([2, 1])
_save_name = _sv_name.text_input("程序名", value="演示流程", key="teach_save_name", label_visibility="collapsed")
if _sv_btn.button("💾 保存程序", use_container_width=True, key="teach_save", disabled=rec.step_count == 0):
    try:
        _p = save_routine(rec.to_routine(name=_save_name or "演示流程"))
        st.toast(f"已保存：{_p.name}", icon="💾")
    except Exception as _e:
        st.error(f"保存失败：{type(_e).__name__}: {_e}")

_lib = list_routines()
if _lib:
    _by_name = {p.stem: p for p in _lib}
    _ld_sel, _ld_btn, _del_btn = st.columns([2, 1, 1])
    _pick = _ld_sel.selectbox("程序库", list(_by_name), key="teach_lib_pick", label_visibility="collapsed")
    if _ld_btn.button("📂 加载到编辑器", use_container_width=True, key="teach_load", disabled=_play_running):
        try:
            _loaded = load_routine(_pick)
            rec.set_steps(_loaded.steps)
            st.toast(f"已加载 {_loaded.name}（{len(_loaded.steps)} 步）", icon="📂")
            st.rerun()
        except Exception as _e:
            st.error(f"加载失败：{type(_e).__name__}: {_e}")
    if _del_btn.button("🗑 删除文件", use_container_width=True, key="teach_del_file", disabled=_play_running):
        try:
            _by_name[_pick].unlink()
            st.toast(f"已删除 {_pick}.json", icon="🗑")
            st.rerun()
        except Exception as _e:
            st.error(f"删除失败：{type(_e).__name__}: {_e}")

# ── ▶ 自动运行 ──
st.markdown("**▶ 自动运行**")
_run_homed = st.checkbox("运动前要求已归零（限位安全）", value=True, key="teach_run_homed")
_run_delay = st.slider("步间停顿 (秒)", 0.0, 3.0, 0.5, 0.1, key="teach_run_delay")
_run_c, _abort_c = st.columns(2)
if _run_c.button(
    "▶ 自动运行当前程序", type="primary", use_container_width=True, key="teach_run",
    disabled=rec.step_count == 0 or _play_running,
):
    _routine = rec.to_routine(name=_save_name or "演示流程")
    _backends = {
        k: st.session_state.get(v)
        for k, v in {"gantry": "backend", "gripper": "gripper", "relay": "relay",
                     "heater": "heater", "spin": "spin"}.items()
    }
    _backends = {k: v for k, v in _backends.items() if v is not None}
    _abort_evt = threading.Event()
    _progress = {"current": 0, "total": len(_routine.steps), "label": "", "running": True, "result": None}
    st.session_state["play_abort"] = _abort_evt
    st.session_state["play_progress"] = _progress
    st.session_state["play_done_ack"] = False
    _player = RoutinePlayer(_backends)
    _opts = PlayerOptions(step_delay_s=float(_run_delay), require_homed=bool(_run_homed))

    def _do_play(routine=_routine, player=_player, opts=_opts, progress=_progress, abort_evt=_abort_evt):
        def _cb(i, total, step):
            progress["current"] = i
            progress["total"] = total
            progress["label"] = step.label
        progress["result"] = player.run(routine, opts, progress_cb=_cb, abort_check=abort_evt.is_set)
        progress["running"] = False

    threading.Thread(target=_do_play, daemon=True).start()
    st.rerun()

if _abort_c.button("🛑 中止运行", use_container_width=True, key="teach_abort", disabled=not _play_running):
    _evt = st.session_state.get("play_abort")
    if _evt is not None:
        _evt.set()
    _b = st.session_state.get("backend")
    if _b is not None:
        try:
            _b.halt()           # 停在途中的龙门运动
        except Exception:
            pass
    _sp = st.session_state.get("spin")
    if _sp is not None:
        try:
            _sp.emergency_stop()  # 旋涂抱闸急停
        except Exception:
            pass
    st.toast("已请求中止运行", icon="🛑")


@st.fragment(run_every="0.5s")
def _play_status() -> None:
    p = st.session_state.get("play_progress")
    if not p:
        return
    if p.get("running"):
        _total = max(int(p.get("total", 1)), 1)
        st.progress(min(p.get("current", 0) / _total, 1.0),
                    text=f"🏃 运行中 {p.get('current', 0)}/{_total} · {p.get('label', '')}")
        return
    _res = p.get("result")
    if _res is None:
        return
    if getattr(_res, "success", False):
        st.success(f"✅ 完成全部 {_res.steps_total} 步")
    elif getattr(_res, "aborted", False):
        st.warning(f"⏹ 已中止：完成 {_res.steps_completed}/{_res.steps_total} 步")
    else:
        st.error(f"❌ {_res.error}")
    # 运行刚结束：触发一次全局 rerun 让「运行」按钮重新可用
    if not st.session_state.get("play_done_ack"):
        st.session_state["play_done_ack"] = True
        st.rerun(scope="app")


_play_status()

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

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── Jog 点动 ──
st.divider()
st.subheader("🕹️ Jog 点动")

_jog_backend = st.session_state.get("backend")
_jog_connected = _jog_backend is not None and _jog_backend.is_connected()

if not _jog_connected:
    st.info("请先点 🔍 查状态 建立连接。")
else:
    _jog_status = _jog_backend.get_status()
    _pn = _jog_status.limit_pins

    if not _jog_status.is_homed:
        st.warning("⚠️ 机器未归零 — 软限位不生效，请谨慎操作。建议先归零。")

    if _pn:
        st.error(f"⚡ 限位触发：{', '.join(c + '轴' for c in _pn)} — 该轴 jog 已禁用")

    jc_step, jc_feed = st.columns(2)
    with jc_step:
        jog_step = st.select_slider(
            "步长 (mm)", options=[0.1, 0.5, 1.0, 5.0, 10.0], value=1.0
        )
    with jc_feed:
        jog_feed = st.select_slider(
            "进给 (mm/min)", options=[50, 100, 200, 500, 1000], value=100
        )

    def _do_jog(axis: str, dist: float) -> None:
        try:
            pos = _jog_backend.jog(axis, dist, feed_mm_min=float(jog_feed))
            st.toast(
                f"{axis}{dist:+.1f} → "
                f"X={pos.x_mm:+.2f} Y={pos.y_mm:+.2f} Z={pos.z_mm:+.2f}",
                icon="✅",
            )
        except L3Error as e:
            st.session_state["last_error"] = e
            st.session_state["last_status"] = None
            st.rerun(scope="app")
        except Exception as e:
            st.session_state["last_error"] = _wrap_unexpected(e)
            st.session_state["last_status"] = None
            st.rerun(scope="app")

    _x_blocked = "X" in _pn
    _y_blocked = "Y" in _pn
    _z_blocked = "Z" in _pn

    jx_label, jx_minus, jx_plus = st.columns([0.6, 1, 1])
    jx_label.markdown(
        '<div style="font-size:1.3rem;font-weight:700;padding-top:0.3rem;">X</div>',
        unsafe_allow_html=True,
    )
    if jx_minus.button(
        f"◀ X−{jog_step}", disabled=_x_blocked, use_container_width=True
    ):
        _do_jog("X", -jog_step)
    if jx_plus.button(
        f"X+{jog_step} ▶", disabled=_x_blocked, use_container_width=True
    ):
        _do_jog("X", jog_step)

    jy_label, jy_minus, jy_plus = st.columns([0.6, 1, 1])
    jy_label.markdown(
        '<div style="font-size:1.3rem;font-weight:700;padding-top:0.3rem;">Y</div>',
        unsafe_allow_html=True,
    )
    if jy_minus.button(
        f"◀ Y−{jog_step}", disabled=_y_blocked, use_container_width=True
    ):
        _do_jog("Y", -jog_step)
    if jy_plus.button(
        f"Y+{jog_step} ▶", disabled=_y_blocked, use_container_width=True
    ):
        _do_jog("Y", jog_step)

    jz_label, jz_minus, jz_plus = st.columns([0.6, 1, 1])
    jz_label.markdown(
        '<div style="font-size:1.3rem;font-weight:700;padding-top:0.3rem;">Z</div>',
        unsafe_allow_html=True,
    )
    if jz_minus.button(
        f"▼ Z−{jog_step}", disabled=_z_blocked, use_container_width=True
    ):
        _do_jog("Z", -jog_step)
    if jz_plus.button(
        f"Z+{jog_step} ▲", disabled=_z_blocked, use_container_width=True
    ):
        _do_jog("Z", jog_step)

    st.caption("Z 轴 jog 自动释放/锁回刹车。限位触发时该轴按钮禁用。")

# ── Z2 控件已退役（W1.5, 2026-07-17）──
# Z2/A 轴 2026-06-30 起由独立 Emm RS485 丝杆滑台取代（LinearStageBackend），
# 面板控件待 W3 正式 Web 面板接入，本应急面板不再提供。

# ── 去这里 ──
st.divider()
st.subheader("🎯 去这里（绝对坐标）")
st.caption(
    "发送 `$J=G90 X… Y… Z… F…`。Z 刹车自动释放/锁回，移动中点顶部 🛑 停 可中止。"
    "软限位校验在 backend 入口做（双重防线）。"
)

sl = CFG.soft_limits
mc = CFG.motion

cx, cy, cz, cf = st.columns(4)
x_val = cx.number_input(
    "X (mm)",
    min_value=float(sl.x_min_mm),
    max_value=float(sl.x_max_mm),
    value=_clamp(st.session_state.get("move_x", 0.0), float(sl.x_min_mm), float(sl.x_max_mm)),
    step=1.0,
    format="%.3f",
    key="move_x_input",
)
y_val = cy.number_input(
    "Y (mm)",
    min_value=float(sl.y_min_mm),
    max_value=float(sl.y_max_mm),
    value=_clamp(st.session_state.get("move_y", 0.0), float(sl.y_min_mm), float(sl.y_max_mm)),
    step=1.0,
    format="%.3f",
    key="move_y_input",
)
z_val = cz.number_input(
    "Z (mm)",
    min_value=float(sl.z_min_mm),
    max_value=float(sl.z_max_mm),
    value=_clamp(st.session_state.get("move_z", 0.0), float(sl.z_min_mm), float(sl.z_max_mm)),
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

# ── 夹爪（relay CH1）──
st.divider()
st.subheader("🦾 夹爪（继电器 CH1）")

_gripper = st.session_state.get("gripper")
if _gripper is None:
    st.info("请先点上面 🔗 连接全部设备。")
else:
    _g_state = _gripper.get_state().commanded_state
    _g_disp = {
        GripperCommandedState.CLOSED: ("#b41c1c", "✊ 夹紧 (CH1=ON)"),
        GripperCommandedState.OPEN: ("#1f7a1f", "🖐 松开 (CH1=OFF)"),
        GripperCommandedState.UNKNOWN: ("#888888", "❓ 未知（还没发过命令）"),
    }.get(_g_state, ("#888888", "❓ 未知"))
    st.markdown(
        f'<div style="font-size:1.1rem;color:{_g_disp[0]};font-weight:700;">'
        f"{_g_disp[1]}</div>",
        unsafe_allow_html=True,
    )
    _gc_close, _gc_open = st.columns(2)
    if _gc_close.button("✊ 夹紧", type="primary", use_container_width=True, key="grip_close"):
        try:
            _gripper.close(idempotency_key=_idem())
            st.toast("夹爪夹紧 (CH1=ON)", icon="✊")
            st.rerun()
        except L3Error as e:
            st.error(f"❗ {e.human_message}")
        except Exception as e:
            st.error(f"❗ {type(e).__name__}: {e}")
    if _gc_open.button("🖐 松开", use_container_width=True, key="grip_open"):
        try:
            _gripper.open(idempotency_key=_idem())
            st.toast("夹爪松开 (CH1=OFF)", icon="🖐")
            st.rerun()
        except L3Error as e:
            st.error(f"❗ {e.human_message}")
        except Exception as e:
            st.error(f"❗ {type(e).__name__}: {e}")
    st.caption(
        "CH1=ON→夹合，CH1=OFF→松开（gripper_hardware.md）。"
        "与 Z 刹车 CH2 共用同一继电器实例（注入进龙门那个）。"
    )

# ── 工艺继电器通道（CH3-8）──
st.divider()
st.subheader("🔌 工艺继电器通道（CH3-8）")
st.warning(
    "⚠️ 通道映射**未定**（师兄 config 冲突：nitrogen=2 撞 z_brake、pump=3 撞 vacuum=3）。"
    "CH1=夹爪 / CH2=Z刹车 固定、不在此。逐个开关，听吸合声 + 看执行器，敲定后回填标签。"
)

_relay = st.session_state.get("relay")
if _relay is None:
    st.info("请先点上面 🔗 连接全部设备。")
else:
    _relay_channels = _relay.get_state().channels
    for _ch in PROCESS_CHANNELS:
        _lbl_key = f"proc_label_{_ch}"
        if _lbl_key not in st.session_state:
            st.session_state[_lbl_key] = PROCESS_CHANNEL_DEFAULT_LABELS[_ch]
        _lc, _sc, _bc = st.columns([2.5, 1, 1])
        _lc.text_input(
            f"CH{_ch} 标签", key=_lbl_key, label_visibility="collapsed"
        )
        _on_now = bool(_relay_channels.get(_ch, False))
        _sc.markdown(
            f'<div style="padding-top:0.45rem;font-weight:700;'
            f'color:{"#b41c1c" if _on_now else "#888"};">'
            f'CH{_ch} {"🔴 ON" if _on_now else "⚪ OFF"}</div>',
            unsafe_allow_html=True,
        )
        if _bc.button(
            "关闭" if _on_now else "打开",
            key=f"proc_toggle_{_ch}",
            type="secondary" if _on_now else "primary",
            use_container_width=True,
        ):
            try:
                if _on_now:
                    _relay.ch_off(_ch, idempotency_key=_idem())
                else:
                    _relay.ch_on(_ch, idempotency_key=_idem())
                st.toast(f"CH{_ch} → {'OFF' if _on_now else 'ON'}", icon="🔌")
                st.rerun()
            except L3Error as e:
                st.error(f"❗ CH{_ch} {e.human_message}")
            except Exception as e:
                st.error(f"❗ CH{_ch} {type(e).__name__}: {e}")
    st.caption(
        "⚠️ CH3-8 直接通断 24V 输出，先确认接的执行器（真空泵/氮气阀/spin电源…）安全再开。"
    )

# ── 真空泵（继电器 CH3 · 吸附旋涂台薄片）──
st.divider()
st.subheader("🌪 真空泵（CH3 · 吸附旋涂台薄片）")

_vac_relay = st.session_state.get("relay")
if _vac_relay is None:
    st.info("请先点上面 🔗 连接全部设备。")
else:
    _vac_on = bool(_vac_relay.get_state().channels.get(VACUUM_CHANNEL, False))
    st.markdown(
        f'<div style="font-size:1.1rem;color:{"#1c5fb4" if _vac_on else "#888"};font-weight:700;">'
        f'{"🌪 吸附中 (CH3=ON · 泵开)" if _vac_on else "⚪ 已释放 (CH3=OFF · 泵关)"}</div>',
        unsafe_allow_html=True,
    )
    _vc_on, _vc_off = st.columns(2)
    if _vc_on.button("🌪 吸附（泵开）", type="primary", use_container_width=True, key="vac_on", disabled=_vac_on):
        try:
            _vac_relay.ch_on(VACUUM_CHANNEL, idempotency_key=_idem())
            st.toast("真空泵开 → 吸附薄片", icon="🌪")
            st.rerun()
        except L3Error as e:
            st.error(f"❗ {e.human_message}")
        except Exception as e:
            st.error(f"❗ {type(e).__name__}: {e}")
    if _vc_off.button("⚪ 释放（泵关）", use_container_width=True, key="vac_off", disabled=not _vac_on):
        try:
            _vac_relay.ch_off(VACUUM_CHANNEL, idempotency_key=_idem())
            st.toast("真空泵关 → 释放", icon="⚪")
            st.rerun()
        except L3Error as e:
            st.error(f"❗ {e.human_message}")
        except Exception as e:
            st.error(f"❗ {type(e).__name__}: {e}")
    st.caption(
        "CH3 通电=泵开=吸附薄片于旋涂台；断电=释放。典型流程：装样 → 吸附 → 旋涂 → 释放 → 取样。"
    )

# ── 加热台 AI-516（RS485 slave3，共享 lock）──
st.divider()
st.subheader("🔥 加热台 AI-516")

_heater = st.session_state.get("heater")
if _heater is None:
    st.info("请先点上面 🔗 连接全部设备。")
else:
    _hpv_btn, _hpv_disp = st.columns([1, 3])
    if _hpv_btn.button("🌡 读 PV", key="heater_read_pv"):
        try:
            _pv = _heater.read_pv()
            st.session_state["heater_last_pv"] = (float(_pv), time.time())
            st.toast(f"PV = {_pv:.1f} ℃", icon="🌡")
        except Exception as e:
            st.error(f"❗ 读 PV 失败：{type(e).__name__}: {e}")
    _hp = st.session_state.get("heater_last_pv")
    if _hp is not None:
        _pv, _pv_ts = _hp
        _hpv_disp.markdown(
            f'<div style="font-family:monospace;font-size:1.4rem;padding-top:0.15rem;">'
            f"PV = {_pv:.1f} ℃ "
            f'<span style="font-size:0.8rem;color:#888;">'
            f"（{time.time() - _pv_ts:.0f}s 前）</span></div>",
            unsafe_allow_html=True,
        )
    st.error("⚠️ 设 SV 会**真加热**！PM 在旁、设低温、干烧免责见手册。")
    _hsv_in, _hsv_btn = st.columns([2, 1])
    _sv_target = _hsv_in.number_input(
        "SV 目标温度 (℃)",
        min_value=0.0,
        max_value=120.0,
        value=40.0,
        step=5.0,
        format="%.1f",
        key="heater_sv_input",
        help="driver run_on_sv_write=True：写 SV 后自动 Srun=0 启动控温。",
    )
    if _hsv_btn.button("🔥 设 SV（会加热）", type="primary", key="heater_set_sv"):
        try:
            _heater.write_sv(float(_sv_target))
            st.toast(f"已写 SV={_sv_target:.1f}℃ 并启动控温", icon="🔥")
            st.rerun()
        except Exception as e:
            st.error(f"❗ 写 SV 失败：{type(e).__name__}: {e}")
    st.caption(
        "PV ×10 已在 driver 内除好（直接 ℃）。读 PV / 写 SV 走 RS485 open-use-close，"
        "和旋涂共用一把 lock —— **别和旋涂同时点**；**不进 100ms 实时刷新**（§2.2）。"
    )

# ── 旋涂电机 DBLS400（RS485 slave2，共享 lock）──⚠️最危险──
st.divider()
st.subheader("🌀 旋涂电机 DBLS400")
st.error(
    "🛑 **安全第一**：卡盘/样品装牢 · 急停可达 · 从低速起（默认 100 RPM）。"
    f"面板上限夹到 {SPIN_MAX_RPM_PANEL} RPM（bring-up 仅验证过 100 RPM 卡盘平衡；"
    "升速前先在 100 RPM 确认无异常振动）。方向 forward = 逆时针(CCW)。"
)

_spin = st.session_state.get("spin")
if _spin is None:
    st.info("请先点上面 🔗 连接全部设备。")
else:
    # 独立旋涂急停 —— 始终最显眼，和龙门急停（feedhold，不停电机）分开
    if st.button(
        "🛑 旋涂急停（stop + 抱闸）",
        type="primary",
        use_container_width=True,
        key="spin_estop",
    ):
        try:
            _spin.emergency_stop()  # = stop(use_brake=True)
            st.session_state["spin_running"] = False
            st.toast("旋涂电机已急停（抱闸）", icon="🛑")
            st.rerun()
        except Exception as e:
            st.error(f"❗ 旋涂急停失败：{type(e).__name__}: {e}")

    _spin_running = bool(st.session_state.get("spin_running"))
    st.markdown(
        f'<div style="font-weight:700;color:{"#1c5fb4" if _spin_running else "#888"};">'
        f'软件状态：{"🏃 RUNNING" if _spin_running else "⏹ 停止 / 未运行"}</div>',
        unsafe_allow_html=True,
    )

    _sp_unlock, _sp_start, _sp_stop = st.columns(3)
    if _sp_unlock.button("1️⃣ 解锁/就绪", key="spin_unlock"):
        try:
            _ok = _spin.unlock()
            st.toast("已解锁/就绪" if _ok else "解锁返回 False", icon="✅" if _ok else "⚠️")
            st.rerun()
        except Exception as e:
            st.error(f"❗ 解锁失败：{type(e).__name__}: {e}")
    if _sp_start.button("2️⃣ 启动 (forward)", key="spin_start"):
        try:
            _ok, _msg = _spin.start("forward")
            if _ok:
                st.session_state["spin_running"] = True
                st.toast(f"启动：{_msg}", icon="🌀")
                st.rerun()
            else:
                st.error(f"❗ 启动失败：{_msg}")
        except Exception as e:
            st.error(f"❗ 启动异常：{type(e).__name__}: {e}")
    if _sp_stop.button("⏹ 停止 (stop)", key="spin_stop"):
        try:
            _ok = _spin.stop(use_brake=True)
            st.session_state["spin_running"] = False
            st.toast("已停止" if _ok else "停止返回 False", icon="⏹")
            st.rerun()
        except Exception as e:
            st.error(f"❗ 停止失败：{type(e).__name__}: {e}")

    _spd_slider, _spd_btn = st.columns([3, 1])
    _spin_rpm = _spd_slider.slider(
        "目标转速 (RPM)",
        min_value=0,
        max_value=SPIN_MAX_RPM_PANEL,
        value=SPIN_DEFAULT_RPM,
        step=50,
        key="spin_rpm_slider",
    )
    if _spd_btn.button(
        "设速",
        type="primary",
        key="spin_set_speed",
        disabled=not _spin_running,
        help=None if _spin_running else "先点 2️⃣ 启动 进入 RUNNING 才能设速",
    ):
        try:
            _ok, _msg = _spin.set_speed(float(_spin_rpm))
            if _ok:
                st.toast(_msg, icon="⚙️")
            else:
                st.error(f"❗ 设速失败：{_msg}")
        except Exception as e:
            st.error(f"❗ 设速异常：{type(e).__name__}: {e}")

    _rs_btn, _rs_disp = st.columns([1, 3])
    if _rs_btn.button("📈 读实际转速", key="spin_read_speed"):
        try:
            _rpm = _spin.get_actual_speed()
            st.session_state["spin_last_rpm"] = (float(_rpm), time.time())
        except Exception as e:
            st.error(f"❗ 读转速失败：{type(e).__name__}: {e}")
    _slr = st.session_state.get("spin_last_rpm")
    if _slr is not None:
        _rpm, _rpm_ts = _slr
        _rs_disp.markdown(
            f'<div style="font-family:monospace;font-size:1.3rem;padding-top:0.15rem;">'
            f"实际 = {_rpm:.0f} RPM "
            f'<span style="font-size:0.8rem;color:#888;">'
            f"（×2.5 换算 · {time.time() - _rpm_ts:.0f}s 前）</span></div>",
            unsafe_allow_html=True,
        )

    if st.button("🔒 锁定 (lock，抱闸)", key="spin_lock"):
        try:
            _spin.lock()
            st.session_state["spin_running"] = False
            st.toast("电机已锁定（抱闸）", icon="🔒")
            st.rerun()
        except Exception as e:
            st.error(f"❗ 锁定失败：{type(e).__name__}: {e}")

    st.caption(
        "顺序：解锁 → 启动 → 设速 →（读速）→ 停止 → 锁定。set_speed 需先进 RUNNING。"
        "返回值是 (bool, str) tuple。与加热共用 RS485 一把 lock —— 别同时点。"
    )

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
