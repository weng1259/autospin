"""RelayBackend —— DSTUR-T80 USB 继电器（8 路）完整后端。

Phase 3.3 的第一个新 backend。抬 Slice 2 的 `dstur_relay.py` 到 ADR-004
七条原则的可验证落地：

1. **类型签名**：`ch_on` / `ch_off` 返回 `RelayActionResult | RelayActionPlan`，
   纯 pydantic（types.py）。
2. **结构化错误**：所有失败路径走 `RelayCommunicationError`，可被 GantryBackend
   Z-brake 路径捕获后包成 `BrakeError` 再抛（保留 Issue #025 场景的
   `suggested_action_zh`）。
3. **幂等 + TTL**：`@observable` 默认 5 min；额外在 `_set_channel` 内部做
   state-memo idempotency —— 目标 state 和当前一致时直接 `was_noop=True`，
   不发字节，这**就是** Issue #025 brake-skip 修复的核心路径。
4. **状态查询不阻塞**：`get_state()` 只读 `_state_lock`，不抢 `_write_lock`；
   当 `ch_on` 写串口时 `get_state` 立即返回。
5. **安全边界**：`channel` ∈ [1, 8] 是 DSTUR-T80 硬件硬上限；越界立即抛错。
6. **observable**：`@observable` → runlog.db + event_bus（Phase 3.1 既有）。
7. **dry-run**：`ch_on(..., dry_run=True)` 返回 `RelayActionPlan`，soft_limit
   等价的 channel 校验仍执行。

### USB 重连路径（Issue #025 第二 bug 修复）

串口拔插恢复后 `serial.Serial` 持 stale fd，`SerialException("Device not
configured")` 是典型症状。`_write_with_retry` 首次写失败 → `_try_reconnect`
关闭旧 fd + 重开新 Serial → 再写一次；二次失败才抛 `RelayCommunicationError`。

### 与 GantryBackend 的并存

GantryBackend 的 grbl 串口 (`/dev/cu.wchusbserial*`) 和 DSTUR-T80 串口
(`/dev/cu.usbmodem*`) 是**不同端口**，两个 `serial.Serial` 实例互不干扰。
0.3s 命令间隔（`_SETTLE_S`）是为了让 DSTUR-T80 继电器物理动作有时间完成
+ 减小 EMI 峰值累积。
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Union

import serial

from ..observable import observable
from .errors import RelayCommunicationError
from .types import RelayActionPlan, RelayActionResult, RelayState


DEFAULT_PORT = "/dev/cu.usbmodem6670E00119391"
DEFAULT_BAUD = 9600
_SETTLE_S = 0.3
_RECONNECT_BACKOFF_S = 0.5
_CHANNEL_MIN = 1
_CHANNEL_MAX = 8


def _frame(channel: int, on: bool) -> bytes:
    """DSTUR-T80 线协议：0xA0 [CH] [STATE] [CHECKSUM]。"""
    state = 0x01 if on else 0x00
    checksum = (0xA0 + channel + state) & 0xFF
    return bytes([0xA0, channel, state, checksum])


def _validate_channel(channel: int) -> None:
    if not (_CHANNEL_MIN <= channel <= _CHANNEL_MAX):
        raise RelayCommunicationError(
            human_message=f"通道号 {channel} 越界（合法范围 1-8）",
            agent_message=(
                f"DSTUR channel must be in [{_CHANNEL_MIN}, {_CHANNEL_MAX}], "
                f"got {channel}."
            ),
        )


class RelayBackend:
    """DSTUR-T80 8 路继电器后端。线程安全 —— 写串口互斥，读状态无锁。"""

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baud: int = DEFAULT_BAUD,
        settle_s: float = _SETTLE_S,
    ) -> None:
        self.port = port
        self.baud = baud
        self._settle_s = settle_s
        self._ser: Optional[serial.Serial] = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_cmd_ts: float = 0.0
        # Backend 记忆的"已下发" state。DSTUR-T80 协议无读回，这是一个
        # optimistic 假设——"我们写下去了，命令真的生效了"。
        # 初始假设所有通道 OFF（USB 上电默认态 + CH2=OFF 锁刹车是安全缺省）。
        self._channels: dict[int, bool] = {i: False for i in range(1, 9)}
        self._last_channels_update_ts: float = 0.0

    # ─────────────────────── 连接生命周期 ───────────────────────

    def connect(self) -> None:
        """打开 DSTUR-T80 串口。不发任何 reset —— 初始 state 信任"全 OFF"默认。"""
        if self._ser is not None:
            return  # 已连，幂等
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=1.0)
        except (serial.SerialException, OSError) as e:
            raise RelayCommunicationError(
                human_message=f"无法打开继电器串口 {self.port}",
                agent_message=(
                    f"DSTUR-T80 serial.open failed: {e!r}. "
                    f"Verify {self.port} exists and is not held by another process."
                ),
            ) from e
        time.sleep(_RECONNECT_BACKOFF_S)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def is_connected(self) -> bool:
        return self._ser is not None

    # ─────────────────────── 读路径（无 write_lock） ───────────────────────

    def get_state(self) -> RelayState:
        """快照当前 8 通道状态。不抢 `_write_lock`——写串口时读立即返回。"""
        with self._state_lock:
            channels = dict(self._channels)
            last_ts = self._last_channels_update_ts
        if last_ts == 0.0:
            # 从未下发过命令，no stale
            last_update_ms_ago = 0.0
        else:
            last_update_ms_ago = (time.time() - last_ts) * 1000.0
        return RelayState(
            channels=channels,
            last_update_ms_ago=last_update_ms_ago,
        )

    # ─────────────────────── 写路径（@observable 包装） ───────────────────────

    @observable
    def ch_on(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> Union[RelayActionResult, RelayActionPlan]:
        """打开通道——24V 输出到 CH{channel}。幂等：已 ON 时 was_noop=True。"""
        return self._set_channel(channel, target=True, dry_run=dry_run)

    @observable
    def ch_off(
        self,
        channel: int,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> Union[RelayActionResult, RelayActionPlan]:
        """关闭通道——断 24V 输出。幂等：已 OFF 时 was_noop=True。"""
        return self._set_channel(channel, target=False, dry_run=dry_run)

    def _set_channel(
        self,
        channel: int,
        target: bool,
        dry_run: bool,
    ) -> Union[RelayActionResult, RelayActionPlan]:
        _validate_channel(channel)

        with self._state_lock:
            current = self._channels.get(channel, False)

        if dry_run:
            return RelayActionPlan(
                channel=channel,
                target_state=target,
                current_state=current,
                would_write=(current != target),
            )

        # State-memo idempotency —— Issue #025 brake-skip 的核心
        if current == target:
            return RelayActionResult(
                success=True,
                channel=channel,
                state_after=target,
                was_noop=True,
                duration_ms=0.0,
                event_id="",  # @observable 会填
            )

        t0 = time.time()
        self._write_with_retry(channel, target)
        duration_ms = (time.time() - t0) * 1000.0

        with self._state_lock:
            self._channels[channel] = target
            self._last_channels_update_ts = time.time()

        return RelayActionResult(
            success=True,
            channel=channel,
            state_after=target,
            was_noop=False,
            duration_ms=duration_ms,
            event_id="",
        )

    # ─────────────────────── force 写（bypass state-memo） ───────────────────────
    #
    # Issue #025 2026-04-24 现场教训：DSTUR-T80 无读回，state-memo 是 optimistic
    # 假设"我写下去了就生效了"。但 EMI / USB 抖动会让物理状态静默偏离 memo。
    # 再遇到同一 target 时 `_set_channel` 命中 noop 不发字节 → 永远回不到物理
    # 正确状态。
    #
    # `_ch_on_force` / `_ch_off_force` 为 GantryBackend 的 Z 刹车路径专用：
    # 跳过 `current == target` 早退，强制写一次字节。state-memo 仍然更新到
    # target，所以下一次 `ch_on`/`ch_off` 看到一致状态时不会错误重写。
    #
    # 私有（下划线前缀）——不对 Agent/UI 暴露，不进 schema 导出，API 合同 v1 不变。

    def _ch_on_force(self, channel: int) -> None:
        """强制写 ON，无视 state-memo。仅供同包 backend 使用。"""
        self._force_set_channel(channel, True)

    def _ch_off_force(self, channel: int) -> None:
        """强制写 OFF，无视 state-memo。仅供同包 backend 使用。"""
        self._force_set_channel(channel, False)

    def _force_set_channel(self, channel: int, target: bool) -> None:
        _validate_channel(channel)
        self._write_with_retry(channel, target)
        with self._state_lock:
            self._channels[channel] = target
            self._last_channels_update_ts = time.time()

    # ─────────────────────── 串口实际写 + 重连 ───────────────────────

    def _write_with_retry(self, channel: int, on: bool) -> None:
        """写一次 → 失败 reconnect → 再写一次。二次失败抛 RelayCommunicationError。"""
        if self._ser is None:
            raise RelayCommunicationError(
                human_message="继电器未连接",
                agent_message="RelayBackend not connected; call connect() first.",
            )
        try:
            self._write_locked(channel, on)
            return
        except (serial.SerialException, OSError) as e_first:
            first_err = e_first

        # 走重连路径
        self._try_reconnect()
        if self._ser is None:
            raise RelayCommunicationError(
                human_message=f"继电器写失败且重连失败：{first_err}",
                agent_message=(
                    f"DSTUR write failed, reconnect also failed. "
                    f"First error: {first_err!r}. Port {self.port} may be detached."
                ),
            ) from first_err
        try:
            self._write_locked(channel, on)
        except (serial.SerialException, OSError) as e_second:
            raise RelayCommunicationError(
                human_message=f"继电器重连后写入仍失败：{e_second}",
                agent_message=(
                    f"DSTUR write failed after reconnect: "
                    f"first={first_err!r}, second={e_second!r}"
                ),
            ) from e_second

    def _write_locked(self, channel: int, on: bool) -> None:
        with self._write_lock:
            elapsed = time.time() - self._last_cmd_ts
            if elapsed < self._settle_s:
                time.sleep(self._settle_s - elapsed)
            assert self._ser is not None  # _write_with_retry 已检查
            self._ser.write(_frame(channel, on))
            self._ser.flush()
            self._last_cmd_ts = time.time()

    def _try_reconnect(self) -> None:
        """USB 拔插后 fd stale，必须显式 close + open。不抛——失败 `_ser` 置 None。"""
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=1.0)
            time.sleep(_RECONNECT_BACKOFF_S)
        except (serial.SerialException, OSError):
            self._ser = None
