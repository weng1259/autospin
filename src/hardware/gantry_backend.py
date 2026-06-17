"""GantryBackend — 三轴运动适配器（grbl-Mega-5X via pyserial）。

Slice 1: connect / close / get_status / get_position / is_connected / is_homed.
Slice 2: home() 完整实现，含 Z 刹车时序 + bCNC char-counting 流控基础设施。
Slice 3: move_to / halt，后台 status poller 线程（UI 可 100ms 读快照），
        start_move_async 让 UI 不阻塞在长 move 上。

bCNC char-counting 算法（来自 bCNC `Sender.py` L644-860）：
    cline = 已发出但未 ack 的命令字节数列表
    sline = 对应的命令字符串（用于 error 归因）
    发新命令前置：sum(cline) + len(new) <= RX_BUFFER_SIZE
    收到 'ok'    : 弹出 cline[0] / sline[0]
    收到 'error:' / 'ALARM:' : 弹出 + 抛 AlarmStateError

Status poller 与 send_line 共用一把 `_lock`：
- 发命令的代码路径（`_send_line_blocking`）完整持锁；
- 后台 poller 每 `status_poll_interval_ms` 以 50ms 超时尝试上锁，拿不到就
  跳过一轮。这样 `_wait_idle` 阶段（未持锁）poller 能持续刷新快照，而归零
  / move 发送阶段（持锁）不会和 poller 抢串口。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Optional

import serial

from ..config import L3Config, get_config
from ..observable import observable
from .errors import (
    AlarmStateError,
    BrakeError,
    GrblConfigMismatchError,
    ConnectionError as L3ConnectionError,
    HomingTimeoutError,
    L3Error,
    MachineNotHomedError,
    OperationConflictError,
    RelayCommunicationError,
)
from .relay_backend import RelayBackend
from .types import (
    HomePlan,
    HomeResult,
    GrblSettingMismatch,
    GrblSettingsSnapshot,
    GrblSettingsValidationResult,
    MachineState,
    MachineStatus,
    MovePlan,
    MoveResult,
    Position,
    RecoveryResult,
)

# home `$H` 在当前三轴配置（导程 75mm/转、682.67 步/mm、Z 顶部归零）下
# 的经验耗时。Phase 4 可按最近 runlog 统计替换。
_HOME_ESTIMATED_DURATION_S = 30.0

RX_BUFFER_SIZE = 256  # grbl-Mega-5X (Mega 2560 RX serial buffer)

# Z 轴刹车走 DSTUR-T80 CH2（硬件接线约定，见 MEMORY.md）。
Z_BRAKE_CHANNEL = 2

# `_lock_brake` 后到下一次 `_release_brake` 之间的 grace period，防快速切换
# 引 EMI。Memo "跑 grbl 串口的 Python 脚本最好和 DSTUR 继电器串口用独立的
# serial 实例，中间隔 0.5s 空白" 的 Python 层落地。2026-04-24 pick-and-place
# 场景里 Step N finally lock → Step N+1 start release 的紧贴切换是当时
# alarm 的直接导火索（见 Issue #025）。
_BRAKE_LOCK_GRACE_S = 0.2

# Z 位置相等判阈值：grbl 回报 MPos 精度 3 位小数（682.67 步/mm → 理论
# 精度 0.001465mm），0.01mm 远大于传感器噪声，稳妥判等。
_Z_UNCHANGED_EPSILON_MM = 0.01

GRBL_EXPECTED_SETTINGS: dict[str, str] = {
    "$0": "10",
    "$1": "255",
    "$3": "6",
    "$4": "0",
    "$5": "1",
    "$10": "2",
    "$20": "0",
    "$21": "1",
    "$22": "1",
    "$23": "0",
    "$24": "25.000",
    "$25": "500.000",
    "$26": "250",
    "$27": "5.000",
    "$100": "682.670",
    "$101": "682.670",
    "$102": "682.670",
    "$110": "3000.000",
    "$111": "3000.000",
    "$112": "3000.000",
    "$120": "200.000",
    "$121": "200.000",
    "$122": "200.000",
    "$130": "280.000",
    "$131": "280.000",
    "$132": "95.000",
}

STATUS_REGEX = re.compile(
    r"<(?P<state>[A-Za-z]+)(?::\d+)?"
    r"\|(?:MPos|WPos):(?P<mx>-?[\d.]+),(?P<my>-?[\d.]+),(?P<mz>-?[\d.]+)"
    r"(?:,(?P<ma>-?[\d.]+))?"
    r"(?:\|Bf:(?P<bf_planner>\d+),(?P<bf_rx>\d+))?"
    r".*?>"
)
ALARM_REGEX = re.compile(r"ALARM:(\d+)")
LIMIT_PINS_REGEX = re.compile(r"(?:^|\|)Pn:(?P<pins>[A-Za-z]+)(?:\||>)")
Z2_MIN_MM = 0.0
Z2_MAX_MM = 125.0


def _now_ms() -> float:
    return time.time() * 1000.0


class GantryBackend:
    """Direct pyserial backend for grbl-Mega-5X.

    端口默认 /dev/cu.wchusbserial110（Arduino CH340）；baud 115200。
    Z 刹车走独立的 DSTUR-T80 USB 继电器实例（默认
    /dev/cu.usbmodem6670E00119391），与 grbl 串口物理隔离。
    """

    def __init__(
        self,
        port: str = "/dev/cu.wchusbserial110",
        baud: int = 115200,
        relay: Optional[RelayBackend] = None,
        config: Optional[L3Config] = None,
    ):
        self.port = port
        self.baud = baud
        self.config = config if config is not None else get_config()

        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._cline: list[int] = []
        self._sline: list[str] = []
        self._status = MachineStatus(
            state=MachineState.DISCONNECTED,
            position=Position(x_mm=0.0, y_mm=0.0, z_mm=0.0, z2_mm=0.0),
        )
        self._status_ts_ms: float = 0.0
        self._is_homed: bool = False
        self._alarm_code: Optional[int] = None
        self._last_home_ts_ms: Optional[float] = None
        self._z2_initialized: bool = False
        self._manual_mode: bool = False
        self._pre_manual_step_idle_delay: Optional[str] = None
        # RelayBackend 共享给可能的 GripperBackend（CH1 夹爪 + CH2 Z 刹车同一 DSTUR-T80）。
        # 调用方如果已构造了 RelayBackend 给 GripperBackend 用，传进来让 GantryBackend 复用。
        self._relay: RelayBackend = relay if relay is not None else RelayBackend()

        # background poller
        self._poller_stop = threading.Event()
        self._poller_thread: Optional[threading.Thread] = None

        # async move orchestration (Slice 3 UI 不阻塞在 move_to 上)
        self._move_thread: Optional[threading.Thread] = None
        self._last_move_result: Optional[MoveResult] = None
        self._last_move_error: Optional[L3Error] = None
        # halt() 置位；_wait_idle 碰到 HOLD 状态时当成终点优雅退出。
        # 开新 move_to 前会清零。
        self._halt_requested = False
        self._abort_requested = threading.Event()

    # ── lifecycle ──

    def connect(self) -> None:
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        except (serial.SerialException, OSError) as e:
            raise L3ConnectionError(
                human_message=f"无法打开串口 {self.port}",
                agent_message=(
                    f"serial.open failed: {e!r}. Verify {self.port} exists "
                    f"and no other process holds it (cncjs/OpenBuilds/Arduino IDE)."
                ),
            ) from e
        # AVR resets on DTR assert; grbl prints its banner within ~2s.
        time.sleep(2.0)
        try:
            self._ser.reset_input_buffer()
            self._poll_status_sync(timeout_s=2.0)
        except (serial.SerialException, OSError) as e:
            self._drop_serial()
            raise L3ConnectionError(
                human_message=f"连接 {self.port} 后读取状态失败",
                agent_message=f"post-open status poll failed: {e!r}",
            ) from e

        try:
            self.validate_grbl_settings()
        except GrblConfigMismatchError:
            self.repair_grbl_settings()
            self._poll_status_sync(timeout_s=1.0)

        # 启动后台 status poller
        self._poller_stop.clear()
        self._poller_thread = threading.Thread(
            target=self._poller_loop,
            name="GantryStatusPoller",
            daemon=True,
        )
        self._poller_thread.start()

    def close(self) -> None:
        # 先停 poller，避免它在串口关闭后误触
        self._poller_stop.set()
        t = self._poller_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._poller_thread = None

        self._drop_serial()
        try:
            self._relay.close()
        except Exception:
            pass

    # ── state queries (ADR-004 §原则 4) ──

    def get_status(self) -> MachineStatus:
        """返回最近一次快照 —— 不走串口，<1ms 返回。

        快照由后台 poller 每 ~200ms 刷新。若从未刷新过（刚 connect 就调用），
        或 poller 刚好拿不到锁，`last_update_ms_ago` 会稍大。
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message="GantryBackend not connected; call connect() first.",
            )
        return self._status.model_copy(
            update={
                "is_homed": self._is_homed,
                "alarm_code": self._alarm_code,
                "last_update_ms_ago": _now_ms() - self._status_ts_ms,
            }
        )

    def get_position(self) -> Position:
        return self.get_status().position

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def is_homed(self) -> bool:
        return self._is_homed

    def _require_connected(self, action: str) -> None:
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message=f"{action}: GantryBackend not connected.",
            )

    def _assert_z2_target(self, z2_mm: float) -> None:
        if not (Z2_MIN_MM <= z2_mm <= Z2_MAX_MM):
            raise L3Error(
                human_message=(
                    f"Z2 = {z2_mm} 超出安全行程 [{Z2_MIN_MM}, {Z2_MAX_MM}] mm"
                ),
                agent_message=(
                    f"Z2 target {z2_mm} mm outside safe range "
                    f"[{Z2_MIN_MM}, {Z2_MAX_MM}]; command not sent."
                ),
            )

    def initialize_z2_at_top(self) -> MachineStatus:
        """Declare the current no-limit Z2 position as A0/top.

        Z2 currently has no HOME sensor. This method must only be called when
        the slide is physically at the top safe position.
        It intentionally keeps `$22=1` so normal XYZ homing via `$H` remains
        enabled; A/Z2 is handled by `G92 A0` plus software limits, not by
        grbl homing.
        """
        self._require_connected("initialize_z2_at_top")
        self._send_line_blocking(
            "$X\n",
            timeout_s=3.0,
            timeout_msg="$X 未在 3s 内返回 ok",
        )
        self._send_line_blocking(
            "$20=0\n",
            timeout_s=3.0,
            timeout_msg="$20=0 未在 3s 内返回 ok",
        )
        self._send_line_blocking(
            "$21=0\n",
            timeout_s=3.0,
            timeout_msg="$21=0 未在 3s 内返回 ok",
        )
        self._send_line_blocking(
            "$22=1\n",
            timeout_s=3.0,
            timeout_msg="$22=1 未在 3s 内返回 ok",
        )
        self._send_line_blocking(
            "G92 A0\n",
            timeout_s=3.0,
            timeout_msg="G92 A0 未在 3s 内返回 ok",
        )
        self._z2_initialized = True
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        return self.get_status()

    def declare_z2_position(self, z2_mm: float) -> MachineStatus:
        """Declare the current no-limit Z2 position as an absolute A coordinate.

        This is for recovery/calibration after Z2 was moved outside this web
        controller. It sends `G92 A...` only; it does not move the A/Z2 axis.
        """
        target = float(z2_mm)
        self._require_connected("declare_z2_position")
        self._assert_z2_target(target)
        self._send_line_blocking(
            "$X\n",
            timeout_s=3.0,
            timeout_msg="$X did not return ok within 3s",
        )
        self._send_line_blocking(
            f"G92 A{target:.3f}\n",
            timeout_s=3.0,
            timeout_msg=f"G92 A{target:.3f} did not return ok within 3s",
        )
        self._z2_initialized = True
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        return self.get_status()

    def move_z2_to(
        self,
        z2_mm: float,
        *,
        feed_mm_min: float = 100.0,
        timeout_s: float = 30.0,
    ) -> MachineStatus:
        """Move the added Z2 rail as grbl A axis, using absolute A coordinates."""
        self._require_connected("move_z2_to")
        if self._manual_mode:
            raise L3Error(
                human_message="导轨处于人工模式，请先退出人工模式再移动 Z2",
                agent_message="move_z2_to rejected because manual mode is active.",
            )
        self._assert_z2_target(float(z2_mm))
        if feed_mm_min <= 0:
            raise L3Error(
                human_message=f"Z2 进给速度 {feed_mm_min} 必须为正数",
                agent_message=f"feed_mm_min={feed_mm_min} is not positive.",
            )
        if not self._z2_initialized:
            raise L3Error(
                human_message="Z2 尚未初始化 A0，请先确认滑台在最高点并调用 initialize_z2_at_top()",
                agent_message="Z2 origin unknown; call initialize_z2_at_top() before motion.",
            )

        self._send_line_blocking(
            "G90\n",
            timeout_s=3.0,
            timeout_msg="G90 未在 3s 内返回 ok",
        )
        self._send_line_blocking(
            f"G0 A{float(z2_mm):.3f} F{float(feed_mm_min):.0f}\n",
            timeout_s=max(5.0, timeout_s),
            timeout_msg=f"Z2 移动未在 {timeout_s}s 内 ack",
        )
        self._wait_idle(timeout_s=timeout_s)
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        return self.get_status()

    def move_z2_rel(
        self,
        dz2_mm: float,
        *,
        feed_mm_min: float = 100.0,
        timeout_s: float = 30.0,
    ) -> MachineStatus:
        current = self.get_status().position.z2_mm
        return self.move_z2_to(
            current + float(dz2_mm),
            feed_mm_min=feed_mm_min,
            timeout_s=timeout_s,
        )

    def park_z2(self, *, feed_mm_min: float = 100.0) -> MachineStatus:
        return self.move_z2_to(Z2_MIN_MM, feed_mm_min=feed_mm_min)

    def manual_jog_rel(
        self,
        dx_mm: float = 0.0,
        dy_mm: float = 0.0,
        dz_mm: float = 0.0,
        *,
        feed_mm_min: float = 300.0,
        timeout_s: float = 15.0,
    ) -> MachineStatus:
        """Small unhomed recovery jog for moving away from limit switches.

        This intentionally bypasses homed-state and work-envelope checks, so it
        is only for manual recovery after halt/alarm. It never moves A/Z2.
        """
        self._require_connected("manual_jog_rel")
        if self._manual_mode:
            raise L3Error(
                human_message="导轨处于人工模式，请先退出人工模式再点动",
                agent_message="manual_jog_rel rejected because manual mode is active.",
            )
        deltas = (float(dx_mm), float(dy_mm), float(dz_mm))
        if all(abs(v) < 1e-9 for v in deltas):
            return self.get_status()
        if any(abs(v) > 20.0 for v in deltas):
            raise L3Error(
                human_message="手动脱困单次点动不能超过 20 mm",
                agent_message=f"manual_jog_rel delta too large: {deltas}",
            )
        feed = float(feed_mm_min)
        if not (0 < feed <= 600.0):
            raise L3Error(
                human_message="手动脱困速度必须在 (0, 600] mm/min",
                agent_message=f"manual_jog_rel feed_mm_min={feed} outside (0, 600].",
            )

        axes: list[str] = []
        for axis, delta in (("X", deltas[0]), ("Y", deltas[1]), ("Z", deltas[2])):
            if abs(delta) >= 1e-9:
                axes.append(f"{axis}{delta:.3f}")

        self.unlock_alarm()
        self._release_brake()
        try:
            self._send_line_blocking(
                "G91\n",
                timeout_s=3.0,
                timeout_msg="G91 未在 3s 内返回 ok",
            )
            self._send_line_blocking(
                f"G0 {' '.join(axes)} F{feed:.0f}\n",
                timeout_s=max(5.0, timeout_s),
                timeout_msg="手动脱困点动未在限定时间内 ack",
            )
            self._wait_idle(timeout_s=timeout_s)
        finally:
            try:
                self._send_line_blocking(
                    "G90\n",
                    timeout_s=3.0,
                    timeout_msg="G90 未在 3s 内返回 ok",
                )
            finally:
                try:
                    self._lock_brake()
                except BrakeError:
                    pass

        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        return self.get_status()

    # ── Z 刹车封装（Phase 3.3 起走 RelayBackend）──
    def get_grbl_settings(self) -> GrblSettingsSnapshot:
        """读取 grbl `$$` settings dump 的结构化快照。"""
        return GrblSettingsSnapshot(settings=self._query_grbl_settings())

    def validate_grbl_settings(self) -> GrblSettingsValidationResult:
        """检查关键 grbl 参数是否与仓库定义一致；不一致则抛结构化错误。"""
        t0 = time.time()
        snapshot = self.get_grbl_settings()
        mismatches = self._grbl_settings_mismatches(snapshot.settings)
        duration_ms = (time.time() - t0) * 1000.0
        if mismatches:
            mismatch_text = ", ".join(
                f"{m.key}: actual={m.actual!r}, expected={m.expected!r}"
                for m in mismatches
            )
            raise GrblConfigMismatchError(
                human_message=f"grbl 关键参数不一致：{mismatch_text}",
                agent_message=(
                    "grbl settings mismatch; call repair_grbl_settings() before "
                    f"motion. mismatches={mismatches!r}"
                ),
            )
        return GrblSettingsValidationResult(
            success=True,
            repaired=False,
            mismatches=[],
            snapshot=snapshot,
            duration_ms=duration_ms,
            event_id="",
        )

    def repair_grbl_settings(self) -> GrblSettingsValidationResult:
        """把关键 grbl 参数写回仓库定义，并复查。"""
        t0 = time.time()
        for key, value in GRBL_EXPECTED_SETTINGS.items():
            self._send_line_blocking(
                f"{key}={value}\n",
                timeout_s=3.0,
                timeout_msg=f"写入 {key}={value} 超时",
            )
        snapshot = self.get_grbl_settings()
        mismatches = self._grbl_settings_mismatches(snapshot.settings)
        if mismatches:
            mismatch_text = ", ".join(
                f"{m.key}: actual={m.actual!r}, expected={m.expected!r}"
                for m in mismatches
            )
            raise GrblConfigMismatchError(
                human_message=f"grbl 参数修复后仍不一致：{mismatch_text}",
                agent_message=(
                    "repair_grbl_settings() wrote the expected settings but "
                    f"verification still mismatched: {mismatches!r}"
                ),
            )
        return GrblSettingsValidationResult(
            success=True,
            repaired=True,
            mismatches=[],
            snapshot=snapshot,
            duration_ms=(time.time() - t0) * 1000.0,
            event_id="",
        )

    # ── Z 刹车封装（Phase 3.3 起走 RelayBackend，2026-04-24 改走 force 路径）──
    #
    # `RelayCommunicationError` 在这里**必须**包成 `BrakeError`——Z 刹车失败
    # 的上下文比通用继电器错误更具体：suggested_action_zh 直接指向 "检查
    # /dev/cu.usbmodem* 端口"，agent 和 UI 据此决定恢复路径。
    #
    # ### 为什么走 `_ch_on_force` / `_ch_off_force` 而不是 `ch_on` / `ch_off`
    #
    # Issue #025 现场（2026-04-24 pick-and-place）证明：RelayBackend state-memo
    # 在 EMI / USB 抖动下会和物理状态失同步（backend 认为 CH2=ON 但物理是 OFF），
    # 后续 `ch_on` 命中 noop 永远回不来。Z 刹车是**安全关键**操作——失同步等于
    # 电机失控——必须保证每次命令都真写字节。
    #
    # 原来依赖 state-memo 实现的 Issue #025 brake-skip（纯 XY 移动不切 CH2）
    # 路径上移到 `move_to` 入口的 `z_unchanged` 判断（见下方），GantryBackend
    # 层自己决定"要不要碰继电器"，不再依赖 RelayBackend 内部的 memo。

    def _release_brake(self) -> None:
        if not self._relay.is_connected():
            self._relay.connect()
        try:
            self._relay._ch_on_force(Z_BRAKE_CHANNEL)
        except RelayCommunicationError as e:
            raise BrakeError(
                human_message=f"Z 刹车释放失败：{e.human_message}",
                agent_message=f"_ch_on_force({Z_BRAKE_CHANNEL}) failed: {e.agent_message}",
            ) from e

    def _lock_brake(self) -> None:
        try:
            self._relay._ch_off_force(Z_BRAKE_CHANNEL)
        except RelayCommunicationError as e:
            raise BrakeError(
                human_message=f"Z 刹车锁回失败：{e.human_message}",
                agent_message=f"_ch_off_force({Z_BRAKE_CHANNEL}) failed: {e.agent_message}",
            ) from e
        # EMI grace period：防 lock → 立即 release 的瞬间切换扰动限位传感器
        time.sleep(_BRAKE_LOCK_GRACE_S)

    def set_z_brake_released(self, released: bool) -> MachineStatus:
        """Manual Z brake control. True releases CH2, False locks CH2."""
        self._require_connected("set_z_brake_released")
        if released:
            self._release_brake()
        else:
            self._lock_brake()
        try:
            self._poll_status_sync(timeout_s=0.5)
        except Exception:
            pass
        return self.get_status()

    def enter_manual_mode(
        self,
        *,
        release_xy: bool = True,
        release_z: bool = False,
    ) -> MachineStatus:
        """Enter human handoff mode.

        The hardware does not expose reliable per-axis stepper disable across
        all grbl-Mega builds, so this uses the conservative path: stop motion,
        lock the Z brake, then shorten grbl's step idle delay so the steppers
        release. Z remains mechanically locked.
        """
        self._require_connected("enter_manual_mode")
        if release_z:
            raise L3Error(
                human_message="人工模式不允许释放 Z/Z2 垂直轴",
                agent_message=(
                    "enter_manual_mode(release_z=True) rejected; vertical axes "
                    "must remain locked unless dedicated safety hardware exists."
                ),
            )

        try:
            status = self.get_status()
            if status.state in (MachineState.RUN, MachineState.JOG, MachineState.HOME):
                self.halt()
        except L3Error:
            raise
        except Exception:
            pass

        self._lock_brake()

        if release_xy:
            if self._pre_manual_step_idle_delay is None:
                try:
                    settings = self._read_grbl_settings(timeout_s=3.0)
                    self._pre_manual_step_idle_delay = settings.get("$1", "255")
                except Exception:
                    self._pre_manual_step_idle_delay = "255"
            self._send_line_blocking(
                "$1=25\n",
                timeout_s=3.0,
                timeout_msg="$1=25 did not return ok within 3s",
            )
            time.sleep(0.05)

        self._manual_mode = True
        self._is_homed = False
        try:
            self._poll_status_sync(timeout_s=0.5)
        except Exception:
            pass
        return self.get_status()

    def exit_manual_mode(self, *, rehome: bool = True) -> MachineStatus:
        """Leave human handoff mode and restore the pre-handoff motor policy."""
        self._require_connected("exit_manual_mode")

        restore_value = self._pre_manual_step_idle_delay or "255"
        self._send_line_blocking(
            f"$1={restore_value}\n",
            timeout_s=3.0,
            timeout_msg=f"$1={restore_value} did not return ok within 3s",
        )
        self._pre_manual_step_idle_delay = None
        self._manual_mode = False

        if rehome:
            result = self.home(idempotency_key=f"manual-exit-home-{uuid.uuid4().hex}")
            if isinstance(result, HomeResult):
                return self.get_status()

        try:
            self._poll_status_sync(timeout_s=0.5)
        except Exception:
            pass
        return self.get_status()

    # ── actions ──

    @observable(idempotency_ttl_s=24 * 3600)  # home 是"高物理代价 + 长耗时"动作：断线重连后必须命中当日缓存
    def home(
        self, *, idempotency_key: str, dry_run: bool = False
    ) -> HomeResult | HomePlan:
        """XYZ 归零。Z 先归（向 +），然后 X+Y 并行（grbl `$44/$45` 配置）。

        自动处理 Z 刹车时序：归零开始前释放 CH2，归零完成（或异常）后锁回 CH2。
        `$1=255` 让电机常通电，刹车释放期间 Z 不会掉。

        幂等性由 @observable 装饰器接管：同 `idempotency_key` 调用 **24h 内**
        直接返回缓存结果，不重复机械动作。TTL 长是因为归零 30s+ 的物理代价
        ≫ 缓存 stale 代价；Agent session 断线重连到同一天前就归过的 key
        必须命中（否则会再归一次，磨损电机 + 刹车）。

        ### dry_run=True（ADR-004 §原则 7）

        返回 `HomePlan`（序列 + 预计时长），**不发任何字节**到串口，不动
        Z 刹车。Agent 可先拿 plan 给人确认再提交真实调用。v0 序列和时长
        都硬编码经验值。

        Raises:
            ConnectionError: 串口未连接或中途掉线（dry_run=True 时不 raise，
                plan 计算不依赖 connection）
            AlarmStateError: grbl 报错（含归零失败的 alarm 码）
            BrakeError: DSTUR-T80 继电器无法操作
            HomingTimeoutError: 90s 内未完成
        """
        if dry_run:
            return HomePlan(
                sequence=[
                    "RelayBackend.ch_on(CH2) — release Z brake",
                    "$H (grbl auto-home Z→X/Y)",
                    "RelayBackend.ch_off(CH2) — lock Z brake",
                ],
                estimated_duration_s=_HOME_ESTIMATED_DURATION_S,
            )

        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message="GantryBackend not connected; call connect() first.",
            )

        t0 = time.time()

        # Z brake release（必须先于任何 Z 运动；下方 finally 保证锁回）
        self._release_brake()

        try:
            self._send_line_blocking(
                "$H\n",
                timeout_s=90.0,
                timeout_msg="归零未在 90s 内完成",
            )
        finally:
            try:
                self._lock_brake()
            except BrakeError:
                # 不掩盖原始错误；锁刹车失败会在下一次 get_status 时被发现
                pass

        self._is_homed = True
        self._alarm_code = None
        self._last_home_ts_ms = _now_ms()

        # 归零后拉一次同步快照，避免 poller 还没跑到。这个刷新不是归零动作本身，
        # 串口偶发写超时时不应把已经完成的 $H 标记为失败。
        try:
            self._poll_status_sync(timeout_s=1.0)
        except (serial.SerialException, OSError):
            pass
        final_pos = self._status.position
        return HomeResult(
            success=True,
            position_after_pulloff=final_pos,
            duration_ms=(time.time() - t0) * 1000.0,
            event_id="",  # 由 @observable 覆盖为 runlog 的 event_id
        )

    @observable
    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: Optional[float] = None,
        wait_for_idle: bool = True,
        timeout_s: Optional[float] = None,
        dry_run: bool = False,
    ) -> MoveResult | MovePlan:
        """绝对坐标运动（`$J=G90 X.. Y.. Z.. F..` jog 命令）。

        **为什么用 `$J=` 不用 `G1`**（Slice 5 回归 ADR-002 §15-18 路线）：
        - `G1` 是铣削 gcode，`!` feedhold 后只能 `~`（继续原 target）或
          `\\x18`（soft-reset，丢 is_homed）→ halt 后进退两难
        - `$J=` 是 jog 命令，`\\x85` jog-cancel 立即回 Idle，**保留 is_homed
          和 MPos** → halt 即"就地刹车"，可以直接下一个 move_to
        - bCNC / OpenBuilds 都走 `$J=` 路线；cncjs 不用 `$J=` 是我们否决它的
          核心理由（ADR-002）

        流程：
        1. 校验 connect / homed / soft_limits / feed；
        2. 释放 Z 刹车（无论本次是否走 Z）；
        3. 发 `$J=G90 ...`，等 grbl 回 `ok`（命令进 planner，~10ms）；
        4. `wait_for_idle=True` 则轮询快照直到 state==IDLE 或 timeout；
        5. 锁回 Z 刹车；返回 MoveResult。

        注：`$J=` 期间 grbl 状态显示为 `Jog` 而非 `Run`，`_wait_idle` 会把
        Idle 当完成（Jog 当运动中）。

        ### dry_run=True（ADR-004 §原则 7）

        返回 `MovePlan` 而不发字节：soft_limits 和 feed 校验仍然执行（plan
        可报告会 raise 的错误让 Agent 提前知道），但**不动机器、不动 Z 刹车**。
        若已 connect+homed，plan 含 current_position / distance / duration 估算；
        未 connect 时这些字段为 None。

        Raises:
            ConnectionError: 仅非 dry-run 时（plan 计算不依赖串口）
            MachineNotHomedError / SoftLimitExceededError /
            AlarmStateError / HomingTimeoutError / BrakeError。
        """
        if self._manual_mode and not dry_run:
            raise L3Error(
                human_message="导轨处于人工模式，请先退出人工模式并重新归零",
                agent_message="move_to rejected because manual mode is active.",
            )

        # 无论 dry-run 都校验 soft_limits 和 feed —— 这样 dry-run 能提前暴露
        # Agent 后续真实调用会出的 SoftLimitExceededError / 进给错
        feed = feed_mm_min if feed_mm_min is not None else self.config.motion.default_feed_mm_min
        if not (0 < feed <= self.config.motion.max_feed_mm_min):
            raise L3Error(
                human_message=f"进给速度 {feed} 超出 (0, {self.config.motion.max_feed_mm_min}] mm/min",
                agent_message=(
                    f"feed_mm_min={feed} outside allowed range "
                    f"(0, {self.config.motion.max_feed_mm_min}]"
                ),
            )
        self.config.soft_limits.assert_contains(target)

        if dry_run:
            # 有 connection 才有 current_position 可估；否则 plan 距离 None
            current_pos: Optional[Position] = None
            distance_mm: Optional[float] = None
            duration_s: Optional[float] = None
            if self._ser is not None:
                current_pos = self._status.position
                distance_mm = (
                    (target.x_mm - current_pos.x_mm) ** 2
                    + (target.y_mm - current_pos.y_mm) ** 2
                    + (target.z_mm - current_pos.z_mm) ** 2
                ) ** 0.5
                duration_s = distance_mm / (feed / 60.0) if distance_mm > 0 else 0.0
            return MovePlan(
                target=target,
                feed_mm_min=feed,
                distance_mm=distance_mm,
                estimated_duration_s=duration_s,
                current_position=current_pos,
            )

        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message="GantryBackend not connected; call connect() first.",
            )
        if not self._is_homed:
            raise MachineNotHomedError(
                human_message="机器未归零，请先点 🏠 归零按钮",
                agent_message=(
                    "is_homed=False; call gantry.home(idempotency_key=...) "
                    "before move_to."
                ),
            )

        timeout = timeout_s if timeout_s is not None else self.config.motion.move_timeout_s
        self._halt_requested = False

        t0 = time.time()

        # Z 刹车（Issue #025 brake-skip，2026-04-24 从 RelayBackend state-memo
        # 上移到这里）：Z 位置不变时完全跳过 release/lock，继电器一个字节都不
        # 发——省 EMI + 省磨损。Z 要动时走 `_release/_lock_brake` 的 force 路径，
        # 不再依赖 RelayBackend 的 optimistic state-memo。
        z_unchanged = (
            abs(target.z_mm - self._status.position.z_mm) < _Z_UNCHANGED_EPSILON_MM
        )

        if not z_unchanged:
            self._release_brake()

        try:
            # `$J=` jog 命令：halt 可 cancel 保留 is_homed（见方法 docstring）
            cmd = (
                f"$J=G90 X{target.x_mm:.3f} Y{target.y_mm:.3f} "
                f"Z{target.z_mm:.3f} F{feed:.0f}\n"
            )
            self._send_line_blocking(
                cmd,
                timeout_s=max(5.0, timeout),
                timeout_msg=f"移动未在 {timeout}s 内 ack",
            )
            # 强制一次同步快照：`ok` 表示命令进 planner，motion 刚开始；
            # poller 快照可能还停留在发令前的 IDLE。不刷新的话 _wait_idle 第一
            # 次就看到 stale IDLE → 立即返回 → 谎报运动完成。
            try:
                self._poll_status_sync(timeout_s=0.5)
            except Exception:
                pass
            if wait_for_idle:
                self._wait_idle(timeout_s=timeout)
        finally:
            if not z_unchanged:
                try:
                    self._lock_brake()
                except BrakeError:
                    pass

        final_pos = self.get_status().position
        return MoveResult(
            success=True,
            final_position=final_pos,
            duration_ms=(time.time() - t0) * 1000.0,
            event_id="",
        )

    @observable
    def halt(self) -> MachineStatus:
        """立刻停止：feedhold `!` + jog cancel `\\x85`。幂等。

        因为 `move_to` 用 `$J=` jog 命令（Slice 5），`\\x85` 对运动**真正有效**——
        立即 cancel jog 回 Idle，**保留 is_homed 和 MPos**。PM 按 🛑 停后可以
        直接下一个 move_to，不需要重新归零。

        （历史：Slice 3 里 move_to 用的是 G1，`\\x85` 对 G1 无效，halt 后
        会卡在 Hold；Slice 5 改 `$J=` 后这个坑消失。）

        返回停止后的快照——正常情况应该是 Idle；若因硬件异常 stuck 在 Hold
        也视为成功（由 `_wait_idle` 的 Hold 特判兜住）。

        线程安全：None 检查在锁内做第二次（TOCTOU 防护）。外层 pre-lock 检查
        只是快速失败优化；真正的判定必须在 lock 内，因为另一条线程可能在
        我们等 lock 期间调 `_drop_serial()` 把 `_ser` 置 None。
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message="GantryBackend not connected; call halt() after connect().",
            )
        self._halt_requested = True
        # 实时命令绕过 cline/sline，但仍走 _lock 防止和 poller 抢串口
        with self._lock:
            if self._ser is None:
                # 等 lock 期间另一个线程 dropped serial（典型场景：move worker 在
                # _send_line_blocking 里碰到 USB 抖动 → _drop_serial → 释放 lock）
                raise L3ConnectionError(
                    human_message="串口在 halt 过程中被其他线程关闭",
                    agent_message=(
                        "halt: serial dropped while waiting for lock; "
                        "likely concurrent _send_line_blocking serial failure."
                    ),
                )
            try:
                self._ser.write(b"!")     # feedhold
                self._ser.flush()
                time.sleep(0.05)
                self._ser.write(b"\x85")  # jog cancel
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                self._drop_serial()
                raise L3ConnectionError(
                    human_message=f"串口写入失败：{e}",
                    agent_message=f"halt write failed: {e!r}",
                ) from e

        # 等状态稳定（Idle 或 Hold）。Jog 中 `!` 是 cancel → Idle；运动中是 Hold。
        deadline = time.time() + 2.0
        while time.time() < deadline:
            st = self.get_status()
            if st.state in (MachineState.IDLE, MachineState.HOLD):
                return st
            time.sleep(0.05)
        # 超时：返回现状，不抛错（halt 本身应永远"成功"）
        return self.get_status()

    def abort_motion_immediate(self) -> MachineStatus:
        """Best-effort immediate abort for homing or wedged motion.

        Unlike `halt()`, this does not wait for `_lock`. Homing `$H` is not a
        jog command, so feedhold/jog-cancel may not stop it quickly enough; grbl
        soft-reset (`Ctrl-X`) is the reliable emergency escape.
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接，无法急停",
                agent_message="abort_motion_immediate: GantryBackend not connected.",
            )
        self._abort_requested.set()
        self._halt_requested = True
        try:
            self._ser.write(b"\x18")
            self._ser.flush()
        except (serial.SerialException, OSError) as e:
            self._drop_serial()
            raise L3ConnectionError(
                human_message=f"串口急停写入失败：{e}",
                agent_message=f"abort_motion_immediate write failed: {e!r}",
            ) from e
        self._is_homed = False
        self._alarm_code = None
        try:
            self._lock_brake()
        except Exception:
            pass
        time.sleep(0.3)
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._cline.clear()
        self._sline.clear()
        try:
            self._poll_status_sync(timeout_s=0.5)
        except Exception:
            pass
        return self.get_status()

    @observable
    def soft_reset(self) -> None:
        """发 ctrl-X (`\\x18`) —— grbl firmware 状态机重置。幂等。

        副作用（与 AVR reset 不同，AVR 只在 DTR 脉冲时 reset）：
        - grbl planner 清空，motion 立即停
        - machine coordinates 归零（`is_homed` 必须 reset 为 False）
        - alarm 位清除，但若 `$22=1` 会立刻重回 alarm:11（Homing required）
        - 串口 banner 再次打印，约 200-300ms 到达

        不重连 DSTUR 刹车（那是独立串口）。后续恢复动作（unlock / home）
        由调用者负责。
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接，无法 soft-reset",
                agent_message="soft_reset: GantryBackend not connected.",
            )
        with self._lock:
            if self._ser is None:  # TOCTOU：等 lock 期间被别的线程 dropped
                raise L3ConnectionError(
                    human_message="串口在 soft-reset 过程中被其他线程关闭",
                    agent_message="soft_reset: serial dropped while waiting for lock.",
                )
            try:
                self._ser.write(b"\x18")
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                self._drop_serial()
                raise L3ConnectionError(
                    human_message=f"串口写入失败：{e}",
                    agent_message=f"soft_reset write failed: {e!r}",
                ) from e
            # banner 最长 ~300ms 内到达；清空输入 + 重置 char-counting
            time.sleep(0.3)
            try:
                self._ser.reset_input_buffer()
            except (serial.SerialException, OSError):
                pass
            self._cline.clear()
            self._sline.clear()

        # 本 backend 内部镜像状态也要 reset
        self._is_homed = False
        self._alarm_code = None
        self._halt_requested = False
        self._last_move_result = None
        self._last_move_error = None

        # 让下一次 get_status 看到真实状态（Idle 或 alarm:11）
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass

    @observable
    def unlock_alarm(self) -> MachineStatus:
        """发 `$X` 清 grbl alarm 位。返回清完后的 MachineStatus。幂等。

        如果当前不在 Alarm，`$X` 是无害 no-op（grbl 回 ok）。调用后抓一次
        同步快照，返回 alarm 清除后的状态。

        Raises:
            ConnectionError: 串口未连接 / 写入失败
            AlarmStateError: 极少数情况 grbl 对 `$X` 本身回 error（不该发生）
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接，无法 $X",
                agent_message="unlock_alarm: GantryBackend not connected.",
            )
        self._send_line_blocking(
            "$X\n",
            timeout_s=3.0,
            timeout_msg="$X 命令 3s 内未返回 ok",
        )
        self._alarm_code = None
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        return self.get_status()

    def get_homing_diagnostics(self) -> dict[str, object]:
        """Read-only homing diagnostics for ALARM:8 style failures.

        This sends `?` and `$$` only. It does not change grbl settings or move
        the machine.
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接，无法读取归零诊断",
                agent_message="get_homing_diagnostics: GantryBackend not connected.",
            )

        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        status = self.get_status()
        settings = self._read_grbl_settings()
        homing_settings = {
            key: settings[key]
            for key in ("$5", "$22", "$23", "$24", "$25", "$26", "$27")
            if key in settings
        }
        limit_pins = list(status.limit_pins)
        checks: list[str] = []
        if limit_pins:
            checks.append(
                "当前限位输入仍触发: "
                + "".join(limit_pins)
                + "；先手动离开限位，再检查 $5 限位反相。若 A 没有限位，需让 A 限位输入保持未触发或从固件归零循环中移除。"
            )
        else:
            checks.append("当前 ? 状态没有 Pn:X/Y/Z/A，限位输入未持续触发。")
        if homing_settings.get("$22") != "1":
            checks.append("$22 不是 1，$H 会被 grbl 拒绝；应先启用 homing。")
        if "$27" in homing_settings:
            try:
                pull_off = float(homing_settings["$27"])
                if pull_off < 3.0:
                    checks.append(
                        f"$27={pull_off:g}mm 偏小；ALARM:8 时建议试 3-5mm。"
                    )
            except ValueError:
                pass
        checks.append("若归零方向不对，检查 $23；若限位极性不对，检查 $5。")
        return {
            "status": status,
            "limit_pins": limit_pins,
            "settings": homing_settings,
            "checks": checks,
        }

    @observable(idempotency_ttl_s=24 * 3600)  # recover 内部会 home，同 home 的 TTL 理由
    def recover_from_alarm(
        self,
        *,
        idempotency_key: str,
        skip_rehome: bool = False,
    ) -> RecoveryResult:
        """Alarm / Hold 一键恢复。组合 `soft_reset` + `unlock_alarm` + `home`。

        入口状态矩阵（Slice 5 §Q2）：
        - **Alarm** → `\\x18` → (若进 alarm:11) `$X` → `$H`（主路径）
        - **Hold** → `\\x18` → `$X` → `$H`（罕见兜底——`$J=` + `\\x85` 正常
          halt 会直接回 Idle；只有在 `$J=` 被 grbl reject 或外部客户端发了
          `!` 时才会 stuck 在 Hold）
        - **Idle** → no-op，立即返回 `actions_taken=[]`（天然幂等）
        - **Run / Jog / Home** → raise `OperationConflictError`（先 halt）
        - **Disconnected** → raise `ConnectionError`（走 Slice 4 「🔌 重新连接」）

        幂等：同 `idempotency_key` **24h** 内命中缓存直接返回上次结果（recover
        内部会 home，按 home 的长 TTL 规则。见 observable.py §TTL 分级）。
        Agent session 断线重连到同一天内已恢复过的 key 必须命中，否则会再
        `$H` 归一次零，物理代价不可接受。

        Raises:
            ConnectionError: 串口掉线
            OperationConflictError: 入口状态是 Run/Jog/Home
            AlarmStateError / HomingTimeoutError / BrakeError: 内部
                unlock / home 失败时冒泡；PM 看到具体子错误，再决定下一步。
        """
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接，无法恢复",
                agent_message="recover_from_alarm: GantryBackend not connected.",
            )

        # 强刷一次快照，不依赖 poller 新鲜度
        try:
            self._poll_status_sync(timeout_s=1.0)
        except Exception:
            pass
        entry_state = self._status.state
        t0 = time.time()
        actions_taken: list[str] = []

        if entry_state == MachineState.IDLE:
            # 天然幂等 no-op：机器已经干净
            return RecoveryResult(
                success=True,
                entry_state=entry_state,
                actions_taken=[],
                final_status=self.get_status(),
                duration_ms=(time.time() - t0) * 1000.0,
                event_id="",
            )

        if entry_state in (MachineState.RUN, MachineState.JOG, MachineState.HOME):
            raise OperationConflictError(
                human_message="机器正在运动，请先点 🛑 停 再恢复",
                agent_message=(
                    f"recover_from_alarm called from state={entry_state.value}; "
                    f"halt() first then retry."
                ),
            )

        # Alarm 或 Hold ── 执行恢复序列
        self.soft_reset()
        actions_taken.append("soft_reset")

        # soft-reset 后 `$22=1` 会让 grbl 进 alarm:11（要求归零）。无论入口是
        # Alarm 还是 Hold，这里都 $X 一下让状态清到 Idle 才能下 $H。
        try:
            self._poll_status_sync(timeout_s=2.0)
        except Exception:
            pass
        if self._status.state == MachineState.ALARM:
            self.unlock_alarm()
            actions_taken.append("unlock_alarm")

        if not skip_rehome:
            # 新 uuid —— 避免 5 min 内上一次归零的 idem 缓存命中（恢复必须真动）
            self.home(idempotency_key=str(uuid.uuid4()))
            actions_taken.append("home")

        return RecoveryResult(
            success=True,
            entry_state=entry_state,
            actions_taken=actions_taken,
            final_status=self.get_status(),
            duration_ms=(time.time() - t0) * 1000.0,
            event_id="",
        )

    # ── async move orchestration (for Streamlit UI) ──

    def start_move_async(
        self,
        target: Position,
        *,
        feed_mm_min: Optional[float] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        """在后台线程跑 move_to，立即返回；UI 通过 is_move_in_progress /
        consume_last_move_result 轮询结果。

        前置校验（connect/homed/soft_limits）在主线程同步抛错，方便 UI 立刻
        展示；移动中真正的 alarm 等异常由后台线程捕获存到 _last_move_error。
        """
        if self._move_thread is not None and self._move_thread.is_alive():
            raise OperationConflictError(
                human_message="上一次移动还没结束，请先点 🛑 停 或等待完成",
                agent_message="Previous move_to still running; halt() or wait.",
            )
        if self._ser is None:
            raise L3ConnectionError(
                human_message="串口未连接",
                agent_message="Cannot start move: GantryBackend not connected.",
            )
        if not self._is_homed:
            raise MachineNotHomedError(
                human_message="机器未归零，请先点 🏠 归零按钮",
                agent_message="is_homed=False; run home() first.",
            )
        self.config.soft_limits.assert_contains(target)

        self._last_move_result = None
        self._last_move_error = None

        def _runner() -> None:
            try:
                result = self.move_to(
                    target,
                    feed_mm_min=feed_mm_min,
                    timeout_s=timeout_s,
                )
                # dry_run=False（默认），return 必为 MoveResult
                assert isinstance(result, MoveResult)
                self._last_move_result = result
            except L3Error as e:
                self._last_move_error = e
            except Exception as e:  # pragma: no cover - unexpected path
                self._last_move_error = L3Error(
                    human_message=f"意外错误：{type(e).__name__}: {e}",
                    agent_message=f"Unexpected {type(e).__name__}: {e!r}",
                )

        t = threading.Thread(target=_runner, name="GantryMoveWorker", daemon=True)
        t.start()
        self._move_thread = t

    def is_move_in_progress(self) -> bool:
        t = self._move_thread
        return t is not None and t.is_alive()

    def consume_last_move_result(self) -> tuple[Optional[MoveResult], Optional[L3Error]]:
        """返回最近一次异步 move 的结果/错误；只返回一次（随后清空）。"""
        r, e = self._last_move_result, self._last_move_error
        self._last_move_result = None
        self._last_move_error = None
        return r, e

    # ── internals ──

    def _drop_serial(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._cline.clear()
        self._sline.clear()
        self._status = self._status.model_copy(
            update={"state": MachineState.DISCONNECTED}
        )

    def _poller_loop(self) -> None:
        """后台线程：以 `status_poll_interval_ms` 间隔尝试刷新快照。

        拿不到 lock（主线程在发命令）就跳过本轮。串口异常不 crash 线程 —— 让
        主线程的下一次 get_status 去抛 ConnectionError。
        """
        interval_s = self.config.motion.status_poll_interval_ms / 1000.0
        while not self._poller_stop.wait(interval_s):
            if self._ser is None:
                continue
            if not self._lock.acquire(timeout=0.05):
                continue
            try:
                if self._ser is None:
                    continue
                try:
                    self._ser.write(b"?")
                    self._ser.flush()
                except (serial.SerialException, OSError):
                    # USB 断开 / 被占用 — 下次 get_status 会抛
                    continue
                deadline = time.time() + 0.3
                while time.time() < deadline:
                    try:
                        raw = self._ser.readline().decode("ascii", "ignore").strip()
                    except (serial.SerialException, OSError):
                        break
                    if not raw:
                        continue
                    if raw.startswith("<"):
                        self._parse_status_line(raw)
                        break
                    # 其他内容（ok / error） 在 poller 路径下丢弃 —— 发命令
                    # 期间 lock 被主线程持有，poller 不会看到活跃命令的 ack。
            finally:
                self._lock.release()

    def _poll_status_sync(self, timeout_s: float) -> None:
        """同步发 `?` 并读到 `<...>` —— 用于 connect() 和 home() 后强刷。

        线程安全：检查 `_ser` 在 lock 内做（TOCTOU 防护），为 None 时静默返回
        —— 调用方通常是 wrap 在 try/except 里的非关键路径，不需要抛错。
        """
        with self._lock:
            if self._ser is None:
                return  # 已被其他线程 dropped；调用方的下一次关键操作会自己抛错
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                self._ser.write(b"?")
                self._ser.flush()
                inner_deadline = min(deadline, time.time() + 0.3)
                while time.time() < inner_deadline:
                    raw = self._ser.readline().decode("ascii", "ignore").strip()
                    if not raw:
                        continue
                    if raw.startswith("<"):
                        self._parse_status_line(raw)
                        return

    def _query_grbl_settings(self, timeout_s: float = 5.0) -> dict[str, str]:
        """发送 `$$` 并收集 settings dump。"""
        with self._lock:
            if self._ser is None:
                raise L3ConnectionError(
                    human_message="串口未连接",
                    agent_message="GantryBackend not connected; call connect() first.",
                )
            try:
                self._ser.reset_input_buffer()
                self._ser.write(b"$$\n")
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                self._drop_serial()
                raise L3ConnectionError(
                    human_message=f"串口写入失败：{e}",
                    agent_message=f"settings query write failed: {e!r}",
                ) from e

            deadline = time.time() + timeout_s
            settings: dict[str, str] = {}
            while time.time() < deadline:
                raw = self._ser.readline().decode("ascii", "ignore").strip()
                if not raw:
                    continue
                if raw == "ok":
                    return settings
                if raw.startswith("$") and "=" in raw:
                    key, value = raw.split("=", 1)
                    settings[key] = value
                    continue
                if raw.startswith("<"):
                    self._parse_status_line(raw)
                    continue
                if raw.startswith("ALARM:"):
                    m = ALARM_REGEX.search(raw)
                    self._alarm_code = int(m.group(1)) if m else None
                    raise AlarmStateError(
                        human_message=f"读取 grbl settings 时进 alarm（{raw}）",
                        agent_message=f"settings query returned {raw!r}",
                    )
                if raw.startswith("error:"):
                    raise L3Error(
                        human_message=f"读取 grbl settings 时返回 {raw}",
                        agent_message=f"settings query returned {raw!r}",
                    )
            raise HomingTimeoutError(
                human_message=f"读取 grbl settings 未在 {timeout_s}s 内完成",
                agent_message="Timed out waiting for grbl $$ dump to finish.",
            )

    def _grbl_settings_mismatches(
        self, actual: dict[str, str]
    ) -> list[GrblSettingMismatch]:
        mismatches: list[GrblSettingMismatch] = []
        for key, expected in GRBL_EXPECTED_SETTINGS.items():
            got = actual.get(key)
            if got != expected:
                mismatches.append(
                    GrblSettingMismatch(key=key, expected=expected, actual=got)
                )
        return mismatches

    def _wait_idle(self, timeout_s: float) -> None:
        """等到 state == IDLE。依赖后台 poller 刷新快照（不自己持锁）。

        若期间调 `halt()`，`_halt_requested` 置位：
        - 正常路径（`$J=` + `\\x85`）：grbl 立即 cancel jog → 状态直接 Idle →
          正常 return（跟没 halt 一样，只是没走完）
        - 异常兜底：若机器 stuck 在 Hold（例如 `$J=` 本身 error、或手动 bCNC
          发了 `!`），也视为正常终止，避免 _wait_idle 永远等——后续交由
          Slice 5 的 recover_from_alarm Hold 分支兜底

        快照新鲜度要求：超过 `stale_threshold_ms` 未更新就强制同步刷。防止看到
        发令前的 stale IDLE 而误判完成。
        """
        stale_threshold_ms = self.config.motion.status_poll_interval_ms * 2
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self.get_status()
            if st.last_update_ms_ago > stale_threshold_ms:
                try:
                    self._poll_status_sync(timeout_s=0.3)
                except Exception:
                    pass
                continue
            if st.state == MachineState.IDLE:
                return
            if self._halt_requested and st.state == MachineState.HOLD:
                # 异常兜底：$J= + \x85 正常会直接回 Idle，进 Hold 说明
                # cancel 没生效（命令本身被 reject 或外部客户端发了 `!`）。
                # 不抛错、不等 Idle，避免用户操作后 UI 永远 spinning
                return
            if st.state == MachineState.ALARM:
                raise AlarmStateError(
                    human_message=f"运动中进 alarm（alarm_code={st.alarm_code}）",
                    agent_message=(
                        f"Post-move state=ALARM:{st.alarm_code}; "
                        f"call unlock_alarm() or home() to recover."
                    ),
                )
            time.sleep(0.05)
        raise HomingTimeoutError(
            human_message=f"运动未在 {timeout_s}s 内完成",
            agent_message=(
                f"Motion did not reach Idle within {timeout_s}s. "
                f"Last state={self._status.state.value}."
            ),
        )

    def _parse_status_line(self, raw: str) -> None:
        m = STATUS_REGEX.match(raw)
        if not m:
            return
        state_str = m.group("state").lower()
        try:
            state = MachineState(state_str)
        except ValueError:
            state = MachineState.UNKNOWN
        bf_planner = m.group("bf_planner")
        bf_rx = m.group("bf_rx")
        ma = m.group("ma")
        limit_pins = self._parse_limit_pins(raw)
        self._status = MachineStatus(
            state=state,
            position=Position(
                x_mm=float(m.group("mx")),
                y_mm=float(m.group("my")),
                z_mm=float(m.group("mz")),
                z2_mm=float(ma) if ma is not None else self._status.position.z2_mm,
            ),
            alarm_code=self._alarm_code,
            is_homed=self._is_homed,
            limit_pins=limit_pins,
            planner_buffer_free=int(bf_planner) if bf_planner else None,
            rx_buffer_free=int(bf_rx) if bf_rx else None,
            raw=raw,
        )
        self._status_ts_ms = _now_ms()

    def _parse_limit_pins(self, raw: str) -> list[str]:
        m = LIMIT_PINS_REGEX.search(raw)
        if not m:
            return []
        return [pin for pin in ("X", "Y", "Z", "A") if pin in m.group("pins").upper()]

    def _read_grbl_settings(self, timeout_s: float = 3.0) -> dict[str, str]:
        assert self._ser is not None
        settings: dict[str, str] = {}
        with self._lock:
            if self._ser is None:
                raise L3ConnectionError(
                    human_message="串口在读取 $$ 前被关闭",
                    agent_message="_read_grbl_settings: serial is None inside lock.",
                )
            deadline = time.time() + timeout_s
            try:
                self._ser.write(b"$$\n")
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                self._drop_serial()
                raise L3ConnectionError(
                    human_message=f"串口写入失败：{e}",
                    agent_message=f"Serial write failed while reading $$: {e!r}",
                ) from e

            while time.time() < deadline:
                raw = self._ser.readline().decode("ascii", "ignore").strip()
                if not raw:
                    continue
                if raw.startswith("<"):
                    self._parse_status_line(raw)
                    continue
                if raw.lower() == "ok":
                    return settings
                if raw.startswith("$") and "=" in raw:
                    key, value = raw.split("=", 1)
                    settings[key] = value
                    continue
                if "error:" in raw.lower() or "ALARM:" in raw:
                    raise self._make_alarm_error(raw, "$$")
        raise HomingTimeoutError(
            human_message="读取 grbl $$ 参数超时",
            agent_message="Timed out waiting for grbl $$ response.",
        )

    # bCNC char-counting helpers ────────────────────────────────────────

    def _send_line_blocking(
        self, line: str, *, timeout_s: float, timeout_msg: str
    ) -> None:
        """发一行，等到 grbl 回 'ok' 才返回。'error:'/'ALARM:' 抛错。

        遵循 bCNC 流控：先确认 RX buffer 有空间，再 write。

        线程安全：None 检查在 lock 内（TOCTOU 防护）。原来用 `assert` 会在
        race 时抛 AssertionError → @observable 包成 L3.UNEXPECTED → UI 显示丑陋
        栈追踪。改成 L3ConnectionError 让上层正常走「🔌 重新连接」路径。
        """
        with self._lock:
            if self._ser is None:
                raise L3ConnectionError(
                    human_message="串口在发命令前被关闭",
                    agent_message=(
                        f"_send_line_blocking({line!r}): serial is None inside lock; "
                        f"concurrent _drop_serial likely fired."
                    ),
                )
            deadline = time.time() + timeout_s
            while sum(self._cline) + len(line) > RX_BUFFER_SIZE:
                if self._abort_requested.is_set():
                    self._abort_requested.clear()
                    self._cline.clear()
                    self._sline.clear()
                    raise HomingTimeoutError(
                        human_message="操作已被急停中断",
                        agent_message=f"Command {line.rstrip()!r} aborted by emergency stop.",
                    )
                if time.time() > deadline:
                    raise HomingTimeoutError(
                        human_message="grbl RX buffer 长时间满，无法发命令",
                        agent_message=(
                            f"RX buffer stayed full > {timeout_s}s; "
                            f"cline={self._cline}"
                        ),
                    )
                self._drain_once()

            target = line.rstrip()
            try:
                self._ser.write(line.encode("ascii"))
                self._ser.flush()
            except (serial.SerialException, OSError) as e:
                self._drop_serial()
                raise L3ConnectionError(
                    human_message=f"串口写入失败：{e}",
                    agent_message=f"Serial write failed: {e!r}",
                ) from e
            self._cline.append(len(line))
            self._sline.append(target)

            while self._sline and self._sline[0] == target:
                if self._abort_requested.is_set():
                    self._abort_requested.clear()
                    self._cline.clear()
                    self._sline.clear()
                    raise HomingTimeoutError(
                        human_message="操作已被急停中断",
                        agent_message=f"Command {target!r} aborted by emergency stop.",
                    )
                if time.time() > deadline:
                    # 主动尝试取消正在进行的归零
                    try:
                        self._ser.write(b"\x18")  # ctrl-X soft reset
                    except Exception:
                        pass
                    raise HomingTimeoutError(
                        human_message=timeout_msg,
                        agent_message=f"{timeout_msg} (line={target!r})",
                    )
                try:
                    self._drain_once()
                except (serial.SerialException, OSError) as e:
                    self._drop_serial()
                    raise L3ConnectionError(
                        human_message=f"串口读取失败：{e}",
                        agent_message=f"Serial read failed during ack wait: {e!r}",
                    ) from e

    def _drain_once(self) -> None:
        """读一行，更新 cline/sline 或 status；'error:'/'ALARM:' 抛 AlarmStateError。"""
        assert self._ser is not None
        if self._ser.in_waiting == 0:
            time.sleep(0.005)
            return
        raw = self._ser.readline().decode("ascii", "ignore").strip()
        if not raw:
            return
        if raw.startswith("<"):
            self._parse_status_line(raw)
        elif "ok" in raw.lower():
            if self._cline:
                self._cline.pop(0)
            if self._sline:
                self._sline.pop(0)
        elif "error:" in raw.lower() or "ALARM:" in raw:
            errline = self._sline.pop(0) if self._sline else "<unknown>"
            if self._cline:
                self._cline.pop(0)
            raise self._make_alarm_error(raw, errline)

    def _make_alarm_error(self, raw: str, errline: str) -> AlarmStateError:
        m = ALARM_REGEX.search(raw)
        self._alarm_code = int(m.group(1)) if m else None
        self._status = self._status.model_copy(
            update={
                "state": MachineState.ALARM,
                "alarm_code": self._alarm_code,
                "raw": raw,
            }
        )
        if self._alarm_code == 8:
            pins = "".join(self._status.limit_pins) or "无"
            exc = AlarmStateError(
                human_message=(
                    "grbl 归零失败：ALARM:8。通常是归零后 pull-off 没有释放限位；"
                    f"当前限位触发={pins}。请先 $X 解锁，手动离开限位，发送 ? 确认无 Pn:X/Y/Z/A，"
                    "再检查 $5 限位反相、$23 归零方向、$27 pull-off 距离；若 A 没有限位，"
                    "需让 A 限位输入保持未触发或从固件归零循环中移除。"
                ),
                agent_message=(
                    f"grbl returned {raw!r} for {errline!r}; alarm_code=8. "
                    "Homing failed because limit switch did not clear after pull-off. "
                    f"Current limit_pins={self._status.limit_pins}. Check $5, $23, $27; "
                    "if A has no switch, keep A limit input inactive or remove A from homing."
                ),
            )
            exc.suggested_action_zh = (
                "ALARM:8：先 $X，手动离开限位，? 确认没有 Pn:X/Y/Z/A；"
                "再查 $5/$23/$27。若 A 没有限位，处理 A 限位输入或固件归零循环。"
            )
            exc.suggested_action = (
                "ALARM:8: unlock, move off switches, confirm no Pn pins, then check $5/$23/$27 and A homing input."
            )
            return exc
        return AlarmStateError(
            human_message=f"grbl 报错：{raw}（命令 {errline}）",
            agent_message=(
                f"grbl returned {raw!r} for {errline!r}; "
                f"alarm_code={self._alarm_code}."
            ),
        )
