"""Immutable canonical paid request snapshot domain model.

Defines the paid request schema, canonical serialization, cryptographic
hashing, and strict validation for paid provider operations.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from gamefactory.core.domain.errors import PaidRequestIncompatibleError, PaidRequestInvalidError
from gamefactory.core.execution.redaction import redactor

PAID_REQUEST_SCHEMA: str = "paid-request-0.6.0"


_HEX_64_LOWERCASE = re.compile(r"^[0-9a-f]{64}$")
_WIN_DRIVE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")

_TOP_LEVEL_KEYS = frozenset(
    {"schema", "provider", "operation", "adapter", "binding", "request", "cost"}
)
_BINDING_KEYS = frozenset(
    {
        "asset_id",
        "revision_number",
        "concept_version",
        "concept_sha256",
        "specification_sha256",
        "profile_id",
        "profile_version",
    }
)
_COST_KEYS = frozenset({"estimate", "reservation", "unit"})

_FORBIDDEN_KEY_SUBSTRINGS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
)

_FORBIDDEN_KEY_EXACT_OR_SUBSTRINGS = (
    "timestamp",
    "created_at",
    "updated_at",
    "report_path",
)


def _is_absolute_path(val: str) -> bool:
    """Detect POSIX absolute paths, Windows drive paths, or UNC network paths."""
    if val.startswith("/") or val.startswith("\\\\") or val.startswith("//"):
        return True
    if _WIN_DRIVE_PATH.match(val):
        return True
    return False


def _validate_types_and_secrets(val: Any, key_name: str | None = None) -> None:
    """Recursively validate allowed value types, secret-like strings, paths, and key names."""
    if isinstance(val, bool):
        return
    elif isinstance(val, int):
        return
    elif isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise PaidRequestInvalidError(
                f"Float value for '{key_name or 'root'}' must be finite, got {val}"
            )
        return
    elif val is None:
        return
    elif isinstance(val, str):
        if _is_absolute_path(val):
            raise PaidRequestInvalidError(
                f"Absolute filesystem path not allowed in paid request snapshot: '{val}'"
            )
        redacted = redactor.redact_text(val)
        if redacted != val:
            raise PaidRequestInvalidError(
                f"Credential or secret-like value detected in '{key_name or 'root'}'"
            )
        return
    elif isinstance(val, list):
        for idx, item in enumerate(val):
            _validate_types_and_secrets(item, f"{key_name or 'list'}[{idx}]")
        return
    elif isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise PaidRequestInvalidError(
                    f"All dictionary keys must be strings, got {type(k).__name__}"
                )
            k_upper = k.upper()
            if any(sub in k_upper for sub in _FORBIDDEN_KEY_SUBSTRINGS):
                raise PaidRequestInvalidError(
                    f"Disallowed credential-like key in paid request snapshot: '{k}'"
                )
            k_lower = k.lower()
            if any(sub in k_lower for sub in _FORBIDDEN_KEY_EXACT_OR_SUBSTRINGS) or k_lower in (
                "time",
                "date",
            ):
                raise PaidRequestInvalidError(
                    f"Disallowed timestamp or report path key in paid request snapshot: '{k}'"
                )
            _validate_types_and_secrets(v, k)
        return
    else:
        raise PaidRequestInvalidError(
            f"Disallowed value type in paid request snapshot: {type(val).__name__}"
        )


def validate_paid_request(snapshot: dict[str, Any]) -> None:
    """Strictly validate a paid request snapshot dictionary against PAID_REQUEST_SCHEMA."""
    if not isinstance(snapshot, dict):
        raise PaidRequestInvalidError("Paid request snapshot must be a dict")

    # Validate recursive types, forbidden keys, paths, and credentials
    _validate_types_and_secrets(snapshot)

    # 1. Exact top-level keys
    if set(snapshot.keys()) != _TOP_LEVEL_KEYS:
        missing = _TOP_LEVEL_KEYS - set(snapshot.keys())
        extra = set(snapshot.keys()) - _TOP_LEVEL_KEYS
        raise PaidRequestInvalidError(
            f"Invalid top-level keys in paid request snapshot. Missing: {missing}, Extra: {extra}"
        )

    # 2. Schema version
    if snapshot["schema"] != PAID_REQUEST_SCHEMA:
        raise PaidRequestInvalidError(
            f"Invalid schema version '{snapshot.get('schema')}'; expected '{PAID_REQUEST_SCHEMA}'"
        )

    # 3. Provider & operation
    if not isinstance(snapshot["provider"], str) or not snapshot["provider"]:
        raise PaidRequestInvalidError("Snapshot provider must be a non-empty string")
    if not isinstance(snapshot["operation"], str) or not snapshot["operation"]:
        raise PaidRequestInvalidError("Snapshot operation must be a non-empty string")

    # 4. Adapter
    adapter = snapshot["adapter"]
    if not isinstance(adapter, dict):
        raise PaidRequestInvalidError("Snapshot adapter must be an object")
    if "id" not in adapter or not isinstance(adapter["id"], str) or not adapter["id"]:
        raise PaidRequestInvalidError("Snapshot adapter must have non-empty string 'id'")
    if (
        "contract_version" not in adapter
        or not isinstance(adapter["contract_version"], int)
        or isinstance(adapter["contract_version"], bool)
        or adapter["contract_version"] < 1
    ):
        raise PaidRequestInvalidError("Snapshot adapter contract_version must be an integer >= 1")

    # 5. Binding
    binding = snapshot["binding"]
    if not isinstance(binding, dict):
        raise PaidRequestInvalidError("Snapshot binding must be an object")
    if set(binding.keys()) != _BINDING_KEYS:
        missing_b = _BINDING_KEYS - set(binding.keys())
        extra_b = set(binding.keys()) - _BINDING_KEYS
        raise PaidRequestInvalidError(
            f"Invalid binding keys in paid request snapshot. Missing: {missing_b}, Extra: {extra_b}"
        )

    if not isinstance(binding["asset_id"], str) or not binding["asset_id"]:
        raise PaidRequestInvalidError("binding.asset_id must be a non-empty string")
    for int_key in ("revision_number", "concept_version"):
        val = binding[int_key]
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise PaidRequestInvalidError(f"binding.{int_key} must be an integer >= 1")

    for hash_key in ("concept_sha256", "specification_sha256"):
        h_val = binding[hash_key]
        if not isinstance(h_val, str) or not _HEX_64_LOWERCASE.match(h_val):
            raise PaidRequestInvalidError(
                f"binding.{hash_key} must be a 64-character lowercase hex SHA-256 hash"
            )

    if binding["profile_id"] is not None and (
        not isinstance(binding["profile_id"], str) or not binding["profile_id"]
    ):
        raise PaidRequestInvalidError("binding.profile_id must be a non-empty string or None")

    if binding["profile_version"] is not None and (
        not isinstance(binding["profile_version"], int)
        or isinstance(binding["profile_version"], bool)
        or binding["profile_version"] < 1
    ):
        raise PaidRequestInvalidError("binding.profile_version must be an integer >= 1 or None")

    # 6. Cost
    cost = snapshot["cost"]
    if not isinstance(cost, dict):
        raise PaidRequestInvalidError("Snapshot cost must be an object")
    if set(cost.keys()) != _COST_KEYS:
        missing_c = _COST_KEYS - set(cost.keys())
        extra_c = set(cost.keys()) - _COST_KEYS
        raise PaidRequestInvalidError(
            f"Invalid cost keys in paid request snapshot. Missing: {missing_c}, Extra: {extra_c}"
        )

    estimate = cost["estimate"]
    if estimate is not None:
        if isinstance(estimate, bool) or not isinstance(estimate, (int, float)):
            raise PaidRequestInvalidError("cost.estimate must be numeric or None")
        if math.isnan(estimate) or math.isinf(estimate) or estimate < 0:
            raise PaidRequestInvalidError("cost.estimate must be finite and >= 0")

    reservation = cost["reservation"]
    if isinstance(reservation, bool) or not isinstance(reservation, (int, float)):
        raise PaidRequestInvalidError("cost.reservation must be numeric")
    if math.isnan(reservation) or math.isinf(reservation) or reservation < 0:
        raise PaidRequestInvalidError("cost.reservation must be finite and >= 0")

    unit = cost["unit"]
    if not isinstance(unit, str) or not unit:
        raise PaidRequestInvalidError("cost.unit must be a non-empty string")

    # 7. Request
    if not isinstance(snapshot["request"], dict):
        raise PaidRequestInvalidError("Snapshot request must be an object")


def canonical_json(snapshot: dict[str, Any]) -> str:
    """Produce deterministic canonical JSON string for a paid request snapshot."""
    if not isinstance(snapshot, dict):
        raise PaidRequestInvalidError("Snapshot must be a dictionary")
    _validate_types_and_secrets(snapshot)
    return json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def paid_request_sha256(snapshot: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of canonical UTF-8 bytes."""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaidRequestSnapshot:
    """Immutable paid request snapshot binding an approved operation to exact provider inputs."""

    _canonical_json: str = field(repr=False)
    sha256: str

    def __init__(self, content: dict[str, Any], sha256: str) -> None:
        validate_paid_request(content)
        canonical = canonical_json(content)
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if not isinstance(sha256, str) or sha256.lower() != actual:
            raise PaidRequestInvalidError(
                f"Paid request snapshot SHA-256 does not match its content: "
                f"declared {sha256}, computed {actual}"
            )
        object.__setattr__(self, "_canonical_json", canonical)
        object.__setattr__(self, "sha256", actual)

    @property
    def content(self) -> dict[str, Any]:
        """Return a fresh dictionary parsed from canonical JSON to ensure immutability."""
        parsed: dict[str, Any] = json.loads(self._canonical_json)
        return parsed

    @property
    def canonical_json(self) -> str:
        """Return the immutable canonical JSON text."""
        return self._canonical_json

    @classmethod
    def from_content(cls, content: dict[str, Any]) -> PaidRequestSnapshot:
        """Validate, deep-copy, serialize, and hash a paid request dictionary."""
        copied = copy.deepcopy(content)
        validate_paid_request(copied)
        digest = paid_request_sha256(copied)
        return cls(content=copied, sha256=digest)

    @classmethod
    def load_verified(cls, canonical_text: str, expected_sha256: str) -> PaidRequestSnapshot:
        """Load and verify a snapshot from canonical text, raising on digest mismatch."""
        actual_sha = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        if actual_sha.lower() != expected_sha256.lower():
            raise PaidRequestInvalidError(
                f"Paid request snapshot SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}"
            )
        try:
            content = json.loads(canonical_text)
        except Exception as exc:
            raise PaidRequestInvalidError("Invalid JSON in canonical snapshot text") from exc

        validate_paid_request(content)
        re_canonical = canonical_json(content)
        if re_canonical != canonical_text:
            raise PaidRequestInvalidError("Snapshot text is not in canonical JSON form")
        return cls(content=content, sha256=actual_sha)


