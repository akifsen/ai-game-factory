"""V0.8-6 readiness-gated authored local animation clip preview export (test-only)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.animation_clip_input import (
    MAX_LOCAL_ANIMATION_CLIP_BYTES,
    _bone_has_positive_finite_weight,
    _decoded_contract_joint_names,
    assert_animation_clip_skin_influence,
    load_local_animation_clip,
)
from gamefactory.adapters.assets.internal_skin_decode import (
    DecodedInternalSkinnedGLB,
    decode_internal_skinned_glb,
)
from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    load_bounded_publication_control_json,
)
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.animation_clip import (
    ANIMATION_CLIP_SCHEMA_VERSION,
    AuthoredAnimationClip,
    parse_animation_clip_document,
)
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

ACCEPTANCE_CLIP_ID = "arm_wave_01"
ACCEPTANCE_CLIP_DURATION_S = 1.5

ANIMATION_CLIP_MANIFEST_SCHEMA = "candidate-character-animation-clip-preview-manifest-0.8.0"
ANIMATION_CLIP_STATUS_TEST_ONLY = PREVIEW_STATUS_TEST_ONLY

_UPSTREAM_PREVIEW_FILES: frozenset[str] = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
    }
)
_CLIP_PACKAGE_FILES: frozenset[str] = frozenset(
    _UPSTREAM_PREVIEW_FILES
    | {
        "animation_clip_preview.tscn",
        "animation_clip_preview_player.gd",
        "animation_clip.json",
        "animation_clip_manifest.json",
        "project.godot",
    }
)
_CLIP_MANIFEST_KEYS = frozenset(
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
        "clip_sha256",
        "file_digests",
        "animation_clip_preview_status",
        "production_eligible",
        "promotion_eligible",
    }
)
_MAX_CLIP_PACKAGE_TOTAL_BYTES = 128 * 1024 * 1024
_GLB_PACKAGE_LIMIT = 64 * 1024 * 1024
_JSON_PACKAGE_LIMIT = 1024 * 1024
_ANIMATION_CLIP_JSON_PACKAGE_LIMIT = MAX_LOCAL_ANIMATION_CLIP_BYTES
_GODOT_ANIMATION_MIN_DURATION_SECONDS = 0.001


@dataclass(frozen=True)
class CandidateAnimationClipPreviewResult:
    """Typed animation clip preview export outcome; never production- or promotion-eligible."""

    workflow_id: str
    animation_clip_preview_root: str
    animation_clip_manifest_sha256: str
    upstream_preview_manifest_sha256: str
    processed_glb_sha256: str
    clip_sha256: str
    snapshot_fingerprint: str
    evidence_execution_id: str
    evidence_attempt_number: int
    manifest_artifact_id: str
    result_artifact_id: str
    marker_artifact_id: str
    animation_clip_preview_scene_sha256: str
    production_eligible: bool = False
    promotion_eligible: bool = False
    animation_clip_preview_status: str = ANIMATION_CLIP_STATUS_TEST_ONLY


def _y_rotation_quaternion_xyzw(degrees: float) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return (0.0, math.sin(half), 0.0, math.cos(half))


def build_acceptance_arm_wave_clip_document() -> dict[str, Any]:
    """Trusted acceptance fixture: arm_wave_01 (distinct from V0.8-5 rig_smoke_01)."""
    identity = [0.0, 0.0, 0.0, 1.0]
    y30 = list(_y_rotation_quaternion_xyzw(30.0))
    return {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": ACCEPTANCE_CLIP_ID,
        "duration_seconds": ACCEPTANCE_CLIP_DURATION_S,
        "loop": False,
        "tracks": [
            {
                "bone": "LeftUpperArm",
                "keyframes": [
                    {"time": 0.0, "rotation_xyzw": identity},
                    {"time": 0.75, "rotation_xyzw": y30},
                    {"time": 1.5, "rotation_xyzw": identity},
                ],
            },
            {
                "bone": "Spine",
                "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
            },
        ],
    }


def acceptance_arm_wave_clip_raw_bytes() -> bytes:
    """Deterministic UTF-8 bytes for the trusted acceptance clip fixture."""
    return json.dumps(
        build_acceptance_arm_wave_clip_document(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _clip_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation clip preview publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation clip preview publish parent")
    return publish_resolved / ".gf" / "candidate_animation_clip_preview_staging"


def _fresh_clip_stage(publish_parent: Path, project_root: Path) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation clip preview publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="animation clip preview project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation clip preview publish parent")
    _reject_preview_lexical(project_resolved, label="animation clip preview project root")

    staging_parent_lexical = _clip_staging_parent(publish_resolved)
    _reject_preview_lexical(staging_parent_lexical, label="animation clip preview staging parent")
    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="animation clip preview staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="animation clip preview staging container")
    if stage_lexical.exists():
        raise ArtifactError("animation clip preview staging collision")
    stage_lexical.mkdir(parents=True, exist_ok=False)
    stage = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage, label="animation clip preview staging container")
    _assert_same_volume(stage, publish_resolved)
    if path_crosses_link(stage):
        raise ValidationError("animation clip preview staging container crosses a link")
    return stage, staging_parent


def _build_animation_clip_preview_tscn(
    *,
    expected_clip_sha256: str,
    verified_weighted_bone_names: tuple[str, ...],
) -> str:
    bones_literal = ", ".join(f'"{name}"' for name in verified_weighted_bone_names)
    return (
        "[gd_scene load_steps=3 format=3]\n\n"
        '[ext_resource type="PackedScene" path="res://character.tscn" id="1_character"]\n'
        '[ext_resource type="Script" path="res://animation_clip_preview_player.gd" id="2_player"]\n\n'
        '[node name="AnimationClipPreview" type="Node3D"]\n\n'
        '[node name="Character" parent="." instance=ExtResource("1_character")]\n\n'
        '[node name="AnimationPlayer" type="AnimationPlayer" parent="."]\n'
        'script = ExtResource("2_player")\n'
        f'expected_clip_sha256 = "{expected_clip_sha256}"\n'
        f"verified_weighted_bone_names = PackedStringArray({bones_literal})\n"
    )


def _build_animation_clip_project_godot() -> str:
    return (
        "config_version=5\n\n"
        "[application]\n"
        'config/name="CandidateCharacterAnimationClipPreview"\n'
        'run/main_scene="res://animation_clip_preview.tscn"\n\n'
        "[rendering]\n"
        'renderer/rendering_method="gl_compatibility"\n'
    )


def _player_script_bytes() -> bytes:
    raw = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("animation_clip_preview_player.gd")
        .read_bytes()
    )
    return raw.replace(b"\r\n", b"\n")


def _package_file_byte_limit(name: str) -> int:
    if name == "character.glb":
        return _GLB_PACKAGE_LIMIT
    if name == "animation_clip.json":
        return _ANIMATION_CLIP_JSON_PACKAGE_LIMIT
    return _JSON_PACKAGE_LIMIT


def _read_bounded_package_file(path: Path) -> bytes:
    _reject_preview_lexical(path.absolute(), label=path.name)
    limit = _package_file_byte_limit(path.name)
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValidationError(f"animation clip package file {path.name} exceeds its byte limit")
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValidationError(f"animation clip package file {path.name} exceeds its byte limit")
    if len(payload) != info.st_size:
        raise ValidationError(f"animation clip package file {path.name} size changed during read")
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


def _assert_godot_animation_clip_duration(clip: AuthoredAnimationClip) -> None:
    if clip.duration_seconds < _GODOT_ANIMATION_MIN_DURATION_SECONDS:
        raise ValidationError(
            "animation clip duration_seconds is below the Godot adapter minimum of 0.001"
        )


def _verified_weighted_bone_names(decoded: DecodedInternalSkinnedGLB) -> tuple[str, ...]:
    joint_names = _decoded_contract_joint_names(decoded)
    weighted: list[str] = []
    for slot, name in enumerate(joint_names):
        if _bone_has_positive_finite_weight(decoded, slot):
            weighted.append(name)
    return tuple(weighted)


def _decode_verified_glb_snapshot(
    glb_bytes: bytes, expected_sha256: str
) -> DecodedInternalSkinnedGLB:
    digest = hashlib.sha256(glb_bytes).hexdigest()
    if digest != expected_sha256:
        raise ValidationError("preview GLB digest does not match verified binding")
    fd, tmp_name = tempfile.mkstemp(prefix="gf_clip_glb_", suffix=".glb")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(glb_bytes)
        decoded = decode_internal_skinned_glb(tmp_path, max_file_size_bytes=_GLB_PACKAGE_LIMIT)
    finally:
        tmp_path.unlink(missing_ok=True)
    if decoded.sha256 != expected_sha256:
        raise ValidationError("decoded GLB digest does not match verified binding")
    return decoded


@dataclass(frozen=True)
class _BoundAuthorizedClipSource:
    raw_bytes: bytes
    sha256: str
    clip: AuthoredAnimationClip
    verified_glb_sha256: str
    verified_weighted_bone_names: tuple[str, ...]

    def assert_unchanged(self, other: _BoundAuthorizedClipSource) -> None:
        if other.raw_bytes != self.raw_bytes or other.sha256 != self.sha256:
            raise CandidateCurrentnessError("source clip bindings drifted during export")
        if other.verified_glb_sha256 != self.verified_glb_sha256:
            raise CandidateCurrentnessError("verified GLB binding drifted during export")
        if other.verified_weighted_bone_names != self.verified_weighted_bone_names:
            raise CandidateCurrentnessError(
                "verified weighted bone capability drifted during export"
            )

    @classmethod
    def bind(
        cls,
        clip_path: Path,
        preview_dir: Path,
        bindings: _PreviewBindings,
    ) -> _BoundAuthorizedClipSource:
        loaded = load_local_animation_clip(clip_path)
        _assert_godot_animation_clip_duration(loaded.clip)
        glb_path = preview_dir.resolve(strict=True) / "character.glb"
        glb_bytes = _read_bounded_package_file(glb_path)
        expected_sha = bindings.processed_art.content_hash
        decoded = _decode_verified_glb_snapshot(glb_bytes, expected_sha)
        assert_animation_clip_skin_influence(loaded.clip, decoded)
        bone_names = _verified_weighted_bone_names(decoded)
        return cls(
            raw_bytes=loaded.raw_bytes,
            sha256=loaded.sha256,
            clip=loaded.clip,
            verified_glb_sha256=expected_sha,
            verified_weighted_bone_names=bone_names,
        )


def _clip_bindings_digest(
    bindings: _PreviewBindings,
    workflow_id: str,
    upstream_preview_manifest_sha256: str,
    clip_sha256: str,
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
            "clip_sha256": clip_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_clip_manifest(
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    upstream_preview_manifest_sha256: str,
    clip_sha256: str,
    file_digests: dict[str, str],
) -> dict[str, Any]:
    readiness = bindings.readiness
    from gamefactory.core.domain.v08_candidate_contracts import candidate_spec_fingerprint

    return {
        "schema_version": ANIMATION_CLIP_MANIFEST_SCHEMA,
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
        "clip_sha256": clip_sha256,
        "file_digests": dict(sorted(file_digests.items())),
        "animation_clip_preview_status": ANIMATION_CLIP_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _clip_package_file_bytes(
    upstream_bytes: dict[str, bytes],
    clip_raw_bytes: bytes,
    *,
    expected_clip_sha256: str,
    verified_weighted_bone_names: tuple[str, ...],
) -> dict[str, bytes]:
    payloads = dict(upstream_bytes)
    payloads["animation_clip.json"] = clip_raw_bytes
    payloads["animation_clip_preview.tscn"] = _build_animation_clip_preview_tscn(
        expected_clip_sha256=expected_clip_sha256,
        verified_weighted_bone_names=verified_weighted_bone_names,
    ).encode("utf-8")
    payloads["animation_clip_preview_player.gd"] = _player_script_bytes()
    payloads["project.godot"] = _build_animation_clip_project_godot().encode("utf-8")
    return payloads


def _bounded_package_digest(path: Path) -> tuple[bytes, str]:
    payload = _read_bounded_package_file(path)
    return payload, hashlib.sha256(payload).hexdigest()


def _inspect_clip_preview_directory(
    clip_preview_dir: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    lexical = Path(clip_preview_dir).absolute()
    _reject_preview_lexical(lexical, label="animation clip preview directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="animation clip preview directory")
    if not root.is_dir():
        raise ValidationError("animation clip preview path is not a directory")
    names = {item.name for item in root.iterdir()}
    if names != _CLIP_PACKAGE_FILES:
        extra = sorted(names - _CLIP_PACKAGE_FILES)
        missing = sorted(_CLIP_PACKAGE_FILES - names)
        raise ValidationError(
            f"animation clip preview file set mismatch (missing={missing}, extra={extra})"
        )
    for name in names:
        path = root / name
        _reject_preview_lexical(path, label=name)
        if path_crosses_link(path):
            raise ValidationError(f"animation clip preview file {name} crosses a link")
        if not path.is_file():
            raise ValidationError(f"animation clip preview entry {name} is not a regular file")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValidationError(f"animation clip preview entry {name} is not a regular file")

    manifest_path = root / "animation_clip_manifest.json"
    manifest_doc = load_bounded_publication_control_json(
        manifest_path, label="animation clip preview manifest"
    )
    if set(manifest_doc) != _CLIP_MANIFEST_KEYS:
        raise ValidationError("animation clip preview manifest fields do not match the contract")
    digests_declared = manifest_doc.get("file_digests")
    if not isinstance(digests_declared, dict):
        raise ValidationError("animation clip preview manifest file_digests must be an object")
    expected_names = sorted(_CLIP_PACKAGE_FILES - {"animation_clip_manifest.json"})
    if sorted(digests_declared) != expected_names:
        raise ValidationError("animation clip preview manifest file_digests keys do not match")
    digests_live: dict[str, str] = {}
    total = 0
    for name in expected_names:
        path = root / name
        size = path.stat().st_size
        total += size
        if total > _MAX_CLIP_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation clip preview package exceeds byte limit on inspection")
        _, digest = _bounded_package_digest(path)
        digests_live[name] = digest
        declared = digests_declared.get(name)
        if not isinstance(declared, str) or declared != digest:
            raise CandidateCurrentnessError(f"animation clip preview file {name} digest mismatch")
    clip_sha = manifest_doc.get("clip_sha256")
    if not isinstance(clip_sha, str):
        raise ValidationError("animation clip preview manifest clip_sha256 must be a string")
    clip_bytes, clip_digest = _bounded_package_digest(root / "animation_clip.json")
    if clip_digest != clip_sha:
        raise CandidateCurrentnessError(
            "animation clip.json digest does not match manifest clip_sha256"
        )
    try:
        clip_doc = parse_strict_runtime_json_object(
            clip_bytes, max_bytes=_ANIMATION_CLIP_JSON_PACKAGE_LIMIT
        )
    except CandidateRuntimeJsonError as exc:
        raise ValidationError("animation clip.json JSON policy violation") from exc
    except (RecursionError, ValueError) as exc:
        raise ValidationError("animation clip.json JSON policy violation") from exc
    try:
        parse_animation_clip_document(clip_doc)
    except (ValidationError, TypeError) as exc:
        raise ValidationError("animation clip.json is not a valid closed clip document") from exc
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


def _clip_preview_matches_bindings(
    handlers: CandidateWorkflowHandlers,
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    preview_dir: Path,
    clip_path: Path,
    manifest_doc: dict[str, Any],
    digests_live: dict[str, str],
) -> bool:
    if not _upstream_matches_bindings(handlers, bindings, workflow_id, preview_dir):
        return False
    try:
        bound = _BoundAuthorizedClipSource.bind(clip_path, preview_dir, bindings)
    except (OSError, ValidationError):
        return False
    upstream_bytes = _read_upstream_preview_files(preview_dir)
    upstream_manifest_sha = _upstream_preview_manifest_sha256(upstream_bytes)
    expected_bytes = _clip_package_file_bytes(
        upstream_bytes,
        bound.raw_bytes,
        expected_clip_sha256=bound.sha256,
        verified_weighted_bone_names=bound.verified_weighted_bone_names,
    )
    expected_digests = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in expected_bytes.items()
    }
    if digests_live != expected_digests:
        return False
    expected_manifest = _build_clip_manifest(
        bindings,
        workflow_id=workflow_id,
        upstream_preview_manifest_sha256=upstream_manifest_sha,
        clip_sha256=bound.sha256,
        file_digests=expected_digests,
    )
    return _canonical_json_bytes(manifest_doc) == _canonical_json_bytes(expected_manifest)


def _write_clip_preview_tree(
    stage: Path,
    bindings: _PreviewBindings,
    upstream_bytes: dict[str, bytes],
    bound_source: _BoundAuthorizedClipSource,
    *,
    workflow_id: str,
) -> dict[str, str]:
    clip_raw_bytes = bound_source.raw_bytes
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="animation clip preview staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="animation clip preview staging container")

    package_bytes = _clip_package_file_bytes(
        upstream_bytes,
        clip_raw_bytes,
        expected_clip_sha256=bound_source.sha256,
        verified_weighted_bone_names=bound_source.verified_weighted_bone_names,
    )
    file_digests: dict[str, str] = {}
    total = 0
    clip_sha = bound_source.sha256
    for name in sorted(_CLIP_PACKAGE_FILES - {"animation_clip_manifest.json"}):
        path = stage_resolved / name
        path_lexical = path.absolute()
        _reject_preview_lexical(path_lexical, label=name)
        payload = package_bytes[name]
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=name)
        size = path.stat().st_size
        total += size
        if total > _MAX_CLIP_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation clip preview package exceeds byte limit")
        file_digests[name] = hashlib.sha256(payload).hexdigest()

    upstream_manifest_sha = _upstream_preview_manifest_sha256(upstream_bytes)
    manifest = _build_clip_manifest(
        bindings,
        workflow_id=workflow_id,
        upstream_preview_manifest_sha256=upstream_manifest_sha,
        clip_sha256=clip_sha,
        file_digests=file_digests,
    )
    manifest_path = stage_resolved / "animation_clip_manifest.json"
    _reject_preview_lexical(manifest_path.absolute(), label="animation_clip_manifest.json")
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise ValidationError("animation clip preview manifest exceeds JSON byte limit")
    manifest_path.write_bytes(manifest_bytes)
    _reject_preview_lexical(
        manifest_path.resolve(strict=True), label="animation_clip_manifest.json"
    )
    return file_digests


def export_rigged_character_animation_clip_preview(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_path: Path | str,
    output_dir: Path | str,
) -> CandidateAnimationClipPreviewResult:
    """Export animation clip preview from live C2-B evidence, V0.8-4 preview, and a local clip."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow_id)
    upstream_root = Path(preview_dir)
    source_clip_path = Path(clip_path)
    bindings_before = _resolve_preview_bindings(handlers, workflow_id)
    if not _upstream_matches_bindings(handlers, bindings_before, workflow_id, upstream_root):
        raise CandidateCurrentnessError("upstream V0.8-4 preview is missing or not current")

    bound_before = _BoundAuthorizedClipSource.bind(source_clip_path, upstream_root, bindings_before)

    destination = _validate_output_dir(Path(output_dir))
    _assert_destination_fresh(destination)

    upstream_before = _read_upstream_preview_files(upstream_root)
    upstream_manifest_before = _upstream_preview_manifest_sha256(upstream_before)
    digest_before = _clip_bindings_digest(
        bindings_before, workflow_id, upstream_manifest_before, bound_before.sha256
    )

    stage, staging_parent = _fresh_clip_stage(destination.parent, handlers.root)
    try:
        _write_clip_preview_tree(
            stage,
            bindings_before,
            upstream_before,
            bound_before,
            workflow_id=workflow_id,
        )
        bindings_after = _resolve_preview_bindings(handlers, workflow_id)
        if not _upstream_matches_bindings(handlers, bindings_after, workflow_id, upstream_root):
            raise CandidateCurrentnessError("upstream preview became stale during staging")
        bound_after = _BoundAuthorizedClipSource.bind(
            source_clip_path, upstream_root, bindings_after
        )
        bound_before.assert_unchanged(bound_after)
        upstream_after = _read_upstream_preview_files(upstream_root)
        upstream_manifest_after = _upstream_preview_manifest_sha256(upstream_after)
        digest_after = _clip_bindings_digest(
            bindings_after, workflow_id, upstream_manifest_after, bound_after.sha256
        )
        if digest_after != digest_before or upstream_after != upstream_before:
            raise CandidateCurrentnessError(
                "readiness, upstream preview, or source clip bindings drifted during staging"
            )
        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(
            publish_parent_lexical, label="animation clip preview publish parent"
        )
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="animation clip preview publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("animation clip preview publication path mismatch")
        manifest_doc, digests_live = _inspect_clip_preview_directory(destination)
        bindings_publish = _resolve_preview_bindings(handlers, workflow_id)
        bound_publish = _BoundAuthorizedClipSource.bind(
            source_clip_path, upstream_root, bindings_publish
        )
        bound_before.assert_unchanged(bound_publish)
        if not _clip_preview_matches_bindings(
            handlers,
            bindings_publish,
            workflow_id=workflow_id,
            preview_dir=upstream_root,
            clip_path=source_clip_path,
            manifest_doc=manifest_doc,
            digests_live=digests_live,
        ):
            raise ArtifactError("published animation clip preview does not match live bindings")
        manifest_sha = hashlib.sha256(
            _read_bounded_package_file(destination / "animation_clip_manifest.json")
        ).hexdigest()
        glb_sha = manifest_doc.get("processed_glb_sha256")
        if not isinstance(glb_sha, str):
            raise ArtifactError("animation clip manifest missing processed_glb_sha256")
        if digests_live.get("character.glb") != glb_sha:
            raise ArtifactError("published animation clip preview GLB hash mismatch")
        upstream_sha = manifest_doc.get("upstream_preview_manifest_sha256")
        if not isinstance(upstream_sha, str):
            raise ArtifactError("animation clip manifest missing upstream_preview_manifest_sha256")
        if digests_live.get("preview-manifest.json") != upstream_sha:
            raise ArtifactError("upstream preview manifest hash mismatch in animation clip package")
        clip_manifest_sha = manifest_doc.get("clip_sha256")
        if clip_manifest_sha != bound_before.sha256:
            raise ArtifactError("published clip_sha256 does not match source clip")
        scene_sha = digests_live["animation_clip_preview.tscn"]
        readiness = bindings_before.readiness
    except Exception:
        _discard_owned_preview_stage(stage, staging_parent)
        raise

    return CandidateAnimationClipPreviewResult(
        workflow_id=workflow_id,
        animation_clip_preview_root=destination.as_posix(),
        animation_clip_manifest_sha256=manifest_sha,
        upstream_preview_manifest_sha256=str(upstream_sha),
        processed_glb_sha256=str(glb_sha),
        clip_sha256=str(clip_manifest_sha),
        snapshot_fingerprint=readiness.snapshot.fingerprint(),
        evidence_execution_id=readiness.evidence_execution_id,
        evidence_attempt_number=readiness.evidence_attempt_number,
        manifest_artifact_id=readiness.manifest_artifact_id,
        result_artifact_id=readiness.result_artifact_id,
        marker_artifact_id=readiness.marker_artifact_id,
        animation_clip_preview_scene_sha256=scene_sha,
        production_eligible=False,
        promotion_eligible=False,
        animation_clip_preview_status=ANIMATION_CLIP_STATUS_TEST_ONLY,
    )


