"""Hardware controller exports.

The package intentionally lazy-loads controllers so importing one hardware
module does not require every optional serial dependency or package import path
to be valid at once.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MotorController",
    "PipetteController",
    "RelayManager",
    "XYZStage",
    "HeatingStageController",
]


def __getattr__(name: str) -> Any:
    if name == "MotorController":
        from .spin_motor import MotorController

        return MotorController
    if name == "PipetteController":
        from .pipette import PipetteController

        return PipetteController
    if name == "RelayManager":
        from .relay import RelayManager

        return RelayManager
    if name == "XYZStage":
        from .xyz_stage import XYZStage

        return XYZStage
    if name == "HeatingStageController":
        from .heating_stage import HeatingStageController

        return HeatingStageController
    raise AttributeError(name)
