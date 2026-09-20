from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.build_runtime_package import build_runtime_package


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_package_excludes_non_runtime_top_level_directories(tmp_path) -> None:
    output = build_runtime_package(tmp_path / "autospin-runtime")

    assert (output / "src" / "hardware").is_dir()
    assert (output / "recipes" / "perovskite_experiment.json").is_file()
    assert (output / "tools" / "run_webserver.py").is_file()
    for excluded in (
        "autospin_system",
        "firmware",
        "hardware",
        "migration_snapshot",
        "tests",
    ):
        assert not (output / excluded).exists()


def test_isolated_runtime_imports_and_resolves_local_recipes(tmp_path) -> None:
    output = build_runtime_package(tmp_path / "isolated-runtime")
    check = (
        "import json; "
        "from src.recipe_storage import resolve_recipes_path; "
        "from src.webapp import DeviceRegistry, create_app; "
        "from src.webapp.routes_experiments import _load_multi_round_protocols; "
        "p=resolve_recipes_path(); "
        "app=create_app(DeviceRegistry.from_mocks(), token='test'); "
        "protocols=_load_multi_round_protocols('perovskite_experiment.json', recipes_path=p); "
        "print(json.dumps({'recipes':str(p),'routes':len(app.routes),'rounds':len(protocols)}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", check],
        cwd=output,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip())
    assert Path(payload["recipes"]) == output / "recipes"
    assert payload["routes"] > 1
    assert payload["rounds"] == 4