EXPECTED_IMAGE_TO_3D_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "model_type",
        "should_texture",
        "enable_pbr",
        "texture_resolution",
        "target_formats",
        "image_enhancement",
        "remove_lighting",
        "pose_mode",
        "texture_prompt",
        "remesh",
        "rig",
        "animate",
        "variants",
        "target_polycount",
    }
)


def check_image_to_3d_request_dict(req: dict[str, Any], provider_name: str = "meshy") -> None:
    """Validate request dictionary against image-to-3d adapter execution constraints."""
    if not isinstance(req, dict):
        raise PaidRequestIncompatibleError(
            "Request section must be a dictionary", provider=provider_name
        )
    if set(req.keys()) != EXPECTED_IMAGE_TO_3D_REQUEST_KEYS:
        missing = EXPECTED_IMAGE_TO_3D_REQUEST_KEYS - set(req.keys())
        extra = set(req.keys()) - EXPECTED_IMAGE_TO_3D_REQUEST_KEYS
        raise PaidRequestIncompatibleError(
            f"Unknown or missing request keys. Missing: {missing}, Extra: {extra}",
            provider=provider_name,
        )
    if req["model_type"] != "smart-topology":
        raise PaidRequestIncompatibleError(
            f"Unsupported model_type: '{req['model_type']}'; expected 'smart-topology'",
            provider=provider_name,
        )
    if req["texture_resolution"] not in {"2k"}:
        raise PaidRequestIncompatibleError(
            f"Unsupported texture_resolution: '{req['texture_resolution']}'; expected '2k'",
            provider=provider_name,
        )
    if req["target_formats"] != ["glb"]:
        raise PaidRequestIncompatibleError(
            f"Unsupported target_formats: {req['target_formats']}; expected ['glb']",
            provider=provider_name,
        )
    poly = req["target_polycount"]
    if isinstance(poly, bool) or not isinstance(poly, int) or poly < 100 or poly > 15000:
        raise PaidRequestIncompatibleError(
            f"target_polycount must be an integer in 100..15000, got {poly}",
            provider=provider_name,
        )
    for omit_field in (
        "image_enhancement",
        "remove_lighting",
        "pose_mode",
        "texture_prompt",
        "remesh",
    ):
        if req[omit_field] != "omit":
            raise PaidRequestIncompatibleError(
                f"Field '{omit_field}' has unsupported value '{req[omit_field]}'; current adapter version cannot send it (must be 'omit')",
                provider=provider_name,
            )
    if req["rig"] is not False:
        raise PaidRequestIncompatibleError(
            f"Field 'rig' must be False, got {req['rig']}",
            provider=provider_name,
        )
    if req["animate"] is not False:
        raise PaidRequestIncompatibleError(
            f"Field 'animate' must be False, got {req['animate']}",
            provider=provider_name,
        )
    if req["variants"] != 1 or isinstance(req["variants"], bool):
        raise PaidRequestIncompatibleError(
            f"Field 'variants' must be 1, got {req['variants']}",
            provider=provider_name,
        )
    if not isinstance(req["should_texture"], bool):
        raise PaidRequestIncompatibleError(
            "Field 'should_texture' must be a boolean",
            provider=provider_name,
        )
    if not isinstance(req["enable_pbr"], bool):
        raise PaidRequestIncompatibleError(
            "Field 'enable_pbr' must be a boolean",
            provider=provider_name,
        )
