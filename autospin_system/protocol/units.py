"""Small unit conversion helpers used by validation and compilation.

The protocol stores physical quantities with units. These helpers normalize
accepted template units into the base units expected by the current hardware
workers: seconds, microliters, rpm, and Celsius.
"""


def to_seconds(quantity: float, unit: str) -> float:
    """Convert a time quantity to seconds."""

    normalized = unit.strip().lower()
    if normalized in {"s", "sec", "second", "seconds"}:
        return float(quantity)
    if normalized in {"min", "minute", "minutes"}:
        return float(quantity) * 60.0
    raise ValueError(f"Unsupported time unit: {unit}")


def to_ul(quantity: float, unit: str) -> float:
    """Convert a volume quantity to microliters."""

    normalized = unit.strip().lower()
    if normalized in {"ul", "microliter", "microliters"}:
        return float(quantity)
    if normalized == "ml":
        return float(quantity) * 1000.0
    raise ValueError(f"Unsupported volume unit: {unit}")


def to_rpm(quantity: float, unit: str) -> float:
    """Validate and return a speed quantity in rpm."""

    normalized = unit.strip().lower()
    if normalized == "rpm":
        return float(quantity)
    raise ValueError(f"Unsupported speed unit: {unit}")


def to_celsius(quantity: float, unit: str) -> float:
    """Convert a temperature quantity to degrees Celsius."""

    normalized = unit.strip().lower()
    if normalized in {"c", "degc", "degree_c", "degrees_c", "celsius"}:
        return float(quantity)
    raise ValueError(f"Unsupported temperature unit: {unit}")
