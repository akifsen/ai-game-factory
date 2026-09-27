"""Read production receipts without rewriting historical evidence.

V0.5 receipts are profile-qualified. A V0.4 evidence manifest is adapted in
memory to the same fields. The historical file is not modified.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gamefactory.core.domain.errors import ValidationError

_REQUIRED_V05 = (
    "asset_id",
    "revision",
    "profile_id",
    "profile_version",
    "spec_hash",
    "concept_hash",
    "provider_request_fingerprint",
    "provider_task_id",
    "cost",
    "raw_artifact_hash",
    "processed_artifact_hash",
    "validation_hash",
    "runtime_hash",
    "render_hashes",
    "approval_ids",
    "completed_at",
)


def read_production_receipt(source: Path | dict[str, Any]) -> dict[str, Any]:
    """Return a normalized receipt view from a 0.5 receipt or a 0.4 evidence manifest."""
    if isinstance(source, Path):
        payload = json.loads(source.read_text(encoding="utf-8"))
    else:
        payload = source
    if not isinstance(payload, dict):
        raise ValidationError("production receipt must be a JSON object")
    schema = payload.get("schema_version")
    if schema == "production-receipt-0.5.0":
        missing = [key for key in _REQUIRED_V05 if key not in payload]
        if missing:
            raise ValidationError(f"production receipt is missing {missing[0]}")
        return dict(payload)
    if schema == "asset-evidence-0.4.0":
        return _from_v04_manifest(payload)
    raise ValidationError(f"unsupported production receipt schema: {schema}")


def _from_v04_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    files = payload.get("files")
    if not isinstance(files, list):
        raise ValidationError("V0.4 evidence manifest has no file list")
    by_role: dict[str, dict[str, Any]] = {}
    renders: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        digest = item.get("sha256")
        if role == "runtime_capture" and isinstance(item.get("angle"), str):
            renders[str(item["angle"])] = str(digest)
        elif isinstance(role, str) and role not in by_role:
            by_role[role] = item
    final = payload.get("final_review")
    approval_ids: dict[str, Any] = {}
    if isinstance(final, dict) and final.get("fingerprint"):
        approval_ids["final_approval"] = final.get("fingerprint")
    return {
        "schema_version": "production-receipt-0.5.0",
        "source_schema": "asset-evidence-0.4.0",
        "asset_id": payload.get("asset_id"),
        "revision": payload.get("revision"),
        "profile_id": "static_prop",
        "profile_version": 1,
        "profile_qualified": "static_prop@1",
        "profile_schema": "asset-profile-0.5.0",
        "spec_hash": payload.get("specification_fingerprint"),
        "concept_hash": by_role.get("concept", {}).get("sha256"),
        "provider_request_fingerprint": None,
        "provider_task_id": None,
        "cost": None,
        "raw_artifact_hash": by_role.get("raw_glb", {}).get("sha256"),
        "processed_artifact_hash": payload.get("processed_glb_sha256")
        or by_role.get("processed_glb", {}).get("sha256"),
        "validation_hash": by_role.get("validation", {}).get("sha256"),
        "runtime_hash": by_role.get("runtime_observation", {}).get("sha256"),
        "render_hashes": renders,
        "approval_ids": approval_ids,
        "completed_at": None,
        "historical": True,
    }
