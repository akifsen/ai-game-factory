"""V0.8-10a read-only animation review session handoff report (deterministic export)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
    StoredAnimationReviewSession,
)
from gamefactory.adapters.assets.v08_candidate_evidence import (
    atomic_publish_staged_container,
    candidate_evidence_lexical_unsafe,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    _evaluate_session_current,
    _validate_clip_packages,
)
from gamefactory.workflows.v08_candidate_preview import (
    _assert_destination_fresh,
    _assert_same_volume,
    _discard_owned_preview_stage,
    _reject_preview_lexical,
    _validate_output_dir,
)

ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA = "animation-review-handoff-report-0.8.0"
REVIEW_REPORT_JSON_NAME = "review-report.json"
REVIEW_REPORT_MD_NAME = "review-report.md"
_MAX_REPORT_BYTES = 128 * 1024
_SESSION_LOCK_NAME = "session.lock"
_HANDOFF_STAGING_NAMESPACE = "candidate_animation_review_handoff_staging"

_HANDOFF_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "review_root",
        "raw_manifest_sha256",
        "root_payload_sha256",
        "clip_payload_sha256",
        "raw_session_sha256",
        "session_revision",
        "session_current",
        "production_eligible",
        "promotion_eligible",
        "keep_status_is_annotation_not_approval",
        "clips",
    }
)
_HANDOFF_CLIP_KEYS = frozenset(
    {
        "clip_id",
        "status",
        "note",
        "bookmarks",
        "duration_seconds",
    }
)


@dataclass(frozen=True)
class AnimationReviewHandoffResult:
    """Published handoff container paths and captured session identity."""

    handoff_root: Path
    review_report_json: Path
    review_report_markdown: Path
    raw_session_sha256: str
    session_revision: int
    session_current: bool
    production_eligible: Literal[False] = False
    promotion_eligible: Literal[False] = False


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _same_absolute_path(left: Path, right: Path) -> bool:
    if sys.platform == "win32":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


def _resolved_path_inside_root(path_resolved: Path, root_resolved: Path) -> bool:
    if sys.platform == "win32":
        path_s = os.path.normcase(str(path_resolved))
        root_s = os.path.normcase(str(root_resolved))
        if path_s == root_s:
            return True
        return path_s.startswith(root_s + os.sep)
    try:
        path_resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _reject_unsafe_lexical_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    lexical = path
    if candidate_evidence_lexical_unsafe(lexical):
        raise ValidationError(f"{label} is not a bounded lexical path")
    if path_crosses_link(lexical):
        raise ValidationError(f"{label} crosses a symlink or junction")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise ValidationError(f"{label} cannot be resolved: {exc}") from exc
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        raise ValidationError(f"{label} resolves through a link or reparse point")
    return resolved


def _store_for_context(ctx: AnimationReviewSessionWorkflowContext) -> AnimationReviewSessionStore:
    if ctx.session_path is None:
        return AnimationReviewSessionStore(review_root=ctx.review_dir)
    return AnimationReviewSessionStore(review_root=ctx.review_dir, session_path=ctx.session_path)


def _forbidden_output_roots(ctx: AnimationReviewSessionWorkflowContext) -> list[Path]:
    forbidden: list[Path] = [
        Path(ctx.review_dir).absolute(),
        Path(ctx.preview_dir).absolute(),
    ]
    for package in ctx.clip_packages:
        forbidden.append(Path(package.animation_dir).absolute())
        forbidden.append(Path(package.clip_path).absolute())
    store = _store_for_context(ctx)
    session_file = store.session_path
    sidecar_dir = session_file.parent
    lock_file = sidecar_dir / _SESSION_LOCK_NAME
    forbidden.extend((session_file, sidecar_dir, lock_file))
    if ctx.session_path is not None:
        forbidden.append(Path(ctx.session_path).absolute())
    return forbidden


def _paths_bidirectional_overlap(left: Path, right: Path) -> bool:
    if _same_absolute_path(left, right):
        return True
    if _resolved_path_inside_root(left, right):
        return True
    if _resolved_path_inside_root(right, left):
        return True
    return False


def _assert_path_outside_roots(path: Path, roots: list[Path], *, label: str) -> None:
    resolved = _reject_unsafe_lexical_path(path, label=label)
    for root in roots:
        root_resolved = _reject_unsafe_lexical_path(root, label="forbidden root")
        if _resolved_path_inside_root(resolved, root_resolved):
            raise ValidationError(f"{label} must stay outside canonical review and source paths")
        if _same_absolute_path(resolved, root_resolved):
            raise ValidationError(f"{label} must stay outside canonical review and source paths")


def _assert_staging_path_outside_roots(path: Path, roots: list[Path], *, label: str) -> None:
    resolved = _reject_unsafe_lexical_path(path, label=label)
    for root in roots:
        root_resolved = _reject_unsafe_lexical_path(root, label="forbidden root")
        if _paths_bidirectional_overlap(resolved, root_resolved):
            raise ValidationError(f"{label} must stay outside canonical review and source paths")


def _computed_handoff_staging_paths(publish_parent: Path) -> tuple[Path, Path]:
    publish_resolved = _reject_unsafe_lexical_path(publish_parent, label="handoff publish parent")
    gf_dir = publish_resolved / ".gf"
    staging_parent = gf_dir / "candidate_animation_review_handoff_staging"
    return gf_dir, staging_parent


def _assert_handoff_output_safe(
    ctx: AnimationReviewSessionWorkflowContext,
    destination: Path,
) -> None:
    forbidden = _forbidden_output_roots(ctx)
    _assert_staging_path_outside_roots(
        destination,
        forbidden,
        label="handoff output directory",
    )
    publish_parent = destination.parent
    _assert_path_outside_roots(publish_parent, forbidden, label="handoff output parent")
    _assert_handoff_staging_paths_safe(ctx, publish_parent, destination=destination)


def _assert_handoff_staging_paths_safe(
    ctx: AnimationReviewSessionWorkflowContext,
    publish_parent: Path,
    *,
    destination: Path | None = None,
) -> None:
    forbidden = _forbidden_output_roots(ctx)
    gf_dir, staging_parent = _computed_handoff_staging_paths(publish_parent)
    for path, label in (
        (gf_dir, "handoff staging .gf directory"),
        (staging_parent, "handoff staging parent"),
    ):
        _assert_staging_path_outside_roots(path, forbidden, label=label)
    if destination is not None:
        dest_resolved = _reject_unsafe_lexical_path(destination, label="handoff output directory")
        if dest_resolved.name == _HANDOFF_STAGING_NAMESPACE:
            raise ValidationError(
                "handoff output directory must not use the handoff staging namespace",
            )
        for path, label in (
            (gf_dir, "handoff staging .gf directory"),
            (staging_parent, "handoff staging parent"),
        ):
            if _paths_bidirectional_overlap(dest_resolved, path):
                raise ValidationError(
                    f"handoff output directory must not overlap {label}",
                )


def _read_session_raw_sha256(store: AnimationReviewSessionStore) -> str:
    from gamefactory.adapters.assets.animation_review_session_store import (
        _read_bounded_session_bytes,
    )

    raw = _read_bounded_session_bytes(store.session_path)
    return hashlib.sha256(raw).hexdigest()


def _assert_captured_session_unchanged(
    store: AnimationReviewSessionStore,
    *,
    expected_raw_sha256: str,
) -> None:
    actual = _read_session_raw_sha256(store)
    if actual != expected_raw_sha256:
        raise AnimationReviewSessionStoreConflictError(
            "animation review session changed during handoff export",
            details={
                "expected_raw_sha256": expected_raw_sha256,
                "actual_raw_sha256": actual,
            },
        )


def _handoff_staging_parent(publish_parent: Path) -> Path:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="handoff publish parent")
    publish_resolved = publish_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="handoff publish parent")
    return publish_resolved / ".gf" / "candidate_animation_review_handoff_staging"


def _fresh_handoff_stage(
    publish_parent: Path,
    project_root: Path,
    *,
    forbidden_roots: list[Path],
) -> tuple[Path, Path]:
    publish_lexical = publish_parent.absolute()
    _reject_preview_lexical(publish_lexical, label="handoff publish parent")
    project_lexical = project_root.absolute()
    _reject_preview_lexical(project_lexical, label="handoff project root")
    publish_resolved = publish_lexical.resolve(strict=False)
    project_resolved = project_lexical.resolve(strict=False)
    _reject_preview_lexical(publish_resolved, label="handoff publish parent")
    _reject_preview_lexical(project_resolved, label="handoff project root")

    staging_parent_lexical = _handoff_staging_parent(publish_resolved)
    _reject_preview_lexical(staging_parent_lexical, label="handoff staging parent")
    gf_lexical = staging_parent_lexical.parent
    _reject_preview_lexical(gf_lexical, label="handoff staging .gf directory")
    for path, label in (
        (gf_lexical, "handoff staging .gf directory"),
        (staging_parent_lexical, "handoff staging parent"),
    ):
        _assert_staging_path_outside_roots(path, forbidden_roots, label=label)

    staging_parent_lexical.mkdir(parents=True, exist_ok=True)
    staging_parent = staging_parent_lexical.resolve(strict=False)
    _reject_preview_lexical(staging_parent, label="handoff staging parent")

    token = secrets.token_hex(16)
    stage_lexical = staging_parent / f"stage_{token}"
    _reject_preview_lexical(stage_lexical, label="handoff staging container")
    if stage_lexical.exists():
        raise ArtifactError("handoff staging collision")
    owned_stage: Path | None = None
    try:
        stage_lexical.mkdir(parents=True, exist_ok=False)
        owned_stage = stage_lexical
        stage = owned_stage.resolve(strict=True)
        _reject_preview_lexical(stage, label="handoff staging container")
        _assert_same_volume(stage, publish_resolved)
        if path_crosses_link(stage):
            raise ValidationError("handoff staging container crosses a link")
    except Exception:
        if owned_stage is not None:
            _discard_owned_preview_stage(owned_stage, staging_parent)
        raise
    return stage, staging_parent


def _markdown_display_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _markdown_fenced_code_block(text: str) -> str:
    display = _markdown_display_newlines(text)
    max_run = 0
    for match in re.finditer(r"`+", display):
        max_run = max(max_run, len(match.group(0)))
    fence = "`" * max(max_run + 1, 3)
    return f"{fence}\n{display}\n{fence}"


def _build_handoff_json_document(
    stored: StoredAnimationReviewSession,
    *,
    session_current: bool,
) -> dict[str, Any]:
    session = stored.session
    binding = session.binding
    clips: list[dict[str, Any]] = []
    for index, clip_binding in enumerate(binding.clips):
        record = session.clip_records[index]
        clips.append(
            {
                "clip_id": record.clip_id,
                "status": record.status,
                "note": record.note,
                "bookmarks": list(record.bookmarks),
                "duration_seconds": clip_binding.duration_seconds,
            }
        )
    document: dict[str, Any] = {
        "schema_version": ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA,
        "review_root": binding.review_root,
        "raw_manifest_sha256": binding.raw_manifest_sha256,
        "root_payload_sha256": binding.root_payload_sha256,
        "clip_payload_sha256": binding.clip_payload_sha256,
        "raw_session_sha256": stored.raw_sha256,
        "session_revision": session.revision,
        "session_current": session_current,
        "production_eligible": False,
        "promotion_eligible": False,
        "keep_status_is_annotation_not_approval": True,
        "clips": clips,
    }
    if set(document.keys()) != _HANDOFF_REPORT_KEYS:
        raise ValidationError("handoff report fields do not match the contract")
    for entry in clips:
        if set(entry.keys()) != _HANDOFF_CLIP_KEYS:
            raise ValidationError("handoff clip entry fields do not match the contract")
    return document


def _build_handoff_markdown(
    document: dict[str, Any],
    *,
    session_current: bool,
) -> bytes:
    lines: list[str] = []
    if not session_current:
        lines.extend(
            [
                "# SESSION NOT CURRENT",
                "",
                "The durable session does not match the strict live review-set binding.",
                "",
            ]
        )
    lines.extend(
        [
            "# Animation review handoff report",
            "",
            "keep status is annotation only, not approval.",
            "",
            "## Session",
            "",
            "- review_root:",
            _markdown_fenced_code_block(str(document["review_root"])),
            f"- raw_session_sha256: {document['raw_session_sha256']}",
            f"- session_revision: {document['session_revision']}",
            f"- session_current: {str(session_current).lower()}",
            "- production_eligible: false",
            "- promotion_eligible: false",
            "",
            "## Review set binding",
            "",
            f"- raw_manifest_sha256: {document['raw_manifest_sha256']}",
            f"- root_payload_sha256: {document['root_payload_sha256']}",
            f"- clip_payload_sha256: {document['clip_payload_sha256']}",
            "",
            "## Clips",
            "",
        ]
    )
    clips = document["clips"]
    if not isinstance(clips, list):
        raise ValidationError("handoff clips must be a list")
    for entry in clips:
        if not isinstance(entry, dict):
            raise ValidationError("handoff clip entry must be an object")
        lines.append("- clip_id:")
        lines.append(_markdown_fenced_code_block(str(entry["clip_id"])))
        lines.append("- status:")
        lines.append(_markdown_fenced_code_block(str(entry["status"])))
        lines.append(f"- duration_seconds: {entry['duration_seconds']}")
        bookmarks = entry.get("bookmarks")
        if not isinstance(bookmarks, list):
            raise ValidationError("handoff clip bookmarks must be a list")
        bookmark_text = ", ".join(str(item) for item in bookmarks)
        lines.append(f"- bookmarks: {bookmark_text}")
        lines.append("- note:")
        note = entry.get("note")
        if not isinstance(note, str):
            raise ValidationError("handoff clip note must be a string")
        if note:
            lines.append(_markdown_fenced_code_block(note))
        else:
            lines.append(_markdown_fenced_code_block("(empty)"))
        lines.append("")
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _bounded_json_report_bytes(payload: bytes) -> bytes:
    if len(payload) > _MAX_REPORT_BYTES:
        raise ValidationError(f"{REVIEW_REPORT_JSON_NAME} exceeds {_MAX_REPORT_BYTES} bytes")
    return payload


def _bounded_markdown_report_bytes(payload: bytes) -> bytes:
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if len(normalized) > _MAX_REPORT_BYTES:
        raise ValidationError(f"{REVIEW_REPORT_MD_NAME} exceeds {_MAX_REPORT_BYTES} bytes")
    return normalized


def _build_report_payloads(
    stored: StoredAnimationReviewSession,
    *,
    session_current: bool,
) -> tuple[bytes, bytes]:
    document = _build_handoff_json_document(stored, session_current=session_current)
    json_bytes = _bounded_json_report_bytes(_canonical_json_bytes(document))
    md_bytes = _bounded_markdown_report_bytes(
        _build_handoff_markdown(document, session_current=session_current),
    )
    return json_bytes, md_bytes


def _write_handoff_stage(stage: Path, json_bytes: bytes, md_bytes: bytes) -> None:
    stage_lexical = stage.absolute()
    _reject_preview_lexical(stage_lexical, label="handoff staging container")
    stage_resolved = stage_lexical.resolve(strict=True)
    _reject_preview_lexical(stage_resolved, label="handoff staging container")
    json_path = stage_resolved / REVIEW_REPORT_JSON_NAME
    md_path = stage_resolved / REVIEW_REPORT_MD_NAME
    json_path.write_bytes(json_bytes)
    md_path.write_bytes(md_bytes)
    names = {item.name for item in stage_resolved.iterdir()}
    if names != {REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME}:
        raise ArtifactError("handoff staging container has unexpected files")


def export_animation_review_handoff(
    ctx: AnimationReviewSessionWorkflowContext,
    output_dir: Path | str,
) -> AnimationReviewHandoffResult:
    """Export a deterministic read-only handoff report for the durable review session."""
    _validate_clip_packages(ctx.clip_packages)
    destination = _validate_output_dir(Path(output_dir))
    _assert_handoff_output_safe(ctx, destination)
    _assert_destination_fresh(destination)

    store = _store_for_context(ctx)
    stored = store.load()
    _assert_captured_session_unchanged(store, expected_raw_sha256=stored.raw_sha256)
    session_current = _evaluate_session_current(ctx, stored)

    project_root = Path(ctx.handlers.root).absolute()
    _reject_preview_lexical(project_root, label="handoff project root")
    forbidden_roots = _forbidden_output_roots(ctx)
    stage: Path | None = None
    staging_parent: Path | None = None
    try:
        _assert_handoff_staging_paths_safe(ctx, destination.parent, destination=destination)
        stage, staging_parent = _fresh_handoff_stage(
            destination.parent,
            project_root,
            forbidden_roots=forbidden_roots,
        )
        _assert_captured_session_unchanged(store, expected_raw_sha256=stored.raw_sha256)
        session_current = _evaluate_session_current(ctx, stored)
        json_bytes, md_bytes = _build_report_payloads(
            stored,
            session_current=session_current,
        )
        _assert_handoff_output_safe(ctx, destination)
        _assert_handoff_staging_paths_safe(
            ctx,
            destination.parent,
            destination=destination,
        )
        _write_handoff_stage(stage, json_bytes, md_bytes)
        _assert_captured_session_unchanged(store, expected_raw_sha256=stored.raw_sha256)

        publish_parent_lexical = destination.parent.absolute()
        _reject_preview_lexical(publish_parent_lexical, label="handoff publish parent")
        publish_parent_lexical.mkdir(parents=True, exist_ok=True)
        publish_parent = publish_parent_lexical.resolve(strict=False)
        _reject_preview_lexical(publish_parent, label="handoff publish parent")
        published = atomic_publish_staged_container(stage, publish_parent, destination.name)
        if published.resolve() != destination.resolve():
            raise ArtifactError("handoff publication path mismatch")
        stage = published
    except Exception:
        if stage is not None and staging_parent is not None:
            _discard_owned_preview_stage(stage, staging_parent)
        raise

    json_path = destination / REVIEW_REPORT_JSON_NAME
    md_path = destination / REVIEW_REPORT_MD_NAME
    return AnimationReviewHandoffResult(
        handoff_root=destination,
        review_report_json=json_path,
        review_report_markdown=md_path,
        raw_session_sha256=stored.raw_sha256,
        session_revision=stored.session.revision,
        session_current=session_current,
        production_eligible=False,
        promotion_eligible=False,
    )


__all__ = [
    "ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA",
    "AnimationReviewHandoffResult",
    "REVIEW_REPORT_JSON_NAME",
    "REVIEW_REPORT_MD_NAME",
    "export_animation_review_handoff",
]
