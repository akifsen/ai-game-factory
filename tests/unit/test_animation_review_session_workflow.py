"""V0.8-9a animation review session workflow authority (fast unit coverage).

Public-gate integration against a real exported V0.8-8 review set is not exercised here;
Lead must verify at least one unmocked ``animation_review_set_current`` path separately.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
    canonical_review_root_identity,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    animation_review_session_current,
    create_animation_review_session,
    update_animation_review_session,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    ANIMATION_REVIEW_SET_STATUS_TEST_ONLY,
    CandidateAnimationReviewSetSource,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
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
) -> dict[str, bytes]:
    review_root.mkdir(parents=True, exist_ok=True)
    ordered: list[dict[str, Any]] = []
    written: dict[str, bytes] = {}
    for index, (clip_id, doc) in enumerate(clips):
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        slot = f"{index:03d}"
        rel = f"clips/{slot}/animation_clip.json"
        path = review_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        written[str(path)] = raw
        ordered.append(
            _ordered_clip_entry(clip_id=clip_id, raw_sha256=hashlib.sha256(raw).hexdigest())
        )
    manifest_raw = json.dumps(
        _manifest_document(ordered_clips=ordered),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path = review_root / "animation_review_set_manifest.json"
    manifest_path.write_bytes(manifest_raw)
    written[str(manifest_path)] = manifest_raw
    return written


def _snapshot_tree_files(review_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(review_root.rglob("*")):
        if path.is_file():
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _workflow_context(
    tmp_path: Path,
    review_root: Path,
    *,
    session_path: Path | None = None,
) -> AnimationReviewSessionWorkflowContext:
    preview = tmp_path / "preview"
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
    handlers = cast(CandidateWorkflowHandlers, object())
    return AnimationReviewSessionWorkflowContext(
        handlers=handlers,
        workflow_id="wf-test",
        preview_dir=preview,
        clip_packages=packages,
        review_dir=review_root,
        session_path=session_path,
    )


def _patch_gate(monkeypatch: pytest.MonkeyPatch, *, outcomes: list[bool]) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    index = {"i": 0}

    def _gate(*args: Any, **kwargs: Any) -> bool:
        calls.append(args)
        position = index["i"]
        index["i"] = position + 1
        if position >= len(outcomes):
            return outcomes[-1]
        return outcomes[position]

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        _gate,
    )
    return calls


def test_create_maps_binding_fields_and_calls_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA", duration=1.5)),
            ("WalkB", _minimal_clip_document(clip_id="WalkB", duration=2.0)),
        ],
    )
    gate_calls = _patch_gate(monkeypatch, outcomes=[True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    before_tree = _snapshot_tree_files(review_root)

    result = create_animation_review_session(ctx)

    assert result.committed is True
    assert result.current is True
    assert len(gate_calls) >= 2
    manifest_sha = hashlib.sha256(
        (review_root / "animation_review_set_manifest.json").read_bytes()
    ).hexdigest()
    assert result.stored.session.binding.review_root == canonical_review_root_identity(review_root)
    assert result.stored.session.binding.raw_manifest_sha256 == manifest_sha
    assert result.stored.session.binding.root_payload_sha256 == _DIGEST
    assert result.stored.session.binding.clip_payload_sha256 == _DIGEST
    assert [c.clip_id for c in result.stored.session.binding.clips] == ["IdleA", "WalkB"]
    assert result.stored.session.binding.clips[0].duration_seconds == 1.5
    assert (
        result.stored.session.binding.clips[1].raw_clip_sha256
        == hashlib.sha256((review_root / "clips/001/animation_clip.json").read_bytes()).hexdigest()
    )
    assert _snapshot_tree_files(review_root) == before_tree
    assert animation_review_session_current(ctx) is True


def test_create_rejects_stale_gate_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[False])
    ctx = _workflow_context(tmp_path, review_root)
    store = AnimationReviewSessionStore(review_root=review_root)
    with pytest.raises(CandidateCurrentnessError, match="not current"):
        create_animation_review_session(ctx)
    assert not store.session_path.exists()


def test_update_rejects_foreign_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    clip_path = review_root / "clips/000/animation_clip.json"
    new_doc = _minimal_clip_document(clip_id="IdleA", duration=2.0)
    new_raw = json.dumps(new_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    clip_path.write_bytes(new_raw)
    manifest_path = review_root / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ordered_clips"][0]["raw_clip_sha256"] = hashlib.sha256(new_raw).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(CandidateCurrentnessError, match="does not match live"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
        )
    reloaded = AnimationReviewSessionStore(review_root=review_root).load()
    assert reloaded.session == created.stored.session


def test_update_unknown_clip_and_note_and_bookmark_rejected_without_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA", duration=1.0)),
            ("WalkB", _minimal_clip_document(clip_id="WalkB", duration=1.0)),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    store = AnimationReviewSessionStore(review_root=review_root)

    with pytest.raises(ValidationError, match="unknown clip_id"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "Missing", "status": "keep"},
        )

    huge_note = "x" * 2049
    with pytest.raises(ValidationError, match="note"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "SetNote", "clip_id": "IdleA", "note": huge_note},
        )

    with pytest.raises(ValidationError, match="duration_seconds"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "AddBookmark", "clip_id": "IdleA", "timestamp": 5.0},
        )

    assert store.load().session == created.stored.session


def test_raw_sha_conflict_preserves_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    with pytest.raises(AnimationReviewSessionStoreConflictError):
        update_animation_review_session(
            ctx,
            expected_raw_sha256="0" * 64,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )
    assert (
        AnimationReviewSessionStore(review_root=review_root).load().session
        == created.stored.session
    )


def test_manifest_drift_during_precommit_rejects_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    import gamefactory.workflows.animation_review_session as workflow

    original_read = workflow._read_live_review_set_binding
    calls = {"n": 0}

    def read_then_mutate(*args: Any, **kwargs: Any) -> Any:
        binding, snapshot = original_read(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            manifest_path = review_root / "animation_review_set_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["root_payload_digest"] = "e" * 64
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
        return binding, snapshot

    monkeypatch.setattr(workflow, "_read_live_review_set_binding", read_then_mutate)
    _patch_gate(monkeypatch, outcomes=[True, True])
    ctx = _workflow_context(tmp_path, review_root)
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        create_animation_review_session(ctx)
    assert not AnimationReviewSessionStore(review_root=review_root).session_path.exists()


def test_postcommit_drift_returns_committed_true_current_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, False])
    ctx = _workflow_context(tmp_path, review_root)
    result = create_animation_review_session(ctx)
    assert result.committed is True
    assert result.current is False
    assert AnimationReviewSessionStore(review_root=review_root).load().session.revision == 0


def test_historical_load_without_gate_and_stale_current_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, False, False])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    assert animation_review_session_current(ctx) is False
    historical = AnimationReviewSessionStore(review_root=review_root).load()
    assert historical.session.revision == created.stored.session.revision


def test_update_rejects_stale_gate_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, False])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    with pytest.raises(CandidateCurrentnessError, match="not current"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
        )
    assert (
        AnimationReviewSessionStore(review_root=review_root).load().session
        == created.stored.session
    )


def test_deeply_nested_manifest_maps_recursion_to_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    review_root.mkdir(parents=True)
    depth = 3000
    payload = '{"x":' * depth + "0" + "}" * depth
    manifest_bytes = payload.encode("utf-8")
    assert len(manifest_bytes) < 64 * 1024
    (review_root / "animation_review_set_manifest.json").write_bytes(manifest_bytes)
    _patch_gate(monkeypatch, outcomes=[True])
    ctx = _workflow_context(tmp_path, review_root)
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        with pytest.raises(ValidationError):
            create_animation_review_session(ctx)
    finally:
        sys.setrecursionlimit(old_limit)


def test_malformed_manifest_raises_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    review_root.mkdir(parents=True)
    (review_root / "animation_review_set_manifest.json").write_text(
        '{"broken":true}', encoding="utf-8"
    )
    _patch_gate(monkeypatch, outcomes=[True])
    ctx = _workflow_context(tmp_path, review_root)
    with pytest.raises(ValidationError):
        create_animation_review_session(ctx)


def test_explicit_session_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    session_path = tmp_path / "sidecar" / "session.json"
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root, session_path=session_path)
    result = create_animation_review_session(ctx)
    assert session_path.is_file()
    assert session_path.read_bytes()
    assert result.stored.session.binding.review_root == canonical_review_root_identity(review_root)


def _coherent_rehash_clip_slot(
    review_root: Path,
    *,
    slot_index: int,
    clip_id: str,
    duration: float,
) -> None:
    clip_path = review_root / f"clips/{slot_index:03d}/animation_clip.json"
    new_doc = _minimal_clip_document(clip_id=clip_id, duration=duration)
    new_raw = json.dumps(new_doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    clip_path.write_bytes(new_raw)
    manifest_path = review_root / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ordered_clips"][slot_index]["raw_clip_sha256"] = hashlib.sha256(new_raw).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def test_create_rejects_coherent_swap_during_gate_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA", duration=1.0)),
            ("WalkB", _minimal_clip_document(clip_id="WalkB", duration=1.0)),
        ],
    )
    swapped = False

    def gate_then_coherent_swap(*args: Any, **kwargs: Any) -> bool:
        nonlocal swapped
        if not swapped:
            swapped = True
            _coherent_rehash_clip_slot(
                review_root,
                slot_index=0,
                clip_id="IdleA",
                duration=2.0,
            )
        return True

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        gate_then_coherent_swap,
    )
    ctx = _workflow_context(tmp_path, review_root)
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        create_animation_review_session(ctx)
    assert not AnimationReviewSessionStore(review_root=review_root).session_path.exists()


def test_update_rejects_when_session_bound_to_b_gate_sandwich_sees_swap_from_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session pinned to original tree; gate callback coherently swaps slot 0 before re-read."""
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA", duration=1.0)),
            ("WalkB", _minimal_clip_document(clip_id="WalkB", duration=1.0)),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True, True, True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    swap_during_gate = False

    def gate_swap_once(*args: Any, **kwargs: Any) -> bool:
        nonlocal swap_during_gate
        if not swap_during_gate:
            swap_during_gate = True
            _coherent_rehash_clip_slot(
                review_root,
                slot_index=0,
                clip_id="IdleA",
                duration=2.5,
            )
        return True

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        gate_swap_once,
    )
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        update_animation_review_session(
            ctx,
            expected_raw_sha256=created.stored.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
        )
    assert (
        AnimationReviewSessionStore(review_root=review_root).load().session
        == created.stored.session
    )


