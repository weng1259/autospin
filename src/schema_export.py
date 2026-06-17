"""L3 API schema 导出（ADR-004 §原则 1 "严格类型签名"的可验证落地）。

作用：把 `src/hardware/` 下所有公共 pydantic 模型、GantryBackend 方法签名、
L3Error 子类的结构化字段导出为 **一份确定性 JSON**（`docs/api-v1.json`），
作为 L3 与 Agent / UI / 其它 backend consumer 之间的 API 合同。

为什么要这份文件：
- **合同稳定性**：每次 PR 改到 backend 公共签名 / 错误属性 / pydantic 字段
  时，re-run 本脚本会让 git diff 冒出来，CI 上可强制 reviewer 看到 breaking
  change
- **Agent 可读**：Phase 3.5 正式 agent 上场时，Anthropic `@tool(...)` 的
  schema 可直接从本文件复用，不再手抄 pydantic 字段
- **自动生成参考文档**（未来）：从 `docs/api-v1.json` 再二次渲染 Markdown

运行方式：
    tools/spikes/.venv/bin/python -m src.schema_export > docs/api-v1.json

或直接写到默认路径（验收脚本）：
    tools/spikes/.venv/bin/python -m src.schema_export --out docs/api-v1.json

**确定性保证**（ADR-004 §原则 1）：
- 输出不含时间戳 / 机器名 / 绝对路径。纯代码函数。
- JSON key 顺序固定（用 OrderedDict 风格 + sort_keys=False 显式控制）
- 浮点数走 pydantic 默认序列化，不引入本地 locale 敏感格式
"""
from __future__ import annotations

import argparse
import inspect
import json
import sys
from typing import Any, get_type_hints

from .hardware import errors as err_mod
from .hardware import types as types_mod
from .hardware.gantry_backend import GantryBackend
from .hardware.gripper_backend import GripperBackend
from .hardware.relay_backend import RelayBackend


SCHEMA_VERSION = "1.0"


# GantryBackend 公共方法白名单。
# 理由：`start_move_async` / `is_move_in_progress` / `consume_last_move_result`
# 是 Slice 3 为绕过 Streamlit 同步 handler 限制加的**实现细节**，不是产品
# 级 API 合同的一部分。`_*` 以下划线开头的方法天然排除。
GANTRY_PUBLIC_METHODS = [
    "connect",
    "close",
    "get_status",
    "get_position",
    "is_connected",
    "is_homed",
    "get_grbl_settings",
    "validate_grbl_settings",
    "repair_grbl_settings",
    "home",
    "move_to",
    "halt",
    "soft_reset",
    "unlock_alarm",
    "recover_from_alarm",
]


# RelayBackend 公共方法白名单。DSTUR-T80 8 路继电器。
RELAY_PUBLIC_METHODS = [
    "connect",
    "close",
    "is_connected",
    "get_state",
    "ch_on",
    "ch_off",
]


# GripperBackend 公共方法白名单。继电器路径（Phase 3.3），
# RS485 力控 setter 等 Phase 3.5+ 接入后再扩展。
GRIPPER_PUBLIC_METHODS = [
    "get_state",
    "open",
    "close",
]


def _describe_type(t: Any) -> str:
    """把 typing 对象 / class 渲染成 Agent/人类可读的字符串。

    保守渲染：不展开深层 generic（Optional[List[Dict[...]]]）——目前 L3 API
    类型都是简单的 class / Optional[class] / bool / float / str / int。
    """
    if t is type(None):  # noqa: E721
        return "None"
    if t is inspect.Signature.empty:
        return "Any"
    origin = getattr(t, "__origin__", None)
    if origin is not None:
        args = getattr(t, "__args__", ())
        # Optional[X] = Union[X, None]
        if origin.__name__ == "Union" and type(None) in args:
            non_none = [a for a in args if a is not type(None)]  # noqa: E721
            if len(non_none) == 1:
                return f"Optional[{_describe_type(non_none[0])}]"
        inner = ", ".join(_describe_type(a) for a in args)
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{inner}]"
    if inspect.isclass(t):
        return t.__name__
    return str(t)


def _export_pydantic_models() -> dict[str, Any]:
    """遍历 types_mod，导出所有 BaseModel 子类的 JSON schema。"""
    from pydantic import BaseModel

    out: dict[str, Any] = {}
    for name in sorted(dir(types_mod)):
        obj = getattr(types_mod, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and obj.__module__ == types_mod.__name__
        ):
            out[name] = obj.model_json_schema()
    return out


