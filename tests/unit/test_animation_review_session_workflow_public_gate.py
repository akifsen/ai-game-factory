"""Unmocked public review-set gate + session workflow against exported V0.8-8 bytes.

Uses DB-backed managed evidence and real Python export/currentness paths from
``test_v08_candidate_animation_review_set`` fixtures. Process/runtime mocks inside
those fixtures may apply; this does not run Blender or Godot executables.
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gamefactory.adapters.assets.animation_review_session_store import AnimationReviewSessionStore
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    animation_review_session_current,
    create_animation_review_session,
    update_animation_review_session,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    CandidateAnimationReviewSetSource,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from tests.unit.test_v08_candidate_animation_review_set import (
    _copy_review_set_fixture,
    _sources_ab,
    acceptance_clip_a_path,  # noqa: F401
    acceptance_clip_b_path,  # noqa: F401
    authored_arm_reverse_02_clip_raw_bytes,
    build_authored_arm_reverse_02_clip_document,
    managed_review_set_evidence,  # noqa: F401
    shared_clip_preview_a,  # noqa: F401
    shared_clip_preview_b,  # noqa: F401
    shared_preview,  # noqa: F401
    shared_review_set,  # noqa: F401
)
from tests.unit.v08_candidate_c2b_readiness_fixtures import CompletedEvidenceContext


def _workflow_context_from_isolated(
    ctx: CompletedEvidenceContext,
    preview: Path,
    sources: tuple[CandidateAnimationReviewSetSource, ...],
    review_set: Path,
) -> AnimationReviewSessionWorkflowContext:
    return AnimationReviewSessionWorkflowContext(
        handlers=ctx.handlers,
        workflow_id=ctx.workflow_id,
        preview_dir=preview,
        clip_packages=sources,
        review_dir=review_set,
    )


def _review_set_tree_digest(review_set: Path) -> dict[str, str]:
    return {
        str(path.relative_to(review_set)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(review_set.rglob("*"))
        if path.is_file()
    }


@pytest.mark.candidate_slow
def test_unmocked_session_lifecycle_and_authored_source_drift(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
) -> None:
    preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "session-public-gate",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    ctx = _workflow_context_from_isolated(
        managed_review_set_evidence,
        preview,
        sources,
        review_set,
    )
    review_bytes_before = _review_set_tree_digest(review_set)
    shared_bytes_before = _review_set_tree_digest(shared_review_set)

    created = create_animation_review_session(ctx)
    assert created.committed is True
    assert created.current is True

    noted = update_animation_review_session(
        ctx,
        expected_raw_sha256=created.stored.raw_sha256,
        operation={"op": "SetNote", "clip_id": "arm_wave_01", "note": "review A"},
    )
    assert noted.committed is True and noted.current is True

    revised = update_animation_review_session(
        ctx,
        expected_raw_sha256=noted.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "arm_wave_01", "status": "revise"},
    )
    assert revised.committed is True and revised.current is True

    bookmarked = update_animation_review_session(
        ctx,
        expected_raw_sha256=revised.stored.raw_sha256,
        operation={"op": "AddBookmark", "clip_id": "arm_wave_01", "timestamp": 0.75},
    )
    assert bookmarked.committed is True and bookmarked.current is True

    kept = update_animation_review_session(
        ctx,
        expected_raw_sha256=bookmarked.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "keep"},
    )
    assert kept.committed is True and kept.current is True
    assert kept.stored.session.revision == 4

    store = AnimationReviewSessionStore(review_root=review_set)
    reopened = store.load()
    assert reopened.session.revision == kept.stored.session.revision
    assert reopened.raw_sha256 == kept.stored.raw_sha256
    assert animation_review_session_current(ctx) is True
    assert _review_set_tree_digest(review_set) == review_bytes_before
    assert _review_set_tree_digest(shared_review_set) == shared_bytes_before

    drift_clip_path = tmp_path / "session-public-gate-drift-b.json"
    drift_doc = build_authored_arm_reverse_02_clip_document()
    drift_doc["duration_seconds"] = 2.25
    drift_clip_path.write_bytes(
        json.dumps(drift_doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )
    assert drift_clip_path.read_bytes() != authored_arm_reverse_02_clip_raw_bytes()
    clip_b_path.write_bytes(drift_clip_path.read_bytes())

    assert animation_review_session_current(ctx) is False
    with pytest.raises(CandidateCurrentnessError):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=kept.stored.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "revise"},
        )
    historical = store.load()
    assert historical.session.revision == kept.stored.session.revision
    assert historical.raw_sha256 == kept.stored.raw_sha256
    assert _review_set_tree_digest(review_set) == review_bytes_before
    assert _review_set_tree_digest(shared_review_set) == shared_bytes_before
