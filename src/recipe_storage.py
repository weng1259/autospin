"""Project-local storage paths for executable experiment recipes."""
from __future__ import annotations

import os
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECIPES_PATH = REPOSITORY_ROOT / "recipes"
RECIPES_PATH_ENV = "AUTOSPIN_RECIPES_DIR"


def resolve_recipes_path(configured: str | Path | None = None) -> Path:
    """Resolve the recipe directory without depending on a sibling project."""

    if configured is not None:
        return Path(configured).expanduser().resolve()
    override = os.environ.get(RECIPES_PATH_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_RECIPES_PATH.resolve()


def resolve_recipe_file(recipes_path: str | Path, recipe_name: str) -> Path:
    """Return one JSON recipe path while rejecting traversal and subdirectories."""

    safe_name = Path(recipe_name).name
    if safe_name != recipe_name or not safe_name.lower().endswith(".json"):
        raise ValueError("recipe_name must be one JSON filename")
    return Path(recipes_path) / safe_name
