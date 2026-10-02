"""V0.8-6 unit tests: readiness-gated candidate animation clip preview export."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.assets.internal_skin_decode import decode_internal_skinned_glb
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    TaskRepository,
)
from gamefactory.core.domain.animation_clip import (
    ANIMATION_CLIP_SCHEMA_VERSION,
    parse_animation_clip_document,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ExecutionStatus
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    acceptance_arm_wave_clip_raw_bytes,
    animation_clip_preview_current,
    build_acceptance_arm_wave_clip_document,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    select_artifact_for_execution,
)
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


@pytest.fixture
def completed_evidence(tmp_path: Path) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path / "clip-c2b-isolated")


@pytest.fixture(scope="module")
def strict_clip_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path_factory.mktemp("clip-strict-manifest"))


@pytest.fixture(scope="module")
def shared_preview(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    out = tmp_path_factory.mktemp("clip-shared-preview") / "preview"
    export_rigged_character_candidate_preview(
        strict_clip_evidence.handlers, strict_clip_evidence.workflow_id, out
    )
    return out


@pytest.fixture(scope="module")
def acceptance_clip_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clip-fixture") / "arm_wave_01.json"
    path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    return path


@pytest.fixture(scope="module")
def shared_clip_preview(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("clip-shared-package") / "clip-preview"
    export_rigged_character_animation_clip_preview(
        strict_clip_evidence.handlers,
        strict_clip_evidence.workflow_id,
        shared_preview,
        acceptance_clip_path,
        out,
    )
    return out


def _write_clip(tmp_path: Path, document: dict[str, object], *, name: str = "clip.json") -> Path:
    path = tmp_path / name
    path.write_bytes(json.dumps(document).encode("utf-8"))
    return path


def test_verified_weighted_bone_names_excludes_unweighted_joints(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_animation_clip_preview import (
        _verified_weighted_bone_names,
    )

    glb_path = tmp_path / "positive.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    decoded = decode_internal_skinned_glb(glb_path)
    names = _verified_weighted_bone_names(decoded)
    assert "LeftUpperArm" in names
    assert "Spine" in names
    assert "RightUpperArm" not in names
    assert len(names) < 12


def _minimal_duration_clip(duration_seconds: float) -> dict[str, object]:
    doc = build_acceptance_arm_wave_clip_document()
    doc["duration_seconds"] = duration_seconds
    identity = [0.0, 0.0, 0.0, 1.0]
    doc["tracks"] = [
        {
            "bone": "LeftUpperArm",
            "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
        },
        {
            "bone": "Spine",
            "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
        },
    ]
    if duration_seconds > 0.0:
        doc["tracks"][0]["keyframes"].append({"time": duration_seconds, "rotation_xyzw": identity})
    return doc


def _copy_triplet(
    preview: Path, clip_preview: Path, tmp_path: Path, label: str
) -> tuple[Path, Path, Path]:
    clip_copy = tmp_path / f"{label}-clip.json"
    clip_copy.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    out = tmp_path / f"{label}-copy"
    shutil.copytree(clip_preview, out)
    return preview, clip_copy, out


def _processed_glb_path(ctx: CompletedEvidenceContext) -> Path:
    identity_task = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.id.endswith("-IDENTITY")
    )
    identity_exec = ExecutionRepository(ctx.workspace.db).get_latest_attempt(identity_task.id)
    assert identity_exec is not None
    processed = select_artifact_for_execution(
        ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id),
        task_id=identity_task.id,
        artifact_type="candidate-processed-glb",
        execution=identity_exec,
    )
    return ctx.workspace.root / processed.relative_path


def _export_triplet(
    ctx: CompletedEvidenceContext,
    tmp_path: Path,
    label: str,
    *,
    preview: Path | None = None,
    clip_path: Path | None = None,
) -> tuple[Path, Path, Path]:
    preview = preview or tmp_path / f"{label}-preview"
    clip_path = clip_path or tmp_path / f"{label}-clip.json"
    clip_preview = tmp_path / f"{label}-clip-preview"
    if not clip_path.exists():
        clip_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    if not preview.exists():
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
    )
    return preview, clip_path, clip_preview


def test_export_clip_bytes_currentness_and_upstream_unchanged(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _export_triplet(
        ctx, tmp_path, "bytes", preview=shared_preview, clip_path=acceptance_clip_path
    )
    before_invocations = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    glb_upstream = (preview / "character.glb").read_bytes()
    clip_before = clip_path.read_bytes()
    preview_manifest_before = (preview / "preview-manifest.json").read_bytes()
    result_manifest = json.loads(
        (clip_preview / "animation_clip_manifest.json").read_text(encoding="utf-8")
    )
    assert result_manifest.get("production_eligible") is False
    assert result_manifest.get("promotion_eligible") is False
    assert result_manifest.get("clip_sha256") == sha256_file(clip_path)
    assert (clip_preview / "animation_clip.json").read_bytes() == clip_before
    assert (clip_preview / "character.glb").read_bytes() == glb_upstream
    assert (clip_preview / "character.glb").read_bytes() == _processed_glb_path(ctx).read_bytes()
    assert (preview / "preview-manifest.json").read_bytes() == preview_manifest_before
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is True
    )
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, preview) is True
    assert (
        ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before_invocations
    )


def test_export_clip_deterministic_across_fresh_roots(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "shared-preview"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    clip_path = tmp_path / "arm_wave.json"
    clip_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    out_a = tmp_path / "clip-determ-a"
    out_b = tmp_path / "clip-determ-b"
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, clip_path, out_a
    )
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, clip_path, out_b
    )
    for name in (
        "animation_clip_manifest.json",
        "animation_clip_preview.tscn",
        "animation_clip.json",
        "character.glb",
    ):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_export_clip_fails_when_upstream_preview_missing(
    strict_clip_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    missing = tmp_path / "no-preview"
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            missing,
            acceptance_clip_path,
            tmp_path / "clip-fail",
        )


def test_export_clip_fails_when_evidence_not_ready(
    acceptance_clip_path: Path, tmp_path: Path
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-not-ready")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    candidate_workflow_readiness(handlers, workflow_id)
    preview = tmp_path / "preview-placeholder"
    preview.mkdir()
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_clip_preview(
            handlers,
            workflow_id,
            preview,
            acceptance_clip_path,
            tmp_path / "clip-blocked",
        )


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.RUNNING, ExecutionStatus.FAILED, ExecutionStatus.COMPLETED],
)
def test_export_clip_rejects_newer_evidence_attempt(
    completed_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    ctx = completed_evidence
    preview = tmp_path / "attempt-preview"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    inject_newer_evidence_attempt(ctx, status=status, attempt_number=50)
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            preview,
            acceptance_clip_path,
            tmp_path / "clip-new-attempt",
        )


def test_clip_current_false_when_stale_attempt(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    preview, clip_path, clip_preview = _export_triplet(ctx, tmp_path, "stale")
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
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
def test_clip_current_false_on_upstream_drift(
    completed_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    mutate,
) -> None:
    ctx = completed_evidence
    preview, clip_path, clip_preview = _export_triplet(ctx, tmp_path, "drift")
    mutate(ctx)
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_clip_current_false_when_manifest_rehashed_without_canonical_bytes(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "rehash"
    )
    tscn = clip_preview / "animation_clip_preview.tscn"
    text = tscn.read_text(encoding="utf-8")
    tampered = text.replace("AnimationPlayer", "AnimationPlayerX", 1)
    assert tampered != text
    tscn.write_text(tampered, encoding="utf-8", newline="\n")
    manifest_path = clip_preview / "animation_clip_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digests = dict(manifest["file_digests"])
    digests["animation_clip_preview.tscn"] = sha256_file(tscn)
    manifest["file_digests"] = digests
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_clip_rejects_tampered_package_file(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "tamper"
    )
    clip_json = clip_preview / "animation_clip.json"
    clip_json.write_bytes(clip_json.read_bytes() + b" ")
    with pytest.raises(CandidateCurrentnessError):
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_eligible", 0),
        ("promotion_eligible", 0),
        ("evidence_attempt_number", True),
        ("evidence_attempt_number", 1.0),
    ],
)
def test_clip_rejects_manifest_json_type_aliases(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "types"
    )
    path = clip_preview / "animation_clip_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_export_clip_rejects_unweighted_root_and_missing_bone(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-bad-clip"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    base = build_acceptance_arm_wave_clip_document()
    unweighted = dict(base)
    unweighted["tracks"] = [
        {
            "bone": "RightUpperArm",
            "keyframes": [{"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
        }
    ]
    bad_path = _write_clip(tmp_path, unweighted, name="unweighted.json")
    with pytest.raises(ValidationError, match="no positive finite vertex weight"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, bad_path, tmp_path / "out-unweighted"
        )
    root_clip = dict(base)
    root_clip["tracks"] = [
        {
            "bone": "HumanoidRoot",
            "keyframes": [{"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
        }
    ]
    root_path = _write_clip(tmp_path, root_clip, name="root.json")
    with pytest.raises(ValidationError, match="forbidden|not in the internal skin contract"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, root_path, tmp_path / "out-root"
        )
    missing = dict(base)
    missing["tracks"] = [
        {
            "bone": "NotABone",
            "keyframes": [{"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
        }
    ]
    missing_path = _write_clip(tmp_path, missing, name="missing.json")
    with pytest.raises(ValidationError, match="not in the internal skin contract"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, missing_path, tmp_path / "out-missing"
        )


def test_clip_current_false_when_source_clip_changes_whitespace_or_semantic(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "clip-change"
    )
    clip_path.write_bytes(clip_path.read_bytes() + b"\n")
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )
    semantic = build_acceptance_arm_wave_clip_document()
    semantic["duration_seconds"] = 1.25
    clip_path.write_bytes(json.dumps(semantic).encode("utf-8"))
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )
    clip_path.unlink()
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_clip_current_false_when_upstream_preview_stale(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    preview, clip_path, clip_preview = _export_triplet(ctx, tmp_path, "upstream-stale")
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, preview) is False
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_export_refuses_existing_clip_preview_destination(
    strict_clip_evidence: CompletedEvidenceContext, acceptance_clip_path: Path, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-busy-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "clip-preview-busy"
    out.mkdir()
    (out / "occupied.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
        )


def test_export_clip_leaves_upstream_preview_and_db_unchanged(
    strict_clip_evidence: CompletedEvidenceContext, acceptance_clip_path: Path, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-unchanged-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    preview_bytes = {path.name: path.read_bytes() for path in preview.iterdir()}
    glb_before = _processed_glb_path(ctx).read_bytes()
    arts_before = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    export_rigged_character_animation_clip_preview(
        ctx.handlers,
        ctx.workflow_id,
        preview,
        acceptance_clip_path,
        tmp_path / "clip-preview-unchanged",
    )
    assert _processed_glb_path(ctx).read_bytes() == glb_before
    for name, payload in preview_bytes.items():
        assert (preview / name).read_bytes() == payload
    arts_after = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    assert len(arts_before) == len(arts_after)


def test_coherent_clip_rehash_cannot_weaken_authored_contract(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "clip-rehash"
    )
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
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_clip_current_false_on_coherently_rehashed_upstream_preview(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream-rehashed"
    shutil.copytree(shared_preview, upstream)
    collider = upstream / "collider.json"
    doc = json.loads(collider.read_bytes())
    doc["radius_m"] = 99.0
    collider.write_text(json.dumps(doc), encoding="utf-8")
    manifest_path = upstream / "preview-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["collider.json"] = sha256_file(collider)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ctx = strict_clip_evidence
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, upstream) is False
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, upstream, acceptance_clip_path, shared_clip_preview
        )
        is False
    )
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            upstream,
            acceptance_clip_path,
            tmp_path / "blocked-export",
        )


def test_export_clip_fails_when_source_clip_drifts_during_staging(
    strict_clip_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as module

    ctx = strict_clip_evidence
    preview = tmp_path / "preview-drift"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    original_bind = module._BoundAuthorizedClipSource.bind
    calls = 0

    def flaky_bind(clip_path: Path, preview_dir: Path, bindings):
        nonlocal calls
        calls += 1
        bound = original_bind(clip_path, preview_dir, bindings)
        if calls >= 2:
            mutated = json.loads(bound.raw_bytes.decode("utf-8"))
            mutated["clip_id"] = "mutated_clip"
            new_bytes = json.dumps(mutated).encode("utf-8")
            return module._BoundAuthorizedClipSource(
                raw_bytes=new_bytes,
                sha256=hashlib.sha256(new_bytes).hexdigest(),
                clip=parse_animation_clip_document(mutated),
                verified_glb_sha256=bound.verified_glb_sha256,
                verified_weighted_bone_names=bound.verified_weighted_bone_names,
            )
        return bound

    monkeypatch.setattr(module._BoundAuthorizedClipSource, "bind", flaky_bind)
    out = tmp_path / "clip-drift-out"
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
        )
    assert not out.exists()


def test_postpublish_clip_drift_never_current_true(
    strict_clip_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as module

    ctx = strict_clip_evidence
    preview = tmp_path / "preview-postpublish"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "clip-postpublish-out"
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
        with pytest.raises(CandidateCurrentnessError, match="drifted"):
            export_rigged_character_animation_clip_preview(
                ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
            )
        assert out.is_dir()
        assert (
            animation_clip_preview_current(
                ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
            )
            is False
        )
    finally:
        acceptance_clip_path.write_bytes(clip_before)


def test_clip_package_digest_mismatch_raises_currentness_error(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "digest"
    )
    player = clip_preview / "animation_clip_preview_player.gd"
    player.write_bytes(player.read_bytes() + b"\n")
    with pytest.raises(CandidateCurrentnessError, match="digest mismatch"):
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )


def test_export_rejects_godot_duration_below_minimum(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-short-duration"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    clip_path = _write_clip(tmp_path, _minimal_duration_clip(0.0001), name="short.json")
    source_before = clip_path.read_bytes()
    out = tmp_path / "short-out"
    with pytest.raises(ValidationError, match="Godot adapter minimum"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, clip_path, out
        )
    assert not out.exists()
    assert clip_path.read_bytes() == source_before


def test_export_accepts_godot_minimum_and_maximum_duration(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-duration-bounds"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    for duration in (0.001, 10.0):
        clip_path = _write_clip(
            tmp_path, _minimal_duration_clip(duration), name=f"d{duration}.json"
        )
        out = tmp_path / f"out-{duration}"
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, clip_path, out
        )
        assert animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, out
        )


def test_authorization_rejects_glb_bytes_race_after_initial_read(
    strict_clip_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as module

    ctx = strict_clip_evidence
    preview = tmp_path / "preview-glb-race"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    glb_path = preview / "character.glb"
    original_bytes = glb_path.read_bytes()
    real_read = module._read_bounded_package_file
    calls = 0

    def racing_read(path: Path) -> bytes:
        nonlocal calls
        payload = real_read(path)
        if path.name == "character.glb":
            calls += 1
            if calls >= 2:
                glb_path.write_bytes(original_bytes + b" ")
        return payload

    monkeypatch.setattr(module, "_read_bounded_package_file", racing_read)
    out = tmp_path / "glb-race-out"
    with pytest.raises(
        CandidateCurrentnessError, match="upstream preview became stale during staging"
    ):
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            preview,
            acceptance_clip_path,
            out,
        )
    assert calls >= 2
    assert not out.exists()


def test_prepublish_source_clip_mutation_fails_before_output(
    strict_clip_evidence: CompletedEvidenceContext,
    acceptance_clip_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as module

    ctx = strict_clip_evidence
    preview = tmp_path / "preview-prepublish"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "prepublish-out"
    original_write = module._write_clip_preview_tree

    def write_then_mutate_source(*args, **kwargs):
        result = original_write(*args, **kwargs)
        acceptance_clip_path.write_bytes(acceptance_clip_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(module, "_write_clip_preview_tree", write_then_mutate_source)
    with pytest.raises(CandidateCurrentnessError, match="drifted"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            preview,
            acceptance_clip_path,
            out,
        )
    assert not out.exists()


@pytest.mark.parametrize(
    ("field", "mutator"),
    [
        ("bone", lambda doc: doc["tracks"][0].update({"bone": "RightUpperArm"})),
        (
            "quaternion",
            lambda doc: doc["tracks"][0]["keyframes"][0].update(
                {"rotation_xyzw": [0.0, 0.2588190451, 0.0, 0.9659258263]}
            ),
        ),
        ("time", lambda doc: doc["tracks"][0]["keyframes"][0].update({"time": 0.01})),
        ("duration", lambda doc: doc.update({"duration_seconds": 2.0})),
        ("loop", lambda doc: doc.update({"loop": True})),
    ],
    ids=("bone", "quaternion", "time", "duration", "loop"),
)
def test_coherent_package_clip_mutations_fail_source_authority(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    acceptance_clip_path: Path,
    tmp_path: Path,
    field: str,
    mutator,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, f"auth-{field}"
    )
    path = clip_preview / "animation_clip.json"
    doc = json.loads(path.read_bytes())
    mutator(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")
    manifest_path = clip_preview / "animation_clip_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["animation_clip.json"] = sha256_file(path)
    manifest["clip_sha256"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_coherent_clip_whitespace_rehash_fails_source_authority(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, clip_preview = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "auth-sha"
    )
    path = clip_preview / "animation_clip.json"
    path.write_bytes(path.read_bytes() + b" ")
    manifest_path = clip_preview / "animation_clip_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["animation_clip.json"] = sha256_file(path)
    manifest["clip_sha256"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (
        animation_clip_preview_current(
            ctx.handlers, ctx.workflow_id, preview, clip_path, clip_preview
        )
        is False
    )


def test_structural_multi_track_non_endpoint_clip_supported(
    strict_clip_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-structural"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    doc = {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": "struct_multi",
        "duration_seconds": 2.0,
        "loop": False,
        "tracks": [
            {
                "bone": "LeftUpperArm",
                "keyframes": [
                    {"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]},
                    {"time": 1.0, "rotation_xyzw": [0.0, 0.2588190451, 0.0, 0.9659258263]},
                ],
            },
            {
                "bone": "Spine",
                "keyframes": [{"time": 0.5, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
            },
        ],
    }
    clip_path = _write_clip(tmp_path, doc, name="structural.json")
    out = tmp_path / "structural-out"
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, clip_path, out
    )
    assert animation_clip_preview_current(ctx.handlers, ctx.workflow_id, preview, clip_path, out)


def test_upstream_read_rejects_growth_after_size_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as module

    path = tmp_path / "candidate.json"
    path.write_bytes(b"x")
    real_fstat = module.os.fstat

    def grow_after_stat(fd: int):
        info = real_fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"x" * (1024 * 1024 + 1))
        return info

    monkeypatch.setattr(module.os, "fstat", grow_after_stat)
    with pytest.raises(ValidationError, match="byte limit"):
        module._read_bounded_package_file(path)


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink clip preview alias guard"
)
def test_clip_inspect_rejects_directory_junction_alias(
    strict_clip_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_clip_evidence
    preview, clip_path, real = _copy_triplet(
        shared_preview, shared_clip_preview, tmp_path, "alias-real"
    )
    alias = tmp_path / "clip-preview-alias"
    inject_destination_directory_junction("", str(alias))
    target = tmp_path / "clip-preview-alias_junction_target"
    for name in real.iterdir():
        (target / name.name).write_bytes(name.read_bytes())
    with pytest.raises(ValidationError):
        animation_clip_preview_current(ctx.handlers, ctx.workflow_id, preview, clip_path, alias)


def test_export_refuses_symlink_clip_preview_destination(
    strict_clip_evidence: CompletedEvidenceContext, acceptance_clip_path: Path, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-junction-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "clip-preview-junction"
    inject_destination_directory_junction("", str(out))
    with pytest.raises((ValidationError, ArtifactError)):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
        )


def test_export_refuses_nonempty_clip_preview_destination(
    strict_clip_evidence: CompletedEvidenceContext, acceptance_clip_path: Path, tmp_path: Path
) -> None:
    ctx = strict_clip_evidence
    preview = tmp_path / "preview-nonempty-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "clip-preview-nonempty"
    inject_nonempty_destination_directory("", str(out))
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_clip_preview(
            ctx.handlers, ctx.workflow_id, preview, acceptance_clip_path, out
        )
