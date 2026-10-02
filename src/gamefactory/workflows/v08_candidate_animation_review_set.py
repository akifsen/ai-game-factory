"""V0.8-8 multi-clip animation review set export, currentness, and expected payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.animation_clip_input import (
    MAX_LOCAL_ANIMATION_CLIP_BYTES,
    load_local_animation_clip,
)
from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    parse_bounded_publication_json,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.animation_clip import parse_animation_clip_document
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_PACKAGE_FILES,
    _MAX_CLIP_PACKAGE_TOTAL_BYTES,
    _UPSTREAM_PREVIEW_FILES,
    _read_bounded_package_file,
    animation_clip_preview_current,
)
from gamefactory.workflows.v08_candidate_animation_review import (
    _animation_package_root_digest,
    _parse_gated_v086_clip_manifest,
    _read_v086_package_files,
    _review_controller_bytes,
)
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    assert_zero_provider_activity,
)
from gamefactory.workflows.v08_candidate_preview import (
    PREVIEW_STATUS_TEST_ONLY,
    _assert_destination_fresh,
    _assert_same_volume,
    _canonical_json_bytes,
    _discard_owned_preview_stage,
    _reject_preview_lexical,
    _validate_output_dir,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

ANIMATION_REVIEW_SET_MANIFEST_SCHEMA = "candidate-character-animation-review-set-manifest-0.8.0"
ANIMATION_REVIEW_SET_STATUS_TEST_ONLY = PREVIEW_STATUS_TEST_ONLY

MIN_REVIEW_SET_SOURCES = 2
MAX_REVIEW_SET_SOURCES = 8

_MAX_REVIEW_SET_MANIFEST_BYTES = 64 * 1024
_MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_REVIEW_SET_PER_CLIP_RAW_BYTES = MAX_LOCAL_ANIMATION_CLIP_BYTES

_REVIEW_SET_CANONICAL_GD_BASE: frozenset[str] = frozenset(
    {
        "animation_clip_preview_player.gd",
        "animation_review_controller.gd",
    }
)
_REVIEW_SET_CANONICAL_GD_NEW: frozenset[str] = frozenset(
    {
        "animation_review_set_player.gd",
        "animation_review_set_controller.gd",
        "animation_review_set.tscn",
    }
)
_REVIEW_SET_ROOT_FILES: frozenset[str] = frozenset(
    _UPSTREAM_PREVIEW_FILES
    | _REVIEW_SET_CANONICAL_GD_BASE
    | _REVIEW_SET_CANONICAL_GD_NEW
    | {
        "animation_review_set_preview.tscn",
        "animation_review_set_manifest.json",
        "project.godot",
    }
)
_REVIEW_SET_ROOT_DIGEST_FILES: frozenset[str] = frozenset(
    _REVIEW_SET_ROOT_FILES - {"animation_review_set_manifest.json"}
)
_REVIEW_SET_CLIP_SLOT_FILES: frozenset[str] = frozenset(
    {"animation_clip.json", "animation_clip_manifest.json"}
)
_REVIEW_SET_ROOT_INVENTORY_ENTRIES = len(_REVIEW_SET_ROOT_FILES | {"clips"})
_MAX_REVIEW_SET_CLIP_SLOT_INVENTORY = MAX_REVIEW_SET_SOURCES
_MAX_REVIEW_SET_CLIP_SLOT_FILE_INVENTORY = len(_REVIEW_SET_CLIP_SLOT_FILES)

_ORDERED_CLIP_ENTRY_KEYS = frozenset(
    {
        "clip_id",
        "raw_clip_sha256",
        "raw_original_manifest_sha256",
        "source_package_sha256",
        "source_file_digests",
    }
)
_REVIEW_SET_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "evidence_task_id",
        "evidence_execution_id",
        "evidence_attempt_number",
        "manifest_artifact_id",
        "manifest_artifact_sha256",
        "result_artifact_id",
        "result_artifact_sha256",
        "marker_artifact_id",
        "marker_artifact_sha256",
        "snapshot_fingerprint",
        "spec_fingerprint",
        "processed_glb_sha256",
        "upstream_preview_manifest_sha256",
        "shared_preview_identity_digest",
        "ordered_clips",
        "file_digests",
        "root_payload_digest",
        "nested_clips_payload_digest",
        "animation_review_set_status",
        "production_eligible",
        "promotion_eligible",
    }
)

_V086_PREVIEW_TSCN_BONE_RE = re.compile(
    r"verified_weighted_bone_names\s*=\s*PackedStringArray\((.*?)\)",
    re.DOTALL,
)
_V086_PREVIEW_TSCN_CLIP_SHA_RE = re.compile(
    r'expected_clip_sha256\s*=\s*"([0-9a-f]{64})"',
)

_CROSS_SOURCE_MANIFEST_IDENTITY_FIELDS = (
    "workflow_id",
    "evidence_task_id",
    "snapshot_fingerprint",
    "spec_fingerprint",
    "evidence_execution_id",
    "evidence_attempt_number",
    "manifest_artifact_id",
    "manifest_artifact_sha256",
    "result_artifact_id",
    "result_artifact_sha256",
    "marker_artifact_id",
    "marker_artifact_sha256",
    "processed_glb_sha256",
    "upstream_preview_manifest_sha256",
)


@dataclass(frozen=True)
class CandidateAnimationReviewSetSource:
    """One V0.8-6 animation clip preview package plus its authoritative local clip path."""

    animation_dir: Path | str
    clip_path: Path | str


@dataclass(frozen=True)
class CapturedAnimationReviewSetSource:
    """Gated snapshot of one review-set source package and clip bindings."""

    source: CandidateAnimationReviewSetSource
    slot_index: int
    clip_id: str
    v086_bytes: Mapping[str, bytes]
    clip_manifest_doc: Mapping[str, Any]
    verified_weighted_bone_names: tuple[str, ...]


@dataclass(frozen=True)
class ExpectedAnimationReviewSetPayload:
    """Deterministic root and nested clip bytes for a validated review-set capture."""

    root_files: Mapping[str, bytes]
    clip_files: Mapping[str, bytes]


@dataclass(frozen=True)
class CandidateAnimationReviewSetResult:
    """Typed animation review set export outcome; never production- or promotion-eligible."""

    workflow_id: str
    review_set_root: str
    manifest_sha256: str
    ordered_clip_ids: tuple[str, ...]
    processed_glb_sha256: str
    production_eligible: bool = False
    promotion_eligible: bool = False
    animation_review_set_status: str = ANIMATION_REVIEW_SET_STATUS_TEST_ONLY


@dataclass(frozen=True)
class _ReviewSetDirectorySnapshot:
    manifest_doc: dict[str, Any]
    root_files: dict[str, bytes]
    clip_files: dict[str, bytes]


def _intern_identical_file_bytes(
    v086_bytes: Mapping[str, bytes],
    intern: dict[str, bytes],
) -> dict[str, bytes]:
    """Reuse identical payload objects within one capture batch to limit memory amplification."""
    deduped: dict[str, bytes] = {}
    for name, payload in v086_bytes.items():
        digest = hashlib.sha256(payload).hexdigest()
        cached = intern.get(digest)
        if cached is not None and cached == payload:
            deduped[name] = cached
        else:
            intern[digest] = payload
            deduped[name] = payload
    return deduped


def _bounded_directory_entry_names(
    directory: Path,
    *,
    expected_count: int,
    label: str,
) -> frozenset[str]:
    names: set[str] = set()
    for entry in directory.iterdir():
        names.add(entry.name)
        if len(names) > expected_count:
            raise ValidationError(f"animation review set {label} inventory exceeds bound")
    return frozenset(names)


def _clip_slot_directory(slot_index: int) -> str:
    if slot_index < 0 or slot_index >= MAX_REVIEW_SET_SOURCES:
        raise ValidationError("review set clip slot index out of range")
    return f"clips/{slot_index:03d}"


def _player_script_bytes() -> bytes:
    raw = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("animation_clip_preview_player.gd")
        .read_bytes()
    )
    return raw.replace(b"\r\n", b"\n")


def _optional_packaged_resource_bytes(name: str) -> bytes | None:
    try:
        raw = resources.files("gamefactory.resources.v08_candidate").joinpath(name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return raw.replace(b"\r\n", b"\n")


def _require_packaged_resource_bytes(name: str) -> bytes:
    payload = _optional_packaged_resource_bytes(name)
    if payload is None:
        raise ArtifactError(f"packaged review set resource {name} is not available")
    return payload


def trusted_animation_review_set_ui_digests() -> dict[str, str]:
    """SHA-256 digests for all packaged review-set UI resources (LF-normalized)."""
    digests: dict[str, str] = {}
    for name in sorted(_REVIEW_SET_CANONICAL_GD_NEW):
        payload = _require_packaged_resource_bytes(name)
        digests[name] = hashlib.sha256(payload).hexdigest()
    return digests


def _parse_verified_weighted_bone_names_from_preview_tscn(tscn_bytes: bytes) -> tuple[str, ...]:
    text = tscn_bytes.decode("utf-8")
    match = _V086_PREVIEW_TSCN_BONE_RE.search(text)
    if match is None:
        raise ValidationError("animation clip preview scene missing verified_weighted_bone_names")
    inner = match.group(1).strip()
    if not inner:
        return ()
    try:
        parsed = json.loads(f"[{inner}]")
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "animation clip preview scene verified_weighted_bone_names is not a valid string array"
        ) from exc
    if not isinstance(parsed, list):
        raise ValidationError(
            "animation clip preview scene verified_weighted_bone_names is not a valid string array"
        )
    names: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str) or not item:
            raise ValidationError(
                "animation clip preview scene verified_weighted_bone_names entries must be nonempty strings"
            )
        if item in seen:
            raise ValidationError(
                "animation clip preview scene verified_weighted_bone_names contains duplicates"
            )
        seen.add(item)
        names.append(item)
    return tuple(names)


def _assert_captured_review_set_collection(
    captured: Sequence[CapturedAnimationReviewSetSource],
) -> None:
    count = len(captured)
    if count < MIN_REVIEW_SET_SOURCES or count > MAX_REVIEW_SET_SOURCES:
        raise ValidationError(
            f"review set requires between {MIN_REVIEW_SET_SOURCES} and {MAX_REVIEW_SET_SOURCES} captured sources"
        )
    seen_clip_ids: set[str] = set()
    for expected_slot, item in enumerate(captured):
        if item.slot_index != expected_slot:
            raise ValidationError("review set captured slot_index must match enumeration order")
        if item.clip_id in seen_clip_ids:
            raise ValidationError(f"review set clip_id {item.clip_id} is duplicated")
        seen_clip_ids.add(item.clip_id)


def _format_godot_packed_string_array(values: Sequence[str]) -> str:
    return ", ".join(json.dumps(value) for value in values)


def _source_file_digests(v086_bytes: Mapping[str, bytes]) -> dict[str, str]:
    missing = sorted(_CLIP_PACKAGE_FILES - set(v086_bytes))
    if missing:
        raise ValidationError(f"V0.8-6 source package missing files: {missing}")
    return {
        name: hashlib.sha256(v086_bytes[name]).hexdigest() for name in sorted(_CLIP_PACKAGE_FILES)
    }


def _raw_original_manifest_sha256(v086_bytes: Mapping[str, bytes]) -> str:
    manifest = v086_bytes.get("animation_clip_manifest.json")
    if manifest is None:
        raise ValidationError("animation clip manifest is missing from source package")
    return hashlib.sha256(manifest).hexdigest()


def _clip_manifest_identity_doc(clip_manifest_doc: Mapping[str, Any]) -> dict[str, Any]:
    identities: dict[str, Any] = {}
    for key in _CROSS_SOURCE_MANIFEST_IDENTITY_FIELDS:
        value = clip_manifest_doc.get(key)
        if key == "evidence_attempt_number":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(
                    "animation clip manifest evidence_attempt_number must be an integer"
                )
        elif not isinstance(value, str):
            raise ValidationError(f"animation clip manifest {key} must be a string")
        identities[key] = value
    return identities


def _shared_upstream_five_bytes(v086_bytes: Mapping[str, bytes]) -> dict[str, bytes]:
    return {name: v086_bytes[name] for name in sorted(_UPSTREAM_PREVIEW_FILES)}


def compute_shared_preview_identity_digest(
    v086_bytes: Mapping[str, bytes],
    clip_manifest_doc: Mapping[str, Any],
) -> str:
    """Digest of shared V0.8-4 bytes, canonical V0.8-6 base player/project bytes, and identities."""
    shared_five = _shared_upstream_five_bytes(v086_bytes)
    project = v086_bytes.get("project.godot")
    player = v086_bytes.get("animation_clip_preview_player.gd")
    if project is None or player is None:
        raise ValidationError(
            "source package missing project.godot or animation_clip_preview_player.gd"
        )
    preview_tscn = v086_bytes.get("animation_clip_preview.tscn")
    if preview_tscn is None:
        raise ValidationError("source package missing animation_clip_preview.tscn")
    bone_names = _parse_verified_weighted_bone_names_from_preview_tscn(preview_tscn)
    canonical = json.dumps(
        {
            "shared_preview_file_digests": {
                name: hashlib.sha256(shared_five[name]).hexdigest()
                for name in sorted(_UPSTREAM_PREVIEW_FILES)
            },
            "source_project_godot_sha256": hashlib.sha256(project).hexdigest(),
            "source_preview_player_sha256": hashlib.sha256(player).hexdigest(),
            "verified_weighted_bone_names": list(bone_names),
            "clip_manifest_identity": _clip_manifest_identity_doc(clip_manifest_doc),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_review_set_sources(
    sources: Sequence[CandidateAnimationReviewSetSource],
) -> tuple[str, ...]:
    """Validate caller-ordered sources (2..8 unique strict clip_ids); return ordered clip_ids."""
    count = len(sources)
    if count < MIN_REVIEW_SET_SOURCES or count > MAX_REVIEW_SET_SOURCES:
        raise ValidationError(
            f"review set requires between {MIN_REVIEW_SET_SOURCES} and {MAX_REVIEW_SET_SOURCES} sources"
        )
    clip_ids: list[str] = []
    seen: set[str] = set()
    for index, source in enumerate(sources):
        clip_path = Path(source.clip_path)
        _reject_preview_lexical(clip_path.absolute(), label="review set source clip path")
        animation_dir = Path(source.animation_dir)
        _reject_preview_lexical(
            animation_dir.absolute(), label="review set source animation directory"
        )
        try:
            loaded = load_local_animation_clip(clip_path)
        except (OSError, ValidationError) as exc:
            raise ValidationError(
                f"review set source clip {index} is not a valid local clip"
            ) from exc
        if len(loaded.raw_bytes) > _MAX_REVIEW_SET_PER_CLIP_RAW_BYTES:
            raise ValidationError(f"review set source clip {index} exceeds per-clip raw byte limit")
        clip_id = loaded.clip.clip_id
        if clip_id in seen:
            raise ValidationError(f"review set clip_id {clip_id} is duplicated")
        seen.add(clip_id)
        clip_ids.append(clip_id)
    return tuple(clip_ids)


def _assert_gated_source_clip_matches_package(
    v086_bytes: Mapping[str, bytes],
    clip_manifest_doc: Mapping[str, Any],
    clip_path: Path,
) -> str:
    clip_sha = clip_manifest_doc.get("clip_sha256")
    if not isinstance(clip_sha, str):
        raise ValidationError("animation clip manifest clip_sha256 must be a string")
    loaded = load_local_animation_clip(clip_path)
    if loaded.sha256 != clip_sha:
        raise ValidationError("source clip digest does not match gated animation clip manifest")
    packaged_clip = v086_bytes.get("animation_clip.json")
    if packaged_clip is None:
        raise ValidationError("source package animation_clip.json is missing")
    if hashlib.sha256(packaged_clip).hexdigest() != clip_sha:
        raise ValidationError("gated source package animation_clip.json digest mismatch")
    if packaged_clip != loaded.raw_bytes:
        raise ValidationError("gated source package animation_clip.json bytes mismatch")
    try:
        parsed_doc = parse_animation_clip_document(json.loads(packaged_clip.decode("utf-8")))
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValidationError(
            "gated source package animation_clip.json is not a valid clip"
        ) from exc
    if parsed_doc.clip_id != loaded.clip.clip_id:
        raise ValidationError("strict parsed clip_id mismatch")
    preview_tscn = v086_bytes["animation_clip_preview.tscn"]
    sha_match = _V086_PREVIEW_TSCN_CLIP_SHA_RE.search(preview_tscn.decode("utf-8"))
    if sha_match is None or sha_match.group(1) != clip_sha:
        raise ValidationError("source animation_clip_preview.tscn clip binding mismatch")
    return loaded.clip.clip_id


def capture_animation_review_set_source(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    source: CandidateAnimationReviewSetSource,
    *,
    slot_index: int,
    bytes_intern: dict[str, bytes] | None = None,
) -> CapturedAnimationReviewSetSource:
    """Gate one source with a single animation_clip_preview_current authority call."""
    preview_root = Path(preview_dir)
    animation_root = Path(source.animation_dir)
    clip_path = Path(source.clip_path)
    try:
        v086_before = _read_v086_package_files(animation_root)
    except OSError as exc:
        raise CandidateCurrentnessError(
            "review set source V0.8-6 animation clip preview is missing or unreadable"
        ) from exc
    if not animation_clip_preview_current(
        handlers, workflow_id, preview_root, clip_path, animation_root
    ):
        raise CandidateCurrentnessError(
            "review set source V0.8-6 animation clip preview is not current"
        )
    try:
        v086_after = _read_v086_package_files(animation_root)
    except OSError as exc:
        raise CandidateCurrentnessError(
            "review set source V0.8-6 package changed during capture gate"
        ) from exc
    if v086_after != v086_before:
        raise CandidateCurrentnessError(
            "review set source V0.8-6 package bytes drifted during capture gate"
        )
    v086_gated = dict(v086_after)
    if bytes_intern is not None:
        v086_gated = _intern_identical_file_bytes(v086_gated, bytes_intern)
    clip_manifest_doc = _parse_gated_v086_clip_manifest(v086_gated)
    manifest_workflow_id = clip_manifest_doc.get("workflow_id")
    if manifest_workflow_id != workflow_id:
        raise ValidationError(
            "animation clip manifest workflow_id does not match supplied workflow_id"
        )
    clip_id = _assert_gated_source_clip_matches_package(v086_gated, clip_manifest_doc, clip_path)
    preview_tscn = v086_gated["animation_clip_preview.tscn"]
    bone_names = _parse_verified_weighted_bone_names_from_preview_tscn(preview_tscn)
    return CapturedAnimationReviewSetSource(
        source=source,
        slot_index=slot_index,
        clip_id=clip_id,
        v086_bytes=v086_gated,
        clip_manifest_doc=clip_manifest_doc,
        verified_weighted_bone_names=bone_names,
    )


def assert_review_set_shared_identity_before_prepare(
    captured: Sequence[CapturedAnimationReviewSetSource],
) -> str:
    """Require identical cross-source shared preview identity; return the canonical digest."""
    if not captured:
        raise ValidationError("review set capture is empty")
    reference = captured[0]
    ref_five = _shared_upstream_five_bytes(reference.v086_bytes)
    ref_project = reference.v086_bytes["project.godot"]
    ref_player = reference.v086_bytes["animation_clip_preview_player.gd"]
    ref_bones = reference.verified_weighted_bone_names
    ref_identity = _clip_manifest_identity_doc(reference.clip_manifest_doc)
    ref_digest = compute_shared_preview_identity_digest(
        reference.v086_bytes, reference.clip_manifest_doc
    )
    for item in captured[1:]:
        if _shared_upstream_five_bytes(item.v086_bytes) != ref_five:
            raise ValidationError("review set sources disagree on shared V0.8-4 preview bytes")
        if item.v086_bytes["project.godot"] != ref_project:
            raise ValidationError("review set sources disagree on source project.godot bytes")
        if item.v086_bytes["animation_clip_preview_player.gd"] != ref_player:
            raise ValidationError(
                "review set sources disagree on animation_clip_preview_player.gd bytes"
            )
        if item.verified_weighted_bone_names != ref_bones:
            raise ValidationError(
                "review set sources disagree on verified weighted bone capability"
            )
        identity = _clip_manifest_identity_doc(item.clip_manifest_doc)
        if identity != ref_identity:
            raise ValidationError("review set sources disagree on workflow evidence identity")
        digest = compute_shared_preview_identity_digest(item.v086_bytes, item.clip_manifest_doc)
        if digest != ref_digest:
            raise ValidationError("review set sources disagree on shared preview identity digest")
    return ref_digest


def capture_animation_review_set_sources(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    sources: Sequence[CandidateAnimationReviewSetSource],
) -> tuple[CapturedAnimationReviewSetSource, ...]:
    """Validate, gate, and capture all review-set sources in caller order."""
    validate_review_set_sources(sources)
    bytes_intern: dict[str, bytes] = {}
    captured: list[CapturedAnimationReviewSetSource] = []
    for slot_index, source in enumerate(sources):
        captured.append(
            capture_animation_review_set_source(
                handlers,
                workflow_id,
                preview_dir,
                source,
                slot_index=slot_index,
                bytes_intern=bytes_intern,
            )
        )
    _assert_captured_review_set_collection(captured)
    assert_review_set_shared_identity_before_prepare(captured)
    return tuple(captured)


def _build_ordered_clip_manifest_entry(
    captured: CapturedAnimationReviewSetSource,
) -> dict[str, Any]:
    v086 = captured.v086_bytes
    clip_manifest = captured.clip_manifest_doc
    clip_sha = clip_manifest.get("clip_sha256")
    if not isinstance(clip_sha, str):
        raise ValidationError("animation clip manifest clip_sha256 must be a string")
    return {
        "clip_id": captured.clip_id,
        "raw_clip_sha256": clip_sha,
        "raw_original_manifest_sha256": _raw_original_manifest_sha256(v086),
        "source_package_sha256": _animation_package_root_digest(dict(v086)),
        "source_file_digests": _source_file_digests(v086),
    }


def _nested_clips_payload_digest(clip_file_digests: Mapping[str, str]) -> str:
    ordered_paths = sorted(clip_file_digests)
    canonical = json.dumps(
        {path: clip_file_digests[path] for path in ordered_paths},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _root_payload_digest(file_digests: Mapping[str, str]) -> str:
    canonical = json.dumps(
        {name: file_digests[name] for name in sorted(file_digests)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_animation_review_set_project_godot() -> bytes:
    text = (
        "config_version=5\n\n"
        "[application]\n"
        'config/name="CandidateCharacterAnimationReviewSet"\n'
        'run/main_scene="res://animation_review_set.tscn"\n\n'
        "[rendering]\n"
        'renderer/rendering_method="gl_compatibility"\n'
    )
    return text.encode("utf-8")


def _build_animation_review_set_preview_tscn(
    clip_paths: Sequence[str],
    clip_sha256s: Sequence[str],
    clip_ids: Sequence[str],
    verified_weighted_bone_names: tuple[str, ...],
) -> bytes:
    count = len(clip_paths)
    if count != len(clip_sha256s) or count != len(clip_ids):
        raise ValidationError("review set preview clip arrays length mismatch")
    for clip_path in clip_paths:
        if not isinstance(clip_path, str) or not clip_path.startswith("res://"):
            raise ValidationError("review set preview clip_paths entries must be res:// strings")
    for clip_sha in clip_sha256s:
        if not isinstance(clip_sha, str) or re.fullmatch(r"[0-9a-f]{64}", clip_sha) is None:
            raise ValidationError(
                "review set preview clip_sha256s entries must be lowercase sha256 hex"
            )
    for clip_id in clip_ids:
        if not isinstance(clip_id, str) or not clip_id:
            raise ValidationError("review set preview clip_ids entries must be nonempty strings")
    bones_literal = _format_godot_packed_string_array(verified_weighted_bone_names)
    paths_literal = _format_godot_packed_string_array(clip_paths)
    sha_literal = _format_godot_packed_string_array(clip_sha256s)
    ids_literal = _format_godot_packed_string_array(clip_ids)
    text = (
        "[gd_scene load_steps=3 format=3]\n\n"
        '[ext_resource type="PackedScene" path="res://character.tscn" id="1_character"]\n'
        '[ext_resource type="Script" path="res://animation_review_set_player.gd" id="2_player"]\n\n'
        '[node name="AnimationReviewSetPreview" type="Node3D"]\n\n'
        '[node name="Character" parent="." instance=ExtResource("1_character")]\n\n'
        '[node name="AnimationPlayer" type="AnimationPlayer" parent="."]\n'
        'script = ExtResource("2_player")\n'
        f"clip_paths = PackedStringArray({paths_literal})\n"
        f"clip_sha256s = PackedStringArray({sha_literal})\n"
        f"clip_ids = PackedStringArray({ids_literal})\n"
        f"verified_weighted_bone_names = PackedStringArray({bones_literal})\n"
    )
    return text.encode("utf-8")


def build_expected_animation_review_set_payload(
    captured: Sequence[CapturedAnimationReviewSetSource],
    *,
    workflow_id: str,
) -> ExpectedAnimationReviewSetPayload:
    """Build deterministic root and nested clip bytes from gated captures."""
    _assert_captured_review_set_collection(captured)
    shared_identity = assert_review_set_shared_identity_before_prepare(captured)
    reference = captured[0]
    root: dict[str, bytes] = {}
    for name in sorted(_UPSTREAM_PREVIEW_FILES):
        root[name] = reference.v086_bytes[name]
    root["animation_clip_preview_player.gd"] = _player_script_bytes()
    root["animation_review_controller.gd"] = _review_controller_bytes()
    for name in sorted(_REVIEW_SET_CANONICAL_GD_NEW):
        root[name] = _require_packaged_resource_bytes(name)
    root["project.godot"] = _build_animation_review_set_project_godot()
    clip_paths: list[str] = []
    clip_sha256s: list[str] = []
    clip_ids: list[str] = []
    clip_files: dict[str, bytes] = {}
    total_bytes = 0
    for item in captured:
        slot = _clip_slot_directory(item.slot_index)
        rel_json = f"{slot}/animation_clip.json"
        rel_manifest = f"{slot}/animation_clip_manifest.json"
        clip_raw = item.v086_bytes["animation_clip.json"]
        manifest_raw = item.v086_bytes["animation_clip_manifest.json"]
        if len(clip_raw) > _MAX_REVIEW_SET_PER_CLIP_RAW_BYTES:
            raise ValidationError("review set nested clip exceeds per-clip raw byte limit")
        if len(manifest_raw) > _MAX_REVIEW_SET_MANIFEST_BYTES:
            raise ValidationError("review set nested clip manifest exceeds byte limit")
        clip_files[rel_json] = clip_raw
        clip_files[rel_manifest] = manifest_raw
        clip_sha = item.clip_manifest_doc.get("clip_sha256")
        if not isinstance(clip_sha, str):
            raise ValidationError("animation clip manifest clip_sha256 must be a string")
        clip_paths.append(f"res://{rel_json}")
        clip_sha256s.append(clip_sha)
        clip_ids.append(item.clip_id)
        total_bytes += len(clip_raw) + len(manifest_raw)
    root["animation_review_set_preview.tscn"] = _build_animation_review_set_preview_tscn(
        clip_paths,
        clip_sha256s,
        clip_ids,
        reference.verified_weighted_bone_names,
    )
    for payload in root.values():
        total_bytes += len(payload)
    if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
        raise ValidationError("review set payload exceeds total byte limit")
    if total_bytes > _MAX_CLIP_PACKAGE_TOTAL_BYTES:
        raise ValidationError("review set root payload exceeds package byte limit")
    manifest = build_expected_animation_review_set_manifest(
        captured,
        workflow_id=workflow_id,
        shared_preview_identity_digest=shared_identity,
        root_files=root,
        clip_files=clip_files,
    )
    _validate_review_set_manifest_shape(manifest)
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > _MAX_REVIEW_SET_MANIFEST_BYTES:
        raise ValidationError("review set manifest exceeds byte limit")
    root["animation_review_set_manifest.json"] = manifest_bytes
    total_bytes += len(manifest_bytes)
    if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
        raise ValidationError("review set payload exceeds total byte limit")
    return ExpectedAnimationReviewSetPayload(root_files=root, clip_files=clip_files)


def build_expected_animation_review_set_manifest(
    captured: Sequence[CapturedAnimationReviewSetSource],
    *,
    workflow_id: str,
    shared_preview_identity_digest: str,
    root_files: Mapping[str, bytes],
    clip_files: Mapping[str, bytes],
) -> dict[str, Any]:
    """Build the closed review-set manifest document (excludes self from file_digests)."""
    if not captured:
        raise ValidationError("review set manifest requires captured sources")
    _assert_captured_review_set_collection(captured)
    reference_manifest = captured[0].clip_manifest_doc
    identities = _clip_manifest_identity_doc(reference_manifest)
    ordered_clips: list[dict[str, Any]] = []
    for item in captured:
        entry = _build_ordered_clip_manifest_entry(item)
        if set(entry) != _ORDERED_CLIP_ENTRY_KEYS:
            raise ArtifactError("ordered clip manifest entry keys mismatch")
        ordered_clips.append(entry)
    file_digests: dict[str, str] = {}
    for name in sorted(_REVIEW_SET_ROOT_DIGEST_FILES):
        payload = root_files.get(name)
        if payload is None:
            raise ValidationError(f"review set root payload missing {name}")
        file_digests[name] = hashlib.sha256(payload).hexdigest()
    clip_file_digests = {
        path: hashlib.sha256(clip_files[path]).hexdigest() for path in sorted(clip_files)
    }
    for rel_path in clip_file_digests:
        parts = Path(rel_path).parts
        if len(parts) != 3 or parts[0] != "clips" or parts[2] not in _REVIEW_SET_CLIP_SLOT_FILES:
            raise ValidationError("review set nested clip path is invalid")
    if identities["workflow_id"] != workflow_id:
        raise ValidationError("review set manifest workflow_id does not match captured sources")
    return {
        "schema_version": ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
        "workflow_id": workflow_id,
        "evidence_task_id": identities["evidence_task_id"],
        "evidence_execution_id": identities["evidence_execution_id"],
        "evidence_attempt_number": identities["evidence_attempt_number"],
        "manifest_artifact_id": identities["manifest_artifact_id"],
        "manifest_artifact_sha256": identities["manifest_artifact_sha256"],
        "result_artifact_id": identities["result_artifact_id"],
        "result_artifact_sha256": identities["result_artifact_sha256"],
        "marker_artifact_id": identities["marker_artifact_id"],
        "marker_artifact_sha256": identities["marker_artifact_sha256"],
        "snapshot_fingerprint": identities["snapshot_fingerprint"],
        "spec_fingerprint": identities["spec_fingerprint"],
        "processed_glb_sha256": identities["processed_glb_sha256"],
        "upstream_preview_manifest_sha256": identities["upstream_preview_manifest_sha256"],
        "shared_preview_identity_digest": shared_preview_identity_digest,
        "ordered_clips": ordered_clips,
        "file_digests": dict(sorted(file_digests.items())),
        "root_payload_digest": _root_payload_digest(file_digests),
        "nested_clips_payload_digest": _nested_clips_payload_digest(clip_file_digests),
        "animation_review_set_status": ANIMATION_REVIEW_SET_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _validate_review_set_manifest_shape(manifest_doc: Mapping[str, Any]) -> None:
    if set(manifest_doc) != _REVIEW_SET_MANIFEST_KEYS:
        raise ValidationError("animation review set manifest fields do not match the contract")
    if manifest_doc.get("production_eligible") is not False:
        raise ValidationError("animation review set manifest production_eligible must be false")
    if manifest_doc.get("promotion_eligible") is not False:
        raise ValidationError("animation review set manifest promotion_eligible must be false")
    ordered_clips = manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    seen_clip_ids: set[str] = set()
    for entry in ordered_clips:
        if not isinstance(entry, dict) or set(entry) != _ORDERED_CLIP_ENTRY_KEYS:
            raise ValidationError("animation review set ordered clip entry is invalid")
        clip_id = entry.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise ValidationError("animation review set ordered clip entry is invalid")
        if clip_id in seen_clip_ids:
            raise ValidationError(f"review set clip_id {clip_id} is duplicated")
        seen_clip_ids.add(clip_id)


def _expected_ordered_clip_manifest_entries(
    captured: Sequence[CapturedAnimationReviewSetSource],
) -> list[dict[str, Any]]:
    return [_build_ordered_clip_manifest_entry(item) for item in captured]


def _manifest_ordered_clips_semantically_match(
    manifest_doc: Mapping[str, Any],
    captured: Sequence[CapturedAnimationReviewSetSource],
) -> bool:
    ordered_clips = manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        return False
    try:
        expected_entries = _expected_ordered_clip_manifest_entries(captured)
    except ValidationError:
        return False
    return ordered_clips == expected_entries


def _review_set_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation review set publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation review set publish parent")
    return publish_resolved / ".gf" / "candidate_animation_review_set_staging"


def _fresh_review_set_stage(publish_parent: Path, project_root: Path) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation review set publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="animation review set project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation review set publish parent")
    _reject_preview_lexical(project_resolved, label="animation review set project root")

    staging_parent_lexical = _review_set_staging_parent(publish_resolved)
    _reject_preview_lexical(staging_parent_lexical, label="animation review set staging parent")
    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="animation review set staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="animation review set staging container")
    if stage_lexical.exists():
        raise ArtifactError("animation review set staging collision")
    stage_lexical.mkdir(parents=True, exist_ok=False)
    stage = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage, label="animation review set staging container")
    _assert_same_volume(stage, publish_resolved)
    if path_crosses_link(stage):
        raise ValidationError("animation review set staging container crosses a link")
    return stage, staging_parent


def _assert_captured_snapshots_unchanged(
    baseline: Sequence[CapturedAnimationReviewSetSource],
    live: Sequence[CapturedAnimationReviewSetSource],
    *,
    detail: str,
) -> None:
    if len(baseline) != len(live):
        raise CandidateCurrentnessError(detail)
    for base, current in zip(baseline, live, strict=True):
        if base.slot_index != current.slot_index or base.clip_id != current.clip_id:
            raise CandidateCurrentnessError(detail)
        if dict(base.v086_bytes) != dict(current.v086_bytes):
            raise CandidateCurrentnessError(detail)
        if base.verified_weighted_bone_names != current.verified_weighted_bone_names:
            raise CandidateCurrentnessError(detail)


def _pin_captured_sources_to_live_disk(
    captured: Sequence[CapturedAnimationReviewSetSource],
) -> bool:
    for item in captured:
        animation_root = Path(item.source.animation_dir)
        try:
            live_v086 = _read_v086_package_files(animation_root)
        except (OSError, ValidationError):
            return False
        if live_v086 != dict(item.v086_bytes):
            return False
        clip_path = Path(item.source.clip_path)
        try:
            loaded = load_local_animation_clip(clip_path)
        except (OSError, ValidationError):
            return False
        if len(loaded.raw_bytes) > _MAX_REVIEW_SET_PER_CLIP_RAW_BYTES:
            return False
        packaged_clip = item.v086_bytes["animation_clip.json"]
        if loaded.raw_bytes != packaged_clip:
            return False
    return True


def _require_pin_captured_sources(
    captured: Sequence[CapturedAnimationReviewSetSource],
    *,
    detail: str,
) -> None:
    if not _pin_captured_sources_to_live_disk(captured):
        raise CandidateCurrentnessError(detail)


def _assert_regular_review_set_file(path: Path, *, label: str) -> None:
    _reject_preview_lexical(path.absolute(), label=label)
    if path_crosses_link(path):
        raise ValidationError(f"animation review set file {label} crosses a link")
    if not path.is_file():
        raise ValidationError(f"animation review set entry {label} is not a regular file")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValidationError(f"animation review set entry {label} is not a regular file")


def _read_bounded_review_set_bytes(path: Path, *, label: str, max_bytes: int) -> bytes:
    _reject_preview_lexical(path.absolute(), label=label)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValidationError(f"animation review set file {label} exceeds its byte limit")
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValidationError(f"animation review set file {label} exceeds its byte limit")
    if len(payload) != info.st_size:
        raise ValidationError(f"animation review set file {label} size changed during read")
    _reject_preview_lexical(path.absolute(), label=label)
    return payload


def _read_review_set_root_file(path: Path, *, label: str) -> bytes:
    _assert_regular_review_set_file(path, label=label)
    if path.name == "animation_review_set_manifest.json":
        return _read_bounded_review_set_bytes(
            path, label=label, max_bytes=_MAX_REVIEW_SET_MANIFEST_BYTES
        )
    return _read_bounded_package_file(path)


def _read_review_set_nested_clip_file(path: Path, *, label: str) -> bytes:
    _assert_regular_review_set_file(path, label=label)
    if path.name == "animation_clip_manifest.json":
        return _read_bounded_review_set_bytes(
            path, label=label, max_bytes=_MAX_REVIEW_SET_MANIFEST_BYTES
        )
    payload = _read_bounded_package_file(path)
    if path.name == "animation_clip.json":
        if len(payload) > _MAX_REVIEW_SET_PER_CLIP_RAW_BYTES:
            raise ValidationError(
                "animation review set nested clip exceeds per-clip raw byte limit"
            )
    return payload


def _inspect_review_set_directory(
    review_dir: Path,
    *,
    expected_clip_count: int,
) -> _ReviewSetDirectorySnapshot:
    lexical = Path(review_dir).absolute()
    _reject_preview_lexical(lexical, label="animation review set directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="animation review set directory")
    if not root.is_dir():
        raise ValidationError("animation review set path is not a directory")

    expected_root_entries = _REVIEW_SET_ROOT_FILES | {"clips"}
    root_names = _bounded_directory_entry_names(
        root,
        expected_count=_REVIEW_SET_ROOT_INVENTORY_ENTRIES,
        label="root inventory",
    )
    if root_names != expected_root_entries:
        extra = sorted(root_names - expected_root_entries)
        missing = sorted(expected_root_entries - root_names)
        raise ValidationError(
            f"animation review set root inventory mismatch (missing={missing}, extra={extra})"
        )

    clips_path = root / "clips"
    _reject_preview_lexical(clips_path.absolute(), label="clips")
    if not clips_path.is_dir():
        raise ValidationError("animation review set clips path is not a directory")
    if path_crosses_link(clips_path):
        raise ValidationError("animation review set clips directory crosses a link")

    slot_names = _bounded_directory_entry_names(
        clips_path,
        expected_count=_MAX_REVIEW_SET_CLIP_SLOT_INVENTORY,
        label="clips inventory",
    )
    for name in slot_names:
        entry = clips_path / name
        if not entry.is_dir():
            raise ValidationError(
                "animation review set clips directory contains a non-directory entry"
            )
        if path_crosses_link(entry):
            raise ValidationError("animation review set clip slot crosses a link")
    expected_slot_names = {f"{index:03d}" for index in range(expected_clip_count)}
    if slot_names != expected_slot_names:
        extra = sorted(slot_names - expected_slot_names)
        missing = sorted(expected_slot_names - slot_names)
        raise ValidationError(
            f"animation review set clip slot set mismatch (missing={missing}, extra={extra})"
        )

    root_files: dict[str, bytes] = {}
    clip_files: dict[str, bytes] = {}
    total_bytes = 0

    for name in sorted(_REVIEW_SET_ROOT_FILES):
        if name == "animation_review_set_manifest.json":
            continue
        path = root / name
        _assert_regular_review_set_file(path, label=name)
        payload = _read_review_set_root_file(path, label=name)
        total_bytes += len(payload)
        if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
            raise ValidationError(
                "animation review set package exceeds total byte limit on inspection"
            )
        root_files[name] = payload

    for slot_name in sorted(expected_slot_names):
        slot_dir = clips_path / slot_name
        _reject_preview_lexical(slot_dir.absolute(), label=f"clips/{slot_name}")
        if not slot_dir.is_dir():
            raise ValidationError(f"animation review set clip slot {slot_name} is not a directory")
        if path_crosses_link(slot_dir):
            raise ValidationError(f"animation review set clip slot {slot_name} crosses a link")
        slot_names_on_disk = _bounded_directory_entry_names(
            slot_dir,
            expected_count=_MAX_REVIEW_SET_CLIP_SLOT_FILE_INVENTORY,
            label=f"clip slot {slot_name} inventory",
        )
        if slot_names_on_disk != _REVIEW_SET_CLIP_SLOT_FILES:
            extra = sorted(slot_names_on_disk - _REVIEW_SET_CLIP_SLOT_FILES)
            missing = sorted(_REVIEW_SET_CLIP_SLOT_FILES - slot_names_on_disk)
            raise ValidationError(
                f"animation review set clip slot {slot_name} file mismatch "
                f"(missing={missing}, extra={extra})"
            )
        for file_name in sorted(_REVIEW_SET_CLIP_SLOT_FILES):
            rel_path = f"clips/{slot_name}/{file_name}"
            path = slot_dir / file_name
            payload = _read_review_set_nested_clip_file(path, label=rel_path)
            total_bytes += len(payload)
            if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
                raise ValidationError(
                    "animation review set package exceeds total byte limit on inspection"
                )
            clip_files[rel_path] = payload

    manifest_path = root / "animation_review_set_manifest.json"
    manifest_raw = _read_review_set_root_file(
        manifest_path, label="animation_review_set_manifest.json"
    )
    if len(manifest_raw) > _MAX_REVIEW_SET_MANIFEST_BYTES:
        raise ValidationError("animation review set manifest exceeds byte limit")
    manifest_doc = parse_bounded_publication_json(manifest_raw)
    _validate_review_set_manifest_shape(manifest_doc)
    total_bytes += len(manifest_raw)
    if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
        raise ValidationError("animation review set package exceeds total byte limit on inspection")
    root_files["animation_review_set_manifest.json"] = manifest_raw

    digests_declared = manifest_doc.get("file_digests")
    if not isinstance(digests_declared, dict):
        raise ValidationError("animation review set manifest file_digests must be an object")
    expected_digest_names = sorted(_REVIEW_SET_ROOT_DIGEST_FILES)
    if sorted(digests_declared) != expected_digest_names:
        raise ValidationError("animation review set manifest file_digests keys do not match")

    for name in expected_digest_names:
        if name not in root_files:
            raise ValidationError(f"animation review set root payload missing {name}")
        payload = root_files[name]
        digest = hashlib.sha256(payload).hexdigest()
        declared = digests_declared.get(name)
        if not isinstance(declared, str) or declared != digest:
            raise CandidateCurrentnessError(
                f"animation review set root file {name} digest mismatch"
            )

    ordered_clips = manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    if len(ordered_clips) != expected_clip_count:
        raise ValidationError("animation review set ordered_clips length mismatch")

    nested_digest_map = {
        rel_path: hashlib.sha256(clip_files[rel_path]).hexdigest()
        for rel_path in sorted(clip_files)
    }
    nested_declared = manifest_doc.get("nested_clips_payload_digest")
    if not isinstance(nested_declared, str):
        raise ValidationError("animation review set nested_clips_payload_digest must be a string")
    if _nested_clips_payload_digest(nested_digest_map) != nested_declared:
        raise CandidateCurrentnessError("animation review set nested clips payload digest mismatch")

    root_digest_map = {
        name: hashlib.sha256(root_files[name]).hexdigest() for name in expected_digest_names
    }
    root_declared = manifest_doc.get("root_payload_digest")
    if not isinstance(root_declared, str):
        raise ValidationError("animation review set root_payload_digest must be a string")
    if _root_payload_digest(root_digest_map) != root_declared:
        raise CandidateCurrentnessError("animation review set root payload digest mismatch")

    return _ReviewSetDirectorySnapshot(
        manifest_doc=manifest_doc,
        root_files=root_files,
        clip_files=clip_files,
    )


def _review_set_matches_captured(
    review_dir: Path,
    captured: Sequence[CapturedAnimationReviewSetSource],
    *,
    workflow_id: str,
) -> bool:
    try:
        expected = build_expected_animation_review_set_payload(captured, workflow_id=workflow_id)
    except ValidationError:
        return False
    try:
        snapshot = _inspect_review_set_directory(review_dir, expected_clip_count=len(captured))
    except OSError:
        return False

    if snapshot.manifest_doc.get("workflow_id") != workflow_id:
        return False
    if not _manifest_ordered_clips_semantically_match(snapshot.manifest_doc, captured):
        return False
    if snapshot.root_files != dict(expected.root_files):
        return False
    if snapshot.clip_files != dict(expected.clip_files):
        return False

    trusted_ui = trusted_animation_review_set_ui_digests()
    for name, trusted_digest in trusted_ui.items():
        payload = snapshot.root_files.get(name)
        if payload is None:
            return False
        if hashlib.sha256(payload).hexdigest() != trusted_digest:
            return False
    return True


def _assert_staged_review_set_matches_expected(
    stage: Path,
    captured: Sequence[CapturedAnimationReviewSetSource],
    *,
    workflow_id: str,
) -> None:
    expected = build_expected_animation_review_set_payload(captured, workflow_id=workflow_id)
    snapshot = _inspect_review_set_directory(stage, expected_clip_count=len(captured))
    if not _manifest_ordered_clips_semantically_match(snapshot.manifest_doc, captured):
        raise CandidateCurrentnessError(
            "staged animation review set ordered_clips do not match authoritative captured sources"
        )
    if snapshot.root_files != dict(expected.root_files):
        raise CandidateCurrentnessError(
            "staged animation review set bytes do not match authoritative captured sources"
        )
    if snapshot.clip_files != dict(expected.clip_files):
        raise CandidateCurrentnessError(
            "staged animation review set nested clip bytes do not match authoritative captures"
        )
    trusted_ui = trusted_animation_review_set_ui_digests()
    for name, trusted_digest in trusted_ui.items():
        payload = snapshot.root_files.get(name)
        if payload is None or hashlib.sha256(payload).hexdigest() != trusted_digest:
            raise CandidateCurrentnessError(
                f"staged animation review set UI resource {name} digest mismatch"
            )


def _write_review_set_tree(stage: Path, expected: ExpectedAnimationReviewSetPayload) -> None:
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="animation review set staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="animation review set staging container")

    total_bytes = 0
    for name in sorted(expected.root_files):
        if name == "animation_review_set_manifest.json":
            continue
        payload = expected.root_files[name]
        path = stage_resolved / name
        _reject_preview_lexical(path.absolute(), label=name)
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=name)
        total_bytes += len(payload)
        if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation review set payload exceeds total byte limit")

    clips_root = stage_resolved / "clips"
    _reject_preview_lexical(clips_root.absolute(), label="clips")
    clips_root.mkdir(parents=True, exist_ok=True)
    _reject_preview_lexical(clips_root.resolve(strict=True), label="clips")

    for rel_path in sorted(expected.clip_files):
        payload = expected.clip_files[rel_path]
        path = stage_resolved / rel_path
        parent_lexical = path.parent.absolute()
        _reject_preview_lexical(parent_lexical, label=rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_resolved = path.parent.resolve(strict=True)
        _reject_preview_lexical(parent_resolved, label=rel_path)
        _reject_preview_lexical(path.absolute(), label=rel_path)
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=rel_path)
        total_bytes += len(payload)
        if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation review set payload exceeds total byte limit")

    manifest_payload = expected.root_files["animation_review_set_manifest.json"]
    if len(manifest_payload) > _MAX_REVIEW_SET_MANIFEST_BYTES:
        raise ValidationError("animation review set manifest exceeds byte limit")
    manifest_path = stage_resolved / "animation_review_set_manifest.json"
    _reject_preview_lexical(manifest_path.absolute(), label="animation_review_set_manifest.json")
    manifest_path.write_bytes(manifest_payload)
    _reject_preview_lexical(
        manifest_path.resolve(strict=True), label="animation_review_set_manifest.json"
    )
    total_bytes += len(manifest_payload)
    if total_bytes > _MAX_REVIEW_SET_PACKAGE_TOTAL_BYTES:
        raise ValidationError("animation review set payload exceeds total byte limit")


def export_rigged_character_animation_review_set(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
    output_dir: Path | str,
) -> CandidateAnimationReviewSetResult:
    """Export a multi-clip animation review set from current V0.8-6 clip preview packages."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow_id)
    preview_root = Path(preview_dir)
    destination = _validate_output_dir(Path(output_dir))
    _assert_destination_fresh(destination)

    pre_captured = capture_animation_review_set_sources(
        handlers, workflow_id, preview_root, clip_packages
    )
    _require_pin_captured_sources(
        pre_captured,
        detail="review set source package or clip bindings drifted before payload prepare",
    )
    expected_pre = build_expected_animation_review_set_payload(
        pre_captured, workflow_id=workflow_id
    )

    stage, staging_parent = _fresh_review_set_stage(destination.parent, handlers.root)
    try:
        _write_review_set_tree(stage, expected_pre)

        mid_captured = capture_animation_review_set_sources(
            handlers, workflow_id, preview_root, clip_packages
        )
        _assert_captured_snapshots_unchanged(
            pre_captured,
            mid_captured,
            detail="review set source captures drifted during pre-publish currentness gate",
        )
        _require_pin_captured_sources(
            mid_captured,
            detail="review set source package or clip bindings drifted before publish inspection",
        )
        _assert_staged_review_set_matches_expected(stage, mid_captured, workflow_id=workflow_id)
        del mid_captured

        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(publish_parent_lexical, label="animation review set publish parent")
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="animation review set publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("animation review set publication path mismatch")

        post_captured = capture_animation_review_set_sources(
            handlers, workflow_id, preview_root, clip_packages
        )
        _assert_captured_snapshots_unchanged(
            pre_captured,
            post_captured,
            detail="review set source captures drifted during post-publish currentness gate",
        )
        _require_pin_captured_sources(
            post_captured,
            detail="review set source package or clip bindings drifted after publish",
        )
        if not _review_set_matches_captured(destination, post_captured, workflow_id=workflow_id):
            raise ArtifactError(
                "published animation review set does not match authoritative captured sources"
            )

        snapshot = _inspect_review_set_directory(
            destination, expected_clip_count=len(post_captured)
        )
        manifest_sha = hashlib.sha256(
            snapshot.root_files["animation_review_set_manifest.json"]
        ).hexdigest()
        processed_glb = snapshot.manifest_doc.get("processed_glb_sha256")
        if not isinstance(processed_glb, str):
            raise ArtifactError("animation review set manifest missing processed_glb_sha256")
        ordered_clip_ids = tuple(item.clip_id for item in post_captured)
    except Exception:
        _discard_owned_preview_stage(stage, staging_parent)
        raise

    return CandidateAnimationReviewSetResult(
        workflow_id=workflow_id,
        review_set_root=destination.as_posix(),
        manifest_sha256=manifest_sha,
        ordered_clip_ids=ordered_clip_ids,
        processed_glb_sha256=processed_glb,
        production_eligible=False,
        promotion_eligible=False,
        animation_review_set_status=ANIMATION_REVIEW_SET_STATUS_TEST_ONLY,
    )


