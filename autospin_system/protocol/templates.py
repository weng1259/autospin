"""Fixed protocol templates used before LLM integration.

These templates play the role that future natural-language parsing will fill:
they produce protocol-shaped JSON. Keeping the same schema now makes the later
LLM step a source replacement rather than an execution rewrite.
"""

from copy import deepcopy


# Template values are deliberately close to real spin-coating recipes, but
# still pass through the same validator as generated protocols.
TEMPLATES = {
    "perovskite_basic": {
        "sample_id": "sample_001",
        "description": (
            "Dispense precursor, spin coat with a timed antisolvent drop, "
            "then anneal on Hotplate1."
        ),
        "operations": [
            {"name": "MoveSample", "from": "storage_tray", "to": "spin_center"},
            {
                "name": "DispenseLiquid",
                "liquid": "perovskite_precursor",
                "volume": {"quantity": 80, "unit": "uL"},
                "target": "spin_center",
            },
            {
                "name": "SpinCoat",
                "steps": [
                    {
                        "speed": {"quantity": 1000, "unit": "rpm"},
                        "duration": {"quantity": 10, "unit": "s"},
                    },
                    {
                        "speed": {"quantity": 4000, "unit": "rpm"},
                        "duration": {"quantity": 30, "unit": "s"},
                    },
                ],
                "timed_events": [
                    {
                        "at": {"quantity": 22, "unit": "s"},
                        "operation": {
                            "name": "DispenseLiquid",
                            "liquid": "antisolvent",
                            "volume": {"quantity": 150, "unit": "uL"},
                            "target": "spin_center",
                        },
                    }
                ],
            },
            {
                "name": "Anneal",
                "temperature": {"quantity": 100, "unit": "C"},
                "duration": {"quantity": 10, "unit": "min"},
                "target": "Hotplate1",
            },
            {"name": "Measure", "method": "manual_note", "target": "film"},
        ],
    },
    "spin_only_test": {
        "sample_id": "spin_test_001",
        "description": "Short spin-only protocol for dry-run verification.",
        "operations": [
            {"name": "MoveSample", "from": "storage_tray", "to": "spin_center"},
            {
                "name": "SpinCoat",
                "steps": [
                    {
                        "speed": {"quantity": 500, "unit": "rpm"},
                        "duration": {"quantity": 3, "unit": "s"},
                    },
                    {
                        "speed": {"quantity": 1500, "unit": "rpm"},
                        "duration": {"quantity": 5, "unit": "s"},
                    },
                ],
            },
        ],
    },
}


def load_template(name: str) -> dict:
    """Return a defensive copy so callers can modify a template safely."""

    if name not in TEMPLATES:
        available = ", ".join(sorted(TEMPLATES))
        raise KeyError(f"Unknown template '{name}'. Available templates: {available}")
    return deepcopy(TEMPLATES[name])
