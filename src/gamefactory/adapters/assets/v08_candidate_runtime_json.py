"""Strict bounded JSON parsing for V0.8-3B candidate runtime artifacts."""

from __future__ import annotations

import json
from typing import Any

MAX_RUNTIME_OBSERVATION_BYTES = 262_144


class CandidateRuntimeJsonError(ValueError):
    """Candidate runtime JSON is malformed or out of policy."""


def _reject_nonfinite_constant(token: str) -> Any:
    raise CandidateRuntimeJsonError(f"invalid JSON constant: {token}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CandidateRuntimeJsonError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def parse_strict_runtime_json_object(raw: str | bytes, *, max_bytes: int) -> dict[str, Any]:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(data) > max_bytes:
        raise CandidateRuntimeJsonError("JSON exceeds the byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateRuntimeJsonError(f"invalid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except json.JSONDecodeError as exc:
        raise CandidateRuntimeJsonError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CandidateRuntimeJsonError("JSON root must be an object")
    return parsed
