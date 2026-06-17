"""L3Error 层级 — Agent-readable 结构化错误（ADR-004 §原则 2）。

所有 L3 自定义错误从 `L3Error` 派生，必须填:
- `error_code`: 稳定标识符，Agent 用它做分支决策
- `severity`: `"warning"` / `"alarm"`，UI 据此决定颜色；
   warning = 用户操作可自行纠正；alarm = 机器侧异常需恢复动作
- `recoverable`: 是否能通过自动恢复动作（unlock / home / reconnect）解决
  （和 severity 是不同维度：SoftLimitExceeded 是 warning 但 recoverable=False
   因为机器不需恢复，只需用户改输入）
- `suggested_action`: 给 Agent 的下一步建议（英文，机器可读）
- `suggested_action_zh`: 给 UI 的中文建议（可选，缺省 UI fallback 英文）
- `human_message`: 给 UI 的中文短句
- `agent_message`: 给 Agent 看的详细描述（含原始上下文，例如 grbl raw line）
"""
from __future__ import annotations

from typing import Literal


Severity = Literal["warning", "alarm"]


class L3Error(Exception):
    error_code: str = "L3.UNKNOWN"
    severity: Severity = "alarm"
    recoverable: bool = False
    suggested_action: str = ""
    suggested_action_zh: str = ""

    def __init__(self, human_message: str, agent_message: str = ""):
        self.human_message = human_message
        self.agent_message = agent_message or human_message
        super().__init__(human_message)


class ConnectionError(L3Error):
    """USB / 串口连接异常 —— 端口不存在、被占用、运行中突然拔线。"""

    error_code = "L3.CONNECTION"
    severity = "alarm"
    recoverable = True
    suggested_action = "Check USB cable and port; reconnect the backend."
    suggested_action_zh = "检查 USB 线和端口占用，然后在 sidebar 点「断开并重连」。"


class MachineNotHomedError(L3Error):
    """机器未归零，但调用了需要 homed 的方法（move_to 等）。"""

    error_code = "L3.MACHINE_NOT_HOMED"
    severity = "warning"
    recoverable = True
    suggested_action = "Call gantry.home() before attempting motion commands."
    suggested_action_zh = "先点「🏠 归零」按钮完成归零，再下运动指令。"


class AlarmStateError(L3Error):
    """grbl 当前在 ALARM 状态。"""

    error_code = "L3.ALARM_STATE"
    severity = "alarm"
    recoverable = True
    suggested_action = "Call unlock_alarm() or home() to recover."
    suggested_action_zh = "点「🔧 清除并恢复」一键 unlock + 重新归零。"


class SoftLimitExceededError(L3Error):
    """请求的目标坐标超过工作空间软限位。"""

    error_code = "L3.SOFT_LIMIT_EXCEEDED"
    severity = "warning"
    recoverable = False
    suggested_action = "Target outside work envelope; adjust Position fields."
    suggested_action_zh = "目标超出工作空间（见 sidebar「软限位」），请调整坐标输入。"


class GrblConfigMismatchError(L3Error):
    """grbl 关键 `$` 参数与仓库定义不一致。"""

    error_code = "L3.CONFIG_MISMATCH"
    severity = "alarm"
    recoverable = True
    suggested_action = "Repair grbl settings before attempting motion."
    suggested_action_zh = "grbl 关键参数不一致，请先修复参数再重新归零/运动。"


class HomingTimeoutError(L3Error):
    """归零或运动指令在超时窗口内未完成。"""

    error_code = "L3.HOMING_TIMEOUT"
    severity = "alarm"
    recoverable = True
    suggested_action = "Homing did not complete; check brake release & sensors."
    suggested_action_zh = (
        "归零/运动超时未完成。检查 Z 刹车（DSTUR CH2）和限位传感器（`$$` 看 `$5=1`），"
        "再重试归零。"
    )


class RelayCommunicationError(L3Error):
    """DSTUR-T80 USB 继电器通信失败——端口不存在、被占用、write 超时、EMI 断联。

    Phase 3.3 起的通用继电器错误。Z 轴刹车专用的 `BrakeError` 是它的子类，
    `GantryBackend` 在 Z 刹车路径捕获本错误后包成 `BrakeError` 重新抛出。
    """

    error_code = "L3.RELAY"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "DSTUR-T80 command failed; verify USB port and retry; if persistent "
        "reconnect the relay backend."
    )
    suggested_action_zh = (
        "继电器命令失败。若单次瞬失可忽略重试；持续失败检查 "
        "/dev/cu.usbmodem6670E00119391 存在且没被其它进程占用。"
    )


class BrakeError(RelayCommunicationError):
    """Z 轴刹车继电器（DSTUR-T80 CH2）操作失败。"""

    error_code = "L3.BRAKE"
    severity = "alarm"
    recoverable = True
    suggested_action = (
        "DSTUR-T80 relay command failed on Z-brake channel; verify the USB "
        "serial port exists and is not held by another process."
    )
    suggested_action_zh = (
        "Z 轴刹车继电器命令失败。确认 /dev/cu.usbmodem6670E00119391 存在且"
        "没被其它进程占用，再重试。"
    )


class OperationConflictError(L3Error):
    """前一个操作还在执行，新请求被拒绝（Slice 3 移动期间再下指令）。"""

    error_code = "L3.OPERATION_CONFLICT"
    severity = "warning"
    recoverable = True
    suggested_action = "Wait for current operation to finish or call halt() first."
    suggested_action_zh = "机器正忙，请先点顶部「🛑 停」或等当前动作完成。"
