"""Inventory loading for protocol validation.

The validator checks generated or template-defined liquid names against this
inventory before any task reaches Maestro. This prevents a protocol from
executing with a liquid name that has no known platform location.
"""

from pathlib import Path

import yaml


BASE_DIR = Path(__file__).resolve().parents[1]
REAGENTS_FILE = BASE_DIR / "config" / "reagents.yaml"


def load_reagent_inventory(path: str | Path | None = None) -> dict:
    """Load the reagent inventory YAML, returning an empty inventory if absent."""

    inventory_path = Path(path) if path else REAGENTS_FILE
    if not inventory_path.exists():
        return {"liquids": {}}
    with inventory_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {"liquids": {}}
