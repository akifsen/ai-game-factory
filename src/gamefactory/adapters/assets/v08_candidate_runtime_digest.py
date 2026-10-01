"""Cross-language request digests for V0.8-3B candidate capsule runtime."""

from __future__ import annotations

import json
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import sha256_bytes
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    JSON_SAFE_INTEGER_MAX,
    CandidateRuntimeIntegerError,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    strict_runtime_int as _strict_runtime_int_core,
)

__all__ = [
    "JSON_SAFE_INTEGER_MAX",
    "CandidateRuntimeDigestError",
    "strict_runtime_int",
    "candidate_runtime_request_digest",
    "candidate_runtime_bound_payload_canonical",
    "candidate_runtime_rest_aabb_canonical_sha256",
    "canonicalize_candidate_runtime_request_for_digest",
]

RUNTIME_REQUEST_TRANSIENT_KEYS = frozenset(
    {
        "glb",
        "output_dir",
        "observation_path",
        "capture_dir",
        "request_digest",
        "bound_payload_canonical",
    }
)
RUNTIME_REQUEST_STRICT_INT_FIELDS = frozenset(
    {
        "revision",
        "strict_attempt_number",
    }
)


class CandidateRuntimeDigestError(ValueError):
    """Bound request payload cannot be canonicalized for digest."""


def strict_runtime_int(value: Any, field: str, *, minimum: int = 1) -> int:
    try:
        return _strict_runtime_int_core(value, field, minimum=minimum)
    except CandidateRuntimeIntegerError as exc:
        raise CandidateRuntimeDigestError(str(exc)) from exc


def candidate_runtime_rest_aabb_canonical_sha256(rest_aabb: dict[str, Any]) -> str:
    """Canonical SHA-256 of rest AABB min/max (binds framing and capsule center policy)."""
    if not isinstance(rest_aabb, dict):
        raise CandidateRuntimeDigestError("rest_aabb must be an object")
    for axis in ("min", "max"):
        if axis not in rest_aabb:
            raise CandidateRuntimeDigestError(f"rest_aabb.{axis} is required")
    canonical = json.dumps(
        {"max": rest_aabb["max"], "min": rest_aabb["min"]},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256_bytes(canonical.encode("utf-8"))


def canonicalize_candidate_runtime_request_for_digest(request: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in request.items() if k not in RUNTIME_REQUEST_TRANSIENT_KEYS}
    if "request_digest" in payload:
        payload = {k: v for k, v in payload.items() if k != "request_digest"}
    out: dict[str, Any] = {}
    for key in sorted(payload):
        value = payload[key]
        if key in RUNTIME_REQUEST_STRICT_INT_FIELDS:
            out[key] = strict_runtime_int(value, key)
        elif key == "viewport" and isinstance(value, dict):
            out[key] = {
                "height": strict_runtime_int(value.get("height"), "viewport.height"),
                "width": strict_runtime_int(value.get("width"), "viewport.width"),
            }
        else:
            out[key] = value
    return out


def candidate_runtime_bound_payload_canonical(payload: dict[str, Any]) -> str:
    """Deterministic UTF-8 JSON text hashed by Python and Godot (ADR 0021 / V0.8-3B)."""
    canonical = canonicalize_candidate_runtime_request_for_digest(payload)
    parts: list[str] = []
    for key in sorted(canonical):
        value_json = json.dumps(
            canonical[key], sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        parts.append(f"{json.dumps(key)}:{value_json}")
    return "{" + ",".join(parts) + "}"


def candidate_runtime_request_digest(request: dict[str, Any]) -> str:
    canonical = candidate_runtime_bound_payload_canonical(request)
    return sha256_bytes(canonical.encode("utf-8"))
