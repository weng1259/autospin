"""Schema 导出契约测试。

这组测试守住 L3 API 合同：
1. 导出确定性（同代码 → 同输出，不含时间戳等非稳定字段）
2. 与 docs/api-v1.json 已 commit 版本完全一致（若 backend 改签名而没更新
   合同文件，这条会炸，提醒 reviewer）
3. 关键字段结构（error_code / severity / 方法参数 kind）符合 ADR-004 约定
"""
from __future__ import annotations

import json
from pathlib import Path

from src.schema_export import build_schema

API_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "docs" / "api-v1.json"


def test_schema_is_deterministic() -> None:
    """相同代码，两次 build → 字节级一致。"""
    a = json.dumps(build_schema(), sort_keys=True)
    b = json.dumps(build_schema(), sort_keys=True)
    assert a == b


def test_schema_matches_checked_in_contract() -> None:
    """导出必须等于 docs/api-v1.json。

    **若这条挂了**：说明你改了 backend 公共签名 / pydantic 字段 / L3Error
    属性。**修复方式**：重新跑 `python -m src.schema_export
    --out docs/api-v1.json` 更新合同文件，并在 PR 描述里解释 breaking
    change（或标注"纯注释更新"）。
    """
    current = json.dumps(build_schema(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    checked_in = API_CONTRACT_PATH.read_text(encoding="utf-8")
    if current != checked_in:
        # 给出友好 diff 提示，不然 CI log 里一堆 JSON
        msg = (
            f"api-v1.json drift detected. "
            f"Re-run: tools/spikes/.venv/bin/python -m src.schema_export "
            f"--out {API_CONTRACT_PATH}"
        )
        assert current == checked_in, msg


def test_all_errors_have_required_fields() -> None:
    """ADR-004 §原则 2：每个 L3Error 子类必须有 error_code / severity /
    recoverable / suggested_action / suggested_action_zh。"""
    schema = build_schema()
    required = {"error_code", "severity", "recoverable", "suggested_action"}
    for name, err in schema["errors"].items():
        if name == "L3Error":
            # 基类允许 suggested_action* 为空，子类不允许（由 test_errors 另测）
            continue
        missing = required - err.keys()
        assert not missing, f"{name} missing fields: {missing}"
        assert err["error_code"].startswith("L3."), (
            f"{name}.error_code must start with 'L3.' prefix, got {err['error_code']!r}"
        )
        assert err["severity"] in {"warning", "alarm"}, (
            f"{name}.severity must be 'warning' or 'alarm', got {err['severity']!r}"
        )


def test_home_and_recover_are_idempotency_key_required() -> None:
    """ADR-004 §原则 3：高代价方法必须要求调用者传 idempotency_key。"""
    methods = build_schema()["backends"]["GantryBackend"]["methods"]
    for method_name in ("home", "recover_from_alarm"):
        params = methods[method_name]["parameters"]
        assert "idempotency_key" in params, f"{method_name} missing idempotency_key"
        key_spec = params["idempotency_key"]
        assert key_spec["required"] is True
        assert key_spec["kind"] == "keyword_only"
        assert key_spec["type"] == "str"


def test_move_to_target_is_position() -> None:
    """move_to 必须接收 Position 而非裸 xyz float（ADR-004 §原则 1：pydantic
    而非裸 dict/tuple）。"""
    methods = build_schema()["backends"]["GantryBackend"]["methods"]
    target = methods["move_to"]["parameters"]["target"]
    assert target["required"] is True
    assert target["type"] == "Position"
