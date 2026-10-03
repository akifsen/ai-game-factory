"""Unmocked public review-set gate, session workflow, and handoff against V0.8-8 bytes.

Uses DB-backed managed evidence and real Python export/currentness paths from
``test_v08_candidate_animation_review_set`` fixtures. Process/runtime mocks inside
those fixtures may apply; this does not run Blender or Godot executables.
Handoff export uses the public ``export_animation_review_handoff`` authority (not mocked).
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import gamefactory.cli.animation_review_compare_viewer as compare_viewer_module
from gamefactory.adapters.assets.animation_review_session_store import AnimationReviewSessionStore
from gamefactory.cli.animation_review_compare_viewer import prepare_animation_review_compare_viewer
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.animation_review_handoff import (
    ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA,
    REVIEW_REPORT_JSON_NAME,
    REVIEW_REPORT_MD_NAME,
    export_animation_review_handoff,
)
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    animation_review_session_current,
    create_animation_review_session,
    update_animation_review_session,
)
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    animation_clip_preview_current,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_preview import (
    candidate_preview_current,
    export_rigged_character_candidate_preview,
)
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
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    CompletedEvidenceContext,
    run_completed_managed_evidence,
)


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


def _assert_handoff_module_under_imported_gamefactory() -> None:
    import gamefactory
    import gamefactory.workflows.animation_review_handoff as handoff_module

    package_root = Path(gamefactory.__file__).resolve().parent
    origin = Path(handoff_module.__file__).resolve()
    assert package_root in origin.parents


def _tree_digests(root: Path, *, label: str) -> dict[str, str]:
    if root.is_file():
        return {label: hashlib.sha256(root.read_bytes()).hexdigest()}
    return {
        f"{label}/{path.relative_to(root).as_posix()}": hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _handoff_fixture_source_digests(
    *,
    preview: Path,
    review_set: Path,
    clip_a: Path,
    clip_b: Path,
    clip_a_path: Path,
    clip_b_path: Path,
    session_path: Path,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    digests.update(_tree_digests(preview, label="preview"))
    digests.update(_tree_digests(review_set, label="review"))
    digests.update(_tree_digests(clip_a, label="clip-preview-a"))
    digests.update(_tree_digests(clip_b, label="clip-preview-b"))
    digests["authored/arm_wave_01.json"] = hashlib.sha256(clip_a_path.read_bytes()).hexdigest()
    digests["authored/arm_reverse_02.json"] = hashlib.sha256(clip_b_path.read_bytes()).hexdigest()
    digests["session/raw"] = hashlib.sha256(session_path.read_bytes()).hexdigest()
    return digests


def _assert_handoff_report_files(root: Path) -> None:
    names = {path.name for path in root.iterdir()}
    assert names == {REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME}


def _review_set_clip_leaf(review_set: Path, clip_id: str) -> Path:
    manifest = json.loads(
        (review_set / "animation_review_set_manifest.json").read_text(encoding="utf-8")
    )
    ordered = manifest["ordered_clips"]
    for index, entry in enumerate(ordered):
        if entry["clip_id"] == clip_id:
            return review_set / f"clips/{index:03d}/animation_clip.json"
    raise AssertionError(f"clip_id {clip_id!r} missing from review set manifest")


def _assert_handoff_reports_identical(dir_a: Path, dir_b: Path) -> None:
    _assert_handoff_report_files(dir_a)
    _assert_handoff_report_files(dir_b)
    for name in (REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes()


_MAX_HANDOFF_REPORT_BYTES = 128 * 1024


def _assert_bound_handoff_report(
    report_dir: Path,
    *,
    expected_raw_sha: str,
    expected_revision: int,
    session_current: bool,
    captured_session_document: dict[str, object],
    reverse_note: str = "",
) -> None:
    json_path = report_dir / REVIEW_REPORT_JSON_NAME
    md_path = report_dir / REVIEW_REPORT_MD_NAME
    assert json_path.stat().st_size <= _MAX_HANDOFF_REPORT_BYTES
    assert md_path.stat().st_size <= _MAX_HANDOFF_REPORT_BYTES

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    binding = captured_session_document.get("binding")
    assert isinstance(binding, dict)

    assert doc["schema_version"] == ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA
    assert doc["review_root"] == binding["review_root"]
    assert doc["raw_manifest_sha256"] == binding["raw_manifest_sha256"]
    assert doc["root_payload_sha256"] == binding["root_payload_sha256"]
    assert doc["clip_payload_sha256"] == binding["clip_payload_sha256"]
    assert doc["raw_session_sha256"] == expected_raw_sha
    assert doc["session_revision"] == expected_revision
    assert doc["session_current"] is session_current
    assert doc["production_eligible"] is False
    assert doc["promotion_eligible"] is False
    assert doc["keep_status_is_annotation_not_approval"] is True
    assert [entry["clip_id"] for entry in doc["clips"]] == [
        "arm_wave_01",
        "arm_reverse_02",
    ]
    wave = doc["clips"][0]
    reverse = doc["clips"][1]
    assert wave["status"] == "revise"
    assert wave["note"] == "review A"
    assert wave["bookmarks"] == [0.75]
    assert wave["duration_seconds"] == 1.5
    assert reverse["status"] == "keep"
    assert reverse["note"] == reverse_note
    assert reverse["bookmarks"] == []
    assert reverse["duration_seconds"] == 2.0

    md = md_path.read_text(encoding="utf-8")
    assert "keep status is annotation only, not approval." in md
    assert "production_eligible: false" in md
    assert "promotion_eligible: false" in md
    assert str(binding["review_root"]) in md
    assert f"raw_session_sha256: {expected_raw_sha}" in md
    assert f"session_revision: {expected_revision}" in md
    assert f"- session_current: {str(session_current).lower()}" in md
    assert f"raw_manifest_sha256: {binding['raw_manifest_sha256']}" in md
    assert f"root_payload_sha256: {binding['root_payload_sha256']}" in md
    assert f"clip_payload_sha256: {binding['clip_payload_sha256']}" in md
    wave_pos = md.find("arm_wave_01")
    reverse_pos = md.find("arm_reverse_02")
    assert wave_pos != -1 and reverse_pos != -1 and wave_pos < reverse_pos
    assert "review A" in md
    assert "- bookmarks: 0.75" in md
    assert "revise" in md
    assert "keep" in md
    if reverse_note:
        assert reverse_note in md
    if session_current:
        assert not md.startswith("# SESSION NOT CURRENT\n")
    else:
        assert md.startswith("# SESSION NOT CURRENT\n")


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
    monkeypatch: pytest.MonkeyPatch,
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

    _assert_handoff_module_under_imported_gamefactory()
    session_path = store.session_path
    source_digests_before_handoff = _handoff_fixture_source_digests(
        preview=preview,
        review_set=review_set,
        clip_a=clip_a,
        clip_b=clip_b,
        clip_a_path=clip_a_path,
        clip_b_path=clip_b_path,
        session_path=session_path,
    )
    handoff_root_a = (tmp_path / "session-public-gate-handoff-a").resolve()
    handoff_root_b = (tmp_path / "session-public-gate-handoff-b").resolve()
    handoff_result_a = export_animation_review_handoff(ctx, handoff_root_a)
    handoff_result_b = export_animation_review_handoff(ctx, handoff_root_b)
    assert handoff_result_a.production_eligible is False
    assert handoff_result_a.promotion_eligible is False
    assert handoff_result_a.session_current is True
    assert handoff_result_a.session_revision == 4
    assert handoff_result_a.raw_session_sha256 == kept.stored.raw_sha256
    assert handoff_result_b.session_current is True
    assert handoff_result_b.raw_session_sha256 == kept.stored.raw_sha256
    _assert_handoff_reports_identical(handoff_root_a, handoff_root_b)
    captured_session_document = json.loads(session_path.read_text(encoding="utf-8"))
    _assert_bound_handoff_report(
        handoff_root_a,
        expected_raw_sha=kept.stored.raw_sha256,
        expected_revision=4,
        session_current=True,
        captured_session_document=captured_session_document,
    )
    assert (
        _handoff_fixture_source_digests(
            preview=preview,
            review_set=review_set,
            clip_a=clip_a,
            clip_b=clip_b,
            clip_a_path=clip_a_path,
            clip_b_path=clip_b_path,
            session_path=session_path,
        )
        == source_digests_before_handoff
    )

    clip_a_path_bytes_before = clip_a_path.read_bytes()
    clip_b_path_bytes_before = clip_b_path.read_bytes()
    assert animation_review_session_current(ctx) is True
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, review_set)
        is True
    )

    foreign = run_completed_managed_evidence(tmp_path / "session-public-gate-foreign-workflow")
    foreign_preview = tmp_path / "session-public-gate-foreign-preview"
    export_rigged_character_candidate_preview(
        foreign.handlers, foreign.workflow_id, foreign_preview
    )
    foreign_clip = tmp_path / "session-public-gate-foreign-v086"
    export_rigged_character_animation_clip_preview(
        foreign.handlers,
        foreign.workflow_id,
        foreign_preview,
        acceptance_clip_b_path,
        foreign_clip,
    )
    assert candidate_preview_current(foreign.handlers, foreign.workflow_id, foreign_preview) is True
    assert (
        animation_clip_preview_current(
            foreign.handlers,
            foreign.workflow_id,
            foreign_preview,
            acceptance_clip_b_path,
            foreign_clip,
        )
        is True
    )
    assert (
        animation_clip_preview_current(ctx.handlers, ctx.workflow_id, preview, clip_a_path, clip_a)
        is True
    )
    assert (
        animation_clip_preview_current(ctx.handlers, ctx.workflow_id, preview, clip_b_path, clip_b)
        is True
    )
    foreign_sources = _sources_ab(
        clip_a,
        clip_a_path,
        foreign_clip,
        acceptance_clip_b_path,
    )
    foreign_compare_overlay = (tmp_path / "session-public-gate-compare-foreign-overlay").resolve()
    with pytest.raises(ValidationError):
        prepare_animation_review_compare_viewer(
            handlers=ctx.handlers,
            workflow_id=ctx.workflow_id,
            preview_dir=preview,
            clip_packages=foreign_sources,
            review_dir=review_set,
            overlay_dir=foreign_compare_overlay,
            left_clip_id="arm_wave_01",
            right_clip_id="arm_reverse_02",
        )
    assert not foreign_compare_overlay.exists()
    assert clip_a_path.read_bytes() == clip_a_path_bytes_before
    assert clip_b_path.read_bytes() == clip_b_path_bytes_before
    assert animation_review_session_current(ctx) is True
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, review_set)
        is True
    )
    assert _review_set_tree_digest(review_set) == review_bytes_before

    midprep_overlay = (tmp_path / "session-public-gate-compare-midprep-overlay").resolve()
    stale_slot_leaf = _review_set_clip_leaf(review_set, "arm_reverse_02")
    stale_slot_b_before = stale_slot_leaf.read_bytes()
    materialize_original = compare_viewer_module._materialize_snapshot
    midprep_hook_executed = {"done": False}

    def _materialize_then_mutate_review_set(snapshot: object, overlay_dir: Path) -> None:
        materialize_original(snapshot, overlay_dir)
        midprep_hook_executed["done"] = True
        drifted = json.loads(stale_slot_b_before.decode("utf-8"))
        drifted["duration_seconds"] = float(drifted.get("duration_seconds", 2.0)) + 0.01
        stale_slot_leaf.write_bytes(
            json.dumps(drifted, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )

    monkeypatch.setattr(
        compare_viewer_module,
        "_materialize_snapshot",
        _materialize_then_mutate_review_set,
    )
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, review_set)
        is True
    )
    try:
        with pytest.raises(
            ValidationError,
            match="(changed during compare viewer preparation|nested clips payload digest mismatch)",
        ):
            prepare_animation_review_compare_viewer(
                handlers=ctx.handlers,
                workflow_id=ctx.workflow_id,
                preview_dir=preview,
                clip_packages=sources,
                review_dir=review_set,
                overlay_dir=midprep_overlay,
                left_clip_id="arm_wave_01",
                right_clip_id="arm_reverse_02",
            )
    finally:
        monkeypatch.undo()
        stale_slot_leaf.write_bytes(stale_slot_b_before)
    assert midprep_hook_executed["done"]
    assert not midprep_overlay.exists()
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, review_set)
        is True
    )
    assert _review_set_tree_digest(review_set) == review_bytes_before

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
    source_digests_at_drift = _handoff_fixture_source_digests(
        preview=preview,
        review_set=review_set,
        clip_a=clip_a,
        clip_b=clip_b,
        clip_a_path=clip_a_path,
        clip_b_path=clip_b_path,
        session_path=session_path,
    )

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

    handoff_stale_root = (tmp_path / "session-public-gate-handoff-stale").resolve()
    stale_handoff = export_animation_review_handoff(ctx, handoff_stale_root)
    assert stale_handoff.production_eligible is False
    assert stale_handoff.promotion_eligible is False
    assert stale_handoff.session_current is False
    assert stale_handoff.session_revision == kept.stored.session.revision
    assert stale_handoff.raw_session_sha256 == kept.stored.raw_sha256
    _assert_bound_handoff_report(
        handoff_stale_root,
        expected_raw_sha=kept.stored.raw_sha256,
        expected_revision=kept.stored.session.revision,
        session_current=False,
        captured_session_document=captured_session_document,
    )
    assert (
        _handoff_fixture_source_digests(
            preview=preview,
            review_set=review_set,
            clip_a=clip_a,
            clip_b=clip_b,
            clip_a_path=clip_a_path,
            clip_b_path=clip_b_path,
            session_path=session_path,
        )
        == source_digests_at_drift
    )
