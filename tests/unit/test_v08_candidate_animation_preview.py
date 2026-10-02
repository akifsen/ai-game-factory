"""V0.8-5 unit tests: readiness-gated candidate animation preview export."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    TaskRepository,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ExecutionStatus
from gamefactory.workflows.v08_candidate_animation_preview import (
    animation_preview_current,
    export_rigged_character_animation_preview,
    validate_animation_spec_document,
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
    return run_completed_managed_evidence(tmp_path / "anim-c2b-isolated")


@pytest.fixture(scope="module")
def strict_animation_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path_factory.mktemp("anim-strict-manifest"))


@pytest.fixture(scope="module")
def shared_preview(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    out = tmp_path_factory.mktemp("animation-shared-preview") / "preview"
    export_rigged_character_candidate_preview(
        strict_animation_evidence.handlers, strict_animation_evidence.workflow_id, out
    )
    return out


@pytest.fixture(scope="module")
def shared_animation(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("animation-shared-package") / "animation"
    export_rigged_character_animation_preview(
        strict_animation_evidence.handlers,
        strict_animation_evidence.workflow_id,
        shared_preview,
        out,
    )
    return out


def _copy_pair(preview: Path, animation: Path, tmp_path: Path, label: str) -> tuple[Path, Path]:
    out = tmp_path / f"{label}-copy"
    shutil.copytree(animation, out)
    return preview, out


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


def _export_pair(
    ctx: CompletedEvidenceContext, tmp_path: Path, label: str, *, preview: Path | None = None
) -> tuple[Path, Path]:
    preview = preview or tmp_path / f"{label}-preview"
    animation = tmp_path / f"{label}-animation"
    if not preview.exists():
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, animation)
    return preview, animation


def test_export_animation_bytes_currentness_and_upstream_unchanged(
    strict_animation_evidence: CompletedEvidenceContext, shared_preview: Path, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview, animation = _export_pair(ctx, tmp_path, "bytes", preview=shared_preview)
    before_invocations = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    glb_upstream = (preview / "character.glb").read_bytes()
    preview_manifest_before = (preview / "preview-manifest.json").read_bytes()
    result_manifest = json.loads(
        (animation / "animation_manifest.json").read_text(encoding="utf-8")
    )
    assert result_manifest.get("production_eligible") is False
    assert result_manifest.get("promotion_eligible") is False
    assert (animation / "character.glb").read_bytes() == glb_upstream
    assert (animation / "character.glb").read_bytes() == _processed_glb_path(ctx).read_bytes()
    assert (preview / "character.glb").read_bytes() == glb_upstream
    assert (preview / "preview-manifest.json").read_bytes() == preview_manifest_before
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is True
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, preview) is True
    assert (
        ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before_invocations
    )


def test_export_animation_deterministic_across_fresh_roots(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview = tmp_path / "shared-preview"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out_a = tmp_path / "anim-determ-a"
    out_b = tmp_path / "anim-determ-b"
    export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, out_a)
    export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, out_b)
    for name in (
        "animation_manifest.json",
        "animation_preview.tscn",
        "animation_spec.json",
        "character.glb",
    ):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_export_animation_fails_when_upstream_preview_missing(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    missing = tmp_path / "no-preview"
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_preview(
            ctx.handlers, ctx.workflow_id, missing, tmp_path / "anim-fail"
        )


def test_export_animation_fails_when_evidence_not_ready(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-not-ready")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    candidate_workflow_readiness(handlers, workflow_id)
    preview = tmp_path / "preview-placeholder"
    preview.mkdir()
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_preview(
            handlers, workflow_id, preview, tmp_path / "anim-blocked"
        )


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.RUNNING, ExecutionStatus.FAILED, ExecutionStatus.COMPLETED],
)
def test_export_animation_rejects_newer_evidence_attempt(
    completed_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    ctx = completed_evidence
    preview = tmp_path / "attempt-preview"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    inject_newer_evidence_attempt(ctx, status=status, attempt_number=50)
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_preview(
            ctx.handlers, ctx.workflow_id, preview, tmp_path / "anim-new-attempt"
        )


def test_animation_current_false_when_stale_attempt(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    preview, animation = _export_pair(ctx, tmp_path, "stale")
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is False


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
def test_animation_current_false_on_upstream_drift(
    completed_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    mutate,
) -> None:
    ctx = completed_evidence
    preview, animation = _export_pair(ctx, tmp_path, "drift")
    mutate(ctx)
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is False


def test_animation_current_false_when_manifest_rehashed_without_canonical_bytes(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_animation_evidence
    preview, animation = _copy_pair(shared_preview, shared_animation, tmp_path, "rehash")
    tscn = animation / "animation_preview.tscn"
    text = tscn.read_text(encoding="utf-8")
    tampered = text.replace("AnimationPlayer", "AnimationPlayerX", 1)
    assert tampered != text
    tscn.write_text(tampered, encoding="utf-8", newline="\n")
    manifest_path = animation / "animation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digests = dict(manifest["file_digests"])
    digests["animation_preview.tscn"] = sha256_file(tscn)
    manifest["file_digests"] = digests
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is False


def test_animation_rejects_tampered_file(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_animation_evidence
    preview, animation = _copy_pair(shared_preview, shared_animation, tmp_path, "tamper")
    spec = animation / "animation_spec.json"
    spec.write_bytes(spec.read_bytes() + b" ")
    with pytest.raises(CandidateCurrentnessError):
        animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_eligible", 0),
        ("promotion_eligible", 0),
        ("evidence_attempt_number", True),
        ("evidence_attempt_number", 1.0),
    ],
)
def test_animation_rejects_manifest_json_type_aliases(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ctx = strict_animation_evidence
    preview, animation = _copy_pair(shared_preview, shared_animation, tmp_path, "types")
    path = animation / "animation_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is False


def test_validate_animation_spec_rejects_wrong_and_nonexistent_bones() -> None:
    from gamefactory.workflows.v08_candidate_animation_preview import _build_animation_spec_document

    base = _build_animation_spec_document()
    wrong = dict(base)
    wrong["bone_name"] = "RightUpperArm"
    with pytest.raises(ValidationError, match="bone_name"):
        validate_animation_spec_document(wrong)
    missing = dict(base)
    missing["bone_name"] = "NotInContract"
    with pytest.raises(ValidationError, match="bone_name"):
        validate_animation_spec_document(missing)


def test_animation_current_false_when_upstream_preview_stale(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    preview, animation = _export_pair(ctx, tmp_path, "upstream-stale")
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, preview) is False
    assert animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation) is False


def test_export_refuses_existing_animation_destination(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview = tmp_path / "preview-busy-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "animation-busy"
    out.mkdir()
    (out / "occupied.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, out)


def test_export_refuses_symlink_animation_destination(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview = tmp_path / "preview-junction-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "animation-junction"
    inject_destination_directory_junction("", str(out))
    with pytest.raises((ValidationError, ArtifactError)):
        export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, out)


def test_export_refuses_nonempty_animation_destination(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview = tmp_path / "preview-nonempty-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    out = tmp_path / "animation-nonempty"
    inject_nonempty_destination_directory("", str(out))
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_animation_preview(ctx.handlers, ctx.workflow_id, preview, out)


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink animation alias guard"
)
def test_animation_inspect_rejects_directory_junction_alias(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_animation_evidence
    preview, real = _copy_pair(shared_preview, shared_animation, tmp_path, "alias-real")
    alias = tmp_path / "animation-alias"
    inject_destination_directory_junction("", str(alias))
    target = tmp_path / "animation-alias_junction_target"
    for name in real.iterdir():
        (target / name.name).write_bytes(name.read_bytes())
    with pytest.raises(ValidationError):
        animation_preview_current(ctx.handlers, ctx.workflow_id, preview, alias)


def test_export_animation_leaves_upstream_preview_and_db_unchanged(
    strict_animation_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = strict_animation_evidence
    preview = tmp_path / "preview-unchanged-src"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    preview_bytes = {path.name: path.read_bytes() for path in preview.iterdir()}
    glb_before = _processed_glb_path(ctx).read_bytes()
    arts_before = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    export_rigged_character_animation_preview(
        ctx.handlers, ctx.workflow_id, preview, tmp_path / "animation-unchanged"
    )
    assert _processed_glb_path(ctx).read_bytes() == glb_before
    for name, payload in preview_bytes.items():
        assert (preview / name).read_bytes() == payload
    arts_after = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    assert len(arts_before) == len(arts_after)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_s", True),
        ("duration_s", 1),
        ("min_affected_displacement", 0),
        ("max_affected_displacement", 1000),
        ("loop", 1),
        ("root_motion", 0),
        ("production_eligible", 0),
        (
            "keyframes",
            [
                {"time_s": 0.0, "euler_deg": [False, 0.0, 0.0]},
                {"time_s": 0.5, "euler_deg": [0.0, 20.0, 0.0]},
                {"time_s": 1.0, "euler_deg": [0.0, 0.0, 0.0]},
            ],
        ),
        ("duration_s", float("nan")),
    ],
)
def test_closed_animation_spec_rejects_typed_and_threshold_tamper(
    field: str, value: object
) -> None:
    from gamefactory.workflows.v08_candidate_animation_preview import _build_animation_spec_document

    doc = _build_animation_spec_document()
    doc[field] = value
    with pytest.raises(ValidationError):
        validate_animation_spec_document(doc)


@pytest.mark.parametrize("bone", ["Spine", "RightUpperArm", "NotInContract"])
def test_closed_animation_spec_rejects_false_track_targets(bone: str) -> None:
    from gamefactory.workflows.v08_candidate_animation_preview import _build_animation_spec_document

    doc = _build_animation_spec_document()
    doc["bone_name"] = bone
    with pytest.raises(ValidationError):
        validate_animation_spec_document(doc)


def test_upstream_read_rejects_growth_after_size_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.v08_candidate_animation_preview as module

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


def test_coherent_spec_rehash_cannot_weaken_animation_contract(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
    tmp_path: Path,
) -> None:
    ctx = strict_animation_evidence
    preview, animation = _copy_pair(shared_preview, shared_animation, tmp_path, "spec-rehash")
    path = animation / "animation_spec.json"
    doc = json.loads(path.read_bytes())
    doc["min_affected_displacement"] = 0.0
    path.write_text(json.dumps(doc), encoding="utf-8")
    manifest_path = animation / "animation_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["animation_spec.json"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValidationError):
        animation_preview_current(ctx.handlers, ctx.workflow_id, preview, animation)


def test_animation_current_false_on_coherently_rehashed_upstream_preview(
    strict_animation_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_animation: Path,
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
    ctx = strict_animation_evidence
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, upstream) is False
    assert (
        animation_preview_current(ctx.handlers, ctx.workflow_id, upstream, shared_animation)
        is False
    )
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_preview(
            ctx.handlers, ctx.workflow_id, upstream, tmp_path / "blocked-export"
        )
