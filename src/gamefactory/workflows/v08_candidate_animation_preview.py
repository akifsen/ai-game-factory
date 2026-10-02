"""V0.8-5 readiness-gated rigged character animation preview export (test-only)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    load_bounded_publication_control_json,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    assert_zero_provider_activity,
)
from gamefactory.workflows.v08_candidate_gates import MAX_CANDIDATE_JSON_ARTIFACT_BYTES
from gamefactory.workflows.v08_candidate_preview import (
    PREVIEW_STATUS_TEST_ONLY,
    _assert_destination_fresh,
    _assert_same_volume,
    _canonical_json_bytes,
    _discard_owned_preview_stage,
    _inspect_preview_directory,
    _preview_matches_bindings,
    _PreviewBindings,
    _reject_preview_lexical,
    _resolve_preview_bindings,
    _validate_output_dir,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

ANIMATION_MANIFEST_SCHEMA = "candidate-character-animation-preview-manifest-0.8.0"
ANIMATION_SPEC_SCHEMA = "candidate-character-animation-spec-0.8.0"
ANIMATION_STATUS_TEST_ONLY = PREVIEW_STATUS_TEST_ONLY

RIG_SMOKE_CLIP_ID = "rig_smoke_01"
RIG_SMOKE_BONE = "LeftUpperArm"
RIG_SMOKE_DURATION_S = 1.0

_UPSTREAM_PREVIEW_FILES: frozenset[str] = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
    }
)
_ANIMATION_PACKAGE_FILES: frozenset[str] = frozenset(
    _UPSTREAM_PREVIEW_FILES
    | {
        "animation_preview.tscn",
        "animation_preview_player.gd",
        "animation_spec.json",
        "animation_manifest.json",
        "project.godot",
    }
)
_ANIMATION_MANIFEST_KEYS = frozenset(
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
        "file_digests",
        "animation_preview_status",
        "production_eligible",
        "promotion_eligible",
    }
)
_ANIMATION_SPEC_KEYS = frozenset(
    {
        "schema_version",
        "clip_id",
        "duration_s",
        "loop",
        "bone_name",
        "root_motion",
        "keyframes",
        "min_affected_displacement",
        "max_affected_displacement",
        "production_eligible",
        "promotion_eligible",
    }
)
_MAX_ANIMATION_TOTAL_BYTES = 128 * 1024 * 1024
_WEIGHTED_CONTRACT_BONES: frozenset[str] = frozenset({RIG_SMOKE_BONE})


@dataclass(frozen=True)
class CandidateAnimationPreviewResult:
    """Typed animation preview export outcome; never production- or promotion-eligible."""

    workflow_id: str
    animation_preview_root: str
    animation_manifest_sha256: str
    upstream_preview_manifest_sha256: str
    processed_glb_sha256: str
    snapshot_fingerprint: str
    evidence_execution_id: str
    evidence_attempt_number: int
    manifest_artifact_id: str
    result_artifact_id: str
    marker_artifact_id: str
    animation_preview_scene_sha256: str
    production_eligible: bool = False
    promotion_eligible: bool = False
    animation_preview_status: str = ANIMATION_STATUS_TEST_ONLY


def _animation_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation preview publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation preview publish parent")
    return publish_resolved / ".gf" / "candidate_animation_preview_staging"


def _fresh_animation_stage(publish_parent: Path, project_root: Path) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation preview publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="animation preview project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation preview publish parent")
    _reject_preview_lexical(project_resolved, label="animation preview project root")

    staging_parent_lexical = _animation_staging_parent(publish_resolved)
    _reject_preview_lexical(staging_parent_lexical, label="animation preview staging parent")
    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="animation preview staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="animation preview staging container")
    if stage_lexical.exists():
        raise ArtifactError("animation preview staging collision")
    stage_lexical.mkdir(parents=True, exist_ok=False)
    stage = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage, label="animation preview staging container")
    _assert_same_volume(stage, publish_resolved)
    if path_crosses_link(stage):
        raise ValidationError("animation preview staging container crosses a link")
    return stage, staging_parent


def _rig_smoke_keyframes() -> list[dict[str, Any]]:
    return [
        {"time_s": 0.0, "euler_deg": [0.0, 0.0, 0.0]},
        {"time_s": 0.5, "euler_deg": [0.0, 20.0, 0.0]},
        {"time_s": 1.0, "euler_deg": [0.0, 0.0, 0.0]},
    ]


def validate_animation_spec_document(doc: dict[str, Any]) -> None:
    """Reject animation specs that deviate from the frozen rig_smoke_01 contract."""
    try:
        canonical = _canonical_json_bytes(doc)
    except (ValueError, TypeError) as exc:
        raise ValidationError("animation spec contains invalid JSON values") from exc
    if canonical != _canonical_json_bytes(_build_animation_spec_document()):
        raise ValidationError("animation spec differs from the canonical bone_name/clip contract")
    if set(doc) != _ANIMATION_SPEC_KEYS:
        raise ValidationError("animation spec fields do not match the contract")
    if doc.get("schema_version") != ANIMATION_SPEC_SCHEMA:
        raise ValidationError("animation spec schema_version mismatch")
    if doc.get("clip_id") != RIG_SMOKE_CLIP_ID:
        raise ValidationError("animation spec clip_id mismatch")
    if doc.get("duration_s") != RIG_SMOKE_DURATION_S:
        raise ValidationError("animation spec duration_s mismatch")
    if doc.get("loop") is not True:
        raise ValidationError("animation spec loop must be true")
    if doc.get("root_motion") is not False:
        raise ValidationError("animation spec root_motion must be false")
    bone = doc.get("bone_name")
    if not isinstance(bone, str) or bone not in _WEIGHTED_CONTRACT_BONES:
        raise ValidationError("animation spec bone_name is not an allowed weighted contract bone")
    keyframes = doc.get("keyframes")
    if keyframes != _rig_smoke_keyframes():
        raise ValidationError("animation spec keyframes do not match rig_smoke_01")
    if doc.get("production_eligible") is not False or doc.get("promotion_eligible") is not False:
        raise ValidationError("animation spec eligibility flags must be false")


def _build_animation_spec_document() -> dict[str, Any]:
    return {
        "schema_version": ANIMATION_SPEC_SCHEMA,
        "clip_id": RIG_SMOKE_CLIP_ID,
        "duration_s": RIG_SMOKE_DURATION_S,
        "loop": True,
        "bone_name": RIG_SMOKE_BONE,
        "root_motion": False,
        "keyframes": _rig_smoke_keyframes(),
        "min_affected_displacement": 0.012,
        "max_affected_displacement": 0.35,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _build_animation_preview_tscn() -> str:
    return (
        "[gd_scene load_steps=3 format=3]\n\n"
        '[ext_resource type="PackedScene" path="res://character.tscn" id="1_character"]\n'
        '[ext_resource type="Script" path="res://animation_preview_player.gd" id="2_player"]\n\n'
        '[node name="AnimationPreview" type="Node3D"]\n\n'
        '[node name="Character" parent="." instance=ExtResource("1_character")]\n\n'
        '[node name="AnimationPlayer" type="AnimationPlayer" parent="."]\n'
        'script = ExtResource("2_player")\n'
    )


def _build_animation_project_godot() -> str:
    return (
        "config_version=5\n\n"
        "[application]\n"
        'config/name="CandidateCharacterAnimationPreview"\n'
        'run/main_scene="res://animation_preview.tscn"\n\n'
        "[rendering]\n"
        'renderer/rendering_method="gl_compatibility"\n'
    )


def _player_script_bytes() -> bytes:
    return (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("animation_preview_player.gd")
        .read_bytes()
    )


def _read_bounded_package_file(path: Path) -> bytes:
    _reject_preview_lexical(path.absolute(), label=path.name)
    limit = 64 * 1024 * 1024 if path.name == "character.glb" else 1024 * 1024
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValidationError(f"animation package file {path.name} exceeds its byte limit")
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValidationError(f"animation package file {path.name} exceeds its byte limit")
    _reject_preview_lexical(path.absolute(), label=path.name)
    return payload


def _read_upstream_preview_files(preview_dir: Path) -> dict[str, bytes]:
    _inspect_preview_directory(preview_dir)
    root = preview_dir.resolve(strict=True)
    payloads: dict[str, bytes] = {}
    for name in sorted(_UPSTREAM_PREVIEW_FILES):
        path = root / name
        payloads[name] = _read_bounded_package_file(path)
    return payloads


def _upstream_preview_manifest_sha256(upstream_bytes: dict[str, bytes]) -> str:
    manifest = upstream_bytes.get("preview-manifest.json")
    if manifest is None:
        raise ValidationError("upstream preview manifest is missing")
    return hashlib.sha256(manifest).hexdigest()


def _animation_bindings_digest(
    bindings: _PreviewBindings,
    workflow_id: str,
    upstream_preview_manifest_sha256: str,
) -> str:
    readiness = bindings.readiness
    canonical = json.dumps(
        {
            "workflow_id": workflow_id,
            "snapshot_fingerprint": readiness.snapshot.fingerprint(),
            "evidence_execution_id": readiness.evidence_execution_id,
            "evidence_attempt_number": readiness.evidence_attempt_number,
            "manifest_artifact_sha256": bindings.manifest_sha256,
            "result_artifact_sha256": bindings.result_sha256,
            "marker_artifact_sha256": bindings.marker_sha256,
            "processed_glb_sha256": bindings.processed_art.content_hash,
            "upstream_preview_manifest_sha256": upstream_preview_manifest_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_animation_manifest(
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    upstream_preview_manifest_sha256: str,
    file_digests: dict[str, str],
) -> dict[str, Any]:
    readiness = bindings.readiness
    from gamefactory.core.domain.v08_candidate_contracts import candidate_spec_fingerprint

    return {
        "schema_version": ANIMATION_MANIFEST_SCHEMA,
        "workflow_id": workflow_id,
        "evidence_task_id": bindings.evidence_task_id,
        "evidence_execution_id": readiness.evidence_execution_id,
        "evidence_attempt_number": readiness.evidence_attempt_number,
        "manifest_artifact_id": readiness.manifest_artifact_id,
        "manifest_artifact_sha256": bindings.manifest_sha256,
        "result_artifact_id": readiness.result_artifact_id,
        "result_artifact_sha256": bindings.result_sha256,
        "marker_artifact_id": readiness.marker_artifact_id,
        "marker_artifact_sha256": bindings.marker_sha256,
        "snapshot_fingerprint": readiness.snapshot.fingerprint(),
        "spec_fingerprint": candidate_spec_fingerprint(bindings.spec),
        "processed_glb_sha256": bindings.processed_art.content_hash,
        "upstream_preview_manifest_sha256": upstream_preview_manifest_sha256,
        "file_digests": dict(sorted(file_digests.items())),
        "animation_preview_status": ANIMATION_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _animation_package_file_bytes(
    upstream_bytes: dict[str, bytes],
) -> dict[str, bytes]:
    spec_doc = _build_animation_spec_document()
    validate_animation_spec_document(spec_doc)
    payloads = dict(upstream_bytes)
    payloads["animation_spec.json"] = _canonical_json_bytes(spec_doc)
    payloads["animation_preview.tscn"] = _build_animation_preview_tscn().encode("utf-8")
    payloads["animation_preview_player.gd"] = _player_script_bytes()
    payloads["project.godot"] = _build_animation_project_godot().encode("utf-8")
    return payloads


def _inspect_animation_directory(animation_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    lexical = Path(animation_dir).absolute()
    _reject_preview_lexical(lexical, label="animation preview directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="animation preview directory")
    if not root.is_dir():
        raise ValidationError("animation preview path is not a directory")
    names = {item.name for item in root.iterdir()}
    if names != _ANIMATION_PACKAGE_FILES:
        extra = sorted(names - _ANIMATION_PACKAGE_FILES)
        missing = sorted(_ANIMATION_PACKAGE_FILES - names)
        raise ValidationError(
            f"animation preview file set mismatch (missing={missing}, extra={extra})"
        )
    for name in names:
        path = root / name
        _reject_preview_lexical(path, label=name)
        if path_crosses_link(path):
            raise ValidationError(f"animation preview file {name} crosses a link")
        if not path.is_file():
            raise ValidationError(f"animation preview entry {name} is not a regular file")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValidationError(f"animation preview entry {name} is not a regular file")

    manifest_path = root / "animation_manifest.json"
    manifest_doc = load_bounded_publication_control_json(
        manifest_path, label="animation preview manifest"
    )
    if set(manifest_doc) != _ANIMATION_MANIFEST_KEYS:
        raise ValidationError("animation preview manifest fields do not match the contract")
    digests_declared = manifest_doc.get("file_digests")
    if not isinstance(digests_declared, dict):
        raise ValidationError("animation preview manifest file_digests must be an object")
    expected_names = sorted(_ANIMATION_PACKAGE_FILES - {"animation_manifest.json"})
    if sorted(digests_declared) != expected_names:
        raise ValidationError("animation preview manifest file_digests keys do not match")
    digests_live: dict[str, str] = {}
    total = 0
    for name in expected_names:
        path = root / name
        size = path.stat().st_size
        total += size
        if total > _MAX_ANIMATION_TOTAL_BYTES:
            raise ValidationError("animation preview package exceeds byte limit on inspection")
        digest = sha256_file(path)
        digests_live[name] = digest
        declared = digests_declared.get(name)
        if not isinstance(declared, str) or declared != digest:
            raise CandidateCurrentnessError(f"animation preview file {name} digest mismatch")
    spec_path = root / "animation_spec.json"
    spec_doc = load_bounded_publication_control_json(spec_path, label="animation spec")
    validate_animation_spec_document(spec_doc)
    return manifest_doc, digests_live


def _upstream_matches_bindings(
    handlers: CandidateWorkflowHandlers,
    bindings: _PreviewBindings,
    workflow_id: str,
    preview_dir: Path,
) -> bool:
    try:
        manifest, digests = _inspect_preview_directory(preview_dir)
        return _preview_matches_bindings(
            handlers,
            bindings,
            workflow_id=workflow_id,
            preview_dir=preview_dir,
            manifest_doc=manifest,
            digests_live=digests,
        )
    except (OSError, ValidationError, CandidateCurrentnessError):
        return False


def _animation_matches_bindings(
    handlers: CandidateWorkflowHandlers,
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    preview_dir: Path,
    animation_dir: Path,
    manifest_doc: dict[str, Any],
    digests_live: dict[str, str],
) -> bool:
    if not _upstream_matches_bindings(handlers, bindings, workflow_id, preview_dir):
        return False
    upstream_bytes = _read_upstream_preview_files(preview_dir)
    upstream_manifest_sha = _upstream_preview_manifest_sha256(upstream_bytes)
    expected_bytes = _animation_package_file_bytes(upstream_bytes)
    expected_digests = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in expected_bytes.items()
    }
    if digests_live != expected_digests:
        return False
    expected_manifest = _build_animation_manifest(
        bindings,
        workflow_id=workflow_id,
        upstream_preview_manifest_sha256=upstream_manifest_sha,
        file_digests=expected_digests,
    )
    return _canonical_json_bytes(manifest_doc) == _canonical_json_bytes(expected_manifest)


def _write_animation_tree(
    stage: Path,
    bindings: _PreviewBindings,
    upstream_bytes: dict[str, bytes],
    *,
    workflow_id: str,
) -> dict[str, str]:
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="animation preview staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="animation preview staging container")

    package_bytes = _animation_package_file_bytes(upstream_bytes)
    file_digests: dict[str, str] = {}
    total = 0
    for name in sorted(_ANIMATION_PACKAGE_FILES - {"animation_manifest.json"}):
        path = stage_resolved / name
        path_lexical = path.absolute()
        _reject_preview_lexical(path_lexical, label=name)
        payload = package_bytes[name]
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=name)
        size = path.stat().st_size
        total += size
        if total > _MAX_ANIMATION_TOTAL_BYTES:
            raise ValidationError("animation preview package exceeds byte limit")
        file_digests[name] = hashlib.sha256(payload).hexdigest()

    upstream_manifest_sha = _upstream_preview_manifest_sha256(upstream_bytes)
    manifest = _build_animation_manifest(
        bindings,
        workflow_id=workflow_id,
        upstream_preview_manifest_sha256=upstream_manifest_sha,
        file_digests=file_digests,
    )
    manifest_path = stage_resolved / "animation_manifest.json"
    _reject_preview_lexical(manifest_path.absolute(), label="animation_manifest.json")
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise ValidationError("animation preview manifest exceeds JSON byte limit")
    manifest_path.write_bytes(manifest_bytes)
    _reject_preview_lexical(manifest_path.resolve(strict=True), label="animation_manifest.json")
    return file_digests


def export_rigged_character_animation_preview(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    output_dir: Path | str,
) -> CandidateAnimationPreviewResult:
    """Export animation preview package from live C2-B evidence and a canonical V0.8-4 preview."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow_id)
    upstream_root = Path(preview_dir)
    bindings_before = _resolve_preview_bindings(handlers, workflow_id)
    if not _upstream_matches_bindings(handlers, bindings_before, workflow_id, upstream_root):
        raise CandidateCurrentnessError("upstream V0.8-4 preview is missing or not current")

    destination = _validate_output_dir(Path(output_dir))
    _assert_destination_fresh(destination)

    upstream_before = _read_upstream_preview_files(upstream_root)
    upstream_manifest_before = _upstream_preview_manifest_sha256(upstream_before)
    digest_before = _animation_bindings_digest(
        bindings_before, workflow_id, upstream_manifest_before
    )

    stage, staging_parent = _fresh_animation_stage(destination.parent, handlers.root)
    try:
        _write_animation_tree(
            stage,
            bindings_before,
            upstream_before,
            workflow_id=workflow_id,
        )
        bindings_after = _resolve_preview_bindings(handlers, workflow_id)
        if not _upstream_matches_bindings(handlers, bindings_after, workflow_id, upstream_root):
            raise CandidateCurrentnessError("upstream preview became stale during staging")
        upstream_after = _read_upstream_preview_files(upstream_root)
        upstream_manifest_after = _upstream_preview_manifest_sha256(upstream_after)
        digest_after = _animation_bindings_digest(
            bindings_after, workflow_id, upstream_manifest_after
        )
        if digest_after != digest_before or upstream_after != upstream_before:
            raise CandidateCurrentnessError(
                "readiness or upstream preview bindings drifted during animation staging"
            )
        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(publish_parent_lexical, label="animation preview publish parent")
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="animation preview publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("animation preview publication path mismatch")
        manifest_doc, digests_live = _inspect_animation_directory(destination)
        if not _animation_matches_bindings(
            handlers,
            _resolve_preview_bindings(handlers, workflow_id),
            workflow_id=workflow_id,
            preview_dir=upstream_root,
            animation_dir=destination,
            manifest_doc=manifest_doc,
            digests_live=digests_live,
        ):
            raise ArtifactError("published animation preview does not match live bindings")
        manifest_sha = sha256_file(destination / "animation_manifest.json")
        glb_sha = manifest_doc.get("processed_glb_sha256")
        if not isinstance(glb_sha, str):
            raise ArtifactError("animation manifest missing processed_glb_sha256")
        if sha256_file(destination / "character.glb") != glb_sha:
            raise ArtifactError("published animation preview GLB hash mismatch")
        upstream_sha = manifest_doc.get("upstream_preview_manifest_sha256")
        if not isinstance(upstream_sha, str):
            raise ArtifactError("animation manifest missing upstream_preview_manifest_sha256")
        if sha256_file(destination / "preview-manifest.json") != upstream_sha:
            raise ArtifactError("upstream preview manifest hash mismatch in animation package")
        scene_sha = digests_live["animation_preview.tscn"]
        readiness = bindings_before.readiness
    except Exception:
        _discard_owned_preview_stage(stage, staging_parent)
        raise

    return CandidateAnimationPreviewResult(
        workflow_id=workflow_id,
        animation_preview_root=destination.as_posix(),
        animation_manifest_sha256=manifest_sha,
        upstream_preview_manifest_sha256=str(upstream_sha),
        processed_glb_sha256=str(glb_sha),
        snapshot_fingerprint=readiness.snapshot.fingerprint(),
        evidence_execution_id=readiness.evidence_execution_id,
        evidence_attempt_number=readiness.evidence_attempt_number,
        manifest_artifact_id=readiness.manifest_artifact_id,
        result_artifact_id=readiness.result_artifact_id,
        marker_artifact_id=readiness.marker_artifact_id,
        animation_preview_scene_sha256=scene_sha,
        production_eligible=False,
        promotion_eligible=False,
        animation_preview_status=ANIMATION_STATUS_TEST_ONLY,
    )


def animation_preview_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    animation_dir: Path | str,
) -> bool:
    """Return True when animation_dir matches live evidence and the canonical upstream preview."""
    upstream_root = Path(preview_dir)
    animation_root = Path(animation_dir)
    try:
        bindings = _resolve_preview_bindings(handlers, workflow_id)
    except (CandidateCurrentnessError, ValidationError, ArtifactError):
        return False
    try:
        manifest, digests_live = _inspect_animation_directory(animation_root)
    except (ValidationError, CandidateCurrentnessError):
        raise
    except OSError:
        return False
    return _animation_matches_bindings(
        handlers,
        bindings,
        workflow_id=workflow_id,
        preview_dir=upstream_root,
        animation_dir=animation_root,
        manifest_doc=manifest,
        digests_live=digests_live,
    )


__all__ = [
    "ANIMATION_MANIFEST_SCHEMA",
    "ANIMATION_SPEC_SCHEMA",
    "ANIMATION_STATUS_TEST_ONLY",
    "CandidateAnimationPreviewResult",
    "RIG_SMOKE_BONE",
    "RIG_SMOKE_CLIP_ID",
    "animation_preview_current",
    "export_rigged_character_animation_preview",
    "validate_animation_spec_document",
]
