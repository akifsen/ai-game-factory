"""V0.8-7 unit tests: animation review export and currentness."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ProviderInvocationRepository,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ExecutionStatus
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_PACKAGE_FILES,
    acceptance_arm_wave_clip_raw_bytes,
    animation_clip_preview_current,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_animation_review import (
    animation_review_current,
    export_rigged_character_animation_review,
    trusted_animation_review_ui_digests,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_preview import (
    candidate_preview_current,
    export_rigged_character_candidate_preview,
)
from gamefactory.workflows.v08_candidate_workflow import candidate_workflow_readiness
from tests.unit.test_v08_candidate_workflow import _run_to_test_only_gate
from tests.unit.v08_candidate_c2b_publication_fixtures import (
    inject_destination_directory_junction,
    inject_nonempty_destination_directory,
)
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    CompletedEvidenceContext,
    inject_newer_evidence_attempt,
    run_completed_managed_evidence,
)
from tests.unit.v08_candidate_upstream_fixtures import (
    drift_source_retained_registration_hash,
    mutate_receipt_coherent_field,
    mutate_retained_profile_bounds_tolerance,
    mutate_retained_spec_semantic_intent,
    set_revision_processed_glb_hash,
)


@pytest.fixture(scope="module")
def managed_review_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path_factory.mktemp("review-managed-c2b"))


@pytest.fixture
def isolated_review_evidence(tmp_path: Path) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path / "review-isolated-c2b")


@pytest.fixture(scope="module")
def shared_preview(
    managed_review_evidence: CompletedEvidenceContext, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    out = tmp_path_factory.mktemp("review-shared-preview") / "preview"
    export_rigged_character_candidate_preview(
        managed_review_evidence.handlers, managed_review_evidence.workflow_id, out
    )
    return out


@pytest.fixture(scope="module")
def acceptance_clip_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("review-clip-fixture") / "arm_wave_01.json"
    path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    return path


@pytest.fixture(scope="module")
def shared_clip_preview(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-shared-clip") / "clip-preview"
    export_rigged_character_animation_clip_preview(
        managed_review_evidence.handlers,
        managed_review_evidence.workflow_id,
        shared_preview,
        acceptance_clip_path,
        out,
    )
    return out


@pytest.fixture(scope="module")
def shared_review(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-shared-package") / "review"
    export_rigged_character_animation_review(
        managed_review_evidence.handlers,
        managed_review_evidence.workflow_id,
        shared_preview,
        acceptance_clip_path,
        shared_clip_preview,
        out,
    )
    return out


def _export_quad(
    ctx: CompletedEvidenceContext,
    tmp_path: Path,
    label: str,
    *,
    preview: Path | None = None,
    clip_path: Path | None = None,
    clip_preview: Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    preview = preview or tmp_path / f"{label}-preview"
    clip_path = clip_path or tmp_path / f"{label}-clip.json"
    clip_preview = clip_preview or tmp_path / f"{label}-clip-preview"
    review = tmp_path / f"{label}-review"
    if not clip_path.exists():
        clip_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    if not preview.exists():
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    if not clip_preview.exists():
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
    export_rigged_character_animation_review(
        ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
    )
    return preview, clip_path, clip_preview, review


def _copy_quad(
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
    label: str,
) -> tuple[Path, Path, Path, Path]:
    clip_copy = tmp_path / f"{label}-clip.json"
    clip_copy.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    preview = tmp_path / f"{label}-preview"
    shutil.copytree(shared_preview, preview)
    clip_preview = tmp_path / f"{label}-clip-preview"
    shutil.copytree(shared_clip_preview, clip_preview)
    review = tmp_path / f"{label}-review"
    shutil.copytree(shared_review, review)
    return preview, clip_copy, clip_preview, review


def _upstream_animation_package_sha256(package_dir: Path) -> str:
    digests = {
        name: hashlib.sha256((package_dir / name).read_bytes()).hexdigest()
        for name in sorted(_CLIP_PACKAGE_FILES)
    }
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coherent_drift_review_embedded_v086_to_b(review: Path) -> None:
    clip_path = review / "animation_clip.json"
    doc = json.loads(clip_path.read_bytes())
    doc["duration_seconds"] = 2.0
    clip_path.write_text(json.dumps(doc), encoding="utf-8")
    clip_manifest_path = review / "animation_clip_manifest.json"
    clip_manifest = json.loads(clip_manifest_path.read_bytes())
    clip_manifest["file_digests"]["animation_clip.json"] = sha256_file(clip_path)
    clip_manifest["clip_sha256"] = sha256_file(clip_path)
    clip_manifest_path.write_text(json.dumps(clip_manifest), encoding="utf-8")
    review_manifest_path = review / "animation_review_manifest.json"
    review_manifest = json.loads(review_manifest_path.read_bytes())
    file_digests = dict(review_manifest["file_digests"])
    for name in sorted(_CLIP_PACKAGE_FILES):
        file_digests[name] = sha256_file(review / name)
    review_manifest["file_digests"] = file_digests
    review_manifest["clip_sha256"] = clip_manifest["clip_sha256"]
    review_manifest["upstream_animation_manifest_sha256"] = sha256_file(clip_manifest_path)
    review_manifest["upstream_animation_package_sha256"] = _upstream_animation_package_sha256(
        review
    )
    review_manifest_path.write_text(json.dumps(review_manifest), encoding="utf-8")


_EXPECTED_REVIEW_FILES = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "animation_clip_preview.tscn",
        "animation_clip_preview_player.gd",
        "animation_clip.json",
        "animation_clip_manifest.json",
        "project.godot",
        "animation_review.tscn",
        "animation_review_controller.gd",
        "animation_review_manifest.json",
    }
)


def test_export_review_thirteen_files_and_v086_bytes_unchanged(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    v086_before = {
        name: (shared_clip_preview / name).read_bytes() for name in shared_clip_preview.iterdir()
    }
    godot_before = (shared_clip_preview / "project.godot").read_bytes()
    preview, clip_path, clip_preview, review = _export_quad(
        ctx,
        tmp_path,
        "bytes",
        preview=shared_preview,
        clip_path=acceptance_clip_path,
        clip_preview=shared_clip_preview,
    )
    assert {p.name for p in review.iterdir()} == _EXPECTED_REVIEW_FILES
    for name in shared_clip_preview.iterdir():
        assert (review / name.name).read_bytes() == name.read_bytes()
    assert (review / "project.godot").read_bytes() == godot_before
    assert (
        b'run/main_scene="res://animation_clip_preview.tscn"'
        in (review / "project.godot").read_bytes()
    )
    assert animation_clip_preview_current(
        ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
    )
    assert animation_review_current(
        ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
    )
    for name, payload in v086_before.items():
        assert (clip_preview / name).read_bytes() == payload


def test_export_review_deterministic_thirteen_files(
    managed_review_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = managed_review_evidence
    _, _, _, review_a = _export_quad(ctx, tmp_path, "det-a")
    _, _, _, review_b = _export_quad(ctx, tmp_path, "det-b")
    for name in sorted(_EXPECTED_REVIEW_FILES):
        assert (review_a / name).read_bytes() == (review_b / name).read_bytes()


def test_trusted_ui_digests_match_packaged_resources() -> None:
    digests = trusted_animation_review_ui_digests()
    assert len(digests) == 2
    assert "animation_review.tscn" in digests
    assert "animation_review_controller.gd" in digests


def test_export_review_fails_when_clip_preview_not_current(
    managed_review_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview = tmp_path / "preview-alone"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            preview,
            acceptance_clip_path,
            tmp_path / "missing-v086",
            tmp_path / "review-fail",
        )


def test_review_current_false_on_stale_evidence(
    isolated_review_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = isolated_review_evidence
    preview, clip_path, clip_preview, review = _export_quad(ctx, tmp_path, "stale")
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is False
    )


@pytest.mark.parametrize(
    "mutate",
    [
        drift_source_retained_registration_hash,
        mutate_retained_spec_semantic_intent,
        mutate_retained_profile_bounds_tolerance,
        lambda ctx: set_revision_processed_glb_hash(ctx, "f" * 64),
        lambda ctx: mutate_receipt_coherent_field(
            ctx, field="snapshot_fingerprint", value="0" * 64
        ),
    ],
    ids=("source-retained", "spec-intent", "profile-bounds", "revision-glb", "receipt-snapshot"),
)
def test_review_current_false_on_upstream_drift(
    isolated_review_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    mutate,
) -> None:
    ctx = isolated_review_evidence
    preview, clip_path, clip_preview, review = _export_quad(ctx, tmp_path, "drift")
    mutate(ctx)
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is False
    )


def test_review_current_false_on_coherent_ui_tamper(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "ui-tamper"
    )
    tscn = review / "animation_review.tscn"
    text = tscn.read_text(encoding="utf-8")
    tampered = text.replace("AnimationReview", "AnimationReviewX", 1)
    tscn.write_text(tampered, encoding="utf-8", newline="\n")
    manifest_path = review / "animation_review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_digests"]["animation_review.tscn"] = sha256_file(tscn)
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is False
    )


def test_review_digest_mismatch_raises_currentness_error(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "digest"
    )
    controller = review / "animation_review_controller.gd"
    controller.write_bytes(controller.read_bytes() + b"\n")
    with pytest.raises(CandidateCurrentnessError, match="digest mismatch"):
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )


def test_review_rejects_extra_file(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "extra"
    )
    (review / "extra.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="file set mismatch"):
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )


def test_review_rejects_nested_directory(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "nested"
    )
    (review / "nested").mkdir()
    with pytest.raises(ValidationError, match="file set mismatch"):
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_eligible", 0),
        ("promotion_eligible", 0),
    ],
)
def test_review_rejects_manifest_json_type_aliases(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "types"
    )
    path = review / "animation_review_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is False
    )


def test_export_refuses_existing_review_destination(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    out = tmp_path / "review-busy"
    out.mkdir()
    (out / "occupied.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            shared_clip_preview,
            out,
        )


def test_export_leaves_v086_and_db_unchanged(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    v086_bytes = {p.name: p.read_bytes() for p in shared_clip_preview.iterdir()}
    arts_before = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    export_rigged_character_animation_review(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        shared_clip_preview,
        tmp_path / "review-unchanged",
    )
    for name, payload in v086_bytes.items():
        assert (shared_clip_preview / name).read_bytes() == payload
    arts_after = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    assert len(arts_before) == len(arts_after)


def test_postpublish_review_drift_leaves_stale_orphan(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review as module

    ctx = managed_review_evidence
    clip_preview = tmp_path / "clip-for-postpublish"
    export_rigged_character_animation_clip_preview(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        clip_preview,
    )
    out = tmp_path / "review-postpublish"
    clip_before = acceptance_clip_path.read_bytes()
    original_publish = module.atomic_publish_staged_container

    def publish_then_mutate_clip(stage, publish_parent, name):
        destination = original_publish(stage, publish_parent, name)
        mutated = json.loads(acceptance_clip_path.read_bytes())
        mutated["clip_id"] = "mutated_after_publish"
        acceptance_clip_path.write_bytes(json.dumps(mutated).encode("utf-8"))
        return destination

    monkeypatch.setattr(module, "atomic_publish_staged_container", publish_then_mutate_clip)
    try:
        with pytest.raises(CandidateCurrentnessError, match="stale after publish|drifted"):
            export_rigged_character_animation_review(
                ctx.handlers,
                ctx.workflow_id,
                shared_preview,
                acceptance_clip_path,
                clip_preview,
                out,
            )
        assert out.is_dir()
        assert (
            animation_review_current(
                ctx.handlers,
                ctx.workflow_id,
                shared_preview,
                acceptance_clip_path,
                clip_preview,
                out,
            )
            is False
        )
    finally:
        acceptance_clip_path.write_bytes(clip_before)


def test_prepublish_v086_mutation_fails_before_output(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review as module

    ctx = managed_review_evidence
    clip_preview = tmp_path / "clip-prepublish"
    export_rigged_character_animation_clip_preview(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        clip_preview,
    )
    out = tmp_path / "prepublish-review"
    original_write = module._write_review_tree

    def write_then_mutate_v086(*args, **kwargs):
        result = original_write(*args, **kwargs)
        glb = clip_preview / "character.glb"
        glb.write_bytes(glb.read_bytes() + b" ")
        return result

    monkeypatch.setattr(module, "_write_review_tree", write_then_mutate_v086)
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            clip_preview,
            out,
        )
    assert not out.exists()


def test_v086_coherent_rehash_not_current_for_review(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    clip_preview = tmp_path / "v086-mutated"
    shutil.copytree(shared_clip_preview, clip_preview)
    path = clip_preview / "animation_clip.json"
    doc = json.loads(path.read_bytes())
    doc["duration_seconds"] = 2.0
    path.write_text(json.dumps(doc), encoding="utf-8")
    manifest_path = clip_preview / "animation_clip_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["animation_clip.json"] = sha256_file(path)
    manifest["clip_sha256"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, shared_preview, acceptance_clip_path, clip_preview
        )
        is False
    )
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            clip_preview,
            tmp_path / "blocked-review",
        )


def test_upstream_read_rejects_growth_after_size_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as clip_module

    path = tmp_path / "candidate.json"
    path.write_bytes(b"x")
    real_fstat = clip_module.os.fstat

    def grow_after_stat(fd: int):
        info = real_fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"x" * (1024 * 1024 + 1))
        return info

    monkeypatch.setattr(clip_module.os, "fstat", grow_after_stat)
    with pytest.raises(ValidationError, match="byte limit"):
        clip_module._read_bounded_package_file(path)


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink review alias guard"
)
def test_review_inspect_rejects_directory_junction_alias(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    preview, clip_path, clip_preview, real = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "alias-real"
    )
    alias = tmp_path / "review-alias"
    inject_destination_directory_junction("", str(alias))
    target = tmp_path / "review-alias_junction_target"
    for name in real.iterdir():
        (target / name.name).write_bytes((real / name.name).read_bytes())
    with pytest.raises(ValidationError):
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, alias
        )


def test_export_refuses_nonempty_review_destination(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    out = tmp_path / "review-nonempty"
    inject_nonempty_destination_directory("", str(out))
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            shared_clip_preview,
            out,
        )


def test_export_review_fails_when_evidence_not_ready(
    acceptance_clip_path: Path, tmp_path: Path
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-not-ready")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    candidate_workflow_readiness(handlers, workflow_id)
    preview = tmp_path / "preview-placeholder"
    preview.mkdir()
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_review(
            handlers,
            workflow_id,
            preview,
            acceptance_clip_path,
            tmp_path / "v086",
            tmp_path / "review-blocked",
        )


def test_export_review_zero_provider_activity(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_evidence
    before = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    export_rigged_character_animation_review(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        shared_clip_preview,
        tmp_path / "review-provider",
    )
    assert ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, shared_preview) is True


def test_export_review_calls_animation_clip_preview_current_three_times(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review as review_module

    ctx = managed_review_evidence
    calls: list[str] = []
    original = review_module.animation_clip_preview_current

    def counting_current(h, w, p, c, a):
        calls.append("call")
        return original(h, w, p, c, a)

    monkeypatch.setattr(review_module, "animation_clip_preview_current", counting_current)
    export_rigged_character_animation_review(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        shared_clip_preview,
        tmp_path / "review-call-count",
    )
    assert len(calls) == 3


def test_animation_review_current_calls_clip_preview_once(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review as review_module

    ctx = managed_review_evidence
    calls: list[str] = []
    original = review_module.animation_clip_preview_current

    def counting_current(h, w, p, c, a):
        calls.append("call")
        return original(h, w, p, c, a)

    monkeypatch.setattr(review_module, "animation_clip_preview_current", counting_current)
    assert (
        animation_review_current(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            shared_clip_preview,
            shared_review,
        )
        is True
    )
    assert len(calls) == 1


def test_review_current_false_on_coherent_v086_snapshot_drift_in_review(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    shared_review: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: clip gate True must not bypass exact-10 snapshot inequality (TOCTOU)."""
    import gamefactory.workflows.v08_candidate_animation_review as review_module

    ctx = managed_review_evidence
    preview, clip_path, clip_preview, review = _copy_quad(
        shared_preview, shared_clip_preview, shared_review, tmp_path, "v086-snapshot-drift"
    )
    review_b = tmp_path / "v086-snapshot-drift-review-b"
    shutil.copytree(review, review_b)
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is True
    )
    original = review_module.animation_clip_preview_current
    gate_calls: list[str] = []

    def gate_then_swap_upstream_to_b(h, w, p, c, a):
        gate_calls.append("call")
        assert original(h, w, p, c, a) is True
        _coherent_drift_review_embedded_v086_to_b(review_b)
        for name in _CLIP_PACKAGE_FILES:
            (a / name).write_bytes((review_b / name).read_bytes())
        for name in _EXPECTED_REVIEW_FILES:
            (review / name).write_bytes((review_b / name).read_bytes())
        return True

    monkeypatch.setattr(
        review_module, "animation_clip_preview_current", gate_then_swap_upstream_to_b
    )
    assert (
        animation_review_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview, review
        )
        is False
    )
    assert len(gate_calls) == 1
    assert original(ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview) is False


def test_export_stage_coherent_corruption_leaves_no_output(
    managed_review_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review as module

    ctx = managed_review_evidence
    clip_preview = tmp_path / "clip-stage-corrupt"
    export_rigged_character_animation_clip_preview(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        acceptance_clip_path,
        clip_preview,
    )
    out = tmp_path / "review-stage-corrupt"
    original_assert = module._assert_staged_review_matches_expected

    def corrupt_stage(stage, v086_bytes, clip_manifest_doc, *, workflow_id: str):
        tscn = stage / "animation_review.tscn"
        text = tscn.read_text(encoding="utf-8")
        tscn.write_text(text.replace("AnimationReview", "AnimationReviewX", 1), encoding="utf-8")
        manifest_path = stage / "animation_review_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["file_digests"]["animation_review.tscn"] = sha256_file(tscn)
        manifest_path.write_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
        original_assert(stage, v086_bytes, clip_manifest_doc, workflow_id=workflow_id)

    monkeypatch.setattr(module, "_assert_staged_review_matches_expected", corrupt_stage)
    with pytest.raises(CandidateCurrentnessError, match="UI resource|digests|manifest"):
        export_rigged_character_animation_review(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            acceptance_clip_path,
            clip_preview,
            out,
        )
    assert not out.exists()
