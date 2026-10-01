"""V0.8-4 unit tests: readiness-gated candidate Godot preview export."""

from __future__ import annotations

import hashlib
import json
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
    return run_completed_managed_evidence(tmp_path / "preview-c2b-isolated")


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


def test_export_preview_bytes_and_currentness(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-a"
    before_invocations = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    result = export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    assert result.production_eligible is False
    assert result.promotion_eligible is False
    assert result.evidence_execution_id
    assert result.evidence_attempt_number >= 1
    assert result.manifest_artifact_id
    assert result.result_artifact_id
    assert result.marker_artifact_id
    assert result.scene_sha256 == sha256_file(out / "character.tscn")
    assert (
        result.processed_glb_sha256
        == hashlib.sha256((out / "character.glb").read_bytes()).hexdigest()
    )
    assert (out / "character.glb").read_bytes() == _processed_glb_path(ctx).read_bytes()
    candidate_doc = json.loads((out / "candidate.json").read_text(encoding="utf-8"))
    assert candidate_doc.get("production_eligible") is False
    assert candidate_doc.get("promotion_eligible") is False
    from gamefactory.core.domain.v08_candidate_contracts import (
        candidate_spec_fingerprint,
        parse_asset_specification_v08_candidate,
    )

    assert candidate_doc["schema_version"] == "candidate-preview-specification-0.8.0"
    retained = parse_asset_specification_v08_candidate(candidate_doc["specification"])
    manifest = json.loads((out / "preview-manifest.json").read_bytes())
    assert candidate_spec_fingerprint(retained) == manifest["spec_fingerprint"]
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is True
    assert (
        ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before_invocations
    )


@pytest.fixture(scope="module")
def strict_manifest_evidence(tmp_path_factory: pytest.TempPathFactory) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path_factory.mktemp("preview-strict-manifest"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_eligible", 0),
        ("promotion_eligible", 0),
        ("evidence_attempt_number", True),
        ("evidence_attempt_number", 1.0),
    ],
)
def test_preview_rejects_manifest_json_type_aliases(
    strict_manifest_evidence: CompletedEvidenceContext, tmp_path: Path, field: str, value: object
) -> None:
    ctx = strict_manifest_evidence
    out = tmp_path / "preview-types"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    path = out / "preview-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is False


@pytest.mark.parametrize("invalid", ["duplicate", "oversized"])
def test_preview_manifest_read_is_strict_and_bounded(
    strict_manifest_evidence: CompletedEvidenceContext, tmp_path: Path, invalid: str
) -> None:
    ctx = strict_manifest_evidence
    out = tmp_path / "preview-invalid-manifest"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    path = out / "preview-manifest.json"
    if invalid == "duplicate":
        raw = path.read_bytes()
        path.write_bytes(b'{"production_eligible":true,' + raw[1:])
    else:
        path.write_bytes(b" " * 1_048_577)
    with pytest.raises(ValidationError, match="duplicate|bounded|byte limit"):
        candidate_preview_current(ctx.handlers, ctx.workflow_id, out)


def test_preview_stage_uses_missing_nested_output_parent(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_preview import _fresh_preview_stage

    project = tmp_path / "source"
    project.mkdir()
    output_parent = tmp_path / "new" / "nested"
    stage, _parent = _fresh_preview_stage(output_parent, project)
    assert stage.is_dir()
    assert stage.is_relative_to(output_parent.resolve())


def test_export_preview_deterministic_across_fresh_roots(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out_a = tmp_path / "preview-determ-a"
    out_b = tmp_path / "preview-determ-b"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out_a)
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out_b)
    for name in ("preview-manifest.json", "character.tscn", "collider.json", "candidate.json"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()
    assert (out_a / "character.glb").read_bytes() == (out_b / "character.glb").read_bytes()


def test_export_fails_when_evidence_not_ready(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-not-ready")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    candidate_workflow_readiness(handlers, workflow_id)
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_candidate_preview(handlers, workflow_id, tmp_path / "preview-fail")


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.RUNNING, ExecutionStatus.FAILED, ExecutionStatus.COMPLETED],
)
def test_export_rejects_newer_evidence_attempt(
    completed_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    status: ExecutionStatus,
) -> None:
    ctx = completed_evidence
    inject_newer_evidence_attempt(ctx, status=status, attempt_number=50)
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_candidate_preview(
            ctx.handlers, ctx.workflow_id, tmp_path / "preview-blocked"
        )


