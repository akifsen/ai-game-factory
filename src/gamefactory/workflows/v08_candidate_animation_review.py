"""V0.8-7 readiness-gated rigged character animation review export (test-only)."""

from __future__ import annotations

import hashlib
import json
import secrets
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    load_bounded_publication_control_json,
    parse_bounded_publication_json,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_MANIFEST_KEYS,
    _CLIP_PACKAGE_FILES,
    _MAX_CLIP_PACKAGE_TOTAL_BYTES,
    _read_bounded_package_file,
    animation_clip_preview_current,
)
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
    _reject_preview_lexical,
    _validate_output_dir,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

ANIMATION_REVIEW_MANIFEST_SCHEMA = "candidate-character-animation-review-manifest-0.8.0"
ANIMATION_REVIEW_STATUS_TEST_ONLY = PREVIEW_STATUS_TEST_ONLY

_REVIEW_UI_FILES: frozenset[str] = frozenset(
    {
        "animation_review.tscn",
        "animation_review_controller.gd",
    }
)
_REVIEW_DIGEST_FILES: frozenset[str] = frozenset(_CLIP_PACKAGE_FILES | _REVIEW_UI_FILES)
_REVIEW_PACKAGE_FILES: frozenset[str] = frozenset(
    _REVIEW_DIGEST_FILES | {"animation_review_manifest.json"}
)
_REVIEW_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "upstream_animation_manifest_sha256",
        "upstream_animation_package_sha256",
        "clip_sha256",
        "file_digests",
        "animation_review_status",
        "production_eligible",
        "promotion_eligible",
    }
)
_CLIP_MANIFEST_IDENTITY_FIELDS = (
    "snapshot_fingerprint",
    "evidence_execution_id",
    "evidence_attempt_number",
    "manifest_artifact_id",
    "result_artifact_id",
    "marker_artifact_id",
    "processed_glb_sha256",
    "upstream_preview_manifest_sha256",
)


@dataclass(frozen=True)
class CandidateAnimationReviewResult:
    """Typed animation review export outcome; never production- or promotion-eligible."""

    workflow_id: str
    animation_review_root: str
    animation_review_manifest_sha256: str
    upstream_animation_manifest_sha256: str
    upstream_preview_manifest_sha256: str
    processed_glb_sha256: str
    clip_sha256: str
    snapshot_fingerprint: str
    evidence_execution_id: str
    evidence_attempt_number: int
    manifest_artifact_id: str
    result_artifact_id: str
    marker_artifact_id: str
    animation_review_scene_sha256: str
    production_eligible: bool = False
    promotion_eligible: bool = False
    animation_review_status: str = ANIMATION_REVIEW_STATUS_TEST_ONLY


def _review_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation review publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation review publish parent")
    return publish_resolved / ".gf" / "candidate_animation_review_staging"


def _fresh_review_stage(publish_parent: Path, project_root: Path) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="animation review publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="animation review project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="animation review publish parent")
    _reject_preview_lexical(project_resolved, label="animation review project root")

    staging_parent_lexical = _review_staging_parent(publish_resolved)
    _reject_preview_lexical(staging_parent_lexical, label="animation review staging parent")
    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="animation review staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="animation review staging container")
    if stage_lexical.exists():
        raise ArtifactError("animation review staging collision")
    stage_lexical.mkdir(parents=True, exist_ok=False)
    stage = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage, label="animation review staging container")
    _assert_same_volume(stage, publish_resolved)
    if path_crosses_link(stage):
        raise ValidationError("animation review staging container crosses a link")
    return stage, staging_parent


def _review_controller_bytes() -> bytes:
    raw = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("animation_review_controller.gd")
        .read_bytes()
    )
    return raw.replace(b"\r\n", b"\n")


def _review_tscn_bytes() -> bytes:
    raw = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("animation_review.tscn")
        .read_bytes()
    )
    return raw.replace(b"\r\n", b"\n")


def trusted_animation_review_ui_digests() -> dict[str, str]:
    """SHA-256 digests for checked-in review UI resources (LF-normalized)."""
    return {
        "animation_review.tscn": hashlib.sha256(_review_tscn_bytes()).hexdigest(),
        "animation_review_controller.gd": hashlib.sha256(_review_controller_bytes()).hexdigest(),
    }


