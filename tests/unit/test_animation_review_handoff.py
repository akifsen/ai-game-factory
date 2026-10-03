"""V0.8-10a animation review handoff report (fast unit coverage).

``animation_review_session_current`` / review-set gate integration is stubbed where noted;
Lead must verify unmocked public gate paths separately.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.animation_review_handoff import (
    REVIEW_REPORT_JSON_NAME,
    REVIEW_REPORT_MD_NAME,
    export_animation_review_handoff,
)
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    create_animation_review_session,
    update_animation_review_session,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    ANIMATION_REVIEW_SET_STATUS_TEST_ONLY,
    CandidateAnimationReviewSetSource,
)
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers

_DIGEST = "a" * 64


def _minimal_clip_document(*, clip_id: str, duration: float = 1.0) -> dict[str, Any]:
    return {
        "schema_version": "rig-animation-clip-0.8.0",
        "clip_id": clip_id,
        "duration_seconds": duration,
        "loop": False,
        "tracks": [
            {
                "bone": "Spine",
                "keyframes": [{"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
            }
        ],
    }


def _ordered_clip_entry(*, clip_id: str, raw_sha256: str) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "raw_clip_sha256": raw_sha256,
        "raw_original_manifest_sha256": _DIGEST,
        "source_package_sha256": _DIGEST,
        "source_file_digests": {"animation_clip.json": raw_sha256},
    }


def _manifest_document(
    *, ordered_clips: list[dict[str, Any]], workflow_id: str = "wf-test"
) -> dict[str, Any]:
    file_digests = {"project.godot": _DIGEST}
    return {
        "schema_version": ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
        "workflow_id": workflow_id,
        "evidence_task_id": "task",
        "evidence_execution_id": "exec",
        "evidence_attempt_number": 1,
        "manifest_artifact_id": "m",
        "manifest_artifact_sha256": _DIGEST,
        "result_artifact_id": "r",
        "result_artifact_sha256": _DIGEST,
        "marker_artifact_id": "k",
        "marker_artifact_sha256": _DIGEST,
        "snapshot_fingerprint": _DIGEST,
        "spec_fingerprint": _DIGEST,
        "processed_glb_sha256": _DIGEST,
        "upstream_preview_manifest_sha256": _DIGEST,
        "shared_preview_identity_digest": _DIGEST,
        "ordered_clips": ordered_clips,
        "file_digests": file_digests,
        "root_payload_digest": _DIGEST,
        "nested_clips_payload_digest": _DIGEST,
        "animation_review_set_status": ANIMATION_REVIEW_SET_STATUS_TEST_ONLY,
        "production_eligible": False,
        "promotion_eligible": False,
    }


def _write_review_tree(
    review_root: Path,
    *,
    clips: list[tuple[str, dict[str, Any]]],
) -> None:
    review_root.mkdir(parents=True, exist_ok=True)
    ordered: list[dict[str, Any]] = []
    for index, (clip_id, doc) in enumerate(clips):
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        slot = f"{index:03d}"
        path = review_root / f"clips/{slot}/animation_clip.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        ordered.append(
            _ordered_clip_entry(clip_id=clip_id, raw_sha256=hashlib.sha256(raw).hexdigest())
        )
    manifest_raw = json.dumps(
        _manifest_document(ordered_clips=ordered),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (review_root / "animation_review_set_manifest.json").write_bytes(manifest_raw)


def _snapshot_tree_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _handlers_root(tmp_path: Path) -> CandidateWorkflowHandlers:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return cast(
        CandidateWorkflowHandlers,
        SimpleNamespace(root=project),
    )


def _workflow_context(
    tmp_path: Path,
    review_root: Path,
    *,
    session_path: Path | None = None,
    preview_dir: Path | None = None,
) -> AnimationReviewSessionWorkflowContext:
    preview = preview_dir if preview_dir is not None else tmp_path / "preview"
    preview.mkdir(parents=True, exist_ok=True)
    pkg_a = tmp_path / "pkg-a"
    pkg_b = tmp_path / "pkg-b"
    pkg_a.mkdir()
    pkg_b.mkdir()
    clip_a = tmp_path / "clip-a.json"
    clip_b = tmp_path / "clip-b.json"
    clip_a.write_text("{}", encoding="utf-8")
    clip_b.write_text("{}", encoding="utf-8")
    packages = (
        CandidateAnimationReviewSetSource(pkg_a, clip_a),
        CandidateAnimationReviewSetSource(pkg_b, clip_b),
    )
    return AnimationReviewSessionWorkflowContext(
        handlers=_handlers_root(tmp_path),
        workflow_id="wf-test",
        preview_dir=preview,
        clip_packages=packages,
        review_dir=review_root,
        session_path=session_path,
    )


def _patch_gate(monkeypatch: pytest.MonkeyPatch, *, outcomes: list[bool]) -> None:
    index = {"i": 0}

    def _gate(*args: Any, **kwargs: Any) -> bool:
        position = index["i"]
        index["i"] = position + 1
        if position >= len(outcomes):
            return outcomes[-1]
        return outcomes[position]

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        _gate,
    )


def _seed_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("ClipB", _minimal_clip_document(clip_id="ClipB", duration=2.0)),
            ("ClipA", _minimal_clip_document(clip_id="ClipA", duration=1.5)),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=created.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "ClipA", "status": "revise"},
    )
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=updated.stored.raw_sha256,
        operation={"op": "SetNote", "clip_id": "ClipA", "note": "note α\nline2"},
    )
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=updated.stored.raw_sha256,
        operation={"op": "AddBookmark", "clip_id": "ClipA", "timestamp": 0.75},
    )
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=updated.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "ClipB", "status": "keep"},
    )
    return ctx, review_root


def _source_tree_snapshot(ctx: Any) -> dict[str, str]:
    roots = [
        Path(ctx.preview_dir),
        Path(ctx.review_dir),
        Path(ctx.handlers.root),
    ]
    for package in ctx.clip_packages:
        roots.append(Path(package.animation_dir))
        roots.append(Path(package.clip_path))
    out: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            out[str(root)] = hashlib.sha256(root.read_bytes()).hexdigest()
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    store = AnimationReviewSessionStore(review_root=Path(ctx.review_dir))
    out[str(store.session_path)] = hashlib.sha256(store.session_path.read_bytes()).hexdigest()
    return out


def test_handoff_positive_order_annotations_revision_and_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    review_before = _snapshot_tree_files(review_root)
    sources_before = _source_tree_snapshot(ctx)
    session_before = hashlib.sha256(
        AnimationReviewSessionStore(review_root=review_root).session_path.read_bytes()
    ).hexdigest()

    out = tmp_path / "handoff-positive"
    result = export_animation_review_handoff(ctx, out)

    assert result.production_eligible is False
    assert result.promotion_eligible is False
    assert result.session_current is True
    assert result.session_revision == 4
    assert result.raw_session_sha256 == session_before
    session_after = hashlib.sha256(
        AnimationReviewSessionStore(review_root=review_root).session_path.read_bytes()
    ).hexdigest()
    assert session_after == session_before
    doc = json.loads((out / REVIEW_REPORT_JSON_NAME).read_text(encoding="utf-8"))
    assert [c["clip_id"] for c in doc["clips"]] == ["ClipB", "ClipA"]
    assert doc["clips"][0]["status"] == "keep"
    assert doc["clips"][0]["duration_seconds"] == 2.0
    assert doc["clips"][1]["status"] == "revise"
    assert doc["clips"][1]["note"] == "note α\nline2"
    assert doc["clips"][1]["bookmarks"] == [0.75]
    assert doc["clips"][1]["duration_seconds"] == 1.5
    assert doc["keep_status_is_annotation_not_approval"] is True
    assert doc["production_eligible"] is False
    assert doc["promotion_eligible"] is False
    md = (out / REVIEW_REPORT_MD_NAME).read_text(encoding="utf-8")
    assert "production_eligible: false" in md
    assert "promotion_eligible: false" in md
    assert _snapshot_tree_files(review_root) == review_before
    assert _source_tree_snapshot(ctx) == sources_before


def test_handoff_deterministic_bytes_across_fresh_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    out_a = tmp_path / "handoff-determ-a"
    out_b = tmp_path / "handoff-determ-b"
    export_animation_review_handoff(ctx, out_a)
    export_animation_review_handoff(ctx, out_b)
    for name in (REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_handoff_stale_session_exports_current_false_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    _patch_gate(monkeypatch, outcomes=[False])
    out = tmp_path / "handoff-stale"
    result = export_animation_review_handoff(ctx, out)
    assert result.session_current is False
    doc = json.loads((out / REVIEW_REPORT_JSON_NAME).read_text(encoding="utf-8"))
    assert doc["session_current"] is False
    md = (out / REVIEW_REPORT_MD_NAME).read_text(encoding="utf-8")
    assert md.startswith("# SESSION NOT CURRENT\n")


def test_handoff_rejects_missing_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("ClipA", _minimal_clip_document(clip_id="ClipA")),
            ("ClipB", _minimal_clip_document(clip_id="ClipB")),
        ],
    )
    ctx = _workflow_context(tmp_path, review_root)
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, tmp_path / "handoff-missing")


def test_handoff_rejects_malformed_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    store.session_path.write_bytes(b"{not-json")
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, tmp_path / "handoff-bad-json")


def test_handoff_rejects_oversized_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    store.session_path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, tmp_path / "handoff-oversize")


def test_handoff_rejects_unknown_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    document = json.loads(store.session_path.read_text(encoding="utf-8"))
    document["clip_records"][0]["status"] = "approved"
    store.session_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, tmp_path / "handoff-bad-status")


def test_handoff_rejects_bookmark_out_of_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    document = json.loads(store.session_path.read_text(encoding="utf-8"))
    document["clip_records"][0]["bookmarks"] = [9.9]
    store.session_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, tmp_path / "handoff-bad-bookmark")


def test_handoff_rejects_session_race_during_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    import gamefactory.workflows.animation_review_handoff as handoff

    original_eval = handoff._evaluate_session_current
    calls = {"n": 0}

    def flaky_eval(
        workflow_ctx: AnimationReviewSessionWorkflowContext,
        stored: Any,
    ) -> bool:
        calls["n"] += 1
        result = original_eval(workflow_ctx, stored)
        if calls["n"] == 2:
            store = AnimationReviewSessionStore(review_root=review_root)
            session_path = store.session_path
            document = json.loads(session_path.read_text(encoding="utf-8"))
            document["revision"] = 99
            session_path.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(handoff, "_evaluate_session_current", flaky_eval)
    staging_parent = tmp_path / ".gf" / "candidate_animation_review_handoff_staging"
    out = tmp_path / "handoff-race"
    with pytest.raises(AnimationReviewSessionStoreConflictError):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    if staging_parent.exists():
        assert list(staging_parent.iterdir()) == []


def test_handoff_rejects_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    out = tmp_path / "handoff-exists"
    out.mkdir()
    with pytest.raises(ArtifactError, match="already exists"):
        export_animation_review_handoff(ctx, out)


def test_handoff_rejects_output_under_review_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="outside"):
        export_animation_review_handoff(ctx, review_root / "nested-handoff")


def test_handoff_atomic_failure_cleans_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    import gamefactory.workflows.animation_review_handoff as handoff

    def boom(*args: Any, **kwargs: Any) -> None:
        raise ArtifactError("handoff publish blocked for test")

    monkeypatch.setattr(handoff, "atomic_publish_staged_container", boom)
    staging_parent = tmp_path / ".gf" / "candidate_animation_review_handoff_staging"
    out = tmp_path / "handoff-cleanup"
    with pytest.raises(ArtifactError, match="handoff publish blocked"):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    if staging_parent.exists():
        assert list(staging_parent.iterdir()) == []


def test_handoff_writer_race_second_fails_without_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    out = tmp_path / "handoff-race-target"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run_export() -> None:
        try:
            barrier.wait()
            export_animation_review_handoff(ctx, out)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run_export) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert out.is_dir()
    assert (out / REVIEW_REPORT_JSON_NAME).is_file()
    assert (out / REVIEW_REPORT_MD_NAME).is_file()
    assert sum(isinstance(exc, ArtifactError) for exc in errors) == 1


def test_handoff_rejects_staging_inside_canonical_preview_gf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("ClipB", _minimal_clip_document(clip_id="ClipB")),
            ("ClipA", _minimal_clip_document(clip_id="ClipA")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True] * 12)
    gf_dir = tmp_path / ".gf"
    gf_dir.mkdir()
    marker = gf_dir / "user-marker.txt"
    marker.write_text("preserve-me", encoding="utf-8")
    gf_before = _snapshot_tree_files(gf_dir)
    ctx = _workflow_context(tmp_path, review_root, preview_dir=gf_dir)
    create_animation_review_session(ctx)
    out = tmp_path / "handoff-out"
    with pytest.raises(ValidationError, match="outside"):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    assert _snapshot_tree_files(gf_dir) == gf_before


def test_handoff_rejects_destination_overlapping_session_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    session_parent = store.session_path.parent
    with pytest.raises(ValidationError, match="outside"):
        export_animation_review_handoff(ctx, session_parent / "nested-handoff")


def test_handoff_preserves_lone_cr_and_crlf_in_json_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    document = json.loads(store.session_path.read_text(encoding="utf-8"))
    document["clip_records"][1]["note"] = "valid\rnote\r\nsecond"
    store.session_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    out = tmp_path / "handoff-crlf"
    export_animation_review_handoff(ctx, out)
    raw_json = (out / REVIEW_REPORT_JSON_NAME).read_bytes()
    assert b"valid\\rnote\\r\\nsecond" in raw_json
    doc = json.loads(raw_json.decode("utf-8"))
    assert doc["clips"][1]["note"] == "valid\rnote\r\nsecond"
    md = (out / REVIEW_REPORT_MD_NAME).read_text(encoding="utf-8")
    assert "valid\nnote\nsecond" in md
    assert "<a href" not in md.lower()


def test_handoff_markdown_renders_untrusted_fields_as_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    store = AnimationReviewSessionStore(review_root=review_root)
    document = json.loads(store.session_path.read_text(encoding="utf-8"))
    document["clip_records"][1]["note"] = (
        "[x](https://evil.example)\n"
        "    four-space\n"
        "`tick` ```fence```\n"
        "## heading\n"
        "<script>alert(1)</script>"
    )
    store.session_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    out = tmp_path / "handoff-md-safe"
    export_animation_review_handoff(ctx, out)
    md = (out / REVIEW_REPORT_MD_NAME).read_text(encoding="utf-8")
    assert "https://evil.example" in md
    assert "&lt;" not in md
    assert "production_eligible: false" in md
    assert "promotion_eligible: false" in md
    assert "## heading" in md
    assert "- review_root:" in md
    doc = json.loads((out / REVIEW_REPORT_JSON_NAME).read_text(encoding="utf-8"))
    assert doc["review_root"] in md


def test_handoff_rejects_session_race_after_payload_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root = _seed_session(tmp_path, monkeypatch)
    import gamefactory.workflows.animation_review_handoff as handoff

    real_write = handoff._write_handoff_stage

    def mutating_write(stage: Path, json_bytes: bytes, md_bytes: bytes) -> None:
        store = AnimationReviewSessionStore(review_root=review_root)
        session_path = store.session_path
        document = json.loads(session_path.read_text(encoding="utf-8"))
        document["revision"] = 77
        session_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        real_write(stage, json_bytes, md_bytes)

    monkeypatch.setattr(handoff, "_write_handoff_stage", mutating_write)
    staging_parent = tmp_path / ".gf" / "candidate_animation_review_handoff_staging"
    out = tmp_path / "handoff-post-write-race"
    with pytest.raises(AnimationReviewSessionStoreConflictError):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    if staging_parent.exists():
        assert list(staging_parent.iterdir()) == []


def test_handoff_rejects_destination_reserved_gf_dir_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    gf_dir = tmp_path / ".gf"
    assert not gf_dir.exists()
    sources_before = _source_tree_snapshot(ctx)
    with pytest.raises(ValidationError):
        export_animation_review_handoff(ctx, gf_dir)
    assert not gf_dir.exists()
    assert _source_tree_snapshot(ctx) == sources_before


def test_handoff_rejects_destination_handoff_staging_namespace_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    out = tmp_path / "candidate_animation_review_handoff_staging"
    assert not out.exists()
    sources_before = _source_tree_snapshot(ctx)
    with pytest.raises(ValidationError, match="staging namespace"):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    assert _source_tree_snapshot(ctx) == sources_before


def _seed_session_under_nested_review_with_explicit_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AnimationReviewSessionWorkflowContext, Path, Path]:
    future_parent = tmp_path / "future"
    review_root = future_parent / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("ClipB", _minimal_clip_document(clip_id="ClipB", duration=2.0)),
            ("ClipA", _minimal_clip_document(clip_id="ClipA", duration=1.5)),
        ],
    )
    explicit_session = tmp_path / "durable-sidecar" / "session.json"
    explicit_session.parent.mkdir(parents=True, exist_ok=True)
    _patch_gate(monkeypatch, outcomes=[True, True])
    ctx = _workflow_context(tmp_path, review_root, session_path=explicit_session)
    create_animation_review_session(ctx)
    return ctx, review_root, explicit_session


def _remove_owned_review_fixture_tree(review_root: Path) -> None:
    future_parent = review_root.parent
    shutil.rmtree(review_root)
    if future_parent.exists() and not any(future_parent.iterdir()):
        future_parent.rmdir()


def test_handoff_rejects_output_directory_ancestor_of_missing_review_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root, explicit_session = _seed_session_under_nested_review_with_explicit_sidecar(
        tmp_path,
        monkeypatch,
    )
    session_before = explicit_session.read_bytes()
    _remove_owned_review_fixture_tree(review_root)
    ancestor_destination = review_root.parent
    assert not ancestor_destination.exists()
    staging_parent = tmp_path / ".gf" / "candidate_animation_review_handoff_staging"
    with pytest.raises(ValidationError, match="outside"):
        export_animation_review_handoff(ctx, ancestor_destination)
    assert not ancestor_destination.exists()
    assert explicit_session.read_bytes() == session_before
    if staging_parent.exists():
        assert list(staging_parent.iterdir()) == []


def test_handoff_exports_stale_session_to_fresh_sibling_after_review_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, review_root, explicit_session = _seed_session_under_nested_review_with_explicit_sidecar(
        tmp_path,
        monkeypatch,
    )
    session_before = explicit_session.read_bytes()
    _remove_owned_review_fixture_tree(review_root)
    _patch_gate(monkeypatch, outcomes=[False])
    out = tmp_path / "handoff-fresh-sibling"
    result = export_animation_review_handoff(ctx, out)
    assert result.session_current is False
    assert out.is_dir()
    assert (out / REVIEW_REPORT_JSON_NAME).is_file()
    assert explicit_session.read_bytes() == session_before
    doc = json.loads((out / REVIEW_REPORT_JSON_NAME).read_text(encoding="utf-8"))
    assert doc["session_current"] is False


def test_handoff_cleans_owned_stage_when_post_mkdir_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _review = _seed_session(tmp_path, monkeypatch)
    import gamefactory.workflows.animation_review_handoff as handoff

    def boom_volume(left: Path, right: Path) -> None:
        raise ValidationError("handoff staging volume check failed for test")

    monkeypatch.setattr(handoff, "_assert_same_volume", boom_volume)
    staging_parent = tmp_path / ".gf" / "candidate_animation_review_handoff_staging"
    out = tmp_path / "handoff-stage-fail"
    with pytest.raises(ValidationError, match="volume"):
        export_animation_review_handoff(ctx, out)
    assert not out.exists()
    if staging_parent.exists():
        assert list(staging_parent.iterdir()) == []
