"""@observable 装饰器 —— 自动 record runlog + publish event_bus。

用法：
    class GantryBackend:
        @observable                     # 用默认 TTL（5 min）
        def move_to(self, ...): ...

        @observable(idempotency_ttl_s=24 * 3600)  # 显式长 TTL（24h）
        def home(self, *, idempotency_key: str) -> HomeResult:
            ...

行为：
1. 调用前 publish `phase="started"` 事件到 event_bus（不入 runlog）。
2. 调用后 publish + record `phase="completed"` 或 `phase="error"`。
3. 若 kwargs 包含 `idempotency_key` 且命中缓存（默认 5 min TTL，可覆盖），
   直接返回缓存结果，**不**触发任何事件（重试不出现在历史里）。
4. pydantic 返回值会被 `.model_dump()` 序列化进 event.result。

### TTL 分级（ADR-004 §原则 3 + 2026-04-22 架构审阅修订）

- **默认 5 min**：move_to / halt / soft_reset / unlock_alarm 等秒级操作——
  吸收网络抖动 / 连点防抖，但缓存过几分钟就 stale-reality（机器可能状态
  已变），不宜长
- **显式 24h**：home / recover_from_alarm 等"高物理代价 + 长耗时"动作——
  Agent session 断线几分钟后用同 key 重试，**必须**命中同一天内的缓存，
  否则会再归零一次（30s 物理动作 + 磨损电机 + 磨损刹车）

装饰器调用形式：
- `@observable`：无参，等价于 `@observable(idempotency_ttl_s=300)`
- `@observable(idempotency_ttl_s=86400)`：有参

ADR-004 §原则 3 + §原则 6 的实现载体。
"""
from __future__ import annotations

import functools
import sys
import time
import traceback
from typing import Any, Callable, ParamSpec, TypeVar, overload

from pydantic import BaseModel

from .event_bus import EVENT_BUS, Event
from .hardware.errors import L3Error
from .runlog import RUNLOG

P = ParamSpec("P")
R = TypeVar("R")

_DEFAULT_IDEM_TTL_S = 5 * 60
_IDEM_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}


def _serialize_params(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if isinstance(v, BaseModel):
            out[k] = v.model_dump()
        else:
            out[k] = v
    if args:
        out["_positional"] = [
            v.model_dump() if isinstance(v, BaseModel) else v for v in args
        ]
    return out


def _serialize_result(result: Any) -> Any:
    if isinstance(result, BaseModel):
        return result.model_dump()
    return result


def _make_wrapper(method: Callable[P, R], ttl_s: int) -> Callable[P, R]:
    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # 装饰的都是实例方法，args[0] 必为 self
        self_obj = args[0]
        method_name = f"{type(self_obj).__name__}.{method.__name__}"
        idem_key_val = kwargs.get("idempotency_key")
        idem_key = idem_key_val if isinstance(idem_key_val, str) else None
        # dry_run 预演不进幂等缓存：否则"先 dry_run 预演、再同 key 真执行"会
        # 命中缓存把真执行静默吞掉——Agent 的标准用法正是这个顺序（2026-07-16
        # 审查修正，读写两侧一起跳过）。
        if bool(kwargs.get("dry_run")):
            idem_key = None

        # ── idempotency short-circuit ──
        if idem_key is not None:
            cache_key = (method_name, idem_key)
            cached = _IDEM_CACHE.get(cache_key)
            if cached is not None:
                ts, cached_result = cached
                if time.time() - ts < ttl_s:
                    return cached_result  # type: ignore[no-any-return]
                del _IDEM_CACHE[cache_key]

        event_id = Event.new_id()
        params = _serialize_params(args[1:], dict(kwargs))

        EVENT_BUS.publish(Event(
            event_id=event_id, method=method_name, phase="started", params=params,
        ))

        t0 = time.time()
        try:
            result: R = method(*args, **kwargs)
        except L3Error as e:
            duration_ms = (time.time() - t0) * 1000
            evt = Event(
                event_id=event_id, method=method_name, phase="error", params=params,
                error_code=e.error_code,
                error_message=e.human_message,
                error_agent_message=e.agent_message,
                duration_ms=duration_ms,
            )
            RUNLOG.record(evt)
            EVENT_BUS.publish(evt)
            raise
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            # 裸 Python 异常（非 L3Error 子类）—— 可能是 backend race condition
            # 或未 wrapping 的原始错误。Slice 4 起完整 traceback 打到 stderr，
            # Streamlit 启动日志里可见，方便事后诊断。runlog 的 error_message
            # 只放一行摘要（schema 暂无 traceback 字段）。
            tb = traceback.format_exc()
            print(
                f"[observable] UNEXPECTED {type(e).__name__} in {method_name}\n{tb}",
                file=sys.stderr,
                flush=True,
            )
            evt = Event(
                event_id=event_id, method=method_name, phase="error", params=params,
                error_code="L3.UNEXPECTED",
                error_message=f"{type(e).__name__}: {e}",
                error_agent_message=repr(e) + "\n" + tb,
                duration_ms=duration_ms,
            )
            RUNLOG.record(evt)
            EVENT_BUS.publish(evt)
            raise

        # 让 result.event_id（如有）跟 runlog 的 event_id 对齐，方便 UI 关联
        if isinstance(result, BaseModel) and "event_id" in type(result).model_fields:
            result = result.model_copy(update={"event_id": event_id})

        duration_ms = (time.time() - t0) * 1000
        evt = Event(
            event_id=event_id, method=method_name, phase="completed", params=params,
            result=_serialize_result(result), duration_ms=duration_ms,
        )
        RUNLOG.record(evt)
        EVENT_BUS.publish(evt)

        if idem_key is not None:
            _IDEM_CACHE[(method_name, idem_key)] = (time.time(), result)
        return result

    # 把 TTL 挂在 wrapper 上，方便测试 introspect（tests/test_idempotency.py）。
    # 名字用 `__observable_ttl_s__` 是 "内部元信息 dunder" 风格：前后双下划线
    # 提示"框架私有，非 API 合同"；不会污染 schema_export（它只看公共方法）。
    wrapper.__observable_ttl_s__ = ttl_s  # type: ignore[attr-defined]
    return wrapper


@overload
def observable(method: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def observable(
    *, idempotency_ttl_s: int = ...
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def observable(
    method: Callable[P, R] | None = None,
    *,
    idempotency_ttl_s: int = _DEFAULT_IDEM_TTL_S,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """既支持 `@observable` 又支持 `@observable(idempotency_ttl_s=...)`。"""
    if method is not None and callable(method):
        # 无参用法：@observable
        return _make_wrapper(method, _DEFAULT_IDEM_TTL_S)

    # 有参用法：@observable(idempotency_ttl_s=N)
    def decorator(m: Callable[P, R]) -> Callable[P, R]:
        return _make_wrapper(m, idempotency_ttl_s)

    return decorator
