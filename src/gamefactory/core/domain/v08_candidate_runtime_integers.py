"""JSON-safe positive integer guards for V0.8-3B candidate runtime contracts."""

from __future__ import annotations

import math
from typing import Any

JSON_SAFE_INTEGER_MAX = 9007199254740991


class CandidateRuntimeIntegerError(ValueError):
    """Counter field cannot be coerced to a JSON-safe runtime integer."""


def strict_runtime_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise CandidateRuntimeIntegerError(f"{field} must not be a boolean")
    if isinstance(value, str):
        raise CandidateRuntimeIntegerError(f"{field} must be an integer")
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise CandidateRuntimeIntegerError(f"{field} must be a finite integer")
        if value != math.trunc(value):
            raise CandidateRuntimeIntegerError(f"{field} must be an integer")
        if value > JSON_SAFE_INTEGER_MAX:
            raise CandidateRuntimeIntegerError(f"{field} exceeds the JSON-safe integer maximum")
        out = int(value)
    else:
        raise CandidateRuntimeIntegerError(f"{field} must be an integer")
    if out < minimum:
        raise CandidateRuntimeIntegerError(f"{field} must be >= {minimum}")
    if out > JSON_SAFE_INTEGER_MAX:
        raise CandidateRuntimeIntegerError(f"{field} exceeds the JSON-safe integer maximum")
    return out
