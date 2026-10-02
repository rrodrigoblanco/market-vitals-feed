"""JSON writing helpers. A failed build must not replace a good file."""

from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# durability.json and gpu_waterfall.json are still owned by the other program.
# yields.json and macro.json are regenerated here, with their old keys kept.
FORBIDDEN_OUTPUTS = frozenset(
    {
        "durability.json",
        "gpu_waterfall.json",
    }
)

VOLATILE_KEYS = frozenset({"generated_at", "quoted_at", "updatedAt", "last_attempt", "last_success"})


def round_half_up(value: Decimal | str | int | float, places: int) -> Decimal:
    exponent = Decimal("1") if places == 0 else Decimal("10") ** (-places)
    return Decimal(str(value)).quantize(exponent, rounding=ROUND_HALF_UP)


def json_number(value: Decimal | int, places: int | None = None) -> int | float:
    """Turn a Decimal into an int or a float JSON can print cleanly."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if places is not None:
        decimal_value = round_half_up(decimal_value, places)
    if decimal_value == decimal_value.to_integral():
        return int(decimal_value)
    return float(decimal_value)


def measure(value: int | float | None, unit: str, **extra: object) -> dict:
    """A number plus a separate unit. The value must never contain the unit text."""
    if isinstance(value, str):
        raise TypeError("measure values must be numbers, not strings")
    payload: dict = {"value": value, "unit": unit}
    payload.update(extra)
    return payload


def canonicalize(obj: object) -> object:
    """Drop clocks so an unchanged dataset does not produce a new commit."""
    if isinstance(obj, dict):
        return {
            key: canonicalize(val)
            for key, val in obj.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [canonicalize(item) for item in obj]
    return obj


def dumps(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON via a temp file and os.replace. A crash keeps the old file."""
    path = Path(path)
    if path.name in FORBIDDEN_OUTPUTS:
        raise RuntimeError(f"Refusing to write protected file {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    text = dumps(payload)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_if_changed(path: Path, payload: dict) -> bool:
    """Write only when the data changed. Returns True if a new file was written."""
    path = Path(path)
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if previous is not None and canonicalize(previous) == canonicalize(payload):
            return False
    atomic_write_json(path, payload)
    return True
