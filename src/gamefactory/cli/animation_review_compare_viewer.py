"""V0.8-12a disposable synchronized A/B animation compare Godot overlay launcher."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import parse_bounded_publication_json
from gamefactory.cli.animation_review_session_bridge import (
    _assert_path_outside_roots,
    _read_bounded_regular_file,
    _reject_unsafe_lexical_path,
)
from gamefactory.cli.animation_review_session_viewer import (
    _assert_fresh_overlay_path,
    _assert_overlay_parent_ready,
    _assert_snapshot_trusted_godot_executables,
    _assert_trusted_executable,
    _capture_review_set_structure,
    _cleanup_owned_tree,
    _materialize_snapshot,
    _snapshots_match,
    _write_exclusive_bytes,
)
from gamefactory.cli.exit_codes import EXIT_CONFIG_ERROR, EXIT_SUCCESS
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_animation_review_set import (
    _MAX_REVIEW_SET_MANIFEST_BYTES,
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
    validate_review_set_sources,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

_SCENE_RESOURCE = "res://animation_review_compare.tscn"
_V08_RESOURCE_PACKAGE = "gamefactory.resources.v08_candidate"
_CONTEXT_LEAF = "compare_context.json"
_COMPARE_SCHEMA_VERSION = "animation-review-compare-0.8.0"
_MAX_CONTEXT_BYTES = 8 * 1024
_ARG_COMPARE_CONTEXT = "--compare-context-file="
_PACKAGED_COMPARE_FILES = (
    "animation_review_compare.tscn",
    "animation_review_compare_controller.gd",
    "animation_review_compare_side.gd",
)


@dataclass(frozen=True)
class PreparedAnimationReviewCompareViewer:
    overlay_dir: Path
    context_file: Path
    left_clip_id: str
    right_clip_id: str

    def godot_argv(self, *, godot_executable: Path) -> tuple[str, ...]:
        godot = _assert_trusted_executable(godot_executable, label="godot executable")
        context = _reject_unsafe_lexical_path(self.context_file, label="compare context file")
        overlay = _reject_unsafe_lexical_path(self.overlay_dir, label="overlay directory")
        return (
            str(godot),
            "--path",
            str(overlay),
            "--scene",
            _SCENE_RESOURCE,
            "--",
            f"{_ARG_COMPARE_CONTEXT}{context}",
        )


def _compare_forbidden_roots(
    handlers: CandidateWorkflowHandlers,
    preview_dir: Path,
    review_dir: Path,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
) -> list[Path]:
    roots: list[Path] = [
        handlers.root.resolve(),
        Path(preview_dir).resolve(),
        Path(review_dir).resolve(),
    ]
    for package in clip_packages:
        roots.append(Path(package.animation_dir).resolve())
        roots.append(Path(package.clip_path).resolve())
    return roots


def _manifest_clip_ids_from_review_dir(review_root: Path) -> frozenset[str]:
    manifest_path = review_root / "animation_review_set_manifest.json"
    raw = _read_bounded_regular_file(
        manifest_path,
        label="animation_review_set_manifest.json",
        max_bytes=_MAX_REVIEW_SET_MANIFEST_BYTES,
    )
    document = parse_bounded_publication_json(raw)
    return _clip_ids_from_manifest_doc(document)


def _clip_ids_from_manifest_doc(manifest_doc: Mapping[str, Any]) -> frozenset[str]:
    if manifest_doc.get("schema_version") != ANIMATION_REVIEW_SET_MANIFEST_SCHEMA:
        raise ValidationError("animation review set manifest schema_version mismatch")
    ordered_clips = manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    clip_ids: set[str] = set()
    for entry in ordered_clips:
        if not isinstance(entry, dict):
            raise ValidationError("animation review set ordered clip entry is invalid")
        clip_id = entry.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise ValidationError("animation review set ordered clip entry is invalid")
        clip_ids.add(clip_id)
    return frozenset(clip_ids)


def _assert_review_set_snapshot_unchanged(
    review_root: Path,
    *,
    original_snapshot: Any,
    failure_message: str,
) -> None:
    current = _capture_review_set_structure(review_root)
    if not _snapshots_match(original_snapshot, current):
        raise ValidationError(failure_message)


def _validate_compare_clip_selectors(
    left_clip_id: object,
    right_clip_id: object,
    *,
    known_clip_ids: frozenset[str],
) -> tuple[str, str]:
    if isinstance(left_clip_id, bool) or isinstance(right_clip_id, bool):
        raise ValidationError("compare clip ids must be strings")
    if not isinstance(left_clip_id, str) or not isinstance(right_clip_id, str):
        raise ValidationError("compare clip ids must be strings")
    if not left_clip_id or not right_clip_id:
        raise ValidationError("compare clip ids must be non-empty strings")
    if left_clip_id == right_clip_id:
        raise ValidationError("compare clip ids must differ")
    if left_clip_id not in known_clip_ids:
        raise ValidationError(f"unknown compare left clip id: {left_clip_id}")
    if right_clip_id not in known_clip_ids:
        raise ValidationError(f"unknown compare right clip id: {right_clip_id}")
    return left_clip_id, right_clip_id


def _assert_review_set_current(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
    review_dir: Path,
) -> None:
    try:
        current = animation_review_set_current(
            handlers,
            workflow_id,
            preview_dir,
            clip_packages,
            review_dir,
        )
    except (ValidationError, CandidateCurrentnessError) as exc:
        raise ValidationError(str(exc)) from exc
    if not current:
        raise ValidationError("animation review set is not current")


def _compare_context_document(left_clip_id: str, right_clip_id: str) -> dict[str, Any]:
    return {
        "schema_version": _COMPARE_SCHEMA_VERSION,
        "left_clip_id": left_clip_id,
        "right_clip_id": right_clip_id,
    }


def _write_compare_context_file(path: Path, left_clip_id: str, right_clip_id: str) -> None:
    document = _compare_context_document(left_clip_id, right_clip_id)
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("compare context is not JSON-encodable") from exc
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise ValidationError("compare context exceeds allowed size")
    if document.get("schema_version") != _COMPARE_SCHEMA_VERSION:
        raise ValidationError("compare context schema_version mismatch")
    _write_exclusive_bytes(path, payload)


def _install_packaged_compare_scenes(overlay_dir: Path) -> None:
    package = resources.files(_V08_RESOURCE_PACKAGE)
    for leaf in _PACKAGED_COMPARE_FILES:
        target = overlay_dir / leaf
        if target.exists():
            raise ValidationError(f"overlay compare resource {leaf} already exists")
        payload = package.joinpath(leaf).read_bytes()
        _write_exclusive_bytes(target, payload)


def prepare_animation_review_compare_viewer(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
    review_dir: Path | str,
    overlay_dir: Path,
    left_clip_id: str,
    right_clip_id: str,
) -> PreparedAnimationReviewCompareViewer:
    """Stage a disposable Godot overlay for side-by-side synchronized clip comparison."""
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValidationError("workflow_id must be a non-empty string")
    preview_root = Path(preview_dir)
    review_root = Path(review_dir)
    _reject_unsafe_lexical_path(preview_root, label="preview_dir")
    _reject_unsafe_lexical_path(review_root, label="review_dir")

    if isinstance(left_clip_id, bool) or isinstance(right_clip_id, bool):
        raise ValidationError("compare clip ids must be strings")
    if not isinstance(left_clip_id, str) or not isinstance(right_clip_id, str):
        raise ValidationError("compare clip ids must be strings")
    if not left_clip_id or not right_clip_id:
        raise ValidationError("compare clip ids must be non-empty strings")
    if left_clip_id == right_clip_id:
        raise ValidationError("compare clip ids must differ")

    overlay = _assert_fresh_overlay_path(overlay_dir)
    forbidden = _compare_forbidden_roots(handlers, preview_root, review_root, clip_packages)
    _assert_path_outside_roots(overlay, forbidden, label="overlay_dir")
    _assert_overlay_parent_ready(overlay)

    membership_ids = _manifest_clip_ids_from_review_dir(review_root)
    _validate_compare_clip_selectors(
        left_clip_id,
        right_clip_id,
        known_clip_ids=membership_ids,
    )

    validate_review_set_sources(clip_packages)

    _assert_review_set_current(
        handlers,
        workflow_id,
        preview_root,
        clip_packages,
        review_root,
    )

    original_snapshot = _capture_review_set_structure(review_root)
    _assert_snapshot_trusted_godot_executables(original_snapshot)
    known_ids = _clip_ids_from_manifest_doc(original_snapshot.manifest_doc)
    left, right = _validate_compare_clip_selectors(
        left_clip_id,
        right_clip_id,
        known_clip_ids=known_ids,
    )
    overlay_owned = False
    context_path: Path | None = None
    try:
        overlay.mkdir(parents=False)
        overlay_owned = True
        _materialize_snapshot(original_snapshot, overlay)
        _assert_review_set_snapshot_unchanged(
            review_root,
            original_snapshot=original_snapshot,
            failure_message="review set bytes changed during compare viewer preparation",
        )
        overlay_snapshot = _capture_review_set_structure(overlay)
        if not _snapshots_match(original_snapshot, overlay_snapshot):
            raise ValidationError("overlay review set bytes drifted during materialization")
        _install_packaged_compare_scenes(overlay)
        context_path = overlay / _CONTEXT_LEAF
        _write_compare_context_file(context_path, left, right)
        _assert_review_set_current(
            handlers,
            workflow_id,
            preview_root,
            clip_packages,
            review_root,
        )
        _assert_review_set_snapshot_unchanged(
            review_root,
            original_snapshot=original_snapshot,
            failure_message="review set bytes changed during compare viewer preparation",
        )
    except Exception:
        if overlay_owned:
            _cleanup_owned_tree(overlay, intended_root=overlay)
        raise

    _assert_path_outside_roots(context_path, forbidden, label="compare context file")

    return PreparedAnimationReviewCompareViewer(
        overlay_dir=overlay,
        context_file=context_path,
        left_clip_id=left,
        right_clip_id=right,
    )


def _subprocess_run_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_animation_review_compare_viewer(
    *,
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    preview_dir: Path | str,
    clip_packages: Sequence[CandidateAnimationReviewSetSource],
    review_dir: Path | str,
    left_clip_id: str,
    right_clip_id: str,
    godot_executable: Path,
) -> int:
    """Create a disposable overlay, launch Godot compare, and remove owned temp state on exit."""
    try:
        godot = _assert_trusted_executable(godot_executable, label="godot executable")
    except (ValidationError, OSError):
        return EXIT_CONFIG_ERROR

    try:
        owned_parent = Path(tempfile.mkdtemp(prefix="gf-anim-review-compare-"))
    except OSError:
        return EXIT_CONFIG_ERROR

    overlay_dir = owned_parent / "overlay"
    exit_code = EXIT_CONFIG_ERROR
    try:
        prepared = prepare_animation_review_compare_viewer(
            handlers,
            workflow_id,
            preview_dir,
            clip_packages,
            review_dir,
            overlay_dir,
            left_clip_id,
            right_clip_id,
        )
        _assert_review_set_current(
            handlers,
            workflow_id,
            Path(preview_dir),
            clip_packages,
            Path(review_dir),
        )
        argv = list(prepared.godot_argv(godot_executable=godot))
        completed = subprocess.run(
            argv,
            check=False,
            shell=False,
            **_subprocess_run_kwargs(),
        )
        exit_code = EXIT_SUCCESS if completed.returncode == 0 else completed.returncode
    except (ValidationError, OSError):
        exit_code = EXIT_CONFIG_ERROR
    finally:
        _cleanup_owned_tree(owned_parent, intended_root=owned_parent)

    return exit_code
