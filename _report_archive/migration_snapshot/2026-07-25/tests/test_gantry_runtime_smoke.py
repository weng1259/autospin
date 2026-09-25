from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.hardware.types import MachineState, MachineStatus, Position
from tools.gantry_runtime_smoke import (
    HARDWARE_CONFIRMATION,
    SAFE_TARGET,
    _parse_args,
    run_smoke,
)


def _registry_with_gantry(gantry: Mock) -> SimpleNamespace:
    return SimpleNamespace(gantry=gantry)


def test_default_mode_builds_registry_without_touching_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gantry = Mock()
    factory = Mock(return_value=_registry_with_gantry(gantry))
    monkeypatch.setattr(
        "tools.gantry_runtime_smoke.DeviceRegistry.from_config",
        factory,
    )

    run_smoke(execute_hardware=False)

    factory.assert_called_once_with()
    gantry.connect.assert_not_called()
    gantry.home.assert_not_called()
    gantry.move_to.assert_not_called()
    gantry.disconnect.assert_not_called()


def test_hardware_mode_runs_required_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gantry = Mock()
    gantry.get_status.return_value = MachineStatus(
        state=MachineState.IDLE,
        position=Position(x_mm=-5.0, y_mm=-5.0, z_mm=-5.0),
        is_homed=False,
    )
    gantry.get_position.return_value = SAFE_TARGET
    factory = Mock(return_value=_registry_with_gantry(gantry))
    monkeypatch.setattr(
        "tools.gantry_runtime_smoke.DeviceRegistry.from_config",
        factory,
    )

    run_smoke(execute_hardware=True)

    assert [item[0] for item in gantry.method_calls] == [
        "connect",
        "get_status",
        "home",
        "move_to",
        "get_position",
        "disconnect",
    ]
    gantry.connect.assert_called_once_with()
    gantry.get_status.assert_called_once_with()
    gantry.home.assert_called_once()
    gantry.move_to.assert_called_once_with(SAFE_TARGET)
    gantry.get_position.assert_called_once_with()
    gantry.disconnect.assert_called_once_with()


def test_hardware_mode_disconnects_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gantry = Mock()
    gantry.get_status.side_effect = RuntimeError("status failed")
    monkeypatch.setattr(
        "tools.gantry_runtime_smoke.DeviceRegistry.from_config",
        Mock(return_value=_registry_with_gantry(gantry)),
    )

    with pytest.raises(RuntimeError, match="status failed"):
        run_smoke(execute_hardware=True)

    gantry.disconnect.assert_called_once_with()
    gantry.home.assert_not_called()
    gantry.move_to.assert_not_called()


def test_hardware_flag_requires_exact_confirmation() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--execute-hardware"])

    args = _parse_args(
        ["--execute-hardware", "--confirm", HARDWARE_CONFIRMATION]
    )
    assert args.execute_hardware is True


def test_default_cli_runs_when_fastapi_import_is_blocked() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    blocker = """
import importlib.abc
import runpy
import sys

class BlockFastAPI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fastapi" or fullname.startswith("fastapi."):
            raise ModuleNotFoundError("FastAPI intentionally unavailable")
        return None

sys.meta_path.insert(0, BlockFastAPI())
runpy.run_path("tools/gantry_runtime_smoke.py", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "no serial connection" in result.stdout
