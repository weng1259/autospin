"""GripperBackend —— 混合伺服力控电夹爪 Y1（YJZK）纯继电器控制路径。

Phase 3.3 的第二个新 backend。**继电器控制**：CH1 通断 = 夹合/松开。
RS485 力度/速度调节路径 Phase 3.5+ 再加（见 gripper_hardware.md 记录）。

接线（源：gripper_hardware.md）：
- V+/V-（绿色螺丝端子）常接 24V（电机供电）
- 控制引脚（标签 "24V"）← DSTUR-T80 CH1。**CH1=ON → 夹合，CH1=OFF → 松开**
- COM- 信号地
- G/T/R（RS485 差分）Phase 3.3 **不用**；Phase 3.5+ 接 USB-RS485 adapter 后用

依赖注入：`GripperBackend(relay=RelayBackend(...), channel=1)`。
好处（ADR-004 §原则 5 单一职责）：
- 一个 pyserial 实例控整个 DSTUR-T80，RelayBackend 独占 EMI-safe 串行化
- GripperBackend 只是"语义翻译层"，不重新实现继电器协议
- Phase 3.5 接 RS485 时只加 `set_force` 不碰开合路径

ADR-004 七原则映射：
1. 签名：`open` / `close` 返回 `GripperActionResult | GripperActionPlan`
2. 错误：透传 `RelayCommunicationError`（CH1 失败 = 开合命令失败，无需二次包装）
3. 幂等：`@observable` 默认 5 min；内部 state-memo 当 commanded_state 已匹配时
   `was_noop=True`，继续节省一次 RelayBackend 往返
4. 状态查询：`get_state()` 只读 `_state_lock`
5. 安全边界：channel 必须 ∈ [1, 8]（RelayBackend 自己验）
6. observable：`@observable` + runlog + event_bus
7. dry-run：透传 RelayBackend.dry_run 逻辑，返回 GripperActionPlan

**重要语义**：`GripperCommandedState.UNKNOWN` 是初始态，不等于 OPEN 也不等于
CLOSED。首次 `open()` 从 UNKNOWN 出发 **必须**实发继电器命令
（state-memo 不短路），否则物理状态和 backend 记忆可能不一致。
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Union

from ..observable import observable
from .relay_backend import RelayBackend
from .types import (
    GripperActionPlan,
    GripperActionResult,
    GripperCommandedState,
    GripperState,
)


DEFAULT_GRIPPER_CHANNEL = 1
DEFAULT_CLOSE_WAIT_S = 1.0


class GripperBackend:
    """夹爪后端。依赖 RelayBackend 共享 DSTUR-T80 串口。"""

    def __init__(
        self,
        relay: RelayBackend,
        *,
        channel: int = DEFAULT_GRIPPER_CHANNEL,
        close_wait_s: float = DEFAULT_CLOSE_WAIT_S,
        emergency_release: bool = True,
    ) -> None:
        if close_wait_s < 0:
            raise ValueError("close_wait_s must be >= 0")
        self._relay = relay
        self._channel = channel
        self._close_wait_s = close_wait_s
        self._emergency_release_enabled = emergency_release
        self._state_lock = threading.Lock()
        self._commanded_state: GripperCommandedState = GripperCommandedState.UNKNOWN
        self._last_command_ts: float = 0.0

    # ─────────────────────── 读路径 ───────────────────────

    def get_state(self) -> GripperState:
        with self._state_lock:
            state = self._commanded_state
            last_ts = self._last_command_ts
        if last_ts == 0.0:
            last_command_ms_ago: Optional[float] = None
        else:
            last_command_ms_ago = (time.time() - last_ts) * 1000.0
        return GripperState(
            commanded_state=state,
            position_known=False,  # Phase 3.5 接 RS485 后翻 True
            last_command_ms_ago=last_command_ms_ago,
        )

    # ─────────────────────── 写路径 ───────────────────────

    @observable
    def open(
        self,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> Union[GripperActionResult, GripperActionPlan]:
        """松开夹爪——CH{channel}=OFF，24V 断开。"""
        return self._set_state(
            target=GripperCommandedState.OPEN,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    @observable
    def close(
        self,
        *,
        idempotency_key: str,
        dry_run: bool = False,
    ) -> Union[GripperActionResult, GripperActionPlan]:
        """夹紧夹爪——CH{channel}=ON，24V 输出。"""
        return self._set_state(
            target=GripperCommandedState.CLOSED,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
        )

    def stop(self) -> GripperState:
        """Stop orchestration without changing the relay output."""
        return self.get_state()

    def emergency_release(self) -> GripperActionResult:
        """Release through RelayBackend; the Gripper never owns serial IO."""
        if not self._emergency_release_enabled:
            raise RuntimeError("Gripper emergency release is disabled by configuration")
        result = self._set_state(
            target=GripperCommandedState.OPEN,
            idempotency_key=f"emergency-release-{time.time_ns()}",
            dry_run=False,
            force_relay=True,
        )
        assert isinstance(result, GripperActionResult)
        return result

    def _set_state(
        self,
        target: GripperCommandedState,
        idempotency_key: str,
        dry_run: bool,
        force_relay: bool = False,
    ) -> Union[GripperActionResult, GripperActionPlan]:
        assert target in (
            GripperCommandedState.OPEN,
            GripperCommandedState.CLOSED,
        ), f"_set_state target must be OPEN/CLOSED, got {target}"

        with self._state_lock:
            current = self._commanded_state

        if dry_run:
            # UNKNOWN 永远视作"需激活"（物理真值未知，必须发命令）
            would_activate = current != target
            return GripperActionPlan(
                target_state=target,
                current_state=current,
                would_activate_relay=would_activate,
                underlying_relay_channel=self._channel,
            )

        # state-memo 只在 commanded_state 已匹配 target 时短路；UNKNOWN 不短路
        if current == target and not force_relay:
            return GripperActionResult(
                success=True,
                commanded_state_after=target,
                was_noop=True,
                duration_ms=0.0,
                event_id="",
            )

        t0 = time.time()
        relay_key = f"gripper-{target.value}-{idempotency_key}"
        if target == GripperCommandedState.CLOSED:
            self._relay.ch_on(self._channel, idempotency_key=relay_key)
        else:
            self._relay.ch_off(self._channel, idempotency_key=relay_key)
        duration_ms = (time.time() - t0) * 1000.0

        with self._state_lock:
            self._commanded_state = target
            self._last_command_ts = time.time()

        if target == GripperCommandedState.CLOSED and self._close_wait_s > 0:
            time.sleep(self._close_wait_s)
        duration_ms = (time.time() - t0) * 1000.0

        return GripperActionResult(
            success=True,
            commanded_state_after=target,
            was_noop=False,
            duration_ms=duration_ms,
            event_id="",
        )
