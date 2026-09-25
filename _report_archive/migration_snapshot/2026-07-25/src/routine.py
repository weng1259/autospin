"""示教-重放（teach & replay）：录制手动操作序列，存成可重放的"程序"。

目标场景：PM 在面板上**手动跑一遍完整流程**（jog/去这里/夹爪开合/继电器/加热设温/
旋涂设速）→ 系统记录成一段「程序」→ 一键**自动重放**（拍演示视频）。

设计原则（ADR-004 风格）：
- 录制的是 **L3 高层动作**（``move_to`` / ``jog`` / ``home`` / ``open`` / ``ch_on`` /
  ``set_sv`` …），不是裸串口字节 —— 重放确定、可读、可编辑、可走干净 L3 的安全校验。
- :class:`RecordingProxy` 透明包住每个 backend：录制开启时，对**白名单方法的成功调用**
  自动记一步（失败的、``dry_run`` 的不记）。面板只需在 connect 时把 backend 包一层。
- 序列化用 JSON（人可读、可手改）；pydantic 参数（如 :class:`Position`）用 ``__type__``
  标签往返还原。
- :class:`RoutinePlayer` 在**干净的真 backend** 上按序回放，每步阻塞到完成；可中止、
  有进度回调、有安全闸（默认：含运动的程序要求机器已归零才允许跑）。

录制的是高层动作而非时间轴：每个动作（move_to/jog/...）回放时本就阻塞到完成，所以
默认回放 = 动作背靠背 + 步间固定小停顿（视觉清晰）+ 显式 ``wait`` 步还原真实等待
（如加热保温）。``t_offset_s`` 仅作录制参考留存。
"""

from __future__ import annotations

import inspect
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from pydantic import BaseModel, Field

from .hardware.types import Position

# ── pydantic 参数的类型标签往返 ──────────────────────────────────────────────
# 录制时方法参数可能是 pydantic 模型（如 move_to 的 Position）。存 JSON 要带类型标签，
# 回放时按标签还原成原类型再调用。

_MODEL_REGISTRY: dict[str, type[BaseModel]] = {}


def register_model(model_cls: type[BaseModel]) -> type[BaseModel]:
    """把一个 pydantic 模型登记进往返注册表（可作装饰器）。"""
    _MODEL_REGISTRY[model_cls.__name__] = model_cls
    return model_cls


register_model(Position)


def _encode(value: Any) -> Any:
    """把动作参数编码成 JSON 安全结构（pydantic→带标签 dict，Enum→值）。"""
    if isinstance(value, BaseModel):
        return {"__type__": type(value).__name__, "fields": value.model_dump(mode="json")}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value: Any) -> Any:
    """:func:`_encode` 的逆操作：把带标签 dict 还原成 pydantic 实例。"""
    if isinstance(value, dict) and "__type__" in value and "fields" in value:
        type_name = value["__type__"]
        cls = _MODEL_REGISTRY.get(type_name)
        if cls is None:
            raise KeyError(f"未注册的参数模型类型：{type_name}（在 routine.register_model 登记）")
        return cls(**value["fields"])
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    return value


# ── 数据模型 ────────────────────────────────────────────────────────────────

# 设备显示名（生成中文标签用）
_DEVICE_ZH = {
    "gantry": "龙门",
    "gripper": "夹爪",
    "relay": "继电器",
    "heater": "加热",
    "spin": "旋涂",
    "pipette": "移液",
    "control": "控制",
}

# 默认可录制方法白名单（按设备）。白名单只是"门"：录制只对实际被调用且在名单内的
# 方法生效，名单里多列几个无害。面板 connect 时按需传入或用此默认。
DEFAULT_RECORDABLE: dict[str, set[str]] = {
    "gantry": {"move_to", "jog", "home", "recover_from_alarm"},
    "gripper": {"open", "close"},
    "relay": {"ch_on", "ch_off"},
    "heater": {"write_sv", "set_sv", "set_temp", "stop"},
    "spin": {"set_speed", "set_rpm", "start", "stop", "spin"},
    "pipette": {"aspirate", "dispense", "home", "move_to"},
}