def test_preview_current_false_when_stale_attempt(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-stale"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is False


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
def test_preview_current_false_on_upstream_drift(
    completed_evidence: CompletedEvidenceContext,
    tmp_path: Path,
    mutate,
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-drift"
    before_invocations = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    mutate(ctx)
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is False
    assert (
        ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before_invocations
    )


def test_preview_current_false_on_spec_drift(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-drift"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    prepare_task = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.id.endswith("-PREPARE")
    )
    prepare_exec = ExecutionRepository(ctx.workspace.db).get_latest_attempt(prepare_task.id)
    assert prepare_exec is not None
    spec_art = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == "candidate-specification"
    )
    spec_path = ctx.workspace.root / spec_art.relative_path
    doc = json.loads(spec_path.read_text(encoding="utf-8"))
    doc["intent"] = doc["intent"] + " drift"
    spec_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is False


def test_preview_current_false_when_manifest_rehashed_without_canonical_bytes(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-rehash"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    tscn = out / "character.tscn"
    text = tscn.read_text(encoding="utf-8")
    tampered = text.replace("radius = ", "radius = 99.0\n# ", 1)
    assert tampered != text
    tscn.write_text(tampered, encoding="utf-8", newline="\n")
    manifest_path = out / "preview-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digests = dict(manifest["file_digests"])
    digests["character.tscn"] = sha256_file(tscn)
    manifest["file_digests"] = digests
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, out) is False


def test_preview_rejects_tampered_file(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-tamper"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    collider = out / "collider.json"
    collider.write_bytes(collider.read_bytes() + b" ")
    with pytest.raises(CandidateCurrentnessError):
        candidate_preview_current(ctx.handlers, ctx.workflow_id, out)


def test_preview_rejects_wrong_workflow_id(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-wf"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    assert candidate_preview_current(ctx.handlers, "WF-NOT-THIS-WORKFLOW", out) is False


def test_export_refuses_existing_destination(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-busy"
    out.mkdir()
    (out / "occupied.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)


def test_export_refuses_symlink_destination(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-junction"
    inject_destination_directory_junction("", str(out))
    with pytest.raises((ValidationError, ArtifactError)):
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)


def test_export_refuses_nonempty_destination_via_lexists(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-nonempty"
    inject_nonempty_destination_directory("", str(out))
    with pytest.raises(ArtifactError, match="already exists"):
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)


def test_export_rejects_staging_parent_junction_without_destination_write(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    out = tmp_path / "preview-safe"
    gf = tmp_path / ".gf"
    inject_destination_directory_junction("", str(gf))
    outside_probe = tmp_path / "outside-write-probe.txt"
    outside_probe.write_text("untouched", encoding="utf-8")
    before = outside_probe.read_text(encoding="utf-8")
    with pytest.raises((ValidationError, ArtifactError)):
        export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, out)
    assert not out.exists()
    assert outside_probe.read_text(encoding="utf-8") == before


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink preview alias guard"
)
def test_preview_inspect_rejects_directory_junction_alias(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    real = tmp_path / "preview-real"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, real)
    alias = tmp_path / "preview-alias"
    inject_destination_directory_junction("", str(alias))
    target = tmp_path / "preview-alias_junction_target"
    for name in real.iterdir():
        dest = target / name.name
        dest.write_bytes(name.read_bytes())
    with pytest.raises(ValidationError):
        candidate_preview_current(ctx.handlers, ctx.workflow_id, alias)


def test_export_leaves_source_and_db_unchanged(
    completed_evidence: CompletedEvidenceContext, tmp_path: Path
) -> None:
    ctx = completed_evidence
    glb_before = _processed_glb_path(ctx).read_bytes()
    arts_before = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    export_rigged_character_candidate_preview(
        ctx.handlers, ctx.workflow_id, tmp_path / "preview-unchanged"
    )
    assert _processed_glb_path(ctx).read_bytes() == glb_before
    arts_after = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    assert len(arts_before) == len(arts_after)