def animation_clip_preview_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_path: Path | str,
    animation_dir: Path | str,
) -> bool:
    """Return True when animation_dir matches live evidence, upstream preview, and source clip."""
    upstream_root = Path(preview_dir)
    clip_root = Path(clip_path)
    animation_root = Path(animation_dir)
    try:
        bindings = _resolve_preview_bindings(handlers, workflow_id)
    except (CandidateCurrentnessError, ValidationError, ArtifactError):
        return False
    try:
        loaded = load_local_animation_clip(clip_root)
    except (OSError, ValidationError):
        return False
    try:
        manifest, digests_live = _inspect_clip_preview_directory(animation_root)
    except (ValidationError, CandidateCurrentnessError):
        raise
    except OSError:
        return False
    if hashlib.sha256(loaded.raw_bytes).hexdigest() != manifest.get("clip_sha256"):
        return False
    return _clip_preview_matches_bindings(
        handlers,
        bindings,
        workflow_id=workflow_id,
        preview_dir=upstream_root,
        clip_path=clip_root,
        manifest_doc=manifest,
        digests_live=digests_live,
    )


__all__ = [
    "ACCEPTANCE_CLIP_DURATION_S",
    "ACCEPTANCE_CLIP_ID",
    "ANIMATION_CLIP_MANIFEST_SCHEMA",
    "ANIMATION_CLIP_STATUS_TEST_ONLY",
    "CandidateAnimationClipPreviewResult",
    "acceptance_arm_wave_clip_raw_bytes",
    "animation_clip_preview_current",
    "build_acceptance_arm_wave_clip_document",
    "export_rigged_character_animation_clip_preview",
]
