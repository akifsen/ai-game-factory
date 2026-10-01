#!/usr/bin/env python3
"""Cold, standard-library-only verification of V0.8-3C candidate evidence envelopes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED = {"manifest.json"}
_MAX_FILE_BYTES = 50_000_000
_MAX_BUNDLE_BYTES = 256_000_000
_MAX_JSON_BYTES = 4_000_000
_MAX_JSON_STRING_LEN = 1_048_576
_MAX_JSON_DEPTH = 64
_MAX_JSON_ARRAY_LEN = 100_000
_MAX_JSON_OBJECT_KEYS = 10_000
_MAX_BUNDLE_FILE_COUNT = 10_000
_MAX_CAPTURE_PNG_BYTES = 8 * 1024 * 1024
_CAPTURE_WIDTH = 1280
_CAPTURE_HEIGHT = 720
_MAX_CAPTURE_DECODED_BYTES = _CAPTURE_WIDTH * _CAPTURE_HEIGHT * 4 + _CAPTURE_HEIGHT

TRUSTED_RIG_VERIFIER_SHA256 = "3e4849b9b0781d45f805a462400b019732a0d8946bce83c6c883064d3973130e"
TRUSTED_RIG_VERIFIER_SHA256_LF = "708e87ba18bba6972a332c2135d80c18cd416345bbfc030398c6ca9db4647291"
TRUSTED_RIG_VERIFIER_SHA256_FIXED = frozenset(
    {
        TRUSTED_RIG_VERIFIER_SHA256,
        TRUSTED_RIG_VERIFIER_SHA256_LF,
    }
)
PINNED_PACKAGED_PROFILE_DOCUMENT_HASH = (
    "dffd7f9f61d3f524e042f4d6aa6882ec7de703d5886243a7f4a69cb1b3a4d08c"
)
PINNED_RUNTIME_CONTRACT_SHA256 = "d814f0bca505e53f0966c4eeb04bf40bb79bfe424bd7fa1c414e66d2db5f3572"
PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256 = (
    "9977cbc306389f178dc939603b9687f4c367e46f19c8bbbd6917dc39142eed27"
)

CANDIDATE_TEST_ONLY_APPROVAL = "candidate_test_only_review"
CANDIDATE_RECEIPT_SCOPE = "candidate_test_only"
CANDIDATE_GRAPH_VERSION = "0.8.0-candidate"
PRE_REVIEW_BINDING_COUNT = 23

_SINGLE_ROLES = frozenset(
    {
        "snapshot",
        "candidate_specification",
        "candidate_profile_document",
        "source_glb",
        "raw_glb",
        "processed_glb",
        "static_validation_report",
        "runtime_request",
        "runtime_observation",
        "runtime_provenance",
        "runtime_import_log",
        "runtime_render_log",
        "rig_attempt_wrapper",
        "test_only_receipt",
        "candidate_runtime_contract",
        "reviewed_candidate_harness",
        "identity_report",
        "approval_scope",
    }
)
_CAPTURE_ROLE = "runtime_capture"
_ALLOWED_MANIFEST_ROLES = _SINGLE_ROLES | {_CAPTURE_ROLE}
_NINE_VIEWS = frozenset(
    {
        "front",
        "rear",
        "left",
        "right",
        "side",
        "three_quarter",
        "three_quarter_front",
        "three_quarter_rear",
        "top",
    }
)


def _capture_view_from_relative_path(rel: str, *, field: str) -> str:
    normalized = rel.replace("\\", "/")
    name = PurePosixPath(normalized).name
    if not name.endswith(".png") or len(name) <= 4:
        raise ValueError(f"{field} capture relative_path must end with <view>.png")
    view = name[:-4]
    if view not in _NINE_VIEWS or PurePosixPath(normalized).stem != view:
        raise ValueError(f"{field} capture relative_path view identity invalid")
    return view


def _runtime_capture_paths_by_view(roles: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in roles.get(_CAPTURE_ROLE, []):
        if not isinstance(item, dict):
            raise ValueError("runtime_capture inventory invalid")
        angle = item.get("angle")
        path = item.get("path")
        if not isinstance(angle, str) or not isinstance(path, str):
            raise ValueError("runtime_capture inventory invalid")
        if angle in mapping:
            raise ValueError(f"duplicate runtime_capture manifest angle: {angle}")
        mapping[angle] = path
    if set(mapping) != _NINE_VIEWS:
        raise ValueError("runtime_capture manifest angles incomplete")
    return mapping


_MANIFEST_FILE_ENTRY_KEYS = frozenset({"path", "role", "size", "sha256", "angle"})
_MANIFEST_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "bundle_id",
        "workflow_id",
        "specification_hash",
        "profile_document_hash",
        "source_glb_sha256",
        "nested_bundle_path",
        "validation_status",
        "runtime_status",
        "reviewed_pins_match",
        "snapshot_fingerprint",
        "fixture_label",
        "files",
    }
)
_ENGINE_ERROR_PATTERNS = (
    re.compile(r"ERROR:", re.IGNORECASE),
    re.compile(r"SCRIPT ERROR", re.IGNORECASE),
    re.compile(r"Parse Error", re.IGNORECASE),
)

_AABB_TOLERANCE = 2e-3
_CENTER_TOLERANCE = 1e-4
_FRAMING_DISTANCE_TOLERANCE = 5e-3
_FILL_TOLERANCE = 1e-3
_FRAMING_NUMERIC_TOLERANCE = 1e-3
_CENTER_OFFSET_TOLERANCE = 1e-3
_PROJECTED_RECT_PIXEL_SLACK = 2.0

_RUNTIME_REQUEST_TRANSIENT_KEYS = frozenset(
    {
        "glb",
        "output_dir",
        "observation_path",
        "capture_dir",
        "request_digest",
        "bound_payload_canonical",
    }
)
_RUNTIME_REQUEST_STRICT_INT_FIELDS = frozenset({"revision", "strict_attempt_number"})
_MAX_SAFE_INT = 9007199254740991
_IDENTITY_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "raw_sha256",
        "processed_sha256",
        "byte_identity",
        "execution_id",
        "attempt_number",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "approval_id",
        "approval_type",
        "status",
        "receipt_scope",
        "production_eligible",
        "promotion_eligible",
        "fingerprint",
        "snapshot_fingerprint",
        "approval_operation_hash",
        "workflow_id",
        "task_id",
        "review_execution_id",
        "approval_status",
    }
)
_SCOPE_ENTRY_KEYS = frozenset(
    {
        "artifact_id",
        "artifact_type",
        "content_hash",
        "task_id",
        "execution_id",
        "attempt_number",
        "relative_path",
        "bundle_path",
        "size",
        "role_or_nested_manifest",
    }
)
_APPROVAL_SCOPE_KEYS = frozenset(
    {
        "schema_version",
        "review_task",
        "persisted_approval_artifact_ids",
        "entries",
        "operation_hash",
        "approval_id",
        "approval_status",
        "review_execution_id",
    }
)
_STATIC_REPORT_KEYS = frozenset({"status", "passed", "summary", "evaluated_at", "findings"})
_RUNTIME_PROVENANCE_KEYS = frozenset(
    {
        "stage_dir",
        "request_path",
        "import_log",
        "render_log",
        "bound_request",
        "import_exit_code",
        "process_exit_code",
        "import_timed_out",
        "process_timed_out",
        "verification",
    }
)
_OBSERVATION_TOP_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "errors",
        "production_eligible",
        "candidate_state",
        "public_status",
        "workflow_id",
        "revision",
        "execution_id",
        "strict_attempt_number",
        "asset_id",
        "processed_glb_sha256",
        "observed_glb_sha256",
        "godot_version",
        "request_digest",
        "harness_sha256",
        "harness_sha256_raw",
        "visual_mesh_name",
        "collision_shape_class",
        "runtime_body_kind",
        "physics_ray_hit",
        "rest_aabb_canonical_sha256",
        "rest_mesh_bounds",
        "capsule",
        "captures",
        "view_framing",
    }
)
_CAPTURE_ENTRY_KEYS = frozenset(
    {
        "view",
        "png_sha256",
        "png_width",
        "png_height",
        "execution_id",
        "revision",
        "strict_attempt_number",
        "request_digest",
    }
)
_VIEW_FRAMING_KEYS = frozenset(
    {
        "view_axis",
        "camera_distance",
        "projected_rect_pixels",
        "height_ratio",
        "fill_ratio",
        "center_offset",
        "horizontally_centered",
        "ok",
        "reason",
    }
)
_PROJECTED_RECT_KEYS = frozenset({"x", "y", "width", "height"})
_REST_BOUNDS_KEYS = frozenset({"min", "max"})
_FORBIDDEN_SCOPE_ARTIFACT_TYPES = frozenset(
    {"candidate-test-only-receipt", "candidate-approval-scope"}
)
_CANDIDATE_SPEC_KEYS = frozenset(
    {
        "schema_version",
        "asset_id",
        "category",
        "profile",
        "profile_version",
        "intent",
        "source_kind",
        "dimensions",
        "orientation",
        "origin_contract",
        "rig_contract_id",
        "visual_mesh_name",
        "geometry_budget",
        "material_budget",
        "texture_budget",
        "lod_policy",
        "style_constraints",
        "target_engine",
        "target_import_path",
        "collider",
        "profile_document_hash",
        "processed_glb_sha256",
    }
)
_DIMENSIONS_KEYS = frozenset({"width_m", "depth_m", "height_m"})
_GEOMETRY_BUDGET_KEYS = frozenset({"max_triangles_lod0", "max_triangles_lod1", "lod_ratio"})
_MATERIAL_BUDGET_KEYS = frozenset({"max_materials"})
_TEXTURE_BUDGET_KEYS = frozenset({"max_dimension"})
_ORIENTATION_KEYS = frozenset({"up", "front"})
_STYLE_CONSTRAINTS_KEYS = frozenset({"family", "silhouette", "readability", "detail_density"})
_COLLIDER_KEYS = frozenset({"policy", "capsule"})
_CAPSULE_KEYS = frozenset({"radius_m", "height_m"})
_PROFILE_DOC_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "version",
        "candidate_state",
        "public_status",
        "categories",
        "geometry_mode",
        "rigging",
        "rest_pose",
        "runtime_body",
        "animation_forbidden",
        "collider_policy",
        "origin_contract",
        "rig_contract_id",
        "visual_mesh_name",
        "accepted_source_kinds",
        "dimension_rules",
        "processing",
        "runtime",
    }
)
_DIMENSION_RULES_KEYS = frozenset({"min_m", "max_m"})
_PROCESSING_POLICY_KEYS = frozenset(
    {
        "lod0_required",
        "lod1_required",
        "allowed_lod_policies",
        "dimension_tolerance_m",
        "rig_forbidden",
        "animation_forbidden",
        "max_materials",
        "max_texture_dimension",
        "max_triangles_lod0",
    }
)
_RUNTIME_POLICY_KEYS = frozenset({"bounds_tolerance_ratio", "bounds_tolerance_floor_m"})
_SNAPSHOT_KEYS = frozenset(
    {
        "graph_version",
        "workflow_id",
        "asset_id",
        "revision_number",
        "specification_hash",
        "profile_document_hash",
        "source_glb_hash",
        "authoritative_source_glb_relative_path",
        "pre_review_artifact_bindings",
        "profile_id",
        "profile_version",
        "asset_revision_spec_hash",
        "asset_revision_raw_glb_hash",
        "asset_revision_processed_glb_hash",
        "retained_specification_sha256",
        "retained_profile_document_sha256",
        "retained_source_sha256",
        "raw_glb_sha256",
        "processed_glb_sha256",
        "static_validation_report_sha256",
        "runtime_request_sha256",
        "runtime_observation_sha256",
        "runtime_capture_hashes",
        "runtime_provenance_sha256",
        "runtime_import_log_sha256",
        "runtime_render_log_sha256",
        "rig_attempt_wrapper_sha256",
        "nested_rig_manifest_sha256",
        "identity_report_sha256",
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
        "runtime_request_digest",
        "nested_bundle_id",
        "nested_rig_manifest_on_disk_sha256",
    }
)
_SNAPSHOT_BINDING_KEYS = frozenset(
    {
        "artifact_id",
        "task_id",
        "role",
        "relative_path",
        "content_sha256",
        "size_bytes",
        "producing_execution_id",
    }
)
_EXECUTION_BLOCK_KEYS = frozenset({"id", "attempt_number", "status"})
_REVIEW_TASK_KEYS = frozenset({"id", "task_type", "cost_class", "parameters"})
_REVIEW_TASK_PARAM_KEYS = frozenset(
    {
        "graph_version",
        "source_kind",
        "asset_id",
        "revision_number",
        "specification",
        "specification_hash",
        "profile_document_hash",
        "source_glb",
        "source_glb_hash",
        "asset_dir",
        "profile_id",
        "profile_version",
        "paid_provider_invocations",
    }
)
_RUNTIME_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "asset_id",
        "workflow_id",
        "revision",
        "execution_id",
        "strict_attempt_number",
        "processed_glb_sha256",
        "profile_document_hash",
        "spec_fingerprint",
        "runtime_contract_sha256",
        "visual_mesh_name",
        "rest_aabb",
        "rest_aabb_canonical_sha256",
        "capsule_center_m",
        "capsule",
        "harness_sha256",
        "nine_view_set",
        "framing",
        "viewport",
        "renderer_profile",
        "godot_version",
        "candidate_state",
        "public_status",
        "production_eligible",
        "bound_payload_canonical",
        "request_digest",
        "glb",
        "capture_dir",
        "observation_path",
    }
)
_STAGED_RUNTIME_GLB = "res://Main.tscn"
_BOUND_RUNTIME_REQUEST_KEYS = _RUNTIME_REQUEST_KEYS - frozenset(
    {"glb", "capture_dir", "observation_path"}
)
_FRAMING_CONTRACT_KEYS = frozenset(
    {
        "fov_degrees",
        "target_screen_fraction",
        "margin_fraction",
        "min_screen_fraction",
        "max_screen_fraction",
    }
)
_VIEWPORT_KEYS = frozenset({"width", "height"})
_OBSERVED_CAPSULE_KEYS = frozenset(
    {"shape_class", "observed_radius_m", "observed_height_m", "center_m"}
)
_CANDIDATE_VISUAL_MESH_NAME = "SM_HumanoidSkin"


def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], name: str) -> None:
    extra = set(obj.keys()) - allowed
    if extra:
        raise ValueError(f"{name} has unknown keys: {sorted(extra)}")


def _manifest_nested_bundle_path(manifest: dict[str, Any]) -> str:
    nested_path = manifest.get("nested_bundle_path")
    if not isinstance(nested_path, str) or not nested_path.strip():
        raise ValueError("manifest nested_bundle_path missing")
    normalized = _path(nested_path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("manifest nested_bundle_path is unsafe")
    return normalized


def _check_specification_against_profile(spec: dict[str, Any], profile_doc: dict[str, Any]) -> None:
    _reject_unknown_keys(spec, _CANDIDATE_SPEC_KEYS, "candidate_specification")
    if spec.get("schema_version") != "0.8.0-candidate":
        raise ValueError("specification schema_version mismatch")
    if spec.get("profile_document_hash") != _canonical_doc_hash(profile_doc):
        raise ValueError(
            "profile_document_hash does not match the canonical CLOSED candidate profile"
        )
    if spec.get("origin_contract") != profile_doc.get("origin_contract"):
        raise ValueError("origin_contract does not match the bound candidate profile")
    if spec.get("rig_contract_id") != profile_doc.get("rig_contract_id"):
        raise ValueError("rig_contract_id does not match the bound candidate profile")
    if spec.get("visual_mesh_name") != profile_doc.get("visual_mesh_name"):
        raise ValueError("visual_mesh_name does not match the bound candidate profile")
    collider = spec.get("collider")
    if not isinstance(collider, dict):
        raise ValueError("specification collider is required")
    _reject_unknown_keys(collider, _COLLIDER_KEYS, "specification.collider")
    if collider.get("policy") != profile_doc.get("collider_policy"):
        raise ValueError("collider policy does not match the bound candidate profile")
    source_kind = spec.get("source_kind")
    accepted = profile_doc.get("accepted_source_kinds")
    if not isinstance(accepted, list) or source_kind not in accepted:
        raise ValueError(f"source_kind {source_kind!r} is not accepted")
    category = spec.get("category")
    categories = profile_doc.get("categories")
    if not isinstance(categories, list) or category not in categories:
        raise ValueError(f"category {category} is not supported by profile")
    if spec.get("profile") != profile_doc.get("profile_id"):
        raise ValueError("specification profile binding mismatch")
    if spec.get("profile_version") != profile_doc.get("version"):
        raise ValueError("specification profile binding mismatch")
    dims = spec.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("specification dimensions are required")
    _reject_unknown_keys(dims, _DIMENSIONS_KEYS, "specification.dimensions")
    dim_rules = profile_doc.get("dimension_rules")
    if not isinstance(dim_rules, dict):
        raise ValueError("profile dimension_rules missing")
    _reject_unknown_keys(dim_rules, _DIMENSION_RULES_KEYS, "profile.dimension_rules")
    min_m = _strict_float_finite(dim_rules["min_m"], "profile.dimension_rules.min_m", minimum=0.0)
    max_m = _strict_float_finite(dim_rules["max_m"], "profile.dimension_rules.max_m", minimum=0.0)
    for axis_key in ("width_m", "depth_m", "height_m"):
        axis_value = _strict_float_finite(dims[axis_key], f"specification.dimensions.{axis_key}")
        if not min_m <= axis_value <= max_m:
            raise ValueError(f"{axis_key} {axis_value} is outside profile dimension rules")
    if spec.get("target_engine") != "godot":
        raise ValueError("specification target_engine must be godot")
    geom = spec.get("geometry_budget")
    mat = spec.get("material_budget")
    tex = spec.get("texture_budget")
    if not isinstance(geom, dict) or not isinstance(mat, dict) or not isinstance(tex, dict):
        raise ValueError("specification budgets are required")
    _reject_unknown_keys(geom, _GEOMETRY_BUDGET_KEYS, "specification.geometry_budget")
    _reject_unknown_keys(mat, _MATERIAL_BUDGET_KEYS, "specification.material_budget")
    _reject_unknown_keys(tex, _TEXTURE_BUDGET_KEYS, "specification.texture_budget")
    processing = profile_doc.get("processing")
    if not isinstance(processing, dict):
        raise ValueError("profile processing missing")
    _reject_unknown_keys(processing, _PROCESSING_POLICY_KEYS, "profile.processing")
    if _strict_nonneg_int(geom["max_triangles_lod0"], "geometry_budget.max_triangles_lod0") > (
        _strict_nonneg_int(processing["max_triangles_lod0"], "processing.max_triangles_lod0")
    ):
        raise ValueError("LOD0 triangle budget exceeds profile maximum")
    if _strict_nonneg_int(mat["max_materials"], "material_budget.max_materials") > (
        _strict_nonneg_int(processing["max_materials"], "processing.max_materials")
    ):
        raise ValueError("material budget exceeds profile maximum")
    if _strict_nonneg_int(tex["max_dimension"], "texture_budget.max_dimension") > (
        _strict_nonneg_int(processing["max_texture_dimension"], "processing.max_texture_dimension")
    ):
        raise ValueError("texture budget exceeds profile maximum")
    orientation = spec.get("orientation")
    if isinstance(orientation, dict):
        _reject_unknown_keys(orientation, _ORIENTATION_KEYS, "specification.orientation")
    style = spec.get("style_constraints")
    if isinstance(style, dict):
        _reject_unknown_keys(style, _STYLE_CONSTRAINTS_KEYS, "specification.style_constraints")
    lod_policy = spec.get("lod_policy")
    allowed_lod = processing.get("allowed_lod_policies")
    if not isinstance(allowed_lod, list) or lod_policy not in allowed_lod:
        raise ValueError("lod_policy is not allowed by candidate profile")


def _validate_profile_document_closed(profile_doc: dict[str, Any]) -> None:
    _reject_unknown_keys(profile_doc, _PROFILE_DOC_KEYS, "candidate_profile_document")
    dim_rules = profile_doc.get("dimension_rules")
    if isinstance(dim_rules, dict):
        _reject_unknown_keys(dim_rules, _DIMENSION_RULES_KEYS, "profile.dimension_rules")
    processing = profile_doc.get("processing")
    if isinstance(processing, dict):
        _reject_unknown_keys(processing, _PROCESSING_POLICY_KEYS, "profile.processing")
    runtime = profile_doc.get("runtime")
    if isinstance(runtime, dict):
        _reject_unknown_keys(runtime, _RUNTIME_POLICY_KEYS, "profile.runtime")


def _validate_snapshot_closed(snapshot: dict[str, Any]) -> None:
    if "snapshot_fingerprint" in snapshot:
        raise ValueError("snapshot payload must not embed snapshot_fingerprint")
    _reject_unknown_keys(snapshot, _SNAPSHOT_KEYS, "snapshot")
    bindings = snapshot.get("pre_review_artifact_bindings")
    if not isinstance(bindings, list):
        raise ValueError("snapshot pre_review_artifact_bindings missing")
    for row in bindings:
        if not isinstance(row, dict):
            raise ValueError("snapshot binding must be object")
        _reject_unknown_keys(row, _SNAPSHOT_BINDING_KEYS, "snapshot binding")
    for key in (
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
    ):
        block = snapshot.get(key)
        if not isinstance(block, dict):
            raise ValueError(f"snapshot {key} missing")
        _reject_unknown_keys(block, _EXECUTION_BLOCK_KEYS, f"snapshot.{key}")


def _validate_review_task_closed(review: dict[str, Any]) -> None:
    _reject_unknown_keys(review, _REVIEW_TASK_KEYS, "approval_scope.review_task")
    if review.get("task_type") != "v08_candidate_test_only_review":
        raise ValueError("review_task task_type invalid")
    if review.get("cost_class") != "LOCAL":
        raise ValueError("review_task cost_class invalid")
    parameters = review.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("approval_scope review_task parameters missing")
    _reject_unknown_keys(parameters, _REVIEW_TASK_PARAM_KEYS, "review_task.parameters")
    if parameters.get("graph_version") != CANDIDATE_GRAPH_VERSION:
        raise ValueError("review_task graph_version mismatch")


def _validate_review_task_parameters(
    parameters: dict[str, Any],
    *,
    snapshot: dict[str, Any],
    spec: dict[str, Any],
    profile_doc: dict[str, Any],
    retained_spec_raw: bytes,
    workflow_id: str,
    source_sha: str,
) -> None:
    if parameters.get("graph_version") != CANDIDATE_GRAPH_VERSION:
        raise ValueError("review_task graph_version mismatch")
    if parameters.get("source_kind") != "local_verified_rig":
        raise ValueError("review_task source_kind must be local_verified_rig")
    asset_id = _strict_nonempty_string(
        parameters.get("asset_id"), "review_task.parameters.asset_id"
    )
    revision_number = _strict_positive_int(
        parameters.get("revision_number"), "review_task.parameters.revision_number"
    )
    spec_hash = _strict_sha256_field(
        parameters.get("specification_hash"), "review_task.parameters.specification_hash"
    )
    profile_hash = _strict_sha256_field(
        parameters.get("profile_document_hash"), "review_task.parameters.profile_document_hash"
    )
    source_glb_hash = _strict_sha256_field(
        parameters.get("source_glb_hash"), "review_task.parameters.source_glb_hash"
    )
    profile_id = _strict_nonempty_string(
        parameters.get("profile_id"), "review_task.parameters.profile_id"
    )
    profile_version = _strict_positive_int(
        parameters.get("profile_version"), "review_task.parameters.profile_version"
    )
    paid = parameters.get("paid_provider_invocations")
    if _strict_nonneg_int(paid, "review_task.parameters.paid_provider_invocations") != 0:
        raise ValueError("review_task paid_provider_invocations must be 0")
    source_glb = _strict_nonempty_string(
        parameters.get("source_glb"), "review_task.parameters.source_glb"
    )
    asset_dir = _strict_nonempty_string(
        parameters.get("asset_dir"), "review_task.parameters.asset_dir"
    )
    embedded = parameters.get("specification")
    if not isinstance(embedded, dict):
        raise ValueError("review_task specification must be an object")
    _check_specification_against_profile(embedded, profile_doc)
    if _canonical_doc_hash(embedded) != spec_hash:
        raise ValueError("review_task specification_hash does not match embedded specification")
    if _sha256(_canonical_json_bytes(embedded)) != _sha256(_canonical_json_bytes(spec)):
        raise ValueError("review_task embedded specification does not match retained specification")
    if _sha256(retained_spec_raw) != snapshot.get("retained_specification_sha256"):
        raise ValueError("retained specification hash mismatch in snapshot")
    if revision_number != snapshot.get("revision_number"):
        raise ValueError("review_task revision_number does not match snapshot")
    if spec_hash != snapshot.get("specification_hash"):
        raise ValueError("review_task specification_hash does not match snapshot")
    if profile_hash != snapshot.get("profile_document_hash"):
        raise ValueError("review_task profile_document_hash does not match snapshot")
    if asset_id != snapshot.get("asset_id") or asset_id != spec.get("asset_id"):
        raise ValueError("review_task asset_id does not match snapshot/specification")
    if source_glb_hash != snapshot.get("source_glb_hash") or source_glb_hash != source_sha:
        raise ValueError("review_task source_glb_hash mismatch")
    if source_glb != snapshot.get("authoritative_source_glb_relative_path"):
        raise ValueError("review_task source_glb does not match snapshot authoritative path")
    if profile_id != snapshot.get("profile_id") or profile_id != profile_doc.get("profile_id"):
        raise ValueError("review_task profile_id mismatch")
    if profile_version != snapshot.get("profile_version") or profile_version != profile_doc.get(
        "version"
    ):
        raise ValueError("review_task profile_version mismatch")
    if profile_hash != _canonical_doc_hash(profile_doc):
        raise ValueError("review_task profile_document_hash does not match profile document")
    if spec_hash != _canonical_doc_hash(spec):
        raise ValueError("review_task specification_hash does not match retained specification")
    expected_asset_dir = f".gamefactory/assets/{asset_id}/r{revision_number:03d}"
    if asset_dir != expected_asset_dir:
        raise ValueError("review_task asset_dir does not match production layout")
    if workflow_id != snapshot.get("workflow_id"):
        raise ValueError("snapshot workflow_id mismatch during review parameter binding")


def _validate_runtime_request_closed(request: dict[str, Any]) -> None:
    _reject_unknown_keys(request, _RUNTIME_REQUEST_KEYS, "runtime_request")
    framing = request.get("framing")
    viewport = request.get("viewport")
    if not isinstance(framing, dict) or not isinstance(viewport, dict):
        raise ValueError("runtime request framing/viewport required")
    _reject_unknown_keys(framing, _FRAMING_CONTRACT_KEYS, "runtime_request.framing")
    _reject_unknown_keys(viewport, _VIEWPORT_KEYS, "runtime_request.viewport")
    capsule = request.get("capsule")
    if isinstance(capsule, dict):
        _reject_unknown_keys(capsule, _CAPSULE_KEYS, "runtime_request.capsule")
    rest = request.get("rest_aabb")
    if isinstance(rest, dict):
        _reject_unknown_keys(rest, _REST_BOUNDS_KEYS, "runtime_request.rest_aabb")
    nine = request.get("nine_view_set")
    if not isinstance(nine, list) or len(nine) != 9 or len(set(nine)) != 9:
        raise ValueError("nine_view_set must contain exactly nine unique views")
    if set(nine) != _NINE_VIEWS:
        raise ValueError("nine_view_set must match frozen contract views")
    if (
        _strict_positive_int(viewport.get("width"), "runtime_request.viewport.width")
        != _CAPTURE_WIDTH
        or _strict_positive_int(viewport.get("height"), "runtime_request.viewport.height")
        != _CAPTURE_HEIGHT
    ):
        raise ValueError("runtime request viewport must be 1280x720")
    staging_glb = request.get("glb")
    staging_capture = request.get("capture_dir")
    staging_observation = request.get("observation_path")
    staging_fields = (staging_glb, staging_capture, staging_observation)
    if any(value is not None for value in staging_fields):
        if any(value is None for value in staging_fields):
            raise ValueError("runtime request staging fields must be complete when any are present")
        if staging_glb != _STAGED_RUNTIME_GLB:
            raise ValueError("runtime request glb must be res://Main.tscn")
        _strict_nonempty_string(staging_capture, "runtime_request.capture_dir")
        _strict_nonempty_string(staging_observation, "runtime_request.observation_path")
    if request.get("renderer_profile") != "gl_compatibility":
        raise ValueError("runtime request renderer_profile must be gl_compatibility")
    if request.get("candidate_state") != "CLOSED":
        raise ValueError("runtime request candidate_state must be CLOSED")
    if request.get("public_status") != "UNSUPPORTED":
        raise ValueError("runtime request public_status must be UNSUPPORTED")
    if request.get("production_eligible") is not False:
        raise ValueError("runtime request production_eligible must be false")
    if request.get("visual_mesh_name") != _CANDIDATE_VISUAL_MESH_NAME:
        raise ValueError("runtime request visual_mesh_name invalid")


def _snapshot_fingerprint_from_payload(payload: dict[str, Any]) -> str:
    if "snapshot_fingerprint" in payload:
        raise ValueError("snapshot payload must not embed snapshot_fingerprint")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return _sha256(raw.encode("utf-8"))


def _execution_block_for_id(snapshot: dict[str, Any], execution_id: str) -> dict[str, Any] | None:
    for key in (
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
    ):
        block = snapshot.get(key)
        if isinstance(block, dict) and block.get("id") == execution_id:
            return block
    return None


def _strict_positive_int(value: Any, field: str) -> int:
    parsed = _strict_runtime_int(value, field, minimum=1)
    if parsed > _MAX_SAFE_INT:
        raise ValueError(f"{field} exceeds safe integer range")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_doc_hash(doc: dict[str, Any]) -> str:
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return _sha256(payload.encode("utf-8"))


def _strict_runtime_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _strict_nonneg_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


def _strict_float_finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return result


def _strict_vec3_finite(value: Any, field: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{field} must be a length-3 array of finite numbers")
    return (
        _strict_float_finite(value[0], f"{field}[0]"),
        _strict_float_finite(value[1], f"{field}[1]"),
        _strict_float_finite(value[2], f"{field}[2]"),
    )


def _strict_bool_literal(value: Any, field: str) -> bool:
    if value is not True and value is not False:
        raise ValueError(f"{field} must be a boolean literal")
    return bool(value)


def _strict_sha256_field(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return value


def _strict_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _canonical_json_bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _validate_json_value(value: Any, *, path: str, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON depth limit exceeded at {path}")
    if isinstance(value, dict):
        if len(value) > _MAX_JSON_OBJECT_KEYS:
            raise ValueError(f"JSON object too large at {path}")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key must be string at {path}")
            if len(key) > _MAX_JSON_STRING_LEN:
                raise ValueError(f"JSON object key exceeds string length limit at {path}")
            _validate_json_value(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ARRAY_LEN:
            raise ValueError(f"JSON array too large at {path}")
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING_LEN:
            raise ValueError(f"JSON string exceeds length limit at {path}")
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    raise ValueError(f"unsupported JSON value at {path}")


def _reviewed_text_sha256(raw: bytes, suffix: str) -> tuple[str, str]:
    raw_digest = _sha256(raw)
    if suffix.casefold() in {".py", ".gd"}:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return _sha256(text.encode("utf-8")), raw_digest
    return raw_digest, raw_digest


def _trusted_rig_verifier_digest_allowed(raw: bytes) -> bool:
    return _sha256(raw) in TRUSTED_RIG_VERIFIER_SHA256_FIXED


def _counterpart_rig_verifier_path(own_script: Path) -> Path | None:
    resolved = own_script.resolve()
    parts = resolved.parts
    if len(parts) >= 5 and parts[-1] == "verify_candidate_bundle.py":
        if parts[-5:-1] == ("src", "gamefactory", "resources", "scripts"):
            return resolved.parents[4] / "scripts" / "verify_rig_bundle.py"
        if parts[-2] == "scripts":
            return (
                resolved.parents[1]
                / "src"
                / "gamefactory"
                / "resources"
                / "scripts"
                / "verify_rig_bundle.py"
            )
    return None


def _read_trusted_rig_verifier_bytes(path: Path, *, field: str) -> bytes:
    if not path.is_file():
        raise ValueError(f"trusted sibling {field} is missing")
    if _path_is_link(path):
        raise ValueError(f"trusted sibling {field} must not be a link")
    raw = path.read_bytes()
    if not _trusted_rig_verifier_digest_allowed(raw):
        raise ValueError(f"trusted sibling {field} digest mismatch")
    return raw


def _load_trusted_rig_verifier() -> Any:
    own_script = Path(__file__).resolve()
    sibling = own_script.with_name("verify_rig_bundle.py")
    raw = _read_trusted_rig_verifier_bytes(sibling, field="verify_rig_bundle.py")
    counterpart = _counterpart_rig_verifier_path(own_script)
    if counterpart is not None and counterpart.is_file():
        if _path_is_link(counterpart):
            raise ValueError("trusted counterpart verify_rig_bundle.py must not be a link")
        counterpart_raw = counterpart.read_bytes()
        if not _trusted_rig_verifier_digest_allowed(counterpart_raw):
            raise ValueError("trusted counterpart verify_rig_bundle.py digest mismatch")
        if counterpart_raw != raw:
            raise ValueError("trusted verify_rig_bundle.py checkout parity mismatch")
    spec = importlib.util.spec_from_file_location("verify_rig_bundle_trusted", sibling)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load trusted rig verifier module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_rig_bundle_trusted"] = module
    spec.loader.exec_module(module)
    return module


def _win_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        lstat = path.lstat()
    except OSError as exc:
        if path.exists() or path.is_symlink():
            raise ValueError(f"indeterminate junction check for {path}") from exc
        return False
    if stat.S_ISLNK(lstat.st_mode):
        return False
    file_attributes = getattr(lstat, "st_file_attributes", None)
    if file_attributes is not None:
        return bool(file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    try:
        import ctypes

        attrs = cast(Any, ctypes).windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            if path.exists() or path.is_symlink():
                raise ValueError(f"indeterminate junction check for {path}")
            return False
        return bool(attrs & 0x400)
    except (AttributeError, OSError) as exc:
        if path.exists() or path.is_symlink():
            raise ValueError(f"indeterminate junction check for {path}") from exc
        return False


def _path_is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError as exc:
        raise ValueError(f"indeterminate link check for {path}") from exc
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError as exc:
            raise ValueError(f"indeterminate link check for {path}") from exc
    return _win_reparse_point(path)


def _linked(path: Path) -> bool:
    return _path_is_link(path)


def _bundle_path_crosses_link(path: Path) -> bool:
    current = path
    while True:
        if _path_is_link(current):
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe manifest path: {value}")
    return pure.as_posix()


def _resolve_under_root(root: Path, rel: str) -> Path:
    rel_norm = _path(rel)
    current = root
    for part in PurePosixPath(rel_norm).parts:
        current = current / part
        if _path_is_link(current):
            raise ValueError(f"bundle path crosses a link: {rel}")
    target = (root / rel_norm).resolve(strict=False)
    root_resolved = root.resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError(f"path escapes bundle root: {rel}")
    if _path_is_link(target):
        raise ValueError(f"bundle path crosses a link: {rel}")
    return target


def _read_bounded_bytes(path: Path, declared_size: int) -> bytes:
    if _path_is_link(path):
        raise ValueError(f"bundle path crosses a link: {path.name}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat {path.name}") from exc
    if size != declared_size:
        raise ValueError(f"file size mismatch for {path.name}")
    if size > _MAX_FILE_BYTES:
        raise ValueError("file exceeds size limit")
    with path.open("rb") as handle:
        raw = handle.read(declared_size + 1)
    if len(raw) != declared_size:
        raise ValueError(f"file size mismatch for {path.name}")
    return raw


def _aggregate_bundle_byte_scan(root: Path) -> None:
    total_size = 0
    file_count = 0
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        if _path_is_link(base):
            raise ValueError("symlink or junction in bundle inventory")
        dirnames[:] = [name for name in dirnames if not _path_is_link(base / name)]
        for name in list(dirnames) + filenames:
            child = base / name
            if _path_is_link(child):
                raise ValueError("symlink or junction in bundle inventory")
        for name in filenames:
            child = base / name
            if not child.is_file():
                continue
            try:
                size = child.stat().st_size
            except OSError as exc:
                rel = child.relative_to(root).as_posix()
                raise ValueError(f"cannot stat bundle file {rel}") from exc
            if size > _MAX_FILE_BYTES:
                raise ValueError("file exceeds size limit")
            total_size += size
            file_count += 1
            if total_size > _MAX_BUNDLE_BYTES:
                raise ValueError("bundle aggregate size exceeds limit")
            if file_count > _MAX_BUNDLE_FILE_COUNT:
                raise ValueError("bundle file count exceeds limit")


def _read_bounded_json_bytes(path: Path, name: str, *, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    if _path_is_link(path):
        raise ValueError(f"{name} crosses a link")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat {name}") from exc
    if size > max_bytes:
        raise ValueError(f"{name} exceeds JSON size limit")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{name} exceeds JSON size limit")
    return raw


def _parse_strict_json(raw: bytes, name: str) -> dict[str, Any]:
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"{name} exceeds JSON size limit")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid numeric token {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    _validate_json_value(value, path=name, depth=0)
    return value


def _parse_strict_json_file(path: Path, name: str) -> dict[str, Any]:
    return _parse_strict_json(_read_bounded_json_bytes(path, name), name)


def _object_bytes(raw: bytes, name: str) -> dict[str, Any]:
    return _parse_strict_json(raw, name)


def _png_paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_unfilter_row(filter_type: int, row: bytes, previous: bytes, bpp: int) -> bytes:
    if filter_type == 0:
        return row
    if filter_type > 4:
        raise ValueError("runtime PNG uses an invalid scanline filter")
    out = bytearray(len(row))
    for index, raw_byte in enumerate(row):
        left = out[index - bpp] if index >= bpp else 0
        up = previous[index] if previous else 0
        up_left = previous[index - bpp] if previous and index >= bpp else 0
        if filter_type == 1:
            recon = (raw_byte + left) & 0xFF
        elif filter_type == 2:
            recon = (raw_byte + up) & 0xFF
        elif filter_type == 3:
            recon = (raw_byte + ((left + up) // 2)) & 0xFF
        else:
            recon = (raw_byte + _png_paeth(left, up, up_left)) & 0xFF
        out[index] = recon
    return bytes(out)


def _decode_runtime_capture_png(raw: bytes) -> tuple[int, int, bytes, int]:
    """Decode non-interlaced RGB/RGBA8 capture PNGs with bounded zlib and filter reversal."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("runtime capture is not a PNG")
    offset = 8
    width: int | None = None
    height: int | None = None
    depth: int | None = None
    color: int | None = None
    compressed = bytearray()
    saw_ihdr = False
    saw_idat = False
    ended = False
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError("runtime PNG chunk is truncated")
        chunk = raw[offset + 8 : offset + 8 + length]
        crc = struct.unpack_from(">I", raw, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != crc:
            raise ValueError("runtime PNG chunk CRC mismatch")
        if kind == b"IHDR":
            if saw_ihdr or length != 13:
                raise ValueError("runtime PNG has invalid IHDR")
            saw_ihdr = True
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if width != _CAPTURE_WIDTH or height != _CAPTURE_HEIGHT:
                raise ValueError("runtime PNG dimensions must be 1280x720")
            if depth != 8 or color not in {2, 6}:
                raise ValueError("runtime PNG must be RGB8 or RGBA8")
            if compression or filtering or interlace:
                raise ValueError("runtime PNG dimensions or encoding are unsupported")
        elif kind == b"PLTE" or kind == b"tRNS":
            raise ValueError("runtime PNG palette or transparency chunk is unsupported")
        elif kind == b"IDAT":
            if not saw_ihdr:
                raise ValueError("runtime PNG IDAT appears before IHDR")
            saw_idat = True
            compressed.extend(chunk)
        elif kind == b"IEND":
            if length or end != len(raw):
                raise ValueError("runtime PNG has invalid trailing data")
            ended = True
            break
        elif kind[0:1].isupper():
            raise ValueError(f"runtime PNG critical chunk is unsupported: {kind!r}")
        offset = end
    channels = {2: 3, 6: 4}.get(color if color is not None else -1, 0)
    if not ended or not saw_ihdr or not saw_idat or width is None or height is None or not channels:
        raise ValueError("runtime PNG is incomplete or unsupported")
    row_bytes = width * channels
    filtered_size = height * (row_bytes + 1)
    if filtered_size > _MAX_CAPTURE_DECODED_BYTES:
        raise ValueError("runtime PNG exceeds decoded size limit")
    try:
        inflater = zlib.decompressobj()
        filtered = inflater.decompress(bytes(compressed), filtered_size + 1)
        if len(filtered) > filtered_size:
            raise ValueError("runtime PNG decoded image exceeds expected size")
    except zlib.error as exc:
        raise ValueError("runtime PNG image data cannot be decoded") from exc
    if (
        len(filtered) != filtered_size
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise ValueError("runtime PNG decoded image size is invalid")
    bpp = channels
    pixels = bytearray()
    previous = b""
    for row in range(height):
        row_start = row * (row_bytes + 1)
        filter_type = filtered[row_start]
        if filter_type > 4:
            raise ValueError("runtime PNG uses an invalid scanline filter")
        row_data = filtered[row_start + 1 : row_start + 1 + row_bytes]
        if len(row_data) != row_bytes:
            raise ValueError("runtime PNG scanline length is invalid")
        pixels.extend(_png_unfilter_row(filter_type, row_data, previous, bpp))
        previous = bytes(pixels[-row_bytes:])
    return width, height, bytes(pixels), channels


def _png_nonblank(pixels: bytes, width: int, height: int, channels: int) -> None:
    if channels not in {3, 4}:
        raise ValueError("runtime PNG channel layout is invalid")
    seen_colors: set[tuple[int, int, int]] = set()
    visible_pixels = 0
    for row in range(height):
        row_start = row * width * channels
        for column in range(width):
            index = row_start + column * channels
            if channels == 4 and pixels[index + 3] == 0:
                continue
            color = (pixels[index], pixels[index + 1], pixels[index + 2])
            visible_pixels += 1
            seen_colors.add(color)
            if len(seen_colors) > 1:
                return
    if channels == 4 and visible_pixels == 0:
        raise ValueError("capture appears fully transparent")
    if visible_pixels == 0 or len(seen_colors) < 2:
        raise ValueError("capture appears blank or uniform")


@dataclass(frozen=True)
class BoundsAABB:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def size(self) -> tuple[float, float, float]:
        return (self.max_x - self.min_x, self.max_y - self.min_y, self.max_z - self.min_z)

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            (self.min_x + self.max_x) / 2.0,
            (self.min_y + self.max_y) / 2.0,
            (self.min_z + self.max_z) / 2.0,
        )


_VIEW_TABLE: dict[str, tuple[tuple[float, float, float], str]] = {
    "front": ((0.0, 0.0, -1.0), "-Z"),
    "rear": ((0.0, 0.0, 1.0), "+Z"),
    "left": ((-1.0, 0.0, 0.0), "-X"),
    "right": ((1.0, 0.0, 0.0), "+X"),
    "side": ((1.0, 0.0, 0.0), "+X"),
    "three_quarter": ((1.0, 0.65, -1.0), "+X-Z"),
    "three_quarter_front": ((1.0, 0.65, -1.0), "+X-Z"),
    "three_quarter_rear": ((1.0, 0.65, 1.0), "+X+Z"),
    "top": ((0.0, 1.0, 0.0), "+Y"),
}


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-9 or not math.isfinite(length):
        raise ValueError("camera direction is degenerate")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _view_direction(view: str) -> tuple[float, float, float]:
    if view not in _NINE_VIEWS:
        raise ValueError(f"view '{view}' has no camera placement")
    return _normalize(_VIEW_TABLE[view][0])


def _view_up(view: str) -> tuple[float, float, float]:
    if view == "top":
        return (0.0, 0.0, -1.0)
    return (0.0, 1.0, 0.0)


def _view_axis_label(view: str) -> str:
    return _VIEW_TABLE[view][1]


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _support_extent(
    axis: tuple[float, float, float], half_extents: tuple[float, float, float]
) -> float:
    return (
        abs(axis[0]) * half_extents[0]
        + abs(axis[1]) * half_extents[1]
        + abs(axis[2]) * half_extents[2]
    )


def _camera_basis(
    view: str,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    d = _view_direction(view)
    up = _view_up(view)
    f = (-d[0], -d[1], -d[2])
    r = _normalize(_cross(f, up))
    u = _cross(r, f)
    return d, up, f, r, u


@dataclass(frozen=True)
class FramingGeometry:
    distance: float
    vertical_fraction: float
    horizontal_fraction: float


def _framing_geometry(
    bounds: BoundsAABB,
    view: str,
    fov_degrees: float,
    target_screen_fraction: float,
    viewport: tuple[int, int],
) -> FramingGeometry:
    size = bounds.size
    if min(size) <= 1e-6:
        raise ValueError("asset bounds are empty")
    hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
    h = (hx, hy, hz)
    d, _up, f, r, u = _camera_basis(view)
    e_r = _support_extent(r, h)
    e_u = _support_extent(u, h)
    e_f = _support_extent(f, h)
    half_fov = math.radians(fov_degrees * 0.5)
    t = math.tan(half_fov)
    aspect = viewport[0] / viewport[1]
    d_c = e_f + max(e_u / (t * target_screen_fraction), e_r / (t * target_screen_fraction * aspect))
    corners = [
        (sx * hx, sy * hy, sz * hz)
        for sx in (-1.0, 1.0)
        for sy in (-1.0, 1.0)
        for sz in (-1.0, 1.0)
    ]

    def _screen_fractions(dist: float) -> tuple[float, float]:
        min_x = float("inf")
        max_x = float("-inf")
        min_y = float("inf")
        max_y = float("-inf")
        for p in corners:
            z = dist + _dot(f, p)
            y = _dot(u, p) / (z * t)
            x = _dot(r, p) / (z * t * aspect)
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
        return (max_y - min_y) * 0.5, (max_x - min_x) * 0.5

    low = e_f + min(1e-4, (d_c - e_f) * 0.5)
    high = d_c
    for _ in range(60):
        mid = (low + high) * 0.5
        v_mid, h_mid = _screen_fractions(mid)
        if max(v_mid, h_mid) > target_screen_fraction:
            low = mid
        else:
            high = mid
    distance = high
    vert_frac, horiz_frac = _screen_fractions(distance)
    return FramingGeometry(
        distance=distance, vertical_fraction=vert_frac, horizontal_fraction=horiz_frac
    )


def _framing_metrics_from_projected_rect(
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    viewport_width: float,
    viewport_height: float,
    margin_fraction: float,
) -> dict[str, Any]:
    margin_x = viewport_width * margin_fraction
    margin_y = viewport_height * margin_fraction
    max_x = x + width
    max_y = y + height
    inside = (
        x >= margin_x
        and y >= margin_y
        and max_x <= viewport_width - margin_x
        and max_y <= viewport_height - margin_y
    )
    height_ratio = height / viewport_height
    fill_ratio = max(height_ratio, width / viewport_width)
    center_x = x + width * 0.5
    center_offset = abs(center_x - viewport_width * 0.5) / viewport_width
    horizontally_centered = center_offset <= 0.08
    return {
        "height_ratio": height_ratio,
        "fill_ratio": fill_ratio,
        "center_offset": center_offset,
        "inside_margin": inside,
        "horizontally_centered": horizontally_centered,
    }


def _candidate_runtime_rest_aabb_canonical_sha256(rest_aabb: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"max": rest_aabb["max"], "min": rest_aabb["min"]},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256(canonical.encode("utf-8"))


def _candidate_runtime_bound_payload_canonical(payload: dict[str, Any]) -> str:
    filtered = {k: v for k, v in payload.items() if k not in _RUNTIME_REQUEST_TRANSIENT_KEYS}
    out: dict[str, Any] = {}
    for key in sorted(filtered):
        value = filtered[key]
        if key in _RUNTIME_REQUEST_STRICT_INT_FIELDS:
            out[key] = _strict_runtime_int(value, key)
        elif key == "viewport" and isinstance(value, dict):
            out[key] = {
                "height": _strict_runtime_int(value.get("height"), "viewport.height"),
                "width": _strict_runtime_int(value.get("width"), "viewport.width"),
            }
        else:
            out[key] = value
    parts: list[str] = []
    for key in sorted(out):
        value_json = json.dumps(out[key], sort_keys=True, separators=(",", ":"), allow_nan=False)
        parts.append(f"{json.dumps(key)}:{value_json}")
    return "{" + ",".join(parts) + "}"


def _candidate_runtime_request_digest(request: dict[str, Any]) -> str:
    return _sha256(_candidate_runtime_bound_payload_canonical(request).encode("utf-8"))


def _visual_aabb_from_decoded(
    decoded: Any,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    positions = decoded.primitive.positions
    if not positions:
        raise ValueError("visual mesh has no positions")
    mins = (
        min(p[0] for p in positions),
        min(p[1] for p in positions),
        min(p[2] for p in positions),
    )
    maxs = (
        max(p[0] for p in positions),
        max(p[1] for p in positions),
        max(p[2] for p in positions),
    )
    return mins, maxs


def _capsule_center_from_aabb(
    mins: tuple[float, float, float], maxs: tuple[float, float, float]
) -> tuple[float, float, float]:
    return ((mins[0] + maxs[0]) / 2.0, (mins[1] + maxs[1]) / 2.0, (mins[2] + maxs[2]) / 2.0)


def _compute_operation_hash(task_id: str, approval_type: str, inputs: dict[str, Any]) -> str:
    normalized = json.dumps(
        {"task_id": task_id, "approval_type": approval_type, "inputs": inputs},
        sort_keys=True,
        allow_nan=False,
    )
    return _sha256(normalized.encode("utf-8"))


def _scan_engine_diag(text: str) -> list[str]:
    return [pattern.pattern for pattern in _ENGINE_ERROR_PATTERNS if pattern.search(text)]


def _one(roles: dict[str, list[dict[str, Any]]], role: str) -> dict[str, Any]:
    return roles[role][0]


def _inventory(
    root: Path, manifest: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bytes]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be an array")
    roles: dict[str, list[dict[str, Any]]] = {}
    listed: set[str] = set()
    total_size = 0
    file_raw: dict[str, bytes] = {}
    capture_angles: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("manifest file entry must be an object")
        unknown = set(item.keys()) - _MANIFEST_FILE_ENTRY_KEYS
        if unknown:
            raise ValueError("manifest file entry has unknown keys")
        role = item.get("role")
        digest, size = item.get("sha256"), item.get("size")
        if not isinstance(role, str):
            raise ValueError("manifest role must be a string")
        if role not in _ALLOWED_MANIFEST_ROLES:
            raise ValueError(f"manifest role is not allowed: {role}")
        rel = _path(item.get("path"))
        angle_field = item.get("angle")
        if role != _CAPTURE_ROLE and angle_field is not None:
            raise ValueError(f"angle is only valid for runtime_capture: {rel}")
        if rel in listed:
            raise ValueError(f"duplicate bundle entry: {rel}")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or type(size) is not int:
            raise ValueError(f"invalid hash or size: {rel}")
        if size < 0:
            raise ValueError(f"invalid declared size: {rel}")
        total_size += size
        if size > _MAX_FILE_BYTES or total_size > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle exceeds size limits")
        target = _resolve_under_root(root, rel)
        if not target.is_file():
            raise ValueError(f"missing bundle file: {rel}")
        raw = _read_bounded_bytes(target, size)
        if _sha256(raw) != digest:
            raise ValueError(f"hash mismatch: {rel}")
        listed.add(rel)
        file_raw[rel] = raw
        roles.setdefault(role, []).append(item)
        if role == _CAPTURE_ROLE:
            angle = item.get("angle")
            if not isinstance(angle, str) or angle not in _NINE_VIEWS:
                raise ValueError(f"runtime_capture missing or invalid angle: {rel}")
            if angle in capture_angles:
                raise ValueError(f"duplicate runtime_capture angle: {angle}")
            capture_angles[angle] = rel
    for role in _SINGLE_ROLES:
        if len(roles.get(role, [])) != 1:
            raise ValueError(f"bundle requires exactly one {role}")
    if len(roles.get(_CAPTURE_ROLE, [])) != 9:
        raise ValueError("bundle requires exactly nine runtime_capture roles")
    if set(capture_angles) != _NINE_VIEWS:
        raise ValueError("runtime_capture angles must cover every placed view exactly once")
    nested_prefix = _manifest_nested_bundle_path(manifest)

    def _under_nested(rel: str) -> bool:
        return rel == nested_prefix or rel.startswith(f"{nested_prefix}/")

    on_disk: set[str] = set()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        if _path_is_link(base):
            raise ValueError("symlink or junction in bundle inventory")
        for name in list(dirnames) + filenames:
            child = base / name
            if _path_is_link(child):
                raise ValueError("symlink or junction in bundle inventory")
        for name in filenames:
            rel_posix = (base / name).relative_to(root).as_posix()
            if _under_nested(rel_posix):
                continue
            on_disk.add(rel_posix)
    if on_disk - _EXCLUDED != listed:
        raise ValueError("unlisted or missing manifest files")
    return roles, file_raw


def _recompute_static_status(
    rig_mod: Any,
    *,
    glb_path: Path,
    spec: dict[str, Any],
    profile_doc: dict[str, Any],
    contract_data: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    decoded = rig_mod._decode_internal_skinned_glb(glb_path)
    skin_findings = rig_mod.run_internal_skin_checks(decoded, contract_data)
    finding_dicts = rig_mod._finding_dicts(skin_findings)
    tol = float(profile_doc["processing"]["dimension_tolerance_m"])
    mins, maxs = _visual_aabb_from_decoded(decoded)
    width, height, depth = maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2]
    capsule = spec.get("collider", {}).get("capsule")
    if not isinstance(capsule, dict):
        raise ValueError("specification collider.capsule is required")
    radius_m = float(capsule["radius_m"])
    height_m = float(capsule["height_m"])
    problems: list[str] = []
    if radius_m < 0.10:
        problems.append(f"radius_m {radius_m} < 0.10")
    if height_m <= 2 * radius_m:
        problems.append("height_m <= 2 x radius_m")
    col_nodes = [
        str(n.get("name"))
        for n in decoded.document.get("nodes", [])
        if isinstance(n, dict) and str(n.get("name", "")).startswith("COL_")
    ]
    if col_nodes:
        problems.append(f"GLB carries collider nodes {col_nodes}")
    fit_ok = (
        height_m <= height + tol
        and 2 * radius_m <= max(width, depth) + tol
        and height_m <= float(spec["dimensions"]["height_m"]) + tol
        and 2 * radius_m
        <= max(float(spec["dimensions"]["width_m"]), float(spec["dimensions"]["depth_m"])) + tol
    )
    extra = []
    if problems:
        extra.append(
            {
                "rule_id": "collider.capsule.shape",
                "severity": "FAIL",
                "expected": "radius_m >= 0.10, height_m > 2 x radius_m",
                "actual": "; ".join(problems),
                "message": "Capsule shape invalid",
            }
        )
    if not fit_ok:
        extra.append(
            {
                "rule_id": "collider.capsule.fit",
                "severity": "FAIL",
                "expected": "capsule fits measured bounds",
                "actual": "capsule fit failed",
                "message": "Capsule fit invalid",
            }
        )
    all_findings = finding_dicts + extra
    status = rig_mod._recomputed_validation_status(all_findings)
    return status, all_findings


def _validate_static_report_semantics(
    rig_mod: Any,
    *,
    glb_path: Path,
    glb_sha: str,
    spec: dict[str, Any],
    profile_doc: dict[str, Any],
    report: dict[str, Any],
    static_status: str,
    recomputed_findings: list[dict[str, Any]],
) -> None:
    _reject_unknown_keys(report, _STATIC_REPORT_KEYS, "static_validation_report")
    if report.get("status") != static_status:
        raise ValueError("static validation report status does not match recomputed semantics")
    if static_status == "PASS" and report.get("passed") is not True:
        raise ValueError("static validation report passed flag invalid for PASS")
    if static_status != "PASS":
        raise ValueError("static validation FAIL rejected")
    if spec.get("processed_glb_sha256") != glb_sha:
        raise ValueError("specification processed_glb_sha256 does not match bundled GLB")
    if spec.get("visual_mesh_name") != "SM_HumanoidSkin":
        raise ValueError("specification visual_mesh_name is not SM_HumanoidSkin")
    if spec.get("origin_contract") != "humanoid_reference_root":
        raise ValueError("specification origin_contract must be humanoid_reference_root")
    if not isinstance(spec.get("rig_contract_id"), str) or not spec.get("rig_contract_id"):
        raise ValueError("specification rig_contract_id is required")
    profile_id = spec.get("profile")
    profile_version = spec.get("profile_version")
    if profile_doc.get("profile_id") != profile_id:
        raise ValueError("profile document profile_id does not match specification")
    if profile_doc.get("version") != profile_version:
        raise ValueError("profile document version does not match specification")
    if profile_hash := _canonical_doc_hash(profile_doc):
        if profile_hash != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
            raise ValueError("profile document hash does not match reviewed pin")
    dims = spec.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("specification dimensions are required")
    tol = float(profile_doc["processing"]["dimension_tolerance_m"])
    decoded = rig_mod._decode_internal_skinned_glb(glb_path)
    mins, maxs = _visual_aabb_from_decoded(decoded)
    measured = (maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2])
    for axis, key in zip(
        ("width", "height", "depth"), ("width_m", "height_m", "depth_m"), strict=True
    ):
        limit = float(dims[key])
        measured_value = measured[{"width": 0, "height": 1, "depth": 2}[axis]]
        if abs(measured_value - limit) > tol:
            raise ValueError(f"measured {axis} outside specification dimensions")
    if decoded.primitive.node_name != _CANDIDATE_VISUAL_MESH_NAME:
        raise ValueError("GLB visual mesh node name is not SM_HumanoidSkin")
    document = decoded.document
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("GLB nodes are invalid")
    visual_names = [
        str(node.get("name"))
        for node in nodes
        if isinstance(node, dict) and str(node.get("name", "")) == spec.get("visual_mesh_name")
    ]
    if len(visual_names) != 1:
        raise ValueError("GLB must contain exactly one visual mesh node for specification")
    if document.get("textures") or document.get("images"):
        raise ValueError("GLB must not embed images or textures")
    material_count = len(document.get("materials", []))
    mat_budget = int(spec.get("material_budget", {}).get("max_materials", 0))
    if material_count > mat_budget:
        raise ValueError("GLB material count exceeds specification budget")
    mesh_node_index = decoded.primitive.node_index
    mesh_index = document["nodes"][mesh_node_index]["mesh"]
    primitive_info = document["meshes"][mesh_index]["primitives"][0]
    vertex_count = len(decoded.primitive.positions)
    rig_mod.read_index_triangles(document, decoded.binary, primitive_info, vertex_count)
    indices_idx = primitive_info.get("indices")
    if indices_idx is None:
        tri_count = vertex_count // 3
    else:
        accessor = document.get("accessors", [])[int(indices_idx)]
        tri_count = int(accessor.get("count", 0)) // 3
    tri_budget = int(spec.get("geometry_budget", {}).get("max_triangles_lod0", 0))
    if tri_count > tri_budget:
        raise ValueError("GLB triangle count exceeds specification budget")
    bundled_findings = report.get("findings")
    if not isinstance(bundled_findings, list):
        raise ValueError("static validation report findings missing")
    if static_status == "PASS" and any(
        isinstance(item, dict) and item.get("severity") == "FAIL" for item in bundled_findings
    ):
        raise ValueError("static validation report claims PASS with FAIL findings")
    if any(
        isinstance(item, dict) and item.get("severity") == "FAIL" for item in recomputed_findings
    ):
        raise ValueError("static validation recomputation produced FAIL findings")


def _validate_runtime_b(
    *,
    roles: dict[str, list[dict[str, Any]]],
    file_raw: dict[str, bytes],
    root: Path,
    glb_path: Path,
    glb_sha: str,
    spec: dict[str, Any],
    contract: dict[str, Any],
    harness_lf: str,
    harness_raw: str,
) -> tuple[str, str]:
    request = _object_bytes(
        file_raw[_one(roles, "runtime_request")["path"]], "runtime_request.json"
    )
    observation = _object_bytes(
        file_raw[_one(roles, "runtime_observation")["path"]], "runtime_observation.json"
    )
    provenance = _object_bytes(
        file_raw[_one(roles, "runtime_provenance")["path"]], "runtime_provenance.json"
    )
    _reject_unknown_keys(provenance, _RUNTIME_PROVENANCE_KEYS, "runtime_provenance")
    _reject_unknown_keys(observation, _OBSERVATION_TOP_KEYS, "runtime_observation")
    if request.get("schema_version") != "candidate-runtime-request-0.8.0":
        raise ValueError("runtime request schema_version mismatch")
    _validate_runtime_request_closed(request)
    for field in _RUNTIME_REQUEST_STRICT_INT_FIELDS:
        _strict_positive_int(request.get(field), f"runtime_request.{field}")
    canonical = request.get("bound_payload_canonical")
    if not isinstance(canonical, str) or canonical != _candidate_runtime_bound_payload_canonical(
        request
    ):
        raise ValueError("bound_payload_canonical is not canonical")
    digest = _candidate_runtime_request_digest(request)
    if request.get("request_digest") != digest:
        raise ValueError("runtime request digest is not self-consistent")
    if observation.get("request_digest") != digest:
        raise ValueError("observation request_digest mismatch")
    if observation.get("schema_version") != "candidate-runtime-observation-0.8.0":
        raise ValueError("runtime observation schema_version mismatch")
    if observation.get("status") != "PASS":
        raise ValueError("runtime observation must report PASS")
    if observation.get("candidate_state") != "CLOSED":
        raise ValueError("candidate_state must be CLOSED")
    if observation.get("public_status") != "UNSUPPORTED":
        raise ValueError("public_status must be UNSUPPORTED")
    errors = observation.get("errors")
    if not isinstance(errors, list) or errors:
        raise ValueError("PASS observation must have empty errors")
    if observation.get("production_eligible") is not False:
        raise ValueError("production_eligible must be false")
    for field in (
        "workflow_id",
        "revision",
        "execution_id",
        "strict_attempt_number",
        "asset_id",
        "processed_glb_sha256",
        "godot_version",
    ):
        if request.get(field) != observation.get(field):
            raise ValueError(f"runtime observation {field} does not match request")
    _strict_positive_int(observation.get("revision"), "runtime_observation.revision")
    _strict_positive_int(
        observation.get("strict_attempt_number"), "runtime_observation.strict_attempt_number"
    )
    if observation.get("observed_glb_sha256") != glb_sha:
        raise ValueError("observed_glb_sha256 does not match bundled GLB")
    if observation.get("visual_mesh_name") != request.get("visual_mesh_name"):
        raise ValueError("visual_mesh_name mismatch between request and observation")
    if observation.get("collision_shape_class") != contract.get("collision_shape_class"):
        raise ValueError("collision_shape_class mismatch")
    if observation.get("runtime_body_kind") != contract.get("runtime_body_kind"):
        raise ValueError("runtime_body_kind mismatch")
    if observation.get("physics_ray_hit") is not True:
        raise ValueError("physics_ray_hit must be true")
    if observation.get("harness_sha256") != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise ValueError("observation harness reviewed hash mismatch")
    if observation.get("harness_sha256_raw") != harness_raw:
        raise ValueError("observation harness raw hash mismatch")
    if request.get("harness_sha256") != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise ValueError("request harness reviewed hash mismatch")
    if request.get("profile_document_hash") != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
        raise ValueError("profile_document_hash is not trusted")
    if request.get("runtime_contract_sha256") != PINNED_RUNTIME_CONTRACT_SHA256:
        raise ValueError("runtime_contract_sha256 is not trusted")
    spec_fp = _canonical_doc_hash(spec)
    if request.get("spec_fingerprint") != spec_fp:
        raise ValueError("spec_fingerprint mismatch")
    if request.get("processed_glb_sha256") != glb_sha:
        raise ValueError("request processed_glb_sha256 mismatch")
    if observation.get("processed_glb_sha256") != glb_sha:
        raise ValueError("observation processed_glb_sha256 mismatch")
    obs_capsule = observation.get("capsule")
    req_capsule = request.get("capsule")
    if not isinstance(obs_capsule, dict) or not isinstance(req_capsule, dict):
        raise ValueError("runtime capsule objects are required")
    _reject_unknown_keys(obs_capsule, _OBSERVED_CAPSULE_KEYS, "runtime_observation.capsule")
    obs_radius = _strict_float_finite(
        obs_capsule.get("observed_radius_m"), "runtime_observation.capsule.observed_radius_m"
    )
    req_radius = _strict_float_finite(
        req_capsule.get("radius_m"), "runtime_request.capsule.radius_m"
    )
    if abs(obs_radius - req_radius) > 1e-5:
        raise ValueError("observed capsule radius differs from request")
    obs_height = _strict_float_finite(
        obs_capsule.get("observed_height_m"), "runtime_observation.capsule.observed_height_m"
    )
    req_height = _strict_float_finite(
        req_capsule.get("height_m"), "runtime_request.capsule.height_m"
    )
    if abs(obs_height - req_height) > 1e-5:
        raise ValueError("observed capsule height differs from request")
    obs_center = _strict_vec3_finite(
        obs_capsule.get("center_m"), "runtime_observation.capsule.center_m"
    )
    req_center = _strict_vec3_finite(
        request.get("capsule_center_m"), "runtime_request.capsule_center_m"
    )
    if any(abs(obs_center[i] - req_center[i]) > _CENTER_TOLERANCE for i in range(3)):
        raise ValueError("observed capsule center differs from request")
    if obs_capsule.get("shape_class") != contract.get("collision_shape_class"):
        raise ValueError("observed capsule shape_class mismatch")
    rest_bounds = observation.get("rest_mesh_bounds")
    if not isinstance(rest_bounds, dict):
        raise ValueError("rest_mesh_bounds missing")
    _reject_unknown_keys(rest_bounds, _REST_BOUNDS_KEYS, "rest_mesh_bounds")
    req_rest_early = request.get("rest_aabb")
    if isinstance(req_rest_early, dict):
        for axis in ("min", "max"):
            observed_axis = _strict_vec3_finite(rest_bounds.get(axis), f"rest_mesh_bounds.{axis}")
            expected_axis = _strict_vec3_finite(
                req_rest_early.get(axis), f"runtime_request.rest_aabb.{axis}"
            )
            if any(abs(observed_axis[i] - expected_axis[i]) > _AABB_TOLERANCE for i in range(3)):
                raise ValueError("rest_mesh_bounds differ from request rest_aabb")
    bound_views = request.get("nine_view_set")
    if not isinstance(bound_views, list) or set(bound_views) != _NINE_VIEWS:
        raise ValueError("nine_view_set must match frozen contract views")
    import_exit = provenance.get("import_exit_code")
    process_exit = provenance.get("process_exit_code")
    if isinstance(import_exit, bool) or not isinstance(import_exit, int):
        raise ValueError("import_exit_code must be a strict integer")
    if isinstance(process_exit, bool) or not isinstance(process_exit, int):
        raise ValueError("process_exit_code must be a strict integer")
    if import_exit != 0 or process_exit != 0:
        raise ValueError("runtime provenance exit codes must be zero")
    if provenance.get("import_timed_out") is not False:
        raise ValueError("import_timed_out must be false")
    if provenance.get("process_timed_out") is not False:
        raise ValueError("process_timed_out must be false")
    stage_dir = provenance.get("stage_dir")
    request_path = provenance.get("request_path")
    stage_dir_s = _strict_nonempty_string(stage_dir, "runtime_provenance.stage_dir")
    request_path_s = _strict_nonempty_string(request_path, "runtime_provenance.request_path")
    if (
        PurePosixPath(request_path_s.replace("\\", "/")).parent.as_posix()
        != PurePosixPath(stage_dir_s.replace("\\", "/")).as_posix()
    ):
        raise ValueError("runtime provenance request_path parent must equal stage_dir")
    stage_norm = PurePosixPath(stage_dir_s.replace("\\", "/")).as_posix()
    request_observation = request.get("observation_path")
    request_capture = request.get("capture_dir")
    if request_observation is not None or request_capture is not None:
        if request_observation is None or request_capture is None:
            raise ValueError("runtime request staging fields must be complete when any are present")
        obs_parent = PurePosixPath(
            _strict_nonempty_string(
                request_observation, "runtime_request.observation_path"
            ).replace("\\", "/")
        ).parent.as_posix()
        cap_parent = PurePosixPath(
            _strict_nonempty_string(request_capture, "runtime_request.capture_dir").replace(
                "\\", "/"
            )
        ).parent.as_posix()
        if obs_parent != stage_norm or cap_parent != stage_norm:
            raise ValueError(
                "runtime request stage paths must be consistent with provenance stage_dir"
            )
    req_path_norm = PurePosixPath(request_path_s.replace("\\", "/")).as_posix()
    if PurePosixPath(req_path_norm).parent.as_posix() != stage_norm:
        raise ValueError("runtime provenance request_path must live under stage_dir")
    bound_in_prov = provenance.get("bound_request")
    if not isinstance(bound_in_prov, dict):
        raise ValueError("runtime provenance missing bound_request")
    _reject_unknown_keys(
        bound_in_prov, _BOUND_RUNTIME_REQUEST_KEYS, "runtime_provenance.bound_request"
    )
    if _candidate_runtime_bound_payload_canonical(
        bound_in_prov
    ) != _candidate_runtime_bound_payload_canonical(request):
        raise ValueError("provenance bound_request does not match bundled runtime request")
    import_log = file_raw[_one(roles, "runtime_import_log")["path"]].decode(
        "utf-8", errors="replace"
    )
    render_log = file_raw[_one(roles, "runtime_render_log")["path"]].decode(
        "utf-8", errors="replace"
    )
    if _scan_engine_diag(import_log) or _scan_engine_diag(render_log):
        raise ValueError("runtime logs contain engine error diagnostics")
    rig_mod = _load_trusted_rig_verifier()
    decoded = rig_mod._decode_internal_skinned_glb(glb_path)
    mins, maxs = _visual_aabb_from_decoded(decoded)
    center = _capsule_center_from_aabb(mins, maxs)
    rest_aabb = {
        "min": [float(mins[0]), float(mins[1]), float(mins[2])],
        "max": [float(maxs[0]), float(maxs[1]), float(maxs[2])],
    }
    rest_digest = _candidate_runtime_rest_aabb_canonical_sha256(rest_aabb)
    if request.get("rest_aabb_canonical_sha256") != rest_digest:
        raise ValueError("request rest_aabb_canonical_sha256 is not GLB-derived")
    req_rest = request.get("rest_aabb")
    if not isinstance(req_rest, dict):
        raise ValueError("request rest_aabb missing")
    for axis in ("min", "max"):
        observed_axis = _strict_vec3_finite(req_rest.get(axis), f"runtime_request.rest_aabb.{axis}")
        for i in range(3):
            if abs(observed_axis[i] - rest_aabb[axis][i]) > _AABB_TOLERANCE:
                raise ValueError("request rest_aabb does not match GLB geometry")
    req_center = _strict_vec3_finite(
        request.get("capsule_center_m"), "runtime_request.capsule_center_m"
    )
    for i in range(3):
        if abs(req_center[i] - center[i]) > _CENTER_TOLERANCE:
            raise ValueError("request capsule_center_m is not GLB-derived midpoint")
        expected_mid = (rest_aabb["min"][i] + rest_aabb["max"][i]) / 2.0
        if req_center[i] != expected_mid:
            raise ValueError("request capsule_center_m is not rest AABB midpoint")
    capsule_spec = spec.get("collider", {}).get("capsule", {})
    req_capsule = request.get("capsule", {})
    req_cap_radius = _strict_float_finite(
        req_capsule.get("radius_m"), "runtime_request.capsule.radius_m"
    )
    req_cap_height = _strict_float_finite(
        req_capsule.get("height_m"), "runtime_request.capsule.height_m"
    )
    if abs(req_cap_radius - float(capsule_spec.get("radius_m"))) > 1e-5:
        raise ValueError("request capsule radius mismatch")
    if abs(req_cap_height - float(capsule_spec.get("height_m"))) > 1e-5:
        raise ValueError("request capsule height mismatch")
    viewport = contract.get("viewport", {})
    vp_w = int(viewport.get("width", 0))
    vp_h = int(viewport.get("height", 0))
    if vp_w != _CAPTURE_WIDTH or vp_h != _CAPTURE_HEIGHT:
        raise ValueError("runtime contract viewport must be 1280x720")
    framing_policy = contract.get("framing", {})
    bounds = BoundsAABB(mins[0], mins[1], mins[2], maxs[0], maxs[1], maxs[2])
    view_framing = observation.get("view_framing", {})
    if not isinstance(view_framing, dict):
        raise ValueError("observation view_framing missing")
    captures = observation.get("captures", [])
    if not isinstance(captures, list) or len(captures) != 9:
        raise ValueError("observation captures incomplete")
    capture_by_view = {entry["view"]: entry for entry in captures if isinstance(entry, dict)}
    if set(capture_by_view) != _NINE_VIEWS:
        raise ValueError("observation captures missing views")
    for view in _NINE_VIEWS:
        measured = view_framing.get(view)
        if not isinstance(measured, dict):
            raise ValueError(f"missing framing for view {view}")
        _reject_unknown_keys(measured, _VIEW_FRAMING_KEYS, f"view_framing.{view}")
        if measured.get("view_axis") != _view_axis_label(view):
            raise ValueError(f"view_axis mismatch for {view}")
        geom = _framing_geometry(
            bounds,
            view,
            fov_degrees=float(framing_policy.get("fov_degrees", 38.0)),
            target_screen_fraction=float(framing_policy.get("target_screen_fraction", 0.65)),
            viewport=(vp_w, vp_h),
        )
        if (
            abs(
                _strict_float_finite(
                    measured.get("camera_distance"), f"view_framing.{view}.camera_distance"
                )
                - geom.distance
            )
            > _FRAMING_DISTANCE_TOLERANCE
        ):
            raise ValueError(f"camera_distance mismatch for {view}")
        rect = measured.get("projected_rect_pixels", {})
        if not isinstance(rect, dict):
            raise ValueError(f"projected_rect_pixels missing for {view}")
        _reject_unknown_keys(
            rect, _PROJECTED_RECT_KEYS, f"view_framing.{view}.projected_rect_pixels"
        )
        horiz_tol = _PROJECTED_RECT_PIXEL_SLACK / float(vp_w)
        vert_tol = _PROJECTED_RECT_PIXEL_SLACK / float(vp_h)
        rect_x = _strict_float_finite(rect.get("x"), f"view_framing.{view}.projected_rect_pixels.x")
        rect_y = _strict_float_finite(rect.get("y"), f"view_framing.{view}.projected_rect_pixels.y")
        rect_w = _strict_float_finite(
            rect.get("width"), f"view_framing.{view}.projected_rect_pixels.width"
        )
        rect_h = _strict_float_finite(
            rect.get("height"), f"view_framing.{view}.projected_rect_pixels.height"
        )
        observed_vert = rect_h / float(vp_h)
        observed_horiz = rect_w / float(vp_w)
        if abs(observed_vert - geom.vertical_fraction) > vert_tol:
            raise ValueError(f"projected_rect height fraction mismatch for {view}")
        if abs(observed_horiz - geom.horizontal_fraction) > horiz_tol:
            raise ValueError(f"projected_rect width fraction mismatch for {view}")
        metrics = _framing_metrics_from_projected_rect(
            x=rect_x,
            y=rect_y,
            width=rect_w,
            height=rect_h,
            viewport_width=float(vp_w),
            viewport_height=float(vp_h),
            margin_fraction=_strict_float_finite(
                framing_policy.get("margin_fraction"), "runtime contract framing.margin_fraction"
            ),
        )
        height_ratio = _strict_float_finite(
            measured.get("height_ratio"), f"view_framing.{view}.height_ratio"
        )
        fill_ratio = _strict_float_finite(
            measured.get("fill_ratio"), f"view_framing.{view}.fill_ratio"
        )
        center_offset = _strict_float_finite(
            measured.get("center_offset"), f"view_framing.{view}.center_offset"
        )
        if abs(height_ratio - metrics["height_ratio"]) > _FRAMING_NUMERIC_TOLERANCE:
            raise ValueError(f"height_ratio mismatch for {view}")
        if abs(fill_ratio - metrics["fill_ratio"]) > _FRAMING_NUMERIC_TOLERANCE:
            raise ValueError(f"fill_ratio mismatch for {view}")
        if abs(center_offset - metrics["center_offset"]) > _CENTER_OFFSET_TOLERANCE:
            raise ValueError(f"center_offset mismatch for {view}")
        horizontally_centered = _strict_bool_literal(
            measured.get("horizontally_centered"), f"view_framing.{view}.horizontally_centered"
        )
        if horizontally_centered != metrics["horizontally_centered"]:
            raise ValueError(f"horizontally_centered mismatch for {view}")
        if not metrics["inside_margin"]:
            raise ValueError(f"projected bounds outside margin for {view}")
        fill = metrics["fill_ratio"]
        min_fill = _strict_float_finite(
            framing_policy.get("min_screen_fraction"),
            "runtime contract framing.min_screen_fraction",
        )
        max_fill = _strict_float_finite(
            framing_policy.get("max_screen_fraction"),
            "runtime contract framing.max_screen_fraction",
        )
        if fill + _FILL_TOLERANCE < min_fill or fill - _FILL_TOLERANCE > max_fill:
            raise ValueError(f"fill_ratio outside policy for {view}")
        policy_ok = (
            metrics["inside_margin"]
            and min_fill <= fill <= max_fill
            and metrics["horizontally_centered"]
        )
        ok_flag = _strict_bool_literal(measured.get("ok"), f"view_framing.{view}.ok")
        if ok_flag is not True:
            raise ValueError(f"view {view} framing ok must be true")
        if ok_flag != policy_ok:
            raise ValueError(f"view {view} ok flag disagrees with recomputed metrics")
        cap_entry = roles[_CAPTURE_ROLE]
        cap_rel = next(item["path"] for item in cap_entry if item.get("angle") == view)
        png_raw = file_raw[cap_rel]
        if len(png_raw) > _MAX_CAPTURE_PNG_BYTES:
            raise ValueError(f"capture PNG too large for {view}")
        width, height, pixels, channels = _decode_runtime_capture_png(png_raw)
        if (width, height) != (vp_w, vp_h):
            raise ValueError(f"capture dimensions mismatch for {view}")
        _png_nonblank(pixels, width, height, channels)
        cap_meta = capture_by_view[view]
        _reject_unknown_keys(cap_meta, _CAPTURE_ENTRY_KEYS, f"captures.{view}")
        _strict_positive_int(cap_meta.get("png_width"), f"captures.{view}.png_width")
        _strict_positive_int(cap_meta.get("png_height"), f"captures.{view}.png_height")
        _strict_positive_int(cap_meta.get("revision"), f"captures.{view}.revision")
        _strict_positive_int(
            cap_meta.get("strict_attempt_number"), f"captures.{view}.strict_attempt_number"
        )
        if cap_meta.get("request_digest") != digest:
            raise ValueError(f"capture request_digest mismatch for {view}")
        if cap_meta.get("execution_id") != request.get("execution_id"):
            raise ValueError(f"capture execution_id mismatch for {view}")
        if cap_meta.get("revision") != request.get("revision"):
            raise ValueError(f"capture revision mismatch for {view}")
        if cap_meta.get("strict_attempt_number") != request.get("strict_attempt_number"):
            raise ValueError(f"capture strict_attempt_number mismatch for {view}")
        if cap_meta.get("png_sha256") != _sha256(png_raw):
            raise ValueError(f"capture png_sha256 mismatch for {view}")
        if int(cap_meta.get("png_width", 0)) != _CAPTURE_WIDTH:
            raise ValueError(f"capture png_width must be {_CAPTURE_WIDTH} for {view}")
        if int(cap_meta.get("png_height", 0)) != _CAPTURE_HEIGHT:
            raise ValueError(f"capture png_height must be {_CAPTURE_HEIGHT} for {view}")
    return digest, "PASS"


def _validate_nested_rig(
    rig_mod: Any,
    *,
    root: Path,
    roles: dict[str, list[dict[str, Any]]],
    file_raw: dict[str, bytes],
    processed_sha: str,
    nested_packaged_path: str,
) -> None:
    wrapper = _object_bytes(
        file_raw[_one(roles, "rig_attempt_wrapper")["path"]], "rig_attempt_wrapper.json"
    )
    historical_nested = wrapper.get("nested_bundle_dir")
    if not isinstance(historical_nested, str) or not historical_nested:
        raise ValueError("rig wrapper nested_bundle_dir missing")
    nested_rel = nested_packaged_path
    nested_dir = _resolve_under_root(root, nested_rel)
    if not nested_dir.is_dir():
        raise ValueError("nested rig bundle directory missing")
    child = rig_mod.verify_bundle(nested_dir)
    if child.get("integrity_outcome") != "VERIFIED" or child.get("runtime_status") != "PASS":
        raise ValueError("nested rig cold verification did not PASS")
    if child.get("validation_status") != "PASS":
        raise ValueError("nested rig validation_status must be PASS")
    manifest_path = nested_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("nested rig manifest missing")
    nested_manifest = _parse_strict_json_file(manifest_path, "nested manifest.json")
    if nested_manifest.get("glb_sha256") != processed_sha:
        raise ValueError("nested rig glb_sha256 does not match processed GLB")
    nested_glb_sha = None
    for entry in nested_manifest.get("files", []):
        if isinstance(entry, dict) and entry.get("role") == "skinned_glb":
            nested_glb_sha = entry.get("sha256")
    if nested_glb_sha != processed_sha:
        raise ValueError("nested rig skinned_glb hash mismatch")
    wrapper_manifest_hash = wrapper.get("nested_manifest_sha256")
    if wrapper_manifest_hash != _sha256(
        _read_bounded_json_bytes(manifest_path, "nested manifest.json")
    ):
        raise ValueError("rig wrapper nested_manifest_sha256 mismatch")
    if wrapper.get("processed_glb_sha256") != processed_sha:
        raise ValueError("rig wrapper processed_glb_sha256 mismatch")


def _validate_snapshot_crossbindings(
    *,
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    roles: dict[str, list[dict[str, Any]]],
    file_raw: dict[str, bytes],
    spec: dict[str, Any],
    profile_doc: dict[str, Any],
    workflow_id: str,
    request_digest: str,
    nested_packaged_path: str,
    root: Path,
) -> None:
    def _role_sha(role: str) -> str:
        digest = _one(roles, role)["sha256"]
        if not isinstance(digest, str):
            raise ValueError(f"manifest role {role} sha256 invalid")
        return digest

    def _role_bytes(role: str) -> bytes:
        return file_raw[_one(roles, role)["path"]]

    def _expect_sha(field: str, expected: str, actual: str) -> None:
        if actual != expected:
            raise ValueError(f"snapshot {field} does not match bundled role bytes")

    _strict_sha256_field(snapshot.get("specification_hash"), "snapshot.specification_hash")
    _strict_sha256_field(snapshot.get("profile_document_hash"), "snapshot.profile_document_hash")
    _strict_sha256_field(snapshot.get("source_glb_hash"), "snapshot.source_glb_hash")
    if snapshot.get("workflow_id") != workflow_id:
        raise ValueError("snapshot workflow_id does not match manifest")
    if manifest.get("workflow_id") != workflow_id:
        raise ValueError("manifest workflow_id mismatch")
    if snapshot.get("graph_version") != CANDIDATE_GRAPH_VERSION:
        raise ValueError("snapshot graph_version mismatch")
    if snapshot.get("asset_id") != spec.get("asset_id"):
        raise ValueError("snapshot asset_id does not match specification")
    if snapshot.get("profile_id") != profile_doc.get("profile_id"):
        raise ValueError("snapshot profile_id does not match profile document")
    if snapshot.get("profile_version") != profile_doc.get("version"):
        raise ValueError("snapshot profile_version does not match profile document")
    auth_path = snapshot.get("authoritative_source_glb_relative_path")
    if not isinstance(auth_path, str) or not auth_path.strip():
        raise ValueError("snapshot authoritative_source_glb_relative_path missing")
    _path(auth_path)
    if snapshot.get("specification_hash") != manifest.get("specification_hash"):
        raise ValueError("snapshot specification_hash does not match manifest")
    if snapshot.get("profile_document_hash") != manifest.get("profile_document_hash"):
        raise ValueError("snapshot profile_document_hash does not match manifest")
    if snapshot.get("source_glb_hash") != manifest.get("source_glb_sha256"):
        raise ValueError("snapshot source_glb_hash does not match manifest")
    source_sha = _role_sha("source_glb")
    raw_sha = _role_sha("raw_glb")
    proc_sha = _role_sha("processed_glb")
    if source_sha != raw_sha or raw_sha != proc_sha:
        raise ValueError("snapshot GLB role hash chain mismatch")
    _expect_sha("source_glb_hash", snapshot.get("source_glb_hash", ""), source_sha)
    _expect_sha("raw_glb_sha256", snapshot.get("raw_glb_sha256", ""), raw_sha)
    _expect_sha("processed_glb_sha256", snapshot.get("processed_glb_sha256", ""), proc_sha)
    _expect_sha(
        "retained_source_sha256",
        snapshot.get("retained_source_sha256", ""),
        source_sha,
    )
    _expect_sha(
        "retained_specification_sha256",
        snapshot.get("retained_specification_sha256", ""),
        _sha256(_role_bytes("candidate_specification")),
    )
    _expect_sha(
        "retained_profile_document_sha256",
        snapshot.get("retained_profile_document_sha256", ""),
        _sha256(_role_bytes("candidate_profile_document")),
    )
    _expect_sha(
        "static_validation_report_sha256",
        snapshot.get("static_validation_report_sha256", ""),
        _sha256(_role_bytes("static_validation_report")),
    )
    _expect_sha(
        "runtime_request_sha256",
        snapshot.get("runtime_request_sha256", ""),
        _sha256(_role_bytes("runtime_request")),
    )
    _expect_sha(
        "runtime_observation_sha256",
        snapshot.get("runtime_observation_sha256", ""),
        _sha256(_role_bytes("runtime_observation")),
    )
    _expect_sha(
        "runtime_provenance_sha256",
        snapshot.get("runtime_provenance_sha256", ""),
        _sha256(_role_bytes("runtime_provenance")),
    )
    _expect_sha(
        "runtime_import_log_sha256",
        snapshot.get("runtime_import_log_sha256", ""),
        _sha256(_role_bytes("runtime_import_log")),
    )
    _expect_sha(
        "runtime_render_log_sha256",
        snapshot.get("runtime_render_log_sha256", ""),
        _sha256(_role_bytes("runtime_render_log")),
    )
    _expect_sha(
        "rig_attempt_wrapper_sha256",
        snapshot.get("rig_attempt_wrapper_sha256", ""),
        _sha256(_role_bytes("rig_attempt_wrapper")),
    )
    _expect_sha(
        "identity_report_sha256",
        snapshot.get("identity_report_sha256", ""),
        _sha256(_role_bytes("identity_report")),
    )
    if snapshot.get("asset_revision_spec_hash") != snapshot.get("specification_hash"):
        raise ValueError("snapshot asset_revision_spec_hash mismatch")
    if snapshot.get("asset_revision_raw_glb_hash") != raw_sha:
        raise ValueError("snapshot asset_revision_raw_glb_hash mismatch")
    rev_proc = snapshot.get("asset_revision_processed_glb_hash")
    if rev_proc is not None and rev_proc != proc_sha:
        raise ValueError("snapshot asset_revision_processed_glb_hash mismatch")
    if snapshot.get("runtime_request_digest") != request_digest:
        raise ValueError("snapshot runtime_request_digest mismatch")
    request = _object_bytes(_role_bytes("runtime_request"), "runtime_request.json")
    observation = _object_bytes(_role_bytes("runtime_observation"), "runtime_observation.json")
    obs_captures = observation.get("captures")
    if not isinstance(obs_captures, list):
        raise ValueError("runtime observation captures missing")
    obs_by_view = {
        entry["view"]: entry
        for entry in obs_captures
        if isinstance(entry, dict) and isinstance(entry.get("view"), str)
    }
    if set(obs_by_view) != _NINE_VIEWS:
        raise ValueError("runtime observation captures missing views")
    expected_capture_hashes = sorted(
        _strict_sha256_field(entry.get("png_sha256"), f"captures.{view}.png_sha256")
        for view, entry in obs_by_view.items()
    )
    capture_hashes = snapshot.get("runtime_capture_hashes")
    if not isinstance(capture_hashes, list) or len(capture_hashes) != 9:
        raise ValueError("snapshot runtime_capture_hashes must be a nine-entry list")
    if capture_hashes != sorted(capture_hashes):
        raise ValueError("snapshot runtime_capture_hashes must be sorted")
    for index, digest in enumerate(capture_hashes):
        _strict_sha256_field(digest, f"snapshot.runtime_capture_hashes[{index}]")
    if capture_hashes != expected_capture_hashes:
        raise ValueError("snapshot runtime_capture_hashes mismatch")
    capture_paths_by_view = _runtime_capture_paths_by_view(roles)
    for item in roles.get(_CAPTURE_ROLE, []):
        angle = item.get("angle")
        rel = item.get("path")
        if not isinstance(angle, str) or not isinstance(rel, str):
            raise ValueError("runtime_capture inventory invalid")
        if capture_paths_by_view.get(angle) != rel:
            raise ValueError(f"runtime_capture manifest path mismatch for view {angle}")
        actual = _sha256(file_raw[rel])
        obs_entry = obs_by_view.get(angle)
        if obs_entry is None:
            raise ValueError(f"runtime observation missing capture view {angle}")
        obs_hash = obs_entry.get("png_sha256")
        if not isinstance(obs_hash, str) or obs_hash != actual:
            raise ValueError(f"runtime capture hash mismatch for view {angle}")
        if actual not in capture_hashes:
            raise ValueError(f"snapshot runtime_capture_hashes missing bundled capture {angle}")
    wrapper = _object_bytes(_role_bytes("rig_attempt_wrapper"), "rig_attempt_wrapper.json")
    if snapshot.get("nested_bundle_id") != wrapper.get("nested_bundle_id"):
        raise ValueError("snapshot nested_bundle_id does not match rig wrapper")
    if wrapper.get("workflow_id") != workflow_id:
        raise ValueError("rig wrapper workflow_id mismatch")
    if wrapper.get("revision_number") != snapshot.get("revision_number"):
        raise ValueError("rig wrapper revision_number mismatch")
    if wrapper.get("specification_hash") != snapshot.get("specification_hash"):
        raise ValueError("rig wrapper specification_hash mismatch")
    if wrapper.get("profile_document_hash") != snapshot.get("profile_document_hash"):
        raise ValueError("rig wrapper profile_document_hash mismatch")
    nested_manifest_path = _resolve_under_root(root, nested_packaged_path) / "manifest.json"
    on_disk_nested_sha = _sha256(_read_bounded_json_bytes(nested_manifest_path, "nested manifest"))
    if snapshot.get("nested_rig_manifest_on_disk_sha256") != on_disk_nested_sha:
        raise ValueError("snapshot nested_rig_manifest_on_disk_sha256 mismatch")
    nested_binding = next(
        (
            row
            for row in snapshot.get("pre_review_artifact_bindings", [])
            if isinstance(row, dict) and row.get("role") == "candidate-nested-rig-manifest"
        ),
        None,
    )
    if nested_binding is None:
        raise ValueError("snapshot missing nested rig manifest binding")
    if snapshot.get("nested_rig_manifest_sha256") != nested_binding.get("content_sha256"):
        raise ValueError("snapshot nested_rig_manifest_sha256 mismatch")
    if request.get("workflow_id") != workflow_id or observation.get("workflow_id") != workflow_id:
        raise ValueError("runtime request/observation workflow_id mismatch")
    if request.get("revision") != snapshot.get("revision_number"):
        raise ValueError("runtime request revision does not match snapshot")
    if observation.get("revision") != snapshot.get("revision_number"):
        raise ValueError("runtime observation revision does not match snapshot")
    capture_exec = snapshot.get("capture_execution")
    if not isinstance(capture_exec, dict):
        raise ValueError("snapshot capture_execution missing")
    if request.get("execution_id") != capture_exec.get("id"):
        raise ValueError("runtime request execution_id does not match snapshot capture_execution")
    if observation.get("execution_id") != capture_exec.get("id"):
        raise ValueError(
            "runtime observation execution_id does not match snapshot capture_execution"
        )
    for key in (
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
    ):
        block = snapshot.get(key)
        if not isinstance(block, dict):
            raise ValueError(f"snapshot {key} missing")
        _strict_positive_int(block.get("attempt_number"), f"snapshot.{key}.attempt_number")
        if block.get("status") != "COMPLETED":
            raise ValueError(f"snapshot {key} must be COMPLETED")
    artifact_to_manifest = {
        "candidate-specification": "candidate_specification",
        "candidate-profile-document": "candidate_profile_document",
        "candidate-source-retained": "source_glb",
        "candidate-raw-glb": "raw_glb",
        "candidate-processed-glb": "processed_glb",
        "candidate-static-validation-report": "static_validation_report",
        "candidate-runtime-request": "runtime_request",
        "candidate-runtime-observation": "runtime_observation",
        "candidate-runtime-provenance": "runtime_provenance",
        "candidate-runtime-import-log": "runtime_import_log",
        "candidate-runtime-render-log": "runtime_render_log",
        "candidate-rig-attempt-wrapper": "rig_attempt_wrapper",
        "candidate-identity-report": "identity_report",
        "candidate-nested-rig-manifest": "nested_rig_manifest",
    }
    capture_binding_count = 0
    capture_hashes_seen: set[str] = set()
    capture_binding_views: set[str] = set()
    for row in snapshot.get("pre_review_artifact_bindings", []):
        if not isinstance(row, dict):
            raise ValueError("snapshot binding must be object")
        artifact_role = row.get("role")
        content_hash = row.get("content_sha256")
        if not isinstance(artifact_role, str) or not isinstance(content_hash, str):
            raise ValueError("snapshot binding role/hash invalid")
        if artifact_role == "candidate-runtime-capture":
            capture_binding_count += 1
            binding_rel = row.get("relative_path")
            if not isinstance(binding_rel, str):
                raise ValueError("snapshot capture binding relative_path missing")
            view = _capture_view_from_relative_path(binding_rel, field="snapshot capture binding")
            if view in capture_binding_views:
                raise ValueError("duplicate snapshot capture view binding")
            capture_binding_views.add(view)
            manifest_rel = capture_paths_by_view.get(view)
            if manifest_rel is None:
                raise ValueError(f"snapshot capture binding missing manifest view {view}")
            manifest_hash = _sha256(file_raw[manifest_rel])
            if content_hash != manifest_hash:
                raise ValueError(f"snapshot capture binding hash mismatch for view {view}")
            obs_entry = obs_by_view.get(view)
            if obs_entry is None:
                raise ValueError(f"snapshot capture binding missing observation view {view}")
            obs_hash = obs_entry.get("png_sha256")
            if not isinstance(obs_hash, str) or obs_hash != content_hash:
                raise ValueError(
                    f"snapshot capture binding does not match observation for view {view}"
                )
            capture_hashes_seen.add(content_hash)
            continue
        manifest_role = artifact_to_manifest.get(artifact_role)
        if manifest_role is None:
            raise ValueError(f"unknown snapshot binding role {artifact_role}")
        if manifest_role == "nested_rig_manifest":
            expected = on_disk_nested_sha
        else:
            expected = _sha256(_role_bytes(manifest_role))
        if content_hash != expected:
            raise ValueError("snapshot binding content_sha256 does not match bundled role bytes")
    if capture_binding_count != 9:
        raise ValueError("snapshot must bind nine runtime captures")
    if capture_binding_views != _NINE_VIEWS:
        raise ValueError("snapshot capture bindings must cover nine distinct views")
    if capture_hashes_seen != set(capture_hashes):
        raise ValueError("snapshot capture bindings disagree with runtime_capture_hashes list")


def _validate_approval_scope(
    *,
    scope: dict[str, Any],
    snapshot: dict[str, Any],
    roles: dict[str, list[dict[str, Any]]],
    file_raw: dict[str, bytes],
    root: Path,
    workflow_id: str,
    snapshot_fingerprint: str,
    nested_packaged_path: str,
    spec: dict[str, Any],
    profile_doc: dict[str, Any],
    source_sha: str,
) -> str:
    _reject_unknown_keys(scope, _APPROVAL_SCOPE_KEYS, "approval_scope")
    scope_approval_id = scope.get("approval_id")
    if not isinstance(scope_approval_id, str) or not scope_approval_id:
        raise ValueError("approval_scope approval_id missing")
    scope_status = scope.get("approval_status")
    if scope_status != "APPROVED":
        raise ValueError("approval_scope approval_status must be APPROVED")
    scope_review_exec = scope.get("review_execution_id")
    if not isinstance(scope_review_exec, str) or not scope_review_exec:
        raise ValueError("approval_scope review_execution_id missing")
    review = scope.get("review_task")
    if isinstance(review, dict):
        _validate_review_task_closed(review)
    bindings = snapshot.get("pre_review_artifact_bindings")
    if not isinstance(bindings, list):
        raise ValueError("snapshot pre_review_artifact_bindings missing")
    if len(bindings) != PRE_REVIEW_BINDING_COUNT:
        raise ValueError("snapshot requires exactly 23 pre_review artifact bindings")
    binding_by_id = {
        row["artifact_id"]: row
        for row in bindings
        if isinstance(row, dict) and row.get("artifact_id")
    }
    if len(binding_by_id) != PRE_REVIEW_BINDING_COUNT:
        raise ValueError("duplicate or missing snapshot artifact ids")
    binding_ids = set(binding_by_id)
    scope_ids = scope.get("persisted_approval_artifact_ids")
    if not isinstance(scope_ids, list):
        raise ValueError("approval_scope persisted_approval_artifact_ids missing")
    if set(scope_ids) != binding_ids:
        raise ValueError("approval_scope artifact id set must equal snapshot 23-id set")
    entries = scope.get("entries")
    if not isinstance(entries, list) or len(entries) != len(scope_ids):
        raise ValueError("approval_scope entries mismatch persisted ids")
    entry_ids = {entry.get("artifact_id") for entry in entries if isinstance(entry, dict)}
    if entry_ids != binding_ids:
        raise ValueError("approval_scope entries do not match snapshot ids")
    wrapper_doc = _object_bytes(file_raw[_one(roles, "rig_attempt_wrapper")["path"]], "rig wrapper")
    nested_bundle_dir = wrapper_doc.get("nested_bundle_dir")
    capture_paths_by_view = _runtime_capture_paths_by_view(roles)
    observation_doc = _object_bytes(
        file_raw[_one(roles, "runtime_observation")["path"]], "runtime_observation.json"
    )
    obs_captures = observation_doc.get("captures")
    if not isinstance(obs_captures, list):
        raise ValueError("runtime observation captures missing")
    obs_by_view = {
        item["view"]: item
        for item in obs_captures
        if isinstance(item, dict) and isinstance(item.get("view"), str)
    }
    if set(obs_by_view) != _NINE_VIEWS:
        raise ValueError("runtime observation captures missing views")
    bundle_paths: set[str] = set()
    scope_capture_views: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("approval_scope entry must be object")
        _reject_unknown_keys(entry, _SCOPE_ENTRY_KEYS, "approval_scope entry")
        artifact_type = entry.get("artifact_type")
        if not isinstance(artifact_type, str) or artifact_type in _FORBIDDEN_SCOPE_ARTIFACT_TYPES:
            raise ValueError(
                "approval_scope must not include post-approval receipt or scope artifacts"
            )
        binding = binding_by_id.get(entry.get("artifact_id"))
        if binding is None:
            raise ValueError("approval_scope entry not in snapshot bindings")
        if binding.get("role") != artifact_type:
            raise ValueError("approval_scope artifact_type does not match snapshot binding role")
        rel = entry.get("relative_path")
        if not isinstance(rel, str) or rel != binding.get("relative_path"):
            raise ValueError("approval_scope relative_path must match snapshot binding")
        if entry.get("task_id") != binding.get("task_id"):
            raise ValueError("approval_scope task_id does not match snapshot binding")
        if entry.get("execution_id") != binding.get("producing_execution_id"):
            raise ValueError("approval_scope execution_id does not match snapshot binding")
        exec_block = _execution_block_for_id(snapshot, str(entry.get("execution_id")))
        if exec_block is None:
            raise ValueError("approval_scope execution_id is not a known snapshot stage")
        attempt = entry.get("attempt_number")
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise ValueError("approval_scope attempt_number must be a strict integer")
        if attempt != exec_block.get("attempt_number"):
            raise ValueError("approval_scope attempt_number does not match producing execution")
        declared_size = entry.get("size")
        if isinstance(declared_size, bool) or not isinstance(declared_size, int):
            raise ValueError("approval_scope size must be a strict integer")
        if declared_size != binding.get("size_bytes"):
            raise ValueError("approval_scope size does not match snapshot binding")
        bundle_path = entry.get("bundle_path")
        if not isinstance(bundle_path, str) or not bundle_path:
            raise ValueError("approval_scope entry missing bundle_path")
        bundle_path_norm = _path(bundle_path)
        if bundle_path_norm in bundle_paths:
            raise ValueError("approval_scope bundle_path mapping must be unique")
        bundle_paths.add(bundle_path_norm)
        role_or_nested = entry.get("role_or_nested_manifest")
        if artifact_type == "candidate-runtime-capture":
            if role_or_nested != _CAPTURE_ROLE:
                raise ValueError("approval_scope capture role_or_nested_manifest invalid")
            view = _capture_view_from_relative_path(str(rel), field="approval_scope capture")
            if view in scope_capture_views:
                raise ValueError("duplicate approval_scope capture view binding")
            scope_capture_views.add(view)
            expected_bundle = capture_paths_by_view.get(view)
            if expected_bundle is None:
                raise ValueError(f"approval_scope capture missing manifest view {view}")
            if bundle_path_norm != expected_bundle:
                raise ValueError(
                    f"approval_scope capture bundle_path does not match view identity for {view}"
                )
            obs_hash = obs_by_view.get(view, {}).get("png_sha256")
            if not isinstance(obs_hash, str) or obs_hash != entry.get("content_hash"):
                raise ValueError(
                    f"approval_scope capture content_hash does not match observation for {view}"
                )
        elif role_or_nested == "rig/manifest.json":
            if not isinstance(nested_bundle_dir, str) or not nested_bundle_dir:
                raise ValueError("rig wrapper nested_bundle_dir missing for nested manifest scope")
            expected_rel = f"{nested_bundle_dir.rstrip('/')}/manifest.json"
            if rel != expected_rel:
                raise ValueError("nested manifest scope relative_path must match wrapper path")
            expected_bundle = f"{nested_packaged_path.rstrip('/')}/manifest.json"
            if bundle_path_norm != expected_bundle:
                raise ValueError(
                    "nested manifest bundle_path must resolve packaged nested manifest"
                )
        elif artifact_type != "candidate-runtime-capture":
            matched_role = roles.get(str(role_or_nested), [])
            role_paths = {item["path"] for item in matched_role if isinstance(item, dict)}
            if bundle_path_norm not in role_paths:
                raise ValueError(
                    f"approval_scope bundle_path does not map to role {role_or_nested}"
                )
        on_disk = _read_bounded_bytes(_resolve_under_root(root, bundle_path_norm), declared_size)
        if entry.get("content_hash") != _sha256(on_disk):
            raise ValueError("approval_scope entry content_hash mismatch")
        if binding.get("content_sha256") != entry.get("content_hash"):
            raise ValueError("approval_scope entry does not match snapshot binding hash")
    review = scope.get("review_task")
    if not isinstance(review, dict):
        raise ValueError("approval_scope review_task missing")
    task_id = review.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("approval_scope review_task id missing")
    parameters = review.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("approval_scope review_task parameters missing")
    retained_spec_raw = file_raw[_one(roles, "candidate_specification")["path"]]
    _validate_review_task_parameters(
        parameters,
        snapshot=snapshot,
        spec=spec,
        profile_doc=profile_doc,
        retained_spec_raw=retained_spec_raw,
        workflow_id=workflow_id,
        source_sha=source_sha,
    )
    handler_context = {
        "workflow_id": workflow_id,
        "revision": parameters.get("revision_number"),
        "specification_hash": parameters.get("specification_hash"),
        "profile_document_hash": parameters.get("profile_document_hash"),
        "snapshot_fingerprint": snapshot_fingerprint,
        "receipt_scope": CANDIDATE_RECEIPT_SCOPE,
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }
    artifacts_sorted = sorted(
        (entry["artifact_id"], entry["content_hash"])
        for entry in entries
        if isinstance(entry, dict)
    )
    operation_inputs = {
        "parameters": parameters,
        "scope": {
            "workflow_id": workflow_id,
            "task_type": review.get("task_type"),
            "cost_class": review.get("cost_class"),
            "provider": None,
            "artifacts": artifacts_sorted,
            "estimated_cost": 0.0,
            "handler_context": handler_context,
        },
    }
    return _compute_operation_hash(task_id, CANDIDATE_TEST_ONLY_APPROVAL, operation_inputs)


def _validate_test_only_receipt(
    *,
    receipt: dict[str, Any],
    scope: dict[str, Any],
    snapshot: dict[str, Any],
    workflow_id: str,
    snapshot_fingerprint: str,
    expected_op_hash: str,
) -> None:
    _reject_unknown_keys(receipt, _RECEIPT_KEYS, "test_only_receipt")
    review = scope.get("review_task")
    if not isinstance(review, dict):
        raise ValueError("approval_scope review_task missing")
    review_task_id = review.get("id")
    if receipt.get("approval_type") != CANDIDATE_TEST_ONLY_APPROVAL:
        raise ValueError("receipt approval_type invalid")
    if receipt.get("receipt_scope") != CANDIDATE_RECEIPT_SCOPE:
        raise ValueError("receipt_scope mismatch")
    if (
        receipt.get("production_eligible") is not False
        or receipt.get("promotion_eligible") is not False
    ):
        raise ValueError("receipt eligibility flags must be false")
    if receipt.get("workflow_id") != workflow_id:
        raise ValueError("receipt workflow_id mismatch")
    if receipt.get("task_id") != review_task_id:
        raise ValueError("receipt task_id mismatch")
    if receipt.get("approval_status") != "APPROVED":
        raise ValueError("receipt approval_status must be APPROVED")
    if receipt.get("status") != "APPROVED":
        raise ValueError("receipt status must be APPROVED")
    if receipt.get("approval_status") != scope.get("approval_status"):
        raise ValueError("receipt approval_status does not match approval_scope")
    review_exec = receipt.get("review_execution_id")
    if not isinstance(review_exec, str) or not review_exec:
        raise ValueError("receipt review_execution_id missing")
    if review_exec != scope.get("review_execution_id"):
        raise ValueError("receipt review_execution_id does not match approval_scope")
    approval_id = receipt.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise ValueError("receipt approval_id missing")
    if approval_id != scope.get("approval_id"):
        raise ValueError("receipt approval_id does not match approval_scope")
    if receipt.get("snapshot_fingerprint") != snapshot_fingerprint:
        raise ValueError("receipt snapshot_fingerprint mismatch")
    op_hash = receipt.get("approval_operation_hash")
    fingerprint = receipt.get("fingerprint")
    if not isinstance(op_hash, str) or not isinstance(fingerprint, str):
        raise ValueError("receipt approval hash fields are required")
    if op_hash != fingerprint:
        raise ValueError("receipt approval_operation_hash conflicts with fingerprint")
    if op_hash != expected_op_hash:
        raise ValueError("receipt approval_operation_hash mismatch")


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = Path(bundle)
    if _bundle_path_crosses_link(bundle):
        raise ValueError("bundle path crosses a symlink or junction")
    if _path_is_link(bundle):
        raise ValueError("bundle root is a symlink or junction")
    root = bundle.resolve(strict=True)
    _aggregate_bundle_byte_scan(root)
    manifest_path = _resolve_under_root(root, "manifest.json")
    manifest = _parse_strict_json_file(manifest_path, "manifest.json")
    unknown_root = set(manifest.keys()) - _MANIFEST_ROOT_KEYS
    if unknown_root:
        raise ValueError("manifest has unknown keys")
    if manifest.get("schema_version") != "candidate-evidence-0.8.0":
        raise ValueError("unsupported manifest schema")
    workflow_id = manifest.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError("manifest workflow_id missing")
    nested_packaged_path = _manifest_nested_bundle_path(manifest)
    manifest_snapshot_fp = manifest.get("snapshot_fingerprint")
    if not isinstance(manifest_snapshot_fp, str) or not _SHA256.fullmatch(manifest_snapshot_fp):
        raise ValueError("manifest snapshot_fingerprint missing")
    roles, file_raw = _inventory(root, manifest)
    rig_mod = _load_trusted_rig_verifier()

    spec = _object_bytes(
        file_raw[_one(roles, "candidate_specification")["path"]], "specification.json"
    )
    profile_doc = _object_bytes(
        file_raw[_one(roles, "candidate_profile_document")["path"]], "profile_document.json"
    )
    if profile_doc.get("schema_version") != "asset-profile-0.8.0-candidate":
        raise ValueError("profile document schema_version mismatch")
    _validate_profile_document_closed(profile_doc)
    profile_hash = _canonical_doc_hash(profile_doc)
    if profile_hash != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
        raise ValueError("profile document hash does not match reviewed pin")
    if manifest.get("profile_document_hash") != profile_hash:
        raise ValueError("manifest profile_document_hash mismatch")
    spec_fp = _canonical_doc_hash(spec)
    if manifest.get("specification_hash") != spec_fp:
        raise ValueError("manifest specification_hash mismatch")
    _check_specification_against_profile(spec, profile_doc)

    source_sha = _one(roles, "source_glb")["sha256"]
    raw_sha = _one(roles, "raw_glb")["sha256"]
    processed_sha = _one(roles, "processed_glb")["sha256"]
    if source_sha != raw_sha or raw_sha != processed_sha:
        raise ValueError("source/raw/processed GLB SHA chain mismatch")
    if manifest.get("source_glb_sha256") != source_sha:
        raise ValueError("manifest source_glb_sha256 mismatch")
    glb_path = _resolve_under_root(root, _one(roles, "processed_glb")["path"])

    contract = _object_bytes(
        file_raw[_one(roles, "candidate_runtime_contract")["path"]],
        "candidate_runtime_contract.json",
    )
    if _canonical_doc_hash(contract) != PINNED_RUNTIME_CONTRACT_SHA256:
        raise ValueError("candidate runtime contract pin mismatch")
    harness_raw_bytes = file_raw[_one(roles, "reviewed_candidate_harness")["path"]]
    harness_lf, harness_raw = _reviewed_text_sha256(harness_raw_bytes, ".gd")
    if harness_lf != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise ValueError("reviewed candidate harness pin mismatch")

    nested_contract_path = None
    nested_rel = nested_packaged_path
    nested_manifest_path = _resolve_under_root(root, nested_rel) / "manifest.json"
    nested_manifest = _parse_strict_json_file(nested_manifest_path, "nested manifest")
    for entry in nested_manifest.get("files", []):
        if isinstance(entry, dict) and entry.get("role") == "rig_verification_contract":
            nested_contract_path = _resolve_under_root(root, str(nested_rel)) / entry["path"]
    if nested_contract_path is None:
        raise ValueError("nested rig contract missing")
    contract_data = rig_mod._parse_contract(
        _object_bytes(
            _read_bounded_json_bytes(nested_contract_path, "rig contract"),
            "rig contract",
        )
    )

    static_status, findings = _recompute_static_status(
        rig_mod, glb_path=glb_path, spec=spec, profile_doc=profile_doc, contract_data=contract_data
    )
    report = _object_bytes(
        file_raw[_one(roles, "static_validation_report")["path"]], "static_validation_report.json"
    )
    _validate_static_report_semantics(
        rig_mod,
        glb_path=glb_path,
        glb_sha=processed_sha,
        spec=spec,
        profile_doc=profile_doc,
        report=report,
        static_status=static_status,
        recomputed_findings=findings,
    )

    request_digest, runtime_status = _validate_runtime_b(
        roles=roles,
        file_raw=file_raw,
        root=root,
        glb_path=glb_path,
        glb_sha=processed_sha,
        spec=spec,
        contract=contract,
        harness_lf=harness_lf,
        harness_raw=harness_raw,
    )
    _validate_nested_rig(
        rig_mod,
        root=root,
        roles=roles,
        file_raw=file_raw,
        processed_sha=processed_sha,
        nested_packaged_path=nested_packaged_path,
    )

    snapshot = _object_bytes(file_raw[_one(roles, "snapshot")["path"]], "snapshot.json")
    _validate_snapshot_closed(snapshot)
    for key in (
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
    ):
        block = snapshot.get(key)
        if not isinstance(block, dict) or block.get("status") != "COMPLETED":
            raise ValueError(f"snapshot {key} must be COMPLETED for current attempt bindings")
    wrapper_entry = _object_bytes(
        file_raw[_one(roles, "rig_attempt_wrapper")["path"]], "rig_attempt_wrapper.json"
    )
    oracle_exec = snapshot.get("oracle_execution")
    if not isinstance(oracle_exec, dict):
        raise ValueError("snapshot oracle_execution missing")
    if wrapper_entry.get("execution_id") != oracle_exec.get("id"):
        raise ValueError("rig wrapper execution_id does not match snapshot oracle_execution")
    snapshot_fingerprint = _snapshot_fingerprint_from_payload(snapshot)
    if manifest_snapshot_fp != snapshot_fingerprint:
        raise ValueError("manifest snapshot_fingerprint mismatch")

    identity = _object_bytes(
        file_raw[_one(roles, "identity_report")["path"]], "identity_report.json"
    )
    _reject_unknown_keys(identity, _IDENTITY_REPORT_KEYS, "identity_report")
    if identity.get("schema_version") != "candidate-identity-report-0.8.0":
        raise ValueError("identity_report schema_version mismatch")
    if identity.get("raw_sha256") != raw_sha or identity.get("processed_sha256") != processed_sha:
        raise ValueError("identity_report GLB SHA mismatch")
    if identity.get("byte_identity") is not True:
        raise ValueError("identity_report byte_identity must be true")
    identity_exec = snapshot.get("identity_execution")
    if not isinstance(identity_exec, dict):
        raise ValueError("snapshot identity_execution missing")
    if identity.get("execution_id") != identity_exec.get("id"):
        raise ValueError("identity_report execution id mismatch")
    attempt = identity.get("attempt_number")
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("identity_report attempt_number must be a strict integer")
    if attempt != identity_exec.get("attempt_number"):
        raise ValueError("identity_report attempt mismatch")
    identity_binding = next(
        (
            row
            for row in snapshot.get("pre_review_artifact_bindings", [])
            if isinstance(row, dict) and row.get("role") == "candidate-identity-report"
        ),
        None,
    )
    if identity_binding is None:
        raise ValueError("snapshot missing identity report binding")
    if identity_binding.get("content_sha256") != _one(roles, "identity_report")["sha256"]:
        raise ValueError("snapshot identity_report binding hash mismatch")

    _validate_snapshot_crossbindings(
        snapshot=snapshot,
        manifest=manifest,
        roles=roles,
        file_raw=file_raw,
        spec=spec,
        profile_doc=profile_doc,
        workflow_id=workflow_id,
        request_digest=request_digest,
        nested_packaged_path=nested_packaged_path,
        root=root,
    )

    scope = _object_bytes(file_raw[_one(roles, "approval_scope")["path"]], "approval_scope.json")
    receipt = _object_bytes(
        file_raw[_one(roles, "test_only_receipt")["path"]], "test_only_receipt.json"
    )
    extra_ids = scope.get("persisted_approval_artifact_ids", [])
    if any(
        isinstance(aid, str) and ("receipt" in aid.lower() or aid.endswith("-RECEIPT"))
        for aid in extra_ids
    ):
        raise ValueError("expanded historical approval scope with prior receipt is unsupported")
    if len(extra_ids) > PRE_REVIEW_BINDING_COUNT:
        raise ValueError("expanded historical approval scope is unsupported")
    expected_op_hash = _validate_approval_scope(
        scope=scope,
        snapshot=snapshot,
        roles=roles,
        file_raw=file_raw,
        root=root,
        workflow_id=workflow_id,
        snapshot_fingerprint=snapshot_fingerprint,
        nested_packaged_path=nested_packaged_path,
        spec=spec,
        profile_doc=profile_doc,
        source_sha=source_sha,
    )
    _validate_test_only_receipt(
        receipt=receipt,
        scope=scope,
        snapshot=snapshot,
        workflow_id=workflow_id,
        snapshot_fingerprint=snapshot_fingerprint,
        expected_op_hash=expected_op_hash,
    )
    if scope.get("operation_hash") not in {None, expected_op_hash}:
        raise ValueError("approval_scope operation_hash mismatch")

    if manifest.get("validation_status") != static_status:
        raise ValueError("manifest validation_status mismatch")
    if manifest.get("runtime_status") != runtime_status:
        raise ValueError("manifest runtime_status mismatch")
    if manifest.get("reviewed_pins_match") is not True:
        raise ValueError("manifest reviewed_pins_match must be true")

    return {
        "outcome": "CONSISTENT_BUT_UNAUTHENTICATED",
        "integrity_outcome": "VERIFIED",
        "validation_status": static_status,
        "runtime_status": runtime_status,
        "execution_provenance": "CONSISTENT_BUT_UNAUTHENTICATED",
        "reviewed_pins_match": True,
        "candidate_evidence_complete": True,
        "production_eligible": False,
        "promotion_eligible": False,
        "cold_attestation_limits": (
            "Cold verification cannot attest live database state, authentic human approval, "
            "or current execution authenticity."
        ),
        "request_digest": request_digest,
        "verified_files": len(manifest.get("files", [])),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -I verify_candidate_bundle.py BUNDLE", file=sys.stderr)
        return 2
    try:
        result = verify_bundle(Path(args[0]))
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ) as exc:
        print("FAILED")
        print(json.dumps({"outcome": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 1
    label = str(result.get("outcome", "FAILED"))
    print(label)
    print(json.dumps(result, sort_keys=True))
    return 0 if label in {"VERIFIED", "CONSISTENT_BUT_UNAUTHENTICATED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