def test_postcommit_candidate_currentness_error_returns_committed_true_current_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    import gamefactory.workflows.animation_review_session as workflow

    original_capture = workflow._capture_current_binding
    calls = {"n": 0}

    def capture_then_raise(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise CandidateCurrentnessError("post-commit review set no longer current")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(workflow, "_capture_current_binding", capture_then_raise)
    _patch_gate(monkeypatch, outcomes=[True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    result = create_animation_review_session(ctx)
    assert result.committed is True
    assert result.current is False
    assert AnimationReviewSessionStore(review_root=review_root).session_path.exists()


def test_postcommit_store_for_context_failure_returns_committed_true_current_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    import gamefactory.workflows.animation_review_session as workflow

    original_store = workflow._store_for_context
    calls = {"n": 0}

    def store_then_fail(ctx: AnimationReviewSessionWorkflowContext) -> AnimationReviewSessionStore:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ValidationError("review_root layout became unsafe after commit")
        return original_store(ctx)

    monkeypatch.setattr(workflow, "_store_for_context", store_then_fail)
    _patch_gate(monkeypatch, outcomes=[True, True, True])
    ctx = _workflow_context(tmp_path, review_root)
    result = create_animation_review_session(ctx)
    assert result.committed is True
    assert result.current is False
    assert AnimationReviewSessionStore(review_root=review_root).session_path.exists()


def test_postcommit_validation_error_returns_committed_true_current_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    gate_calls = {"n": 0}

    def gate_fail_after_commit(*args: Any, **kwargs: Any) -> bool:
        gate_calls["n"] += 1
        if gate_calls["n"] >= 3:
            raise ValidationError("post-commit authority read failure")
        return True

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        gate_fail_after_commit,
    )
    ctx = _workflow_context(tmp_path, review_root)
    result = create_animation_review_session(ctx)
    assert result.committed is True
    assert result.current is False
    assert result.stored.session.revision == 0
    assert AnimationReviewSessionStore(review_root=review_root).session_path.exists()
    assert animation_review_session_current(ctx) is False


def test_postcommit_oserror_returns_committed_true_current_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(monkeypatch, outcomes=[True] * 3)
    ctx = _workflow_context(tmp_path, review_root)
    created = create_animation_review_session(ctx)
    gate_calls = {"n": 0}

    def gate_oserror_after_commit(*args: Any, **kwargs: Any) -> bool:
        gate_calls["n"] += 1
        if gate_calls["n"] >= 3:
            raise OSError("post-commit authority path failure")
        return True

    monkeypatch.setattr(
        "gamefactory.workflows.animation_review_session.animation_review_set_current",
        gate_oserror_after_commit,
    )
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=created.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
    )
    assert updated.committed is True
    assert updated.current is False
    assert updated.stored.raw_sha256 != created.stored.raw_sha256
    assert updated.stored.session.revision == 1
    reloaded = AnimationReviewSessionStore(review_root=review_root).load()
    assert reloaded.raw_sha256 == updated.stored.raw_sha256
    assert reloaded.session.clip_records[0].status == "revise"
    assert animation_review_session_current(ctx) is False


def test_update_applies_operation_when_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_root = tmp_path / "review-set"
    _write_review_tree(
        review_root,
        clips=[
            ("IdleA", _minimal_clip_document(clip_id="IdleA")),
            ("WalkB", _minimal_clip_document(clip_id="WalkB")),
        ],
    )
    _patch_gate(
        monkeypatch,
        outcomes=[True, True, True, True, True, True, True, True],
    )
    ctx = _workflow_context(tmp_path, review_root)
    before_tree = _snapshot_tree_files(review_root)
    created = create_animation_review_session(ctx)
    updated = update_animation_review_session(
        ctx,
        expected_raw_sha256=created.stored.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
    )
    assert updated.committed is True
    assert updated.current is True
    assert updated.stored.session.clip_records[0].status == "keep"
    assert updated.stored.session.revision == 1
    assert _snapshot_tree_files(review_root) == before_tree
