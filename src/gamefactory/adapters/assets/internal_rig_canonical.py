"""Canonical digests for internal rig evidence (V0.8-2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_skin_region import (
    RUNTIME_REQUEST_STRICT_INT_FIELDS,
    strict_region_boundary_tolerance,
    strict_runtime_int,
)

VALIDATOR_CONTRACT_VERSION = "humanoid_12bone_v1"

PINNED_CONTRACT_CANONICAL_SHA256 = (
    "670e55fc5ea867bb546ff29f03c10bd05e65a6707a79954eb3f1d3e81070245e"
)
PINNED_BLENDER_EXPORT_SCRIPT_SHA256 = (
    "43f321887b2ffba010e5876d3a5fd21d88fa04d8a0cec3e09bd5996f30cbe769"
)
PINNED_GODOT_HARNESS_REVIEWED_SHA256 = (
    "d4f406daded207808e5bbdb8d4501c23803136a7f7449a59dc9a47ae0fc1fcea"
)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256_bytes(raw)


def reviewed_text_sha256(path: Path) -> tuple[str, str]:
    """Return (LF-normalized text digest, raw bytes digest) for reviewed .py/.gd sources."""
    raw = path.read_bytes()
    raw_digest = sha256_bytes(raw)
    suffix = path.suffix.casefold()
    if suffix in {".py", ".gd"}:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return sha256_bytes(text.encode("utf-8")), raw_digest
    return raw_digest, raw_digest


def contract_canonical_digest(contract_bytes: bytes) -> str:
    return canonical_json_digest(json.loads(contract_bytes.decode("utf-8")))


def canonicalize_runtime_request_for_digest(request: dict[str, Any]) -> dict[str, Any]:
    """Normalize bound request fields for cross-language SHA-256 (strict int counts, pinned tolerance)."""
    payload = {
        k: v for k, v in request.items() if k not in {"output_path", "request_digest", "glb"}
    }
    out: dict[str, Any] = {}
    for key in sorted(payload):
        value = payload[key]
        if key in RUNTIME_REQUEST_STRICT_INT_FIELDS:
            out[key] = strict_runtime_int(value, key)
        elif key == "region_boundary_tolerance":
            out[key] = strict_region_boundary_tolerance(value)
        else:
            out[key] = value
    return out


def _godot_compatible_runtime_json_bytes(payload: dict[str, Any]) -> bytes:
    """Match Godot JSON.stringify for the top-level region_boundary_tolerance field only."""
    parts: list[str] = []
    for key in sorted(payload):
        value = payload[key]
        if key == "region_boundary_tolerance":
            value_json = "0.000001"
        else:
            value_json = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        parts.append(f"{json.dumps(key)}:{value_json}")
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def runtime_request_digest(request: dict[str, Any]) -> str:
    """Digest bound oracle request fields (excludes transient Godot-only keys)."""
    payload = canonicalize_runtime_request_for_digest(request)
    return sha256_bytes(_godot_compatible_runtime_json_bytes(payload))