# 算作"运动"的龙门动作（决定安全闸是否要求已归零）
_MOTION_ACTIONS = {("gantry", "move_to"), ("gantry", "jog"), ("gantry", "home")}


def _inject_idempotency_key(method: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """若目标方法接受 ``idempotency_key`` 且未提供，注入一个新鲜 uuid。

    @observable 的幂等去重靠 ``idempotency_key`` —— 录制不存它，回放每次现生成，
    否则同一 key 会命中 TTL 缓存把真实动作变成 no-op。
    """
    if "idempotency_key" in kwargs:
        return kwargs
    try:
        params = inspect.signature(method).parameters
    except (ValueError, TypeError):
        return kwargs
    if "idempotency_key" in params:
        kwargs = dict(kwargs)
        kwargs["idempotency_key"] = uuid.uuid4().hex
    return kwargs


def _short(v: Any) -> str:
    """把一个参数值缩成短标签片段。"""
    if isinstance(v, Position):
        return f"x={v.x_mm:g},y={v.y_mm:g},z={v.z_mm:g}"
    if isinstance(v, dict) and v.get("__type__") == "Position":
        f = v["fields"]
        return f"x={f['x_mm']:g},y={f['y_mm']:g},z={f['z_mm']:g}"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _auto_label(device: str, action: str, args: Sequence[Any], kwargs: dict[str, Any]) -> str:
    """从设备/动作/参数生成一条人类可读的中文标签。"""
    if device == "control" and action == "wait":
        return f"等待 {kwargs.get('seconds', 0):g}s"
    parts = [_short(a) for a in args]
    for k, val in kwargs.items():
        if k in ("dry_run", "wait_for_idle", "timeout_s"):
            continue
        parts.append(f"{k}={_short(val)}")
    inner = ", ".join(p for p in parts if p)
    dev = _DEVICE_ZH.get(device, device)
    return f"{dev} {action}" + (f"（{inner}）" if inner else "")


class RoutineStep(BaseModel):
    """程序里的一步。``device='control'`` 是伪设备（目前只有 ``wait``）。"""

    seq: int
    device: str
    action: str
    args: list[Any] = Field(default_factory=list)        # 已编码的位置参数
    kwargs: dict[str, Any] = Field(default_factory=dict)  # 已编码的关键字参数
    t_offset_s: float = 0.0   # 录制时相对开始的秒数（参考用，回放默认不据此计时）
    label: str = ""           # 人类可读描述（自动生成，可手改）
    note: str = ""


class Routine(BaseModel):
    """一段可重放的程序（动作序列）。"""

    name: str
    description: str = ""
    created_at: str = ""      # ISO8601
    steps: list[RoutineStep] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Routine":
        return cls.model_validate_json(text)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Routine":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    @property
    def has_motion(self) -> bool:
        return any((s.device, s.action) in _MOTION_ACTIONS for s in self.steps)


# ── 录制 ────────────────────────────────────────────────────────────────────


class RoutineRecorder:
    """累积手动操作成步骤序列。``clock`` 可注入便于测试。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._armed = False
        self._steps: list[RoutineStep] = []
        self._t0 = 0.0
        self.name = ""

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def step_count(self) -> int:
        return len(self._steps)

    @property
    def steps(self) -> list[RoutineStep]:
        return list(self._steps)

    def arm(self, name: str = "") -> None:
        """开始录制（清空已录步骤、重置计时原点）。"""
        self._armed = True
        self._steps = []
        self._t0 = self._clock()
        self.name = name

    def disarm(self) -> None:
        """停止录制（保留已录步骤，供 :meth:`to_routine` 取用）。"""
        self._armed = False

    def clear(self) -> None:
        self._steps = []

    def _renumber(self) -> None:
        for n, s in enumerate(self._steps):
            s.seq = n

    def remove_last(self) -> None:
        if self._steps:
            self._steps.pop()
            self._renumber()

    def remove_at(self, index: int) -> None:
        if 0 <= index < len(self._steps):
            self._steps.pop(index)
            self._renumber()

    def set_steps(self, steps: Sequence[RoutineStep]) -> None:
        """用一组现成步骤替换当前程序（如从文件加载到编辑器），并保持已 arm。"""
        self._steps = [s.model_copy() for s in steps]
        self._renumber()
        self._armed = True

    def record(
        self,
        device: str,
        action: str,
        args: Sequence[Any] = (),
        kwargs: Optional[dict[str, Any]] = None,
        label: str = "",
    ) -> Optional[RoutineStep]:
        """记一步（仅当已 arm）。``dry_run=True`` 的调用跳过不记。

        ``args``/``kwargs`` 传**原始值**（含 pydantic 实例），内部负责编码。
        """
        if not self._armed:
            return None
        kwargs = dict(kwargs or {})
        if kwargs.get("dry_run") is True:
            return None
        # idempotency_key 是每次调用的临时值（@observable 去重用）——绝不入程序。
        # 回放时由 RoutinePlayer 按方法签名重新注入新鲜 key（否则会命中 TTL 缓存变 no-op）。
        kwargs.pop("idempotency_key", None)
        step = RoutineStep(
            seq=len(self._steps),
            device=device,
            action=action,
            args=[_encode(a) for a in args],
            kwargs={k: _encode(v) for k, v in kwargs.items()},
            t_offset_s=round(self._clock() - self._t0, 3),
            label=label or _auto_label(device, action, args, kwargs),
        )
        self._steps.append(step)
        return step

    def add_wait(self, seconds: float, label: str = "") -> Optional[RoutineStep]:
        """插入一个显式等待步（如手动保温）。仅当已 arm。"""
        return self.record("control", "wait", kwargs={"seconds": float(seconds)}, label=label)

    def to_routine(
        self,
        name: Optional[str] = None,
        description: str = "",
        created_at: Optional[str] = None,
    ) -> Routine:
        return Routine(
            name=name or self.name or "未命名程序",
            description=description,
            created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            steps=list(self._steps),
        )


class RecordingProxy:
    """透明包住一个 backend：对白名单方法的**成功调用**自动录一步。

    面板用法（connect 时包一层）::

        rec = RoutineRecorder()
        gantry = RecordingProxy(GantryBackend(...), rec, "gantry",
                                DEFAULT_RECORDABLE["gantry"])

    之后 ``gantry.move_to(...)`` 照常工作；``rec.is_armed`` 时自动记一步。
    回放请用**未包装的真 backend**（见 :class:`RoutinePlayer`）。
    """

    def __init__(
        self,
        target: Any,
        recorder: RoutineRecorder,
        device: str,
        recordable: Optional[set[str]] = None,
    ) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_device", device)
        object.__setattr__(
            self, "_recordable", set(recordable if recordable is not None else DEFAULT_RECORDABLE.get(device, set()))
        )

    @property
    def unwrapped(self) -> Any:
        """拿回被包的真 backend（回放用）。"""
        return object.__getattribute__(self, "_target")

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_target")
        attr = getattr(target, name)
        recordable = object.__getattribute__(self, "_recordable")
        if name in recordable and callable(attr):
            recorder = object.__getattribute__(self, "_recorder")
            device = object.__getattribute__(self, "_device")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                result = attr(*args, **kwargs)   # 先调真方法；抛错则不记
                recorder.record(device, name, args, kwargs)
                return result

            return wrapper
        return attr


# ── 回放 ────────────────────────────────────────────────────────────────────


class PlayerOptions(BaseModel):
    step_delay_s: float = 0.5    # 步间停顿（视觉清晰，0 = 不停）
    require_homed: bool = True   # 含运动的程序是否要求已归零（安全闸）


class StepResult(BaseModel):
    seq: int
    device: str
    action: str
    label: str
    ok: bool
    error: str = ""
    duration_ms: float = 0.0


class PlayResult(BaseModel):
    success: bool
    steps_total: int
    steps_completed: int
    aborted: bool = False
    error: str = ""
    step_results: list[StepResult] = Field(default_factory=list)


class RoutinePlayer:
    """在真 backend 上按序回放一段程序。

    ``backends`` 是 ``{设备名: backend}``（用**未包装**的真 backend）。
    ``clock``/``sleep`` 可注入便于测试。
    """

    def __init__(
        self,
        backends: dict[str, Any],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backends = backends
        self._clock = clock
        self._sleep = sleep

    def run(
        self,
        routine: Routine,
        options: Optional[PlayerOptions] = None,
        progress_cb: Optional[Callable[[int, int, RoutineStep], None]] = None,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> PlayResult:
        options = options or PlayerOptions()
        total = len(routine.steps)
        results: list[StepResult] = []

        # 安全闸：含运动动作但机器未归零 → 拒跑（守限位安全教训）
        if options.require_homed and routine.has_motion:
            gantry = self._backends.get("gantry")
            if gantry is not None and hasattr(gantry, "is_homed") and not gantry.is_homed():
                return PlayResult(
                    success=False, steps_total=total, steps_completed=0,
                    error="程序含运动动作，但机器未归零。请先归零再运行（限位安全要求）。",
                )

        for i, step in enumerate(routine.steps):
            if abort_check is not None and abort_check():
                return PlayResult(
                    success=False, steps_total=total, steps_completed=i,
                    aborted=True, error="已被用户中止。", step_results=results,
                )
            t0 = self._clock()
            try:
                self._exec(step)
            except Exception as exc:  # noqa: BLE001 — 回放要把任何步错误如实上报
                results.append(StepResult(
                    seq=step.seq, device=step.device, action=step.action, label=step.label,
                    ok=False, error=f"{type(exc).__name__}: {exc}",
                    duration_ms=round((self._clock() - t0) * 1000, 1),
                ))
                return PlayResult(
                    success=False, steps_total=total, steps_completed=i,
                    error=f"第 {i + 1}/{total} 步「{step.label}」失败：{exc}",
                    step_results=results,
                )
            results.append(StepResult(
                seq=step.seq, device=step.device, action=step.action, label=step.label,
                ok=True, duration_ms=round((self._clock() - t0) * 1000, 1),
            ))
            if progress_cb is not None:
                progress_cb(i + 1, total, step)
            if i < total - 1 and options.step_delay_s > 0:
                self._sleep(options.step_delay_s)

        return PlayResult(
            success=True, steps_total=total, steps_completed=total, step_results=results,
        )

    def _exec(self, step: RoutineStep) -> None:
        if step.device == "control":
            if step.action == "wait":
                self._sleep(float(step.kwargs.get("seconds", 0)))
                return
            raise ValueError(f"未知 control 动作：{step.action}")
        backend = self._backends.get(step.device)
        if backend is None:
            raise ValueError(f"程序引用了未连接的设备「{step.device}」。请先连接该设备再运行。")
        method = getattr(backend, step.action, None)
        if method is None or not callable(method):
            raise ValueError(f"设备「{step.device}」没有动作「{step.action}」。")
        args = [_decode(a) for a in step.args]
        kwargs = {k: _decode(v) for k, v in step.kwargs.items()}
        kwargs = _inject_idempotency_key(method, kwargs)
        method(*args, **kwargs)


# ── 程序文件持久化 ──────────────────────────────────────────────────────────

DEFAULT_ROUTINES_DIR = Path("runtime/routines")


def routines_dir(base: str | Path = DEFAULT_ROUTINES_DIR) -> Path:
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(name: str) -> str:
    keep = "-_."
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in name).strip("_")
    return cleaned or "routine"


def save_routine(routine: Routine, base: str | Path = DEFAULT_ROUTINES_DIR) -> Path:
    return routine.save(routines_dir(base) / f"{_safe_filename(routine.name)}.json")


def list_routines(base: str | Path = DEFAULT_ROUTINES_DIR) -> list[Path]:
    return sorted(routines_dir(base).glob("*.json"))


def load_routine(name_or_path: str | Path, base: str | Path = DEFAULT_ROUTINES_DIR) -> Routine:
    p = Path(name_or_path)
    if not p.exists():
        p = routines_dir(base) / f"{_safe_filename(str(name_or_path))}.json"
    return Routine.load(p)
