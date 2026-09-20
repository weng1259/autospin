#!/usr/bin/env python3
"""Build a minimal, standalone AutoSpin runtime directory without deleting files."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "deploy" / "runtime-package.json"
IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    includes = payload.get("include")
    if not isinstance(includes, list) or not includes:
        raise ValueError("runtime manifest must contain a non-empty include list")
    return payload


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORED_NAMES or Path(name).suffix in IGNORED_SUFFIXES
    }


def build_runtime_package(
    output: str | Path,
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> Path:
    """Copy only manifest entries into a new output directory.

    Existing output paths are rejected so this helper never overwrites or
    deletes an earlier deployment package.
    """

    destination = Path(output).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(
            f"output already exists; choose a new empty path: {destination}"
        )
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(manifest_file)
    destination.mkdir(parents=True)
    for raw_entry in manifest["include"]:
        entry = Path(str(raw_entry))
        if entry.is_absolute() or ".." in entry.parts:
            raise ValueError(f"unsafe runtime manifest entry: {raw_entry!r}")
        source = REPOSITORY_ROOT / entry
        if not source.exists():
            raise FileNotFoundError(f"runtime manifest entry not found: {entry}")
        target = destination / entry
        if source.is_dir():
            shutil.copytree(source, target, ignore=_ignore_generated)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    deployed_manifest = destination / "deploy" / "runtime-package.json"
    deployed_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_file, deployed_manifest)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the minimal standalone AutoSpin Raspberry Pi package."
    )
    parser.add_argument(
        "--output",
        required=True,
        help="New destination directory; it must not already exist.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Runtime manifest JSON path.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = build_runtime_package(args.output, manifest_path=args.manifest)
    print(f"Runtime package created: {result}")


if __name__ == "__main__":
    main()
