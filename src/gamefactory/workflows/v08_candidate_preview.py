"""V0.8-4 readiness-gated rigged character Godot preview export (test-only, no promotion)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    candidate_evidence_lexical_unsafe,
    load_bounded_publication_control_json,
)
from gamefactory.adapters.assets.v08_candidate_geometry import (
    capsule_center_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import Artifact, Task
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetSpecificationV08Candidate,
    candidate_spec_fingerprint,
    parse_asset_specification_v08_candidate,
)
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    assert_latest_attempt_completed,
    assert_zero_provider_activity,
    select_artifact_for_execution,
    verify_artifact_bytes_and_hash,
)
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    CandidateEvidenceReadiness,
    assert_candidate_workflow_engine_finalized,
)
from gamefactory.workflows.v08_candidate_gates import (
    MAX_CANDIDATE_JSON_ARTIFACT_BYTES,
    load_strict_json_artifact,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

PREVIEW_MANIFEST_SCHEMA = "candidate-character-preview-manifest-0.8.0"
PREVIEW_COLLIDER_SCHEMA = "candidate-preview-collider-0.8.0"
PREVIEW_CANDIDATE_SCHEMA = "candidate-preview-specification-0.8.0"
PREVIEW_STATUS_TEST_ONLY = "TEST_ONLY"

_PREVIEW_FILES_REQUIRED: frozenset[str] = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "project.godot",
    }
)
_PREVIEW_MANIFEST_KEYS = frozenset(
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
        "file_digests",
        "preview_status",
        "production_eligible",
        "promotion_eligible",
    }
)
_MAX_PREVIEW_GLB_BYTES = 64 * 1024 * 1024
_MAX_PREVIEW_TOTAL_BYTES = 96 * 1024 * 1024
_SAFE_DIR_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class CandidatePreviewResult:
    """Typed preview export outcome; never production- or promotion-eligible."""

    workflow_id: str
    preview_root: str
    manifest_sha256: str
    processed_glb_sha256: str
    snapshot_fingerprint: str
    evidence_execution_id: str
    evidence_attempt_number: int
    manifest_artifact_id: str
    result_artifact_id: str
    marker_artifact_id: str
    scene_sha256: str
    production_eligible: bool = False
    promotion_eligible: bool = False
    preview_status: str = PREVIEW_STATUS_TEST_ONLY


@dataclass(frozen=True)
class _PreviewBindings:
    readiness: CandidateEvidenceReadiness
    evidence_task_id: str
    processed_art: Artifact
    spec_doc: dict[str, Any]
    spec: AssetSpecificationV08Candidate
    capsule_center_m: tuple[float, float, float]
    marker_sha256: str
    manifest_sha256: str
    result_sha256: str


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _reject_preview_lexical(path: Path, *, label: str) -> None:
    if candidate_evidence_lexical_unsafe(path):
        raise ValidationError(f"{label} crosses a link or reparse point")


def _task_by_suffix(tasks: list[Task], suffix: str) -> Task:
    for task in tasks:
        if task.id.endswith(f"-{suffix}"):
            return task
    raise CandidateCurrentnessError(f"candidate task suffix {suffix} is missing from workflow")


def _evidence_task_id(handlers: CandidateWorkflowHandlers, workflow_id: str) -> str:
    for row in handlers.tasks.list_by_workflow(workflow_id):
        if row.task_type == "v08_candidate_evidence":
            return row.id
    raise CandidateCurrentnessError("workflow has no candidate evidence task")


def _artifact_sha256(handlers: CandidateWorkflowHandlers, artifact_id: str) -> str:
    row = handlers.artifacts.get(artifact_id)
    if row is None:
        raise CandidateCurrentnessError(f"artifact {artifact_id} is not registered")
    verify_artifact_bytes_and_hash(
        row, root_path=handlers.root, artifact_manager=handlers.artifact_manager
    )
    path = handlers.root / row.relative_path
    digest = sha256_file(path)
    if digest != row.content_hash:
        raise CandidateCurrentnessError(f"artifact {artifact_id} on-disk hash drifted")
    return digest


def _resolve_preview_bindings(
    handlers: CandidateWorkflowHandlers, workflow_id: str
) -> _PreviewBindings:
    readiness = assert_candidate_workflow_engine_finalized(handlers, workflow_id)
    task_rows = handlers.tasks.list_by_workflow(workflow_id)
    artifact_rows = handlers.artifacts.list_by_workflow(workflow_id)
    prepare_task = _task_by_suffix(task_rows, "PREPARE")
    identity_task = _task_by_suffix(task_rows, "IDENTITY")
    prepare_exec = assert_latest_attempt_completed(
        handlers.executions, prepare_task, purpose="candidate preview prepare"
    )
    identity_exec = assert_latest_attempt_completed(
        handlers.executions, identity_task, purpose="candidate preview identity"
    )
    spec_art = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-specification",
        execution=prepare_exec,
    )
    processed_art = select_artifact_for_execution(
        artifact_rows,
        task_id=identity_task.id,
        artifact_type="candidate-processed-glb",
        execution=identity_exec,
    )
    for art in (spec_art, processed_art):
        verify_artifact_bytes_and_hash(
            art, root_path=handlers.root, artifact_manager=handlers.artifact_manager
        )
    spec_doc = load_strict_json_artifact(handlers.root, spec_art)
    spec = parse_asset_specification_v08_candidate(spec_doc)
    if spec.processed_glb_sha256 != processed_art.content_hash:
        raise CandidateCurrentnessError("processed GLB registration does not match specification")
    glb_path = handlers.root / processed_art.relative_path
    if path_crosses_link(glb_path):
        raise CandidateCurrentnessError("processed GLB path crosses a link")
    glb_size = glb_path.stat().st_size
    if glb_size <= 0 or glb_size > _MAX_PREVIEW_GLB_BYTES:
        raise CandidateCurrentnessError("processed GLB size is out of preview bounds")
    if sha256_file(glb_path) != processed_art.content_hash:
        raise CandidateCurrentnessError("processed GLB bytes drifted from registration")
    decoded = decode_candidate_glb(glb_path)
    if decoded.sha256 != processed_art.content_hash:
        raise CandidateCurrentnessError("decoded processed GLB digest mismatch")
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    center = capsule_center_from_aabb(mins, maxs)
    marker_sha = _artifact_sha256(handlers, readiness.marker_artifact_id)
    manifest_sha = _artifact_sha256(handlers, readiness.manifest_artifact_id)
    result_sha = _artifact_sha256(handlers, readiness.result_artifact_id)
    return _PreviewBindings(
        readiness=readiness,
        evidence_task_id=_evidence_task_id(handlers, workflow_id),
        processed_art=processed_art,
        spec_doc=spec_doc,
        spec=spec,
        capsule_center_m=center,
        marker_sha256=marker_sha,
        manifest_sha256=manifest_sha,
        result_sha256=result_sha,
    )


def _bindings_digest(bindings: _PreviewBindings, workflow_id: str) -> str:
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
            "spec_fingerprint": candidate_spec_fingerprint(bindings.spec),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _godot_float(value: float) -> str:
    text = f"{float(value):.9g}"
    if "e" not in text and "." not in text:
        return f"{text}.0"
    return text


def _build_character_tscn(
    *,
    radius_m: float,
    height_m: float,
    center_m: tuple[float, float, float],
) -> str:
    cx, cy, cz = center_m
    transform = (
        f"Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, "
        f"{_godot_float(cx)}, {_godot_float(cy)}, {_godot_float(cz)})"
    )
    return (
        "[gd_scene load_steps=3 format=3]\n\n"
        '[ext_resource type="PackedScene" path="res://character.glb" id="1_character_glb"]\n\n'
        '[sub_resource type="CapsuleShape3D" id="CapsuleShape3D_preview"]\n'
        f"radius = {_godot_float(radius_m)}\n"
        f"height = {_godot_float(height_m)}\n\n"
        '[node name="CharacterPreview" type="Node3D"]\n\n'
        '[node name="CharacterModel" parent="." instance=ExtResource("1_character_glb")]\n\n'
        '[node name="PhysicsBody" type="StaticBody3D" parent="."]\n\n'
        '[node name="CollisionShape3D" type="CollisionShape3D" parent="PhysicsBody"]\n'
        f"transform = {transform}\n"
        'shape = SubResource("CapsuleShape3D_preview")\n'
    )


def _build_project_godot() -> str:
    return (
        "config_version=5\n\n"
        "[application]\n"
        'config/name="CandidateCharacterPreview"\n'
        'run/main_scene="res://character.tscn"\n\n'
        "[rendering]\n"
        'renderer/rendering_method="gl_compatibility"\n'
    )


def _candidate_json_document(spec_doc: dict[str, Any]) -> dict[str, Any]:
    """Canonical candidate.json envelope with explicit ineligible flags."""
    specification: dict[str, Any] = json.loads(
        json.dumps(spec_doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    return {
        "schema_version": PREVIEW_CANDIDATE_SCHEMA,
        "specification": specification,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _assert_same_volume(left: Path, right: Path) -> None:
    try:
        if os.stat(left).st_dev != os.stat(right).st_dev:
            raise ValidationError("preview staging must be on the same volume as output parent")
    except OSError as exc:
        raise ValidationError("preview staging volume check failed") from exc


def _build_collider_json(
    spec: AssetSpecificationV08Candidate,
    center_m: tuple[float, float, float],
) -> dict[str, Any]:
    capsule = spec.collider.capsule
    if capsule is None:
        raise ValidationError("preview requires capsule collider policy")
    return {
        "schema_version": PREVIEW_COLLIDER_SCHEMA,
        "policy": "capsule",
        "shape_class": "CapsuleShape3D",
        "radius_m": float(capsule.radius_m),
        "height_m": float(capsule.height_m),
        "center_m": [float(center_m[0]), float(center_m[1]), float(center_m[2])],
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _build_preview_manifest(
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    file_digests: dict[str, str],
) -> dict[str, Any]:
    readiness = bindings.readiness
    return {
        "schema_version": PREVIEW_MANIFEST_SCHEMA,
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
        "file_digests": dict(sorted(file_digests.items())),
        "preview_status": PREVIEW_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _preview_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="preview publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="preview publish parent")
    return publish_resolved / ".gf" / "candidate_preview_staging"


def _fresh_preview_stage(publish_parent: Path, project_root: Path) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="preview publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="preview project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="preview publish parent")
    _reject_preview_lexical(project_resolved, label="preview project root")

    staging_parent_lexical = publish_resolved / ".gf" / "candidate_preview_staging"
    _reject_preview_lexical(staging_parent_lexical, label="preview staging parent")
    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="preview staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="preview staging container")
    if stage_lexical.exists():
        raise ArtifactError("preview staging collision")
    stage_lexical.mkdir(parents=True, exist_ok=False)
    stage = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage, label="preview staging container")
    _assert_same_volume(stage, publish_resolved)
    if path_crosses_link(stage):
        raise ValidationError("preview staging container crosses a link")
    return stage, staging_parent


def _discard_owned_preview_stage(stage: Path, staging_parent: Path) -> None:
    """Remove only a validated, owned staging directory; retain uncertain paths."""
    if not stage.exists():
        return
    lexical = stage.absolute()
    if candidate_evidence_lexical_unsafe(lexical) or path_crosses_link(lexical):
        return
    try:
        resolved = lexical.resolve(strict=True)
    except OSError:
        return
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        return
    if not resolved.name.startswith("stage_"):
        return
    try:
        resolved.relative_to(staging_parent.resolve(strict=True))
    except ValueError:
        return
    if not resolved.is_dir():
        return
    shutil.rmtree(resolved)


def _assert_destination_fresh(destination: Path) -> None:
    lexical = destination.absolute()
    _reject_preview_lexical(lexical, label="preview destination")
    if os.path.lexists(str(lexical)):
        raise ArtifactError("preview destination already exists")


def _validate_output_dir(output_dir: Path) -> Path:
    lexical = output_dir.absolute()
    _reject_preview_lexical(lexical, label="preview output directory")
    resolved = lexical.resolve(strict=False)
    name = resolved.name
    if not name or not _SAFE_DIR_COMPONENT.fullmatch(name):
        raise ValidationError("preview output directory name is not a safe path component")
    parent = resolved.parent
    _reject_preview_lexical(parent, label="preview output parent")
    return resolved


def _preview_package_file_bytes(
    handlers: CandidateWorkflowHandlers,
    bindings: _PreviewBindings,
) -> dict[str, bytes]:
    capsule = bindings.spec.collider.capsule
    if capsule is None:
        raise ValidationError("preview requires capsule collider policy")
    glb_path = handlers.root / bindings.processed_art.relative_path
    glb_bytes = glb_path.read_bytes()
    if hashlib.sha256(glb_bytes).hexdigest() != bindings.processed_art.content_hash:
        raise CandidateCurrentnessError("processed GLB bytes drifted from registration")
    tscn_text = _build_character_tscn(
        radius_m=float(capsule.radius_m),
        height_m=float(capsule.height_m),
        center_m=bindings.capsule_center_m,
    )
    return {
        "character.glb": glb_bytes,
        "candidate.json": _canonical_json_bytes(_candidate_json_document(bindings.spec_doc)),
        "collider.json": _canonical_json_bytes(
            _build_collider_json(bindings.spec, bindings.capsule_center_m)
        ),
        "character.tscn": tscn_text.encode("utf-8"),
        "project.godot": _build_project_godot().encode("utf-8"),
    }


def _write_preview_tree(
    handlers: CandidateWorkflowHandlers,
    stage: Path,
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
) -> dict[str, str]:
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="preview staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="preview staging container")

    package_bytes = _preview_package_file_bytes(handlers, bindings)
    file_digests: dict[str, str] = {}
    total = 0
    for name in sorted(_PREVIEW_FILES_REQUIRED - {"preview-manifest.json"}):
        path = stage_resolved / name
        path_lexical = path.absolute()
        _reject_preview_lexical(path_lexical, label=name)
        payload = package_bytes[name]
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=name)
        size = path.stat().st_size
        total += size
        if total > _MAX_PREVIEW_TOTAL_BYTES:
            raise ValidationError("preview package exceeds byte limit")
        file_digests[name] = hashlib.sha256(payload).hexdigest()

    manifest = _build_preview_manifest(bindings, workflow_id=workflow_id, file_digests=file_digests)
    manifest_path = stage_resolved / "preview-manifest.json"
    _reject_preview_lexical(manifest_path.absolute(), label="preview-manifest.json")
    manifest_bytes = _canonical_json_bytes(manifest)
    if len(manifest_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise ValidationError("preview manifest exceeds JSON byte limit")
    manifest_path.write_bytes(manifest_bytes)
    _reject_preview_lexical(manifest_path.resolve(strict=True), label="preview-manifest.json")
    return file_digests


def _inspect_preview_directory(preview_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    lexical = Path(preview_dir).absolute()
    _reject_preview_lexical(lexical, label="preview directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="preview directory")
    if not root.is_dir():
        raise ValidationError("preview path is not a directory")
    names = {item.name for item in root.iterdir()}
    if names != _PREVIEW_FILES_REQUIRED:
        extra = sorted(names - _PREVIEW_FILES_REQUIRED)
        missing = sorted(_PREVIEW_FILES_REQUIRED - names)
        raise ValidationError(
            f"preview directory file set mismatch (missing={missing}, extra={extra})"
        )
    for name in names:
        path = root / name
        _reject_preview_lexical(path, label=name)
        if path_crosses_link(path):
            raise ValidationError(f"preview file {name} crosses a link")
        if not path.is_file():
            raise ValidationError(f"preview entry {name} is not a regular file")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValidationError(f"preview entry {name} is not a regular file")

    manifest_path = root / "preview-manifest.json"
    manifest_doc = load_bounded_publication_control_json(manifest_path, label="preview manifest")
    if set(manifest_doc) != _PREVIEW_MANIFEST_KEYS:
        raise ValidationError("preview manifest fields do not match the contract")
    digests_declared = manifest_doc.get("file_digests")
    if not isinstance(digests_declared, dict):
        raise ValidationError("preview manifest file_digests must be an object")
    expected_names = sorted(_PREVIEW_FILES_REQUIRED - {"preview-manifest.json"})
    if sorted(digests_declared) != expected_names:
        raise ValidationError("preview manifest file_digests keys do not match required files")
    digests_live: dict[str, str] = {}
    total = 0
    for name in expected_names:
        path = root / name
        size = path.stat().st_size
        total += size
        if total > _MAX_PREVIEW_TOTAL_BYTES:
            raise ValidationError("preview package exceeds byte limit on inspection")
        digest = sha256_file(path)
        digests_live[name] = digest
        declared = digests_declared.get(name)
        if not isinstance(declared, str) or declared != digest:
            raise CandidateCurrentnessError(f"preview file {name} digest mismatch")
    return manifest_doc, digests_live


def _preview_matches_bindings(
    handlers: CandidateWorkflowHandlers,
    bindings: _PreviewBindings,
    *,
    workflow_id: str,
    preview_dir: Path,
    manifest_doc: dict[str, Any],
    digests_live: dict[str, str],
) -> bool:
    expected_bytes = _preview_package_file_bytes(handlers, bindings)
    expected_digests = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in expected_bytes.items()
    }
    if digests_live != expected_digests:
        return False
    expected_manifest = _build_preview_manifest(
        bindings, workflow_id=workflow_id, file_digests=expected_digests
    )
    return _canonical_json_bytes(manifest_doc) == _canonical_json_bytes(expected_manifest)


def export_rigged_character_candidate_preview(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    output_dir: Path | str,
) -> CandidatePreviewResult:
    """Export a standalone Godot preview package from engine-finalized C2-B evidence."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow_id)
    destination = _validate_output_dir(Path(output_dir))
    _assert_destination_fresh(destination)

    bindings_before = _resolve_preview_bindings(handlers, workflow_id)
    digest_before = _bindings_digest(bindings_before, workflow_id)
    stage, staging_parent = _fresh_preview_stage(destination.parent, handlers.root)
    try:
        _write_preview_tree(handlers, stage, bindings_before, workflow_id=workflow_id)
        bindings_after = _resolve_preview_bindings(handlers, workflow_id)
        digest_after = _bindings_digest(bindings_after, workflow_id)
        if digest_after != digest_before:
            raise CandidateCurrentnessError("readiness bindings drifted during preview staging")
        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(publish_parent_lexical, label="preview publish parent")
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="preview publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("preview publication path mismatch")
        manifest_doc, digests_live = _inspect_preview_directory(destination)
        if not _preview_matches_bindings(
            handlers,
            bindings_before,
            workflow_id=workflow_id,
            preview_dir=destination,
            manifest_doc=manifest_doc,
            digests_live=digests_live,
        ):
            raise ArtifactError("published preview package does not match live bindings")
        manifest_sha = sha256_file(destination / "preview-manifest.json")
        glb_sha = manifest_doc.get("processed_glb_sha256")
        if not isinstance(glb_sha, str):
            raise ArtifactError("preview manifest missing processed_glb_sha256")
        if sha256_file(destination / "character.glb") != glb_sha:
            raise ArtifactError("published preview GLB hash mismatch")
        scene_sha = digests_live["character.tscn"]
        readiness = bindings_before.readiness
    except Exception:
        _discard_owned_preview_stage(stage, staging_parent)
        raise

    return CandidatePreviewResult(
        workflow_id=workflow_id,
        preview_root=destination.as_posix(),
        manifest_sha256=manifest_sha,
        processed_glb_sha256=str(glb_sha),
        snapshot_fingerprint=readiness.snapshot.fingerprint(),
        evidence_execution_id=readiness.evidence_execution_id,
        evidence_attempt_number=readiness.evidence_attempt_number,
        manifest_artifact_id=readiness.manifest_artifact_id,
        result_artifact_id=readiness.result_artifact_id,
        marker_artifact_id=readiness.marker_artifact_id,
        scene_sha256=scene_sha,
        production_eligible=False,
        promotion_eligible=False,
        preview_status=PREVIEW_STATUS_TEST_ONLY,
    )


def candidate_preview_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
) -> bool:
    """Return True when preview_dir matches live evidence readiness; False when stale/not ready."""
    root = Path(preview_dir)
    try:
        bindings = _resolve_preview_bindings(handlers, workflow_id)
    except (CandidateCurrentnessError, ValidationError, ArtifactError):
        return False
    try:
        manifest, digests_live = _inspect_preview_directory(root)
    except (ValidationError, CandidateCurrentnessError):
        raise
    except OSError:
        return False
    return _preview_matches_bindings(
        handlers,
        bindings,
        workflow_id=workflow_id,
        preview_dir=root,
        manifest_doc=manifest,
        digests_live=digests_live,
    )


__all__ = [
    "CandidatePreviewResult",
    "PREVIEW_MANIFEST_SCHEMA",
    "PREVIEW_STATUS_TEST_ONLY",
    "candidate_preview_current",
    "export_rigged_character_candidate_preview",
]