def _read_v086_package_files(animation_dir: Path) -> dict[str, bytes]:
    lexical = Path(animation_dir).absolute()
    _reject_preview_lexical(lexical, label="animation clip preview directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="animation clip preview directory")
    payloads: dict[str, bytes] = {}
    for name in sorted(_CLIP_PACKAGE_FILES):
        payloads[name] = _read_bounded_package_file(root / name)
    return payloads


def _try_read_v086_package_files(animation_dir: Path) -> dict[str, bytes] | None:
    try:
        return _read_v086_package_files(animation_dir)
    except (OSError, ValidationError):
        return None


def _parse_gated_v086_clip_manifest(v086_bytes: dict[str, bytes]) -> dict[str, Any]:
    manifest_raw = v086_bytes.get("animation_clip_manifest.json")
    if manifest_raw is None:
        raise ValidationError("animation clip manifest is missing")
    manifest_doc = parse_bounded_publication_json(manifest_raw)
    if set(manifest_doc) != _CLIP_MANIFEST_KEYS:
        raise ValidationError("animation clip preview manifest fields do not match the contract")
    return manifest_doc


def _require_v086_snapshot_unchanged(
    animation_dir: Path,
    baseline: dict[str, bytes],
    *,
    detail: str,
) -> None:
    try:
        live = _read_v086_package_files(animation_dir)
    except (OSError, ValidationError) as exc:
        raise CandidateCurrentnessError(detail) from exc
    if live != baseline:
        raise CandidateCurrentnessError(detail)


def _upstream_animation_manifest_sha256(v086_bytes: dict[str, bytes]) -> str:
    manifest = v086_bytes.get("animation_clip_manifest.json")
    if manifest is None:
        raise ValidationError("animation clip manifest is missing")
    return hashlib.sha256(manifest).hexdigest()


def _animation_package_root_digest(v086_bytes: dict[str, bytes]) -> str:
    digests = {
        name: hashlib.sha256(v086_bytes[name]).hexdigest() for name in sorted(_CLIP_PACKAGE_FILES)
    }
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _review_package_file_bytes(v086_bytes: dict[str, bytes]) -> dict[str, bytes]:
    payloads = dict(v086_bytes)
    payloads["animation_review.tscn"] = _review_tscn_bytes()
    payloads["animation_review_controller.gd"] = _review_controller_bytes()
    return payloads


def _bounded_review_digest(path: Path) -> tuple[bytes, str]:
    payload = _read_bounded_package_file(path)
    return payload, hashlib.sha256(payload).hexdigest()


def _build_review_manifest(
    *,
    workflow_id: str,
    upstream_animation_manifest_sha256: str,
    upstream_animation_package_sha256: str,
    clip_sha256: str,
    file_digests: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": ANIMATION_REVIEW_MANIFEST_SCHEMA,
        "workflow_id": workflow_id,
        "upstream_animation_manifest_sha256": upstream_animation_manifest_sha256,
        "upstream_animation_package_sha256": upstream_animation_package_sha256,
        "clip_sha256": clip_sha256,
        "file_digests": dict(sorted(file_digests.items())),
        "animation_review_status": ANIMATION_REVIEW_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _expected_review_from_v086(
    v086_bytes: dict[str, bytes],
    clip_manifest_doc: dict[str, Any],
    *,
    workflow_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    clip_sha = clip_manifest_doc.get("clip_sha256")
    if not isinstance(clip_sha, str):
        raise ValidationError("animation clip manifest clip_sha256 must be a string")
    upstream_anim_sha = _upstream_animation_manifest_sha256(v086_bytes)
    package_sha = _animation_package_root_digest(v086_bytes)
    package_bytes = _review_package_file_bytes(v086_bytes)
    file_digests = {
        name: hashlib.sha256(payload).hexdigest() for name, payload in package_bytes.items()
    }
    manifest = _build_review_manifest(
        workflow_id=workflow_id,
        upstream_animation_manifest_sha256=upstream_anim_sha,
        upstream_animation_package_sha256=package_sha,
        clip_sha256=clip_sha,
        file_digests=file_digests,
    )
    return manifest, file_digests


def _clip_manifest_identities(clip_manifest_doc: dict[str, Any]) -> dict[str, Any]:
    identities: dict[str, Any] = {}
    for key in _CLIP_MANIFEST_IDENTITY_FIELDS:
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


def _inspect_review_directory(
    review_dir: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    lexical = Path(review_dir).absolute()
    _reject_preview_lexical(lexical, label="animation review directory")
    root = lexical.resolve(strict=True)
    _reject_preview_lexical(root, label="animation review directory")
    if not root.is_dir():
        raise ValidationError("animation review path is not a directory")
    names = {item.name for item in root.iterdir()}
    if names != _REVIEW_PACKAGE_FILES:
        extra = sorted(names - _REVIEW_PACKAGE_FILES)
        missing = sorted(_REVIEW_PACKAGE_FILES - names)
        raise ValidationError(
            f"animation review file set mismatch (missing={missing}, extra={extra})"
        )
    for name in names:
        path = root / name
        _reject_preview_lexical(path, label=name)
        if path_crosses_link(path):
            raise ValidationError(f"animation review file {name} crosses a link")
        if not path.is_file():
            raise ValidationError(f"animation review entry {name} is not a regular file")
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise ValidationError(f"animation review entry {name} is not a regular file")

    manifest_path = root / "animation_review_manifest.json"
    manifest_doc = load_bounded_publication_control_json(
        manifest_path, label="animation review manifest"
    )
    if set(manifest_doc) != _REVIEW_MANIFEST_KEYS:
        raise ValidationError("animation review manifest fields do not match the contract")

    digests_declared = manifest_doc.get("file_digests")
    if not isinstance(digests_declared, dict):
        raise ValidationError("animation review manifest file_digests must be an object")
    expected_names = sorted(_REVIEW_DIGEST_FILES)
    if sorted(digests_declared) != expected_names:
        raise ValidationError("animation review manifest file_digests keys do not match")

    digests_live: dict[str, str] = {}
    total = 0
    for name in expected_names:
        path = root / name
        size = path.stat().st_size
        total += size
        if total > _MAX_CLIP_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation review package exceeds byte limit on inspection")
        _, digest = _bounded_review_digest(path)
        digests_live[name] = digest
        declared = digests_declared.get(name)
        if not isinstance(declared, str) or declared != digest:
            raise CandidateCurrentnessError(f"animation review file {name} digest mismatch")

    clip_sha = manifest_doc.get("clip_sha256")
    if not isinstance(clip_sha, str):
        raise ValidationError("animation review manifest clip_sha256 must be a string")
    _, clip_digest = _bounded_review_digest(root / "animation_clip.json")
    if clip_digest != clip_sha:
        raise CandidateCurrentnessError(
            "animation_clip.json digest does not match manifest clip_sha256"
        )

    upstream_anim_sha = manifest_doc.get("upstream_animation_manifest_sha256")
    if not isinstance(upstream_anim_sha, str):
        raise ValidationError(
            "animation review manifest upstream_animation_manifest_sha256 must be a string"
        )
    _, anim_manifest_digest = _bounded_review_digest(root / "animation_clip_manifest.json")
    if anim_manifest_digest != upstream_anim_sha:
        raise CandidateCurrentnessError(
            "animation_clip_manifest.json digest does not match upstream_animation_manifest_sha256"
        )

    package_sha = manifest_doc.get("upstream_animation_package_sha256")
    if not isinstance(package_sha, str):
        raise ValidationError(
            "animation review manifest upstream_animation_package_sha256 must be a string"
        )

    return manifest_doc, digests_live


def _review_matches_upstream(
    *,
    workflow_id: str,
    v086_bytes: dict[str, bytes],
    clip_manifest_doc: dict[str, Any],
    manifest_doc: dict[str, Any],
    digests_live: dict[str, str],
) -> bool:
    try:
        expected_manifest, expected_digests = _expected_review_from_v086(
            v086_bytes, clip_manifest_doc, workflow_id=workflow_id
        )
    except ValidationError:
        return False

    if digests_live != expected_digests:
        return False
    if _canonical_json_bytes(manifest_doc) != _canonical_json_bytes(expected_manifest):
        return False

    upstream_anim_sha = _upstream_animation_manifest_sha256(v086_bytes)
    package_sha = _animation_package_root_digest(v086_bytes)
    clip_sha = clip_manifest_doc.get("clip_sha256")
    if not isinstance(clip_sha, str):
        return False
    if manifest_doc.get("upstream_animation_manifest_sha256") != upstream_anim_sha:
        return False
    if manifest_doc.get("upstream_animation_package_sha256") != package_sha:
        return False
    if manifest_doc.get("clip_sha256") != clip_sha:
        return False

    trusted_ui = trusted_animation_review_ui_digests()
    for name, trusted in trusted_ui.items():
        if digests_live.get(name) != trusted:
            return False
    return True


def _assert_staged_review_matches_expected(
    stage: Path,
    v086_bytes: dict[str, bytes],
    clip_manifest_doc: dict[str, Any],
    *,
    workflow_id: str,
) -> None:
    stage_manifest, stage_digests = _inspect_review_directory(stage)
    expected_manifest, expected_digests = _expected_review_from_v086(
        v086_bytes, clip_manifest_doc, workflow_id=workflow_id
    )
    if stage_digests != expected_digests:
        raise CandidateCurrentnessError(
            "staged animation review digests do not match authoritative upstream package"
        )
    if _canonical_json_bytes(stage_manifest) != _canonical_json_bytes(expected_manifest):
        raise CandidateCurrentnessError(
            "staged animation review manifest does not match expected canonical bindings"
        )
    trusted_ui = trusted_animation_review_ui_digests()
    for name, trusted in trusted_ui.items():
        if stage_digests.get(name) != trusted:
            raise CandidateCurrentnessError(
                f"staged animation review UI resource {name} digest mismatch"
            )


def _write_review_tree(
    stage: Path,
    v086_bytes: dict[str, bytes],
    clip_manifest_doc: dict[str, Any],
    *,
    workflow_id: str,
) -> dict[str, str]:
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="animation review staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="animation review staging container")

    _, expected_digests = _expected_review_from_v086(
        v086_bytes, clip_manifest_doc, workflow_id=workflow_id
    )
    package_bytes = _review_package_file_bytes(v086_bytes)
    file_digests: dict[str, str] = {}
    total = 0
    for name in sorted(_REVIEW_DIGEST_FILES):
        path = stage_resolved / name
        _reject_preview_lexical(path.absolute(), label=name)
        payload = package_bytes[name]
        path.write_bytes(payload)
        _reject_preview_lexical(path.resolve(strict=True), label=name)
        size = path.stat().st_size
        total += size
        if total > _MAX_CLIP_PACKAGE_TOTAL_BYTES:
            raise ValidationError("animation review package exceeds byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_digests[name]:
            raise ArtifactError("animation review staged digest mismatch during write")
        file_digests[name] = digest

    expected_manifest, _ = _expected_review_from_v086(
        v086_bytes, clip_manifest_doc, workflow_id=workflow_id
    )
    manifest_path = stage_resolved / "animation_review_manifest.json"
    _reject_preview_lexical(manifest_path.absolute(), label="animation_review_manifest.json")
    manifest_bytes = _canonical_json_bytes(expected_manifest)
    if len(manifest_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise ValidationError("animation review manifest exceeds JSON byte limit")
    manifest_path.write_bytes(manifest_bytes)
    _reject_preview_lexical(
        manifest_path.resolve(strict=True), label="animation_review_manifest.json"
    )
    return file_digests


def export_rigged_character_animation_review(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_path: Path | str,
    animation_dir: Path | str,
    output_dir: Path | str,
) -> CandidateAnimationReviewResult:
    """Export animation review from a current V0.8-6 animation clip preview package."""
    assert_zero_provider_activity(handlers.artifacts.db, workflow_id)
    upstream_root = Path(preview_dir)
    source_clip_path = Path(clip_path)
    animation_root = Path(animation_dir)
    try:
        v086_initial = _read_v086_package_files(animation_root)
    except OSError as exc:
        raise CandidateCurrentnessError(
            "upstream V0.8-6 animation clip preview is missing or not current"
        ) from exc
    if not animation_clip_preview_current(
        handlers, workflow_id, upstream_root, source_clip_path, animation_root
    ):
        raise CandidateCurrentnessError(
            "upstream V0.8-6 animation clip preview is missing or not current"
        )
    _require_v086_snapshot_unchanged(
        animation_root,
        v086_initial,
        detail="upstream animation package drifted during pre-stage currentness gate",
    )
    clip_manifest_initial = _parse_gated_v086_clip_manifest(v086_initial)
    identities = _clip_manifest_identities(clip_manifest_initial)
    clip_sha_initial = clip_manifest_initial.get("clip_sha256")
    if not isinstance(clip_sha_initial, str):
        raise ValidationError("animation clip manifest clip_sha256 must be a string")

    destination = _validate_output_dir(Path(output_dir))
    _assert_destination_fresh(destination)

    stage, staging_parent = _fresh_review_stage(destination.parent, handlers.root)
    try:
        _write_review_tree(
            stage,
            v086_initial,
            clip_manifest_initial,
            workflow_id=workflow_id,
        )
        _require_v086_snapshot_unchanged(
            animation_root,
            v086_initial,
            detail="upstream animation package drifted before pre-publish currentness gate",
        )
        if not animation_clip_preview_current(
            handlers, workflow_id, upstream_root, source_clip_path, animation_root
        ):
            raise CandidateCurrentnessError(
                "upstream animation clip preview became stale during staging"
            )
        _require_v086_snapshot_unchanged(
            animation_root,
            v086_initial,
            detail="upstream animation package or source clip bindings drifted during staging",
        )
        _assert_staged_review_matches_expected(
            stage,
            v086_initial,
            clip_manifest_initial,
            workflow_id=workflow_id,
        )
        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(publish_parent_lexical, label="animation review publish parent")
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="animation review publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("animation review publication path mismatch")
        _require_v086_snapshot_unchanged(
            animation_root,
            v086_initial,
            detail="upstream animation package drifted before post-publish currentness gate",
        )
        if not animation_clip_preview_current(
            handlers, workflow_id, upstream_root, source_clip_path, animation_root
        ):
            raise CandidateCurrentnessError(
                "upstream animation clip preview became stale after publish"
            )
        _require_v086_snapshot_unchanged(
            animation_root,
            v086_initial,
            detail="upstream animation package drifted after post-publish currentness gate",
        )
        manifest_doc, digests_live = _inspect_review_directory(destination)
        if not _review_matches_upstream(
            workflow_id=workflow_id,
            v086_bytes=v086_initial,
            clip_manifest_doc=clip_manifest_initial,
            manifest_doc=manifest_doc,
            digests_live=digests_live,
        ):
            raise ArtifactError("published animation review does not match authoritative upstream")
        manifest_sha = hashlib.sha256(
            _read_bounded_package_file(destination / "animation_review_manifest.json")
        ).hexdigest()
        upstream_anim_sha = manifest_doc.get("upstream_animation_manifest_sha256")
        if not isinstance(upstream_anim_sha, str):
            raise ArtifactError(
                "animation review manifest missing upstream_animation_manifest_sha256"
            )
        scene_sha = digests_live["animation_review.tscn"]
    except Exception:
        _discard_owned_preview_stage(stage, staging_parent)
        raise

    return CandidateAnimationReviewResult(
        workflow_id=workflow_id,
        animation_review_root=destination.as_posix(),
        animation_review_manifest_sha256=manifest_sha,
        upstream_animation_manifest_sha256=upstream_anim_sha,
        upstream_preview_manifest_sha256=str(identities["upstream_preview_manifest_sha256"]),
        processed_glb_sha256=str(identities["processed_glb_sha256"]),
        clip_sha256=clip_sha_initial,
        snapshot_fingerprint=str(identities["snapshot_fingerprint"]),
        evidence_execution_id=str(identities["evidence_execution_id"]),
        evidence_attempt_number=int(identities["evidence_attempt_number"]),
        manifest_artifact_id=str(identities["manifest_artifact_id"]),
        result_artifact_id=str(identities["result_artifact_id"]),
        marker_artifact_id=str(identities["marker_artifact_id"]),
        animation_review_scene_sha256=scene_sha,
        production_eligible=False,
        promotion_eligible=False,
        animation_review_status=ANIMATION_REVIEW_STATUS_TEST_ONLY,
    )


def animation_review_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_path: Path | str,
    animation_dir: Path | str,
    review_dir: Path | str,
) -> bool:
    """Return True when review_dir matches live upstream animation clip preview authority."""
    upstream_root = Path(preview_dir)
    clip_root = Path(clip_path)
    animation_root = Path(animation_dir)
    review_root = Path(review_dir)
    v086_before = _try_read_v086_package_files(animation_root)
    if v086_before is None:
        return False
    if not animation_clip_preview_current(
        handlers, workflow_id, upstream_root, clip_root, animation_root
    ):
        return False
    v086_after = _try_read_v086_package_files(animation_root)
    if v086_after is None or v086_after != v086_before:
        return False
    try:
        clip_manifest_doc = _parse_gated_v086_clip_manifest(v086_before)
    except ValidationError:
        return False
    try:
        manifest, digests_live = _inspect_review_directory(review_root)
    except (ValidationError, CandidateCurrentnessError):
        raise
    except OSError:
        return False
    return _review_matches_upstream(
        workflow_id=workflow_id,
        v086_bytes=v086_before,
        clip_manifest_doc=clip_manifest_doc,
        manifest_doc=manifest,
        digests_live=digests_live,
    )


__all__ = [
    "ANIMATION_REVIEW_MANIFEST_SCHEMA",
    "ANIMATION_REVIEW_STATUS_TEST_ONLY",
    "CandidateAnimationReviewResult",
    "animation_review_current",
    "export_rigged_character_animation_review",
    "trusted_animation_review_ui_digests",
]
