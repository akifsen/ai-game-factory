"""Bounded natural-language planning domain for the static_prop pilot.

The agent produces only a strict draft (asset id, intent, dimensions, constrained
style). This module derives fixed schema/profile/orientation/budgets and the
managed import path, then validates against the existing asset contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, field_validator

from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    _validate_asset_id_format,
    parse_any_asset_specification,
)
from gamefactory.core.domain.errors import SpecInvalidError, ValidationError

PILOT_PROFILE_ID = "static_prop"
PILOT_SCHEMA_VERSION = "0.4.0"
PILOT_CATEGORY = "prop"
PILOT_TARGET_ENGINE = "godot"
PILOT_GAME_CONTEXT = "Tide Bastion"

_FIXED_FAMILY = "stylized_fantasy"
_FIXED_READABILITY = "high"

_ALLOWED_SILHOUETTES = frozenset({"chunky", "planar", "organic", "angular"})
_ALLOWED_DETAIL_DENSITY = frozenset({"low", "medium", "high"})

_MANAGED_IMPORT_PREFIX = "assets/generated/props"

CODEX_MESSAGE_FILE_NAME = "message.json"
_MAX_CODEX_MESSAGE_BYTES = 262_144


class PlanDimensionsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_m: StrictFloat = Field(..., ge=0.001, le=100.0)
    depth_m: StrictFloat = Field(..., ge=0.001, le=100.0)
    height_m: StrictFloat = Field(..., ge=0.001, le=100.0)


class PlanStyleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    silhouette: str = Field(..., min_length=1, max_length=100)
    detail_density: str = Field(..., min_length=1, max_length=100)

    @field_validator("silhouette")
    @classmethod
    def validate_silhouette(cls, value: str) -> str:
        if value not in _ALLOWED_SILHOUETTES:
            allowed = ", ".join(sorted(_ALLOWED_SILHOUETTES))
            raise ValueError(f"silhouette must be one of: {allowed}")
        return value

    @field_validator("detail_density")
    @classmethod
    def validate_detail_density(cls, value: str) -> str:
        if value not in _ALLOWED_DETAIL_DENSITY:
            allowed = ", ".join(sorted(_ALLOWED_DETAIL_DENSITY))
            raise ValueError(f"detail_density must be one of: {allowed}")
        return value


class StaticPropPlanDraft(BaseModel):
    """Strict agent draft for the static_prop@1 pilot."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., min_length=2, max_length=80)
    intent: str = Field(..., min_length=1, max_length=500)
    dimensions: PlanDimensionsDraft
    style_constraints: PlanStyleDraft

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        return _validate_asset_id_format(value)


def codex_draft_json_schema() -> dict[str, Any]:
    """JSON Schema for Codex --output-schema (draft fields only)."""
    schema = StaticPropPlanDraft.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key}")
        seen.add(key)
        result[key] = value
    return result


def read_bounded_codex_message_json(message_path: Path) -> dict[str, Any]:
    """Load strict JSON object protocol from Codex message file only."""
    if not message_path.is_file():
        raise ValidationError("Codex planner message file is missing")
    try:
        size = message_path.stat().st_size
    except OSError as exc:
        raise ValidationError("Codex planner message file is unreadable") from exc
    if size <= 0 or size > _MAX_CODEX_MESSAGE_BYTES:
        raise ValidationError("Codex planner message file is empty or too large")
    try:
        raw = message_path.read_bytes()
    except OSError as exc:
        raise ValidationError("Codex planner message file is unreadable") from exc
    if len(raw) > _MAX_CODEX_MESSAGE_BYTES:
        raise ValidationError("Codex planner message file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Codex planner message file is not valid UTF-8") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValidationError("Codex planner message file is not valid JSON") from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Codex planner message file must contain a JSON object")
    return parsed


def parse_plan_draft(payload: dict[str, Any] | str) -> StaticPropPlanDraft:
    """Parse and validate a Codex draft payload."""
    data: dict[str, Any]
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValidationError("Planner draft is not valid JSON") from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise ValidationError("Planner draft must be a JSON object")
    try:
        return StaticPropPlanDraft.model_validate(data)
    except Exception as exc:
        raise ValidationError(f"Planner draft failed validation: {exc}") from exc


def managed_target_import_path(asset_id: str) -> str:
    """Return the pilot-managed relative import path for a static prop."""
    if re.search(r"[./\\]", asset_id):
        raise ValidationError("asset_id must not contain path separators")
    return f"{_MANAGED_IMPORT_PREFIX}/{asset_id}/"


def assemble_static_prop_specification(draft: StaticPropPlanDraft) -> dict[str, Any]:
    """Derive a full asset-spec-0.4.0 document from a validated draft."""
    import_path = managed_target_import_path(draft.asset_id)
    return {
        "schema_version": PILOT_SCHEMA_VERSION,
        "asset_id": draft.asset_id,
        "category": PILOT_CATEGORY,
        "profile": PILOT_PROFILE_ID,
        "intent": draft.intent,
        "dimensions": {
            "width_m": draft.dimensions.width_m,
            "depth_m": draft.dimensions.depth_m,
            "height_m": draft.dimensions.height_m,
        },
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_policy": "bottom_center",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
        },
        "material_budget": {"max_materials": 2},
        "texture_budget": {"max_dimension": 2048},
        "collider_policy": "box",
        "lod_policy": "lod0_lod1",
        "style_constraints": {
            "family": _FIXED_FAMILY,
            "silhouette": draft.style_constraints.silhouette,
            "readability": _FIXED_READABILITY,
            "detail_density": draft.style_constraints.detail_density,
        },
        "target_engine": PILOT_TARGET_ENGINE,
        "target_import_path": import_path,
    }


def plan_to_asset_specification(
    draft_payload: dict[str, Any] | str,
) -> AssetSpecification:
    """Validate draft, assemble spec, and parse with the domain contract."""
    draft = parse_plan_draft(draft_payload)
    assembled = assemble_static_prop_specification(draft)
    try:
        spec = parse_any_asset_specification(assembled)
    except SpecInvalidError:
        raise
    except Exception as exc:
        raise SpecInvalidError(f"Assembled specification is invalid: {exc}") from exc
    if not isinstance(spec, AssetSpecification):
        raise ValidationError("Pilot planner only supports asset-spec-0.4.0 static_prop")
    if spec.profile != PILOT_PROFILE_ID:
        raise ValidationError(f"Pilot planner is restricted to profile {PILOT_PROFILE_ID}")
    qualified = spec.bound_profile().qualified
    if qualified != f"{PILOT_PROFILE_ID}@1":
        raise ValidationError(f"Pilot planner requires profile {PILOT_PROFILE_ID}@1, got {qualified}")
    return spec


def build_planner_prompt(user_request: str) -> str:
    """Compose the stdin prompt for Codex exec (static_prop pilot only)."""
    cleaned = user_request.strip()
    if not cleaned:
        raise ValidationError("Planning request cannot be empty")
    return (
        f"You are planning one in-game static prop asset for the {PILOT_GAME_CONTEXT} project.\n"
        "Output ONLY JSON matching the provided schema. Do not include schema_version, profile, "
        "budgets, orientation, target_import_path, or any field outside the schema.\n"
        "Choose a unique lowercase asset_id (prop_ prefix encouraged), a concise intent, realistic "
        "dimensions in meters, and style_constraints.silhouette plus style_constraints.detail_density "
        "from the allowed enums.\n"
        f"Operator request:\n{cleaned}\n"
    )
