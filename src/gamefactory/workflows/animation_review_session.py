"""V0.8-9a thin review-session workflow authority (live review-set gate + durable store).

Mutations publish through ``AnimationReviewSessionStore`` only when the bound V0.8-8
review set is current per the public ``animation_review_set_current`` gate and an
independently re-read sidecar binding matches the pinned snapshot immediately before
commit. Historical sessions remain readable via ``AnimationReviewSessionStore.load``
without a successful gate; ``animation_review_session_current`` is stricter.

Point-in-time semantics: the store sidecar and upstream review-set sources are not
updated under one global transaction with DB or export writers. A successful mutation
returns ``committed=True``; if sources drift only after publication, ``current`` may
be ``False`` without rolling back durable history.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gamefactory.adapters.assets.animation_clip_input import (
    MAX_LOCAL_ANIMATION_CLIP_BYTES,
    load_local_animation_clip,
)
from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
    StoredAnimationReviewSession,
)
from gamefactory.adapters.assets.v08_candidate_evidence import parse_bounded_publication_json
from gamefactory.adapters.assets.v08_candidate_runtime_json import CandidateRuntimeJsonError
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.animation_review_session import (
    AnimationReviewSession,
    ReviewSetBinding,
    ReviewSetClipBinding,
)
from gamefactory.core.domain.errors import FactoryError, ValidationError
from gamefactory.workflows.v08_candidate_animation_review_set import (
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    MAX_REVIEW_SET_SOURCES,
    MIN_REVIEW_SET_SOURCES,
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

_MAX_MANIFEST_BYTES = 64 * 1024
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_ORDERED_CLIP_ENTRY_KEYS = frozenset(
    {
        "clip_id",
        "raw_clip_sha256",
        "raw_original_manifest_sha256",
        "source_package_sha256",
        "source_file_digests",
    }
)
_MANIFEST_KEYS = frozenset(
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


@dataclass(frozen=True)
class AnimationReviewSessionWorkflowContext:
    """Inputs required to evaluate review-set authority and bind a session sidecar."""

    handlers: CandidateWorkflowHandlers
    workflow_id: str
    preview_dir: Path | str
    clip_packages: tuple[CandidateAnimationReviewSetSource, ...]
    review_dir: Path | str
    session_path: Path | str | None = None


@dataclass(frozen=True)
class AnimationReviewSessionMutationResult:
    """Outcome of a successful create or update (durable write already committed)."""

    committed: Literal[True]
    current: bool
    stored: StoredAnimationReviewSession


@dataclass(frozen=True)
class _ReviewSetSidecarSnapshot:
    raw_manifest_sha256: str
    clip_animation_json_sha256: tuple[str, ...]


@dataclass(frozen=True)
class _CapturedReviewSetBinding:
    binding: ReviewSetBinding
    snapshot: _ReviewSetSidecarSnapshot


def _validate_clip_packages(clip_packages: Sequence[CandidateAnimationReviewSetSource]) -> None:
    count = len(clip_packages)
    if count < MIN_REVIEW_SET_SOURCES or count > MAX_REVIEW_SET_SOURCES:
        raise ValidationError(
            f"clip_packages must contain between {MIN_REVIEW_SET_SOURCES} and "
            f"{MAX_REVIEW_SET_SOURCES} sources"
        )


def _store_for_context(ctx: AnimationReviewSessionWorkflowContext) -> AnimationReviewSessionStore:
    if ctx.session_path is None:
        return AnimationReviewSessionStore(review_root=ctx.review_dir)
    return AnimationReviewSessionStore(review_root=ctx.review_dir, session_path=ctx.session_path)


def _not_regular_file_message(label: str) -> str:
    return f"{label} must be a regular file, not a link, junction, or reparse point"


def _assert_stat_regular_single_link(info: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if reparse:
        raise ValidationError(_not_regular_file_message(label))
    if stat.S_ISDIR(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    if info.st_nlink > 1:
        raise ValidationError(f"{label} must not be a hard link")


def _assert_regular_single_link_file(path: Path, *, label: str) -> os.stat_result:
    if path_crosses_link(path):
        raise ValidationError(f"{label} crosses a symlink or junction")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label} does not exist") from None
    except OSError as exc:
        raise ValidationError(f"{label} cannot be inspected: {exc}") from exc
    _assert_stat_regular_single_link(info, label=label)
    return info


def _read_bounded_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    entry = _assert_regular_single_link_file(path, label=label)
    size = entry.st_size
    if size > max_bytes:
        raise ValidationError(f"{label} exceeds {_MAX_MANIFEST_BYTES} bytes")
    with path.open("rb") as handle:
        fd = handle.fileno()
        opened = os.fstat(fd)
        _assert_stat_regular_single_link(opened, label=label)
        if opened.st_nlink > 1:
            raise ValidationError(f"{label} must not be a hard link")
        entry_now = path.lstat()
        if (opened.st_dev, opened.st_ino) != (entry_now.st_dev, entry_now.st_ino):
            raise ValidationError(f"{label} identity changed during read")
        if opened.st_size != size:
            raise ValidationError(f"{label} size changed during read")
        payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValidationError(f"{label} exceeds {max_bytes} bytes")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"{label} identity changed during read")
        if after.st_size != opened.st_size:
            raise ValidationError(f"{label} grew during bounded read")
        if len(payload) != after.st_size:
            raise ValidationError(f"{label} grew during bounded read")
        leaf_after = path.lstat()
        if (leaf_after.st_dev, leaf_after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"{label} identity changed during read")
    return payload


def _validate_sha256_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a 64-character lowercase hex digest")
    return value


def _validate_manifest_for_binding(manifest_doc: dict[str, Any]) -> None:
    if set(manifest_doc.keys()) != _MANIFEST_KEYS:
        raise ValidationError("animation review set manifest fields do not match the contract")
    if manifest_doc.get("schema_version") != ANIMATION_REVIEW_SET_MANIFEST_SCHEMA:
        raise ValidationError("animation review set manifest schema_version mismatch")
    if manifest_doc.get("production_eligible") is not False:
        raise ValidationError("animation review set manifest production_eligible must be false")
    if manifest_doc.get("promotion_eligible") is not False:
        raise ValidationError("animation review set manifest promotion_eligible must be false")
    ordered_clips = manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    seen_ids: set[str] = set()
    for entry in ordered_clips:
        if not isinstance(entry, dict) or set(entry.keys()) != _ORDERED_CLIP_ENTRY_KEYS:
            raise ValidationError("animation review set ordered clip entry is invalid")
        clip_id = entry.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise ValidationError("animation review set ordered clip entry is invalid")
        if clip_id in seen_ids:
            raise ValidationError(f"review set clip_id {clip_id} is duplicated")
        seen_ids.add(clip_id)
        _validate_sha256_hex(entry.get("raw_clip_sha256"), "ordered_clips.raw_clip_sha256")
    _validate_sha256_hex(manifest_doc.get("root_payload_digest"), "root_payload_digest")
    _validate_sha256_hex(
        manifest_doc.get("nested_clips_payload_digest"),
        "nested_clips_payload_digest",
    )


def _read_live_review_set_binding(
    review_dir: Path | str,
    *,
    clip_slot_count: int,
    review_root_identity: str,
) -> tuple[ReviewSetBinding, _ReviewSetSidecarSnapshot]:
    root = Path(review_dir).resolve(strict=False)
    if not root.is_dir():
        raise ValidationError("review_dir is not a directory")
    if path_crosses_link(root):
        raise ValidationError("review_dir crosses a symlink or junction")

    manifest_path = root / "animation_review_set_manifest.json"
    manifest_raw = _read_bounded_regular_file(
        manifest_path,
        label="animation_review_set_manifest.json",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest_doc = parse_bounded_publication_json(manifest_raw)
    except (CandidateRuntimeJsonError, ValueError, RecursionError) as exc:
        raise ValidationError(str(exc)) from exc
    _validate_manifest_for_binding(manifest_doc)

    ordered_clips = manifest_doc["ordered_clips"]
    if len(ordered_clips) != clip_slot_count:
        raise ValidationError("animation review set ordered_clips length mismatch")

    clip_bindings: list[ReviewSetClipBinding] = []
    clip_shas: list[str] = []
    for index in range(clip_slot_count):
        slot = f"{index:03d}"
        clip_path = root / "clips" / slot / "animation_clip.json"
        clip_raw = _read_bounded_regular_file(
            clip_path,
            label=f"clips/{slot}/animation_clip.json",
            max_bytes=MAX_LOCAL_ANIMATION_CLIP_BYTES,
        )
        clip_sha = hashlib.sha256(clip_raw).hexdigest()
        entry = ordered_clips[index]
        declared_sha = entry["raw_clip_sha256"]
        if clip_sha != declared_sha:
            raise ValidationError(
                f"clips/{slot}/animation_clip.json digest does not match manifest ordered_clips"
            )
        loaded = load_local_animation_clip(clip_path)
        if loaded.raw_bytes != clip_raw:
            raise ValidationError(f"clips/{slot}/animation_clip.json identity changed during read")
        clip_id = loaded.clip.clip_id
        duration_seconds = loaded.clip.duration_seconds
        declared_id = entry["clip_id"]
        if clip_id != declared_id:
            raise ValidationError(
                f"clips/{slot}/animation_clip.json clip_id does not match manifest ordered_clips"
            )
        clip_bindings.append(
            ReviewSetClipBinding(
                clip_id=clip_id,
                raw_clip_sha256=clip_sha,
                duration_seconds=duration_seconds,
            )
        )
        clip_shas.append(clip_sha)

    binding = ReviewSetBinding(
        review_root=review_root_identity,
        raw_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        root_payload_sha256=manifest_doc["root_payload_digest"],
        clip_payload_sha256=manifest_doc["nested_clips_payload_digest"],
        clips=tuple(clip_bindings),
    )
    snapshot = _ReviewSetSidecarSnapshot(
        raw_manifest_sha256=binding.raw_manifest_sha256,
        clip_animation_json_sha256=tuple(clip_shas),
    )
    return binding, snapshot


def _snapshot_from_binding(binding: ReviewSetBinding) -> _ReviewSetSidecarSnapshot:
    return _ReviewSetSidecarSnapshot(
        raw_manifest_sha256=binding.raw_manifest_sha256,
        clip_animation_json_sha256=tuple(clip.raw_clip_sha256 for clip in binding.clips),
    )


def _bindings_equal(left: ReviewSetBinding, right: ReviewSetBinding) -> bool:
    return left == right


def _review_set_gate_current(ctx: AnimationReviewSessionWorkflowContext) -> bool:
    return animation_review_set_current(
        ctx.handlers,
        ctx.workflow_id,
        ctx.preview_dir,
        ctx.clip_packages,
        ctx.review_dir,
    )


def _capture_current_binding(
    ctx: AnimationReviewSessionWorkflowContext,
    *,
    review_root_identity: str,
) -> _CapturedReviewSetBinding:
    """Read sidecar binding, run the public gate, re-read, and require a stable binding."""
    clip_count = len(ctx.clip_packages)
    before_binding, before_snapshot = _read_live_review_set_binding(
        ctx.review_dir,
        clip_slot_count=clip_count,
        review_root_identity=review_root_identity,
    )
    if not _review_set_gate_current(ctx):
        raise CandidateCurrentnessError("animation review set is not current")
    after_binding, after_snapshot = _read_live_review_set_binding(
        ctx.review_dir,
        clip_slot_count=clip_count,
        review_root_identity=review_root_identity,
    )
    if after_snapshot != before_snapshot or not _bindings_equal(before_binding, after_binding):
        raise CandidateCurrentnessError("animation review set sidecar binding drifted during gate")
    return _CapturedReviewSetBinding(binding=after_binding, snapshot=after_snapshot)


def _try_capture_current_binding(
    ctx: AnimationReviewSessionWorkflowContext,
    *,
    review_root_identity: str,
) -> _CapturedReviewSetBinding | None:
    try:
        return _capture_current_binding(ctx, review_root_identity=review_root_identity)
    except FactoryError:
        return None
    except OSError:
        return None


def _evaluate_session_current(
    ctx: AnimationReviewSessionWorkflowContext,
    stored: StoredAnimationReviewSession,
) -> bool:
    try:
        store = _store_for_context(ctx)
    except (FactoryError, OSError):
        return False
    if stored.session.binding.review_root != store.review_root_identity:
        return False
    captured = _try_capture_current_binding(ctx, review_root_identity=store.review_root_identity)
    if captured is None:
        return False
    if not _bindings_equal(stored.session.binding, captured.binding):
        return False
    if captured.snapshot != _snapshot_from_binding(stored.session.binding):
        return False
    return True


def _make_precommit(
    ctx: AnimationReviewSessionWorkflowContext,
    *,
    review_root_identity: str,
    pinned_binding: ReviewSetBinding,
    pinned_snapshot: _ReviewSetSidecarSnapshot,
) -> Any:
    def _precommit(session: AnimationReviewSession) -> None:
        captured = _capture_current_binding(ctx, review_root_identity=review_root_identity)
        if not _bindings_equal(captured.binding, pinned_binding):
            raise CandidateCurrentnessError("animation review set sidecar binding drifted")
        if captured.snapshot != pinned_snapshot:
            raise CandidateCurrentnessError("animation review set sidecar binding drifted")
        if not _bindings_equal(session.binding, pinned_binding):
            raise CandidateCurrentnessError(
                "animation review session binding does not match pinned review set"
            )

    return _precommit


def create_animation_review_session(
    ctx: AnimationReviewSessionWorkflowContext,
) -> AnimationReviewSessionMutationResult:
    """Create a durable session sidecar when the live review set and binding are current."""
    _validate_clip_packages(ctx.clip_packages)
    store = _store_for_context(ctx)
    review_root_identity = store.review_root_identity
    captured = _capture_current_binding(ctx, review_root_identity=review_root_identity)
    pinned_binding = captured.binding
    pinned_snapshot = captured.snapshot
    precommit = _make_precommit(
        ctx,
        review_root_identity=review_root_identity,
        pinned_binding=pinned_binding,
        pinned_snapshot=pinned_snapshot,
    )
    stored = store.create(pinned_binding, precommit=precommit)
    current = _evaluate_session_current(ctx, stored)
    return AnimationReviewSessionMutationResult(committed=True, current=current, stored=stored)


def update_animation_review_session(
    ctx: AnimationReviewSessionWorkflowContext,
    *,
    expected_raw_sha256: str,
    operation: dict[str, Any],
) -> AnimationReviewSessionMutationResult:
    """Apply one domain operation when the session binding still matches the live review set."""
    _validate_clip_packages(ctx.clip_packages)
    store = _store_for_context(ctx)
    review_root_identity = store.review_root_identity
    captured = _capture_current_binding(ctx, review_root_identity=review_root_identity)
    pinned_binding = captured.binding
    pinned_snapshot = captured.snapshot
    try:
        existing = store.load()
    except ValidationError:
        raise CandidateCurrentnessError("animation review session is not readable") from None
    if not _bindings_equal(existing.session.binding, pinned_binding):
        raise CandidateCurrentnessError(
            "animation review session binding does not match live review set"
        )
    precommit = _make_precommit(
        ctx,
        review_root_identity=review_root_identity,
        pinned_binding=pinned_binding,
        pinned_snapshot=pinned_snapshot,
    )
    try:
        stored = store.apply_operation(
            expected_raw_sha256=expected_raw_sha256,
            operation=operation,
            precommit=precommit,
        )
    except AnimationReviewSessionStoreConflictError:
        raise
    except ValidationError:
        raise
    current = _evaluate_session_current(ctx, stored)
    return AnimationReviewSessionMutationResult(committed=True, current=current, stored=stored)


def animation_review_session_current(ctx: AnimationReviewSessionWorkflowContext) -> bool:
    """True when the durable session matches a strict live binding and the public gate."""
    _validate_clip_packages(ctx.clip_packages)
    store = _store_for_context(ctx)
    try:
        stored = store.load()
    except ValidationError:
        return False
    return _evaluate_session_current(ctx, stored)


__all__ = [
    "AnimationReviewSessionMutationResult",
    "AnimationReviewSessionWorkflowContext",
    "animation_review_session_current",
    "create_animation_review_session",
    "update_animation_review_session",
]