def _export_enums() -> dict[str, Any]:
    """导出 types_mod 里的 Enum 类。"""
    from enum import Enum

    out: dict[str, Any] = {}
    for name in sorted(dir(types_mod)):
        obj = getattr(types_mod, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, Enum)
            and obj is not Enum
            and obj.__module__ == types_mod.__name__
        ):
            out[name] = {
                "values": [e.value for e in obj],
                "names": [e.name for e in obj],
            }
    return out


def _export_errors() -> dict[str, Any]:
    """导出 L3Error 基类 + 所有子类的结构化字段。

    Agent 读这份时主要看 `error_code` / `severity` / `recoverable` /
    `suggested_action*` 四个字段——这些是 ADR-004 §原则 2 "结构化错误"的
    最小合同。`human_message` / `agent_message` 是运行时实例字段，不在
    schema 里（每次抛错消息不同）。
    """
    out: dict[str, Any] = {}
    for name in sorted(dir(err_mod)):
        obj = getattr(err_mod, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, err_mod.L3Error)
            and obj.__module__ == err_mod.__name__
        ):
            out[name] = {
                "error_code": obj.error_code,
                "severity": obj.severity,
                "recoverable": obj.recoverable,
                "suggested_action": obj.suggested_action,
                "suggested_action_zh": obj.suggested_action_zh,
                "docstring": (obj.__doc__ or "").strip(),
            }
    return out


def _export_method(cls: type, method_name: str) -> dict[str, Any]:
    fn = getattr(cls, method_name)
    sig = inspect.signature(fn)
    # 解析类型注解为真 class 而非字符串（处理 `from __future__ import
    # annotations` 后所有注解都是 string 的情况）。
    try:
        type_hints = get_type_hints(fn)
    except Exception:
        type_hints = {}

    params: dict[str, Any] = {}
    for p_name, p in sig.parameters.items():
        if p_name == "self":
            continue
        kind_map = {
            inspect.Parameter.POSITIONAL_OR_KEYWORD: "positional_or_keyword",
            inspect.Parameter.POSITIONAL_ONLY: "positional_only",
            inspect.Parameter.KEYWORD_ONLY: "keyword_only",
            inspect.Parameter.VAR_POSITIONAL: "var_positional",
            inspect.Parameter.VAR_KEYWORD: "var_keyword",
        }
        param_type = type_hints.get(p_name, p.annotation)
        has_default = p.default is not inspect.Parameter.empty
        entry = {
            "type": _describe_type(param_type),
            "kind": kind_map.get(p.kind, "unknown"),
            "required": not has_default,
        }
        if has_default:
            # default 可能是非 JSON-friendly（Position 等），用 repr 兜底
            try:
                json.dumps(p.default)
                entry["default"] = p.default
            except (TypeError, ValueError):
                entry["default"] = repr(p.default)
        params[p_name] = entry

    ret_type = type_hints.get("return", sig.return_annotation)
    docstring = (fn.__doc__ or "").strip()
    return {
        "parameters": params,
        "returns": _describe_type(ret_type),
        "docstring": docstring,
    }


def _export_backends() -> dict[str, Any]:
    return {
        "GantryBackend": {
            "methods": {m: _export_method(GantryBackend, m) for m in GANTRY_PUBLIC_METHODS},
        },
        "RelayBackend": {
            "methods": {m: _export_method(RelayBackend, m) for m in RELAY_PUBLIC_METHODS},
        },
        "GripperBackend": {
            "methods": {m: _export_method(GripperBackend, m) for m in GRIPPER_PUBLIC_METHODS},
        },
    }


def build_schema() -> dict[str, Any]:
    """组装完整 schema dict。确定性：相同 src → 相同输出。"""
    return {
        "version": SCHEMA_VERSION,
        "models": _export_pydantic_models(),
        "enums": _export_enums(),
        "errors": _export_errors(),
        "backends": _export_backends(),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help="写到该路径；不指定则打印到 stdout",
    )
    args = p.parse_args()

    schema = build_schema()
    # sort_keys=True → 绝对字典序，diff 稳定；indent=2 人类可读
    text = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"✓ schema written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
