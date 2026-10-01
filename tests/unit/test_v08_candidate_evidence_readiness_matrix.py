"""MATRIX-READINESS unit contract (post-publication evidence readiness)."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.v08_candidate_evidence import (
    assert_completion_marker_controls,
    assert_publication_result_bindings,
    assert_trusted_cold_result_semantics,
    canonical_trusted_cold_result_json,
)
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ExecutionStatus, TaskStatus, generate_id
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    assert_candidate_evidence_complete,
    candidate_evidence_readiness,
)
from gamefactory.workflows.v08_candidate_workflow import candidate_workflow_readiness
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    ARTIFACT_REGISTRATION_FIELDS,
    C2_MANIFEST,
    C2_MARKER,
    C2_RESULT,
    CompletedEvidenceContext,
    clone_sibling_workflow_row,
    coherent_rehash_result_trusted,
    fresh_trusted_cold_dict,
    inject_newer_evidence_attempt,
    mutate_artifact_registration_field,
    patch_during_only_cold,
    publication_result_binding_mismatch_cause,
    registration_field_drift_expected_cause,
    run_completed_managed_evidence,
)

pytestmark = pytest.mark.candidate_slow


@pytest.fixture(scope="module")
def completed_evidence_baseline(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    workspace_root = tmp_path_factory.mktemp("readiness-matrix-managed-baseline")
    return run_completed_managed_evidence(workspace_root)


@pytest.fixture(scope="module")
def intrinsic_trusted_cold_template(
    completed_evidence_baseline: CompletedEvidenceContext,
) -> dict[str, Any]:
    return copy.deepcopy(fresh_trusted_cold_dict(completed_evidence_baseline))


def test_completed_managed_unit_workflow_readiness_positive_false_eligibilities(
    tmp_path: Path,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    readiness = assert_candidate_evidence_complete(ctx.handlers, ctx.workflow_id)
    assert readiness.candidate_evidence_complete is True
    assert readiness.production_eligible is False
    assert readiness.promotion_eligible is False
    assert readiness.evidence_execution_id == ctx.evidence_execution_id


def test_c1_pre_evidence_positive_while_evidence_readiness_negative_before_completed(
    tmp_path: Path,
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace
    from tests.unit.test_v08_candidate_workflow import _approve, _run_to_test_only_gate

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    candidate_workflow_readiness(handlers, workflow_id)
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_revalidates_on_second_call_without_cached_trust(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    first = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    second = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert second.candidate_evidence_complete is True
    assert second.evidence_execution_id == first.evidence_execution_id


def test_readiness_second_call_rejects_upstream_drift_after_first_pass(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    rows = AssetRevisionRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
    assert rows
    revisions = AssetRevisionRepository(ctx.workspace.db)
    revisions.save(replace(rows[0], processed_glb_hash="d" * 64))
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"processed_glb_hash|snapshot|source_glb",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_readiness_rejects_evidence_task_running_before_inspection(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    TaskRepository(ctx.workspace.db).update_status(ctx.evidence_task_id, TaskStatus.RUNNING)
    with pytest.raises(CandidateCurrentnessError, match="not completed"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    "task_status",
    [TaskStatus.FAILED, TaskStatus.RUNNING],
    ids=["failed", "running"],
)
def test_readiness_rejects_task_flip_during_only_cold(
    tmp_path: Path, task_status: TaskStatus
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)

    def _flip() -> None:
        TaskRepository(ctx.workspace.db).update_status(ctx.evidence_task_id, task_status)

    with patch_during_only_cold(ctx.workspace.db, _flip) as flags:
        with pytest.raises(CandidateCurrentnessError, match="not completed under lock"):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["cold_ran"] is True
    assert flags["mutated"] is True


@pytest.mark.parametrize(
    "attempt_status",
    [
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.UNCERTAIN,
        ExecutionStatus.COMPLETED,
    ],
)
def test_readiness_rejects_newer_evidence_attempt_during_only_cold(
    tmp_path: Path, attempt_status: ExecutionStatus
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    injected: dict[str, str] = {}

    def _inject() -> None:
        injected["exec_id"] = inject_newer_evidence_attempt(ctx, status=attempt_status)

    with patch_during_only_cold(ctx.workspace.db, _inject) as flags:
        with pytest.raises(
            CandidateCurrentnessError,
            match="execution changed under lock|attempt changed under lock",
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True
    assert injected["exec_id"] != ctx.evidence_execution_id


@pytest.mark.parametrize(
    "attempt_status",
    [
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.UNCERTAIN,
        ExecutionStatus.COMPLETED,
    ],
    ids=["failed", "running", "uncertain", "completed"],
)
def test_readiness_rejects_newer_evidence_attempt_after_completion(
    tmp_path: Path,
    attempt_status: ExecutionStatus,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    baseline = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert baseline.candidate_evidence_complete is True
    injected_id = inject_newer_evidence_attempt(ctx, status=attempt_status)
    assert injected_id != ctx.evidence_execution_id
    if attempt_status in (
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.UNCERTAIN,
    ):
        expected = rf"has status {attempt_status.value}"
    else:
        expected = r"expected exactly one candidate-c2-export-marker bound to execution"
    with pytest.raises(CandidateCurrentnessError, match=expected):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    "execution_status",
    [ExecutionStatus.FAILED, ExecutionStatus.RUNNING],
    ids=["failed", "running"],
)
def test_readiness_rejects_evidence_execution_status_flip_after_completion(
    tmp_path: Path,
    execution_status: ExecutionStatus,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    baseline = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert baseline.candidate_evidence_complete is True
    exec_repo = ExecutionRepository(ctx.workspace.db)
    live = exec_repo.get(ctx.evidence_execution_id)
    assert live is not None
    exec_repo.save(replace(live, status=execution_status))
    with pytest.raises(
        CandidateCurrentnessError,
        match=rf"has status {execution_status.value}",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    "task_status",
    [TaskStatus.FAILED, TaskStatus.RUNNING],
    ids=["failed", "running"],
)
def test_readiness_rejects_evidence_task_status_flip_after_completion(
    tmp_path: Path,
    task_status: TaskStatus,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    baseline = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert baseline.candidate_evidence_complete is True
    TaskRepository(ctx.workspace.db).update_status(ctx.evidence_task_id, task_status)
    with pytest.raises(CandidateCurrentnessError, match="not completed"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


_ARTIFACT_DRIFT_FIELDS = tuple(
    field for field in ARTIFACT_REGISTRATION_FIELDS if field not in ("workflow_id", "task_id")
)


@pytest.mark.parametrize("field", _ARTIFACT_DRIFT_FIELDS)
@pytest.mark.parametrize(
    "artifact_type",
    [C2_MARKER, C2_MANIFEST, C2_RESULT],
    ids=["marker", "manifest", "result"],
)
def test_readiness_rejects_artifact_registration_field_drift_during_only_cold(
    tmp_path: Path,
    field: str,
    artifact_type: str,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    art = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == artifact_type
    )
    if field == "id":
        new_value = generate_id("ART")
    elif field == "artifact_type":
        new_value = "foreign-artifact-type"
    elif field == "producer":
        new_value = "foreign-producer"
    elif field == "relative_path":
        stray = ctx.workspace.root / "stray-readiness.glb"
        stray.write_bytes(b"x")
        new_value = stray.relative_to(ctx.workspace.root).as_posix()
    elif field == "content_hash":
        new_value = "b" * 64
    elif field == "file_size":
        new_value = str(art.file_size + 1)
    elif field == "validation_state":
        new_value = "INVALID"
    elif field == "created_at":
        new_value = "1970-01-01T00:00:00+00:00"
    else:
        raise AssertionError(f"unhandled field {field}")

    def _drift() -> None:
        mutate_artifact_registration_field(
            ctx, artifact_type=artifact_type, field=field, value=new_value
        )

    with patch_during_only_cold(ctx.workspace.db, _drift) as flags:
        expected = registration_field_drift_expected_cause(field, artifact_type)
        with pytest.raises(
            (CandidateCurrentnessError, ArtifactError),
            match=expected,
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True


@pytest.mark.parametrize(
    "artifact_type",
    [C2_MARKER, C2_MANIFEST, C2_RESULT],
    ids=["marker", "manifest", "result"],
)
def test_readiness_rejects_wrong_workflow_id_registration_during_only_cold(
    tmp_path: Path,
    artifact_type: str,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    foreign_workflow_id = clone_sibling_workflow_row(ctx)
    persisted: dict[str, str] = {}

    def _drift() -> None:
        persisted["workflow_id"] = mutate_artifact_registration_field(
            ctx,
            artifact_type=artifact_type,
            field="workflow_id",
            value=foreign_workflow_id,
        )

    with patch_during_only_cold(ctx.workspace.db, _drift) as flags:
        expected = registration_field_drift_expected_cause("workflow_id", artifact_type)
        with pytest.raises(CandidateCurrentnessError, match=expected):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True
    assert persisted["workflow_id"] == foreign_workflow_id
    assert persisted["workflow_id"] != ctx.workflow_id


@pytest.mark.parametrize(
    "artifact_type",
    [C2_MARKER, C2_MANIFEST, C2_RESULT],
    ids=["marker", "manifest", "result"],
)
def test_readiness_rejects_wrong_task_id_registration_during_only_cold(
    tmp_path: Path,
    artifact_type: str,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    prepare_task = next(
        t
        for t in TaskRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    persisted: dict[str, str] = {}

    def _drift() -> None:
        persisted["task_id"] = mutate_artifact_registration_field(
            ctx,
            artifact_type=artifact_type,
            field="task_id",
            value=prepare_task.id,
        )

    with patch_during_only_cold(ctx.workspace.db, _drift) as flags:
        expected = registration_field_drift_expected_cause("task_id", artifact_type)
        with pytest.raises(CandidateCurrentnessError, match=expected):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True
    assert persisted["task_id"] == prepare_task.id


def test_readiness_rejects_artifact_row_delete_during_only_cold(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    marker = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MARKER
    )

    def _delete() -> None:
        with ctx.workspace.db.transaction() as conn:
            conn.execute("DELETE FROM artifacts WHERE id = ?;", (marker.id,))

    with patch_during_only_cold(ctx.workspace.db, _delete) as flags:
        with pytest.raises(CandidateCurrentnessError):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True


def test_readiness_rejects_source_glb_drift_during_only_cold(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)

    def _tamper_source() -> None:
        glb = ctx.workspace.root / "source.glb"
        glb.write_bytes(glb.read_bytes() + b"READINESS_DRIFT")

    with patch_during_only_cold(ctx.workspace.db, _tamper_source) as flags:
        with pytest.raises(
            CandidateCurrentnessError,
            match=r"source_glb bytes|upstream candidate snapshot drifted",
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True


def test_readiness_rejects_on_disk_result_bytes_drift_during_only_cold(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    result_path = ctx.workspace.root / result.relative_path

    def _append_bytes() -> None:
        result_path.write_bytes(result_path.read_bytes() + b" ")

    with patch_during_only_cold(ctx.workspace.db, _append_bytes) as flags:
        with pytest.raises(
            CandidateCurrentnessError,
            match=r"registration hash drifted|file_size mismatch|payload drifted",
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True


def test_readiness_rejects_coherent_marker_doc_drift_during_only_cold(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    marker = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MARKER
    )
    marker_path = ctx.workspace.root / marker.relative_path

    def _drift_marker_doc() -> None:
        doc = json.loads(marker_path.read_text(encoding="utf-8"))
        doc["promotion_eligible"] = True
        marker_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        new_hash = hashlib.sha256(marker_path.read_bytes()).hexdigest()
        ArtifactRepository(ctx.workspace.db).save(
            replace(marker, content_hash=new_hash, file_size=marker_path.stat().st_size)
        )

    with patch_during_only_cold(ctx.workspace.db, _drift_marker_doc) as flags:
        with pytest.raises(
            CandidateCurrentnessError,
            match=(
                r"completion marker metadata drifted under lock|"
                r"promotion_eligible|"
                r"registered file_size mismatch"
            ),
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert flags["mutated"] is True


@pytest.mark.parametrize(
    ("binding_field", "wrong_value"),
    [
        ("workflow_id", "WF-FOREIGN"),
        ("evidence_task_id", "TASK-FOREIGN"),
        ("evidence_execution_id", "EXEC-FOREIGN"),
        ("evidence_attempt_number", 99),
        ("snapshot_fingerprint", "a" * 64),
        ("cold_bundle_payload_digest", "b" * 64),
    ],
)
def test_readiness_rejects_coherent_rehash_wrong_result_binding(
    tmp_path: Path,
    binding_field: str,
    wrong_value: object,
) -> None:
    ctx = run_completed_managed_evidence(tmp_path)
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_RESULT
    )
    marker = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MARKER
    )
    result_path = ctx.workspace.root / result.relative_path
    marker_path = ctx.workspace.root / marker.relative_path

    def _patch_doc(doc_field: str, value: object) -> None:
        nonlocal result, marker
        doc = json.loads(result_path.read_text(encoding="utf-8"))
        doc[doc_field] = value
        result_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
        result = replace(
            result,
            content_hash=result_hash,
            file_size=result_path.stat().st_size,
        )
        ArtifactRepository(ctx.workspace.db).save(result)
        marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_doc["result_sha256"] = result_hash
        marker_path.write_text(
            json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        marker = replace(
            marker,
            content_hash=hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            file_size=marker_path.stat().st_size,
        )
        ArtifactRepository(ctx.workspace.db).save(marker)

    _patch_doc(binding_field, wrong_value)
    reloaded_result = ArtifactRepository(ctx.workspace.db).get(result.id)
    reloaded_marker = ArtifactRepository(ctx.workspace.db).get(marker.id)
    assert reloaded_result is not None and reloaded_marker is not None
    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker_doc["result_sha256"] == reloaded_result.content_hash
    assert reloaded_result.file_size == result_path.stat().st_size
    assert reloaded_marker.file_size == marker_path.stat().st_size

    expected = publication_result_binding_mismatch_cause(binding_field)
    with pytest.raises(CandidateCurrentnessError, match=expected):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_readiness_rejects_coherent_rehash_wrong_verified_files(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)

    def _patch(trusted: dict[str, Any], fresh: dict[str, Any]) -> None:
        trusted["verified_files"] = int(fresh.get("verified_files", 0)) + 1

    coherent_rehash_result_trusted(ctx, patch_trusted=_patch)
    with pytest.raises(CandidateCurrentnessError, match="does not match fresh cold"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_readiness_rejects_coherent_rehash_contradictory_outcome(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)

    def _patch(trusted: dict[str, Any], _fresh: dict[str, Any]) -> None:
        trusted["outcome"] = "FAILED"
        trusted["execution_provenance"] = "AUTHENTICATED_PRODUCTION"

    coherent_rehash_result_trusted(ctx, patch_trusted=_patch)
    with pytest.raises(CandidateCurrentnessError, match="cold result outcome"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_readiness_rejects_coherent_rehash_reviewed_pins_non_bool(tmp_path: Path) -> None:
    ctx = run_completed_managed_evidence(tmp_path)

    def _patch(trusted: dict[str, Any], _fresh: dict[str, Any]) -> None:
        trusted["reviewed_pins_match"] = 1

    coherent_rehash_result_trusted(ctx, patch_trusted=_patch)
    with pytest.raises(CandidateCurrentnessError, match="reviewed_pins_match"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda t: t.update({"outcome": "FAILED"}), "cold result outcome"),
        (
            lambda t: t.update({"execution_provenance": "AUTHENTICATED_PRODUCTION"}),
            "execution_provenance",
        ),
        (lambda t: t.update({"production_eligible": True}), "eligibility flags"),
        (lambda t: t.update({"candidate_evidence_complete": False}), "candidate_evidence_complete"),
        (lambda t: t.update({"validation_status": "FAIL"}), "validation_status"),
    ],
)
def test_intrinsic_trusted_cold_semantics_rejects(
    intrinsic_trusted_cold_template: dict[str, Any],
    mutator: Any,
    match: str,
) -> None:
    trusted = copy.deepcopy(intrinsic_trusted_cold_template)
    mutator(trusted)
    with pytest.raises(ValidationError, match=match):
        assert_trusted_cold_result_semantics(trusted)


def test_intrinsic_publication_result_bindings_reject_wrong_task_id(
    completed_evidence_baseline: CompletedEvidenceContext,
) -> None:
    ctx = completed_evidence_baseline
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    doc = json.loads((ctx.workspace.root / result.relative_path).read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="evidence_task_id"):
        assert_publication_result_bindings(
            doc,
            workflow_id=ctx.workflow_id,
            task_id="TASK-FOREIGN",
            execution_id=ctx.evidence_execution_id,
            attempt_number=int(doc["evidence_attempt_number"]),
            snapshot_fingerprint=str(doc["snapshot_fingerprint"]),
            cold_bundle_payload_digest=str(doc["cold_bundle_payload_digest"]),
        )


def test_intrinsic_completion_marker_controls_reject_promotion_true(
    completed_evidence_baseline: CompletedEvidenceContext,
) -> None:
    ctx = completed_evidence_baseline
    marker = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MARKER
    )
    doc = json.loads((ctx.workspace.root / marker.relative_path).read_text(encoding="utf-8"))
    doc["promotion_eligible"] = True
    with pytest.raises(ValidationError, match="promotion_eligible"):
        assert_completion_marker_controls(doc, workflow_id=ctx.workflow_id)


def test_intrinsic_publication_result_bindings_reject_non_object_trusted_cold(
    completed_evidence_baseline: CompletedEvidenceContext,
) -> None:
    ctx = completed_evidence_baseline
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    doc = json.loads((ctx.workspace.root / result.relative_path).read_text(encoding="utf-8"))
    doc["trusted_cold_result"] = "not-an-object"
    with pytest.raises(ValidationError, match="trusted_cold_result"):
        assert_publication_result_bindings(
            doc,
            workflow_id=ctx.workflow_id,
            task_id=str(doc["evidence_task_id"]),
            execution_id=str(doc["evidence_execution_id"]),
            attempt_number=int(doc["evidence_attempt_number"]),
            snapshot_fingerprint=str(doc["snapshot_fingerprint"]),
            cold_bundle_payload_digest=str(doc["cold_bundle_payload_digest"]),
        )


def test_intrinsic_trusted_cold_semantics_rejects_reviewed_pins_non_bool(
    intrinsic_trusted_cold_template: dict[str, Any],
) -> None:
    trusted = copy.deepcopy(intrinsic_trusted_cold_template)
    trusted["reviewed_pins_match"] = 1
    with pytest.raises(ValidationError, match="reviewed_pins_match"):
        assert_trusted_cold_result_semantics(trusted)


def test_intrinsic_canonical_trusted_cold_requires_exact_bool_types(
    intrinsic_trusted_cold_template: dict[str, Any],
) -> None:
    trusted = intrinsic_trusted_cold_template
    canonical = canonical_trusted_cold_result_json(trusted)
    mutated = copy.deepcopy(trusted)
    mutated["reviewed_pins_match"] = 1
    assert canonical_trusted_cold_result_json(mutated) != canonical


def test_readiness_stored_vs_fresh_canonical_equality_on_valid_completed_workflow(
    tmp_path: Path,
) -> None:
    from gamefactory.adapters.assets.v08_candidate_evidence import (
        trusted_cold_verify_candidate_bundle,
    )
    from tests.unit.v08_candidate_c2b_readiness_fixtures import cold_bundle_dir_for_workflow

    ctx = run_completed_managed_evidence(tmp_path)
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    doc = json.loads((ctx.workspace.root / result.relative_path).read_text(encoding="utf-8"))
    stored = doc["trusted_cold_result"]
    fresh = trusted_cold_verify_candidate_bundle(cold_bundle_dir_for_workflow(ctx))
    assert canonical_trusted_cold_result_json(stored) == canonical_trusted_cold_result_json(fresh)
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert readiness.candidate_evidence_complete is True
