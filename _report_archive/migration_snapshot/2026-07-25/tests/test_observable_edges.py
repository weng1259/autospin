"""@observable 边界路径（骨干覆盖补充）：非 L3Error 的裸 Exception 分支。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import observable as observable_mod
from src.observable import observable
from src.runlog import RunLog


@pytest.fixture
def isolated_runlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> RunLog:
    r = RunLog(db_path=tmp_path / "test_runlog.db")
    monkeypatch.setattr(observable_mod, "RUNLOG", r)
    observable_mod._IDEM_CACHE.clear()
    yield r
    observable_mod._IDEM_CACHE.clear()


def test_observable_wraps_naked_exception_as_unexpected(
    isolated_runlog: RunLog, capsys: pytest.CaptureFixture[str],
) -> None:
    """非 L3Error 子类 → runlog 记 `L3.UNEXPECTED` + stderr 打 traceback。"""

    class Bad:
        @observable
        def fail(self) -> None:
            raise RuntimeError("boom-inner")

    with pytest.raises(RuntimeError, match="boom-inner"):
        Bad().fail()

    events = isolated_runlog.query_recent()
    err = [e for e in events if e["phase"] == "error"]
    assert len(err) == 1
    assert err[0]["error_code"] == "L3.UNEXPECTED"
    assert "RuntimeError" in err[0]["error_message"]

    # stderr 应有 traceback 打印（完整诊断链）
    captured = capsys.readouterr()
    assert "UNEXPECTED RuntimeError" in captured.err
    assert "boom-inner" in captured.err