def animation_review_set_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
    review_dir: Path | str,
) -> bool:
    """Return True when review_dir matches live upstream animation clip preview authority."""
    preview_root = Path(preview_dir)
    review_root = Path(review_dir)
    try:
        captured = capture_animation_review_set_sources(
            handlers, workflow_id, preview_root, clip_packages
        )
    except (CandidateCurrentnessError, ValidationError):
        return False
    if not _pin_captured_sources_to_live_disk(captured):
        return False
    try:
        return _review_set_matches_captured(review_root, captured, workflow_id=workflow_id)
    except (ValidationError, CandidateCurrentnessError):
        raise
    except OSError:
        return False


__all__ = [
    "ANIMATION_REVIEW_SET_MANIFEST_SCHEMA",
    "ANIMATION_REVIEW_SET_STATUS_TEST_ONLY",
    "CandidateAnimationReviewSetResult",
    "CapturedAnimationReviewSetSource",
    "CandidateAnimationReviewSetSource",
    "ExpectedAnimationReviewSetPayload",
    "animation_review_set_current",
    "export_rigged_character_animation_review_set",
    "MAX_REVIEW_SET_SOURCES",
    "MIN_REVIEW_SET_SOURCES",
    "assert_review_set_shared_identity_before_prepare",
    "build_expected_animation_review_set_manifest",
    "build_expected_animation_review_set_payload",
    "capture_animation_review_set_source",
    "capture_animation_review_set_sources",
    "compute_shared_preview_identity_digest",
    "trusted_animation_review_set_ui_digests",
    "validate_review_set_sources",
]
