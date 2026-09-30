"""Managed-DB workflow bridge tests; approval actors here are explicit fixtures."""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from test_assembly_ingest import (
    _generate_valid_assembly_glb,
    _profile,
    _read_glb,
    _spec,
    _write_glb,
)

from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source
from gamefactory.adapters.dcc.assembly_processor import AssemblyProcessor
from gamefactory.adapters.dcc.godot_assembly import AssemblyRuntimeResult
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    AssetRevisionRepository,
    ProjectRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import compute_sha256
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    CostClass,
    Execution,
    ExecutionStatus,
    Project,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from gamefactory.workflows.assembly_production import (
    _ASSEMBLY_ROUTERS,
    AssemblyAdapters,
    _json_hash,
    _runtime_artifact_payloads,
    create_local_assembly_workflow,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerRegistry


def _concept_png(color: tuple[int, int, int] = (30, 60, 90)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


class StopBeforeBlender:
    def __init__(self) -> None:
        self.calls = 0

    def process_assembly(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise ValidationError("test adapter stops before DCC")


class RacingBlender:
    def __init__(self, engine: WorkflowEngine, process_task_id: str) -> None:
        self.engine = engine
        self.process_task_id = process_task_id

    def process_assembly(self, *_args: Any, **_kwargs: Any) -> Any:
        # Model an old worker returning after a newer attempt became authoritative.
        self.engine.exec_repo.save(
            Execution(
                "EXEC-newer-racing-attempt",
                self.process_task_id,
                2,
                status=ExecutionStatus.RUNNING,
            )
        )
        return object()


@pytest.fixture
def assembly_case(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".gamefactory" / "locks").mkdir(parents=True)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project_id = "test-assembly-project"
    ProjectRepository(db).save(Project(project_id, "Assembly fixture", "godot", str(root)))
    profile = _profile()
    spec = _spec(profile)
    original = _generate_valid_assembly_glb(tmp_path / "source.glb")
    package = ingest_assembly_source(
        source_glb_path=original,
        managed_root=root,
        relative_package_dir="sources/assembly-one",
        spec=spec,
        authoring_tool_name="Fixture Blender",
        authoring_tool_version="5.2.1",
        source_front="-Z",
        actor="test-fixture-author",
        reason="A local test source with no semantic mistakes",
    )
    concept = root / "concepts" / "assembly.png"
    concept.parent.mkdir()
    concept.write_bytes(_concept_png())
    concept_provenance = root / "concepts" / "assembly-provenance.json"
    concept_provenance.write_text(
        json.dumps({"schema_version": 1, "origin": "isolated-test-fixture"}), encoding="utf-8"
    )
    engine = WorkflowEngine(root, db, handler_registry=TaskHandlerRegistry())
    blender = StopBeforeBlender()
    created = create_local_assembly_workflow(
        engine,
        project_id,
        spec,
        profile,
        package.package_dir,
        expected_provenance_sha256=package.retained_provenance_sha256,
        concept_image=concept,
        concept_provenance=concept_provenance,
        workflow_id="fixture-assembly-wf",
        adapters=AssemblyAdapters(blender=blender),
    )
    return {
        "root": root,
        "db": db,
        "engine": engine,
        "project_id": project_id,
        "profile": profile,
        "spec": spec,
        "package": package,
        "concept": concept,
        "concept_provenance": concept_provenance,
        "workflow": created.workflow,
        "tasks": created.tasks,
        "revision": created.revision,
        "blender": blender,
    }


def _approve_pending(case: dict[str, Any], approval_id: str, *, comment: str) -> None:
    engine: WorkflowEngine = case["engine"]
    approval = engine.app_repo.get(approval_id)
    assert approval is not None
    task = engine.task_repo.get(approval.task_id)
    workflow = engine.wf_repo.get(approval.workflow_id)
    assert task is not None and workflow is not None
    inputs = engine.approval_inputs(workflow, task, CostClass.LOCAL)
    decided = ApprovalService.approve(
        approval,
        actor="test-fixture-human-operator",
        comment=comment,
        current_inputs=inputs,
    )
    engine.app_repo.save(decided)


def _table_count(db: Database, table: str) -> int:
    allowed = {
        "provider_invocations",
        "provider_operation_intents",
        "cost_ledger",
        "paid_request_snapshots",
        "production_readiness_reports",
    }
    assert table in allowed
    with db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS amount FROM {table}").fetchone()
    assert row is not None
    return int(row["amount"])


def test_local_graph_pauses_at_human_gates_and_never_enters_paid_accounting(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id
    prepare = next(t for t in case["tasks"] if t.task_type.endswith("prepare"))
    prepare_rows = engine.art_repo.list_by_task(prepare.id)
    assert {row.artifact_type for row in prepare_rows} >= {
        "asset-specification-v07",
        "assembly-source-glb",
        "assembly-source-provenance",
        "assembly-source-publication-marker",
        "asset-concept",
        "asset-concept-provenance",
    }
    assert all(row.validation_state in {"VALID", "VERIFIED"} for row in prepare_rows)
    assert [task.task_type for task in case["tasks"]] == [
        "asset_v07_assembly_prepare",
        "asset_v07_assembly_concept_review",
        "asset_v07_assembly_source_review",
        "asset_v07_assembly_process",
        "asset_v07_assembly_validate",
        "asset_v07_assembly_godot",
        "asset_v07_assembly_final_review",
        "asset_v07_assembly_evidence",
    ]
    assert all(task.cost_class == CostClass.LOCAL for task in case["tasks"])

    concept_review = next(t for t in case["tasks"] if t.task_type.endswith("concept_review"))
    assert engine.task_repo.get(concept_review.id).status.value == "BLOCKED"
    _approve_pending(case, result.pending_approval_id, comment="Concept approved in fixture")

    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id
    source_review = next(t for t in case["tasks"] if t.task_type.endswith("source_review"))
    assert engine.task_repo.get(source_review.id).status.value == "BLOCKED"
    _approve_pending(
        case, result.pending_approval_id, comment="Authored semantics approved in fixture"
    )

    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.FAILED
    assert case["blender"].calls == 1, result.error_message
    assert "test adapter stops before DCC" in (result.error_message or "")
    assert case["revision"].raw_glb_hash == case["package"].retained_glb_sha256
    for table in (
        "provider_invocations",
        "provider_operation_intents",
        "cost_ledger",
        "paid_request_snapshots",
        "production_readiness_reports",
    ):
        assert _table_count(case["db"], table) == 0


def test_approval_context_before_prepare_is_read_only_and_fails_closed(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    workflow = case["workflow"]
    revisions_before = [
        row.to_dict() for row in AssetRevisionRepository(case["db"]).list_by_workflow(workflow.id)
    ]
    assert engine.art_repo.list_by_workflow(workflow.id) == []

    for suffix in ("concept_review", "source_review"):
        task = next(row for row in case["tasks"] if row.task_type.endswith(suffix))
        metadata = engine.handler_registry.metadata(task.task_type)
        assert metadata is not None and metadata.approval_context is not None
        with pytest.raises(ArtifactError, match="missing before the prepare stage"):
            metadata.approval_context(workflow, task)

    assert engine.art_repo.list_by_workflow(workflow.id) == []
    assert [
        row.to_dict() for row in AssetRevisionRepository(case["db"]).list_by_workflow(workflow.id)
    ] == revisions_before


def test_atomic_graph_registration_rolls_back_workflow_revision_and_audit(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    db = case["db"]
    workflow_id = "fixture-atomic-graph-failure"
    with db.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_assembly_graph_task BEFORE INSERT ON tasks "
            f"WHEN NEW.workflow_id = '{workflow_id}' "
            "BEGIN SELECT RAISE(ABORT, 'injected task insert failure'); END"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected task insert failure"):
        create_local_assembly_workflow(
            case["engine"],
            case["project_id"],
            case["spec"],
            case["profile"],
            case["package"].package_dir,
            expected_provenance_sha256=case["package"].retained_provenance_sha256,
            concept_image=case["concept"],
            concept_provenance=case["concept_provenance"],
            workflow_id=workflow_id,
        )

    assert case["engine"].wf_repo.get(workflow_id) is None
    assert AssetRevisionRepository(db).list_by_workflow(workflow_id) == []
    assert case["engine"].task_repo.list_by_workflow(workflow_id) == []
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id = ?", (workflow_id,)
            ).fetchone()[0]
            == 0
        )
        conn.execute("DROP TRIGGER fail_assembly_graph_task")
        conn.commit()

    created = create_local_assembly_workflow(
        case["engine"],
        case["project_id"],
        case["spec"],
        case["profile"],
        case["package"].package_dir,
        expected_provenance_sha256=case["package"].retained_provenance_sha256,
        concept_image=case["concept"],
        concept_provenance=case["concept_provenance"],
        workflow_id=workflow_id,
    )
    assert created.revision.revision_number == 2
    assert len(created.tasks) == 8
    assert case["engine"].wf_repo.get(workflow_id) is not None
    assert len(case["engine"].task_repo.list_by_workflow(workflow_id)) == 8
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_type='Workflow' "
                "AND entity_id = ? AND action='REGISTERED'",
                (workflow_id,),
            ).fetchone()[0]
            == 1
        )


def test_creation_time_concept_provenance_pin_cannot_be_silently_replaced(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    case["concept_provenance"].write_text(
        json.dumps({"schema_version": 1, "origin": "changed-after-create"}), encoding="utf-8"
    )
    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.FAILED
    assert "creation-time pin" in (result.error_message or "")
    assert engine.art_repo.list_by_workflow(case["workflow"].id) == []
    assert engine.app_repo.list_by_workflow(case["workflow"].id) == []
    assert [
        row.to_dict()
        for row in AssetRevisionRepository(case["db"]).list_by_workflow(case["workflow"].id)
    ] == [case["revision"].to_dict()]
    for table in (
        "provider_invocations",
        "provider_operation_intents",
        "cost_ledger",
        "paid_request_snapshots",
        "production_readiness_reports",
    ):
        assert _table_count(case["db"], table) == 0


def test_creation_time_publication_marker_pin_cannot_be_silently_replaced(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    marker = case["package"].package_dir / "publication_marker.json"
    marker_data = json.loads(marker.read_text(encoding="utf-8"))
    marker_data["created_at"] = "2001-01-01T00:00:00Z"
    marker.write_text(json.dumps(marker_data, sort_keys=True), encoding="utf-8")
    result = case["engine"].run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.FAILED
    assert "creation-time pin" in (result.error_message or "")
    assert case["engine"].art_repo.list_by_workflow(case["workflow"].id) == []
    assert case["engine"].app_repo.list_by_workflow(case["workflow"].id) == []
    assert [
        row.to_dict()
        for row in AssetRevisionRepository(case["db"]).list_by_workflow(case["workflow"].id)
    ] == [case["revision"].to_dict()]


def test_human_gates_resume_after_engine_restart_without_new_revision(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    first = case["engine"]
    result = first.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.BLOCKED and result.pending_approval_id

    def restart() -> WorkflowEngine:
        engine = WorkflowEngine(case["root"], case["db"], handler_registry=TaskHandlerRegistry())
        created = create_local_assembly_workflow(
            engine,
            case["project_id"],
            case["spec"],
            case["profile"],
            case["package"].package_dir,
            expected_provenance_sha256=case["package"].retained_provenance_sha256,
            concept_image=case["concept"],
            concept_provenance=case["concept_provenance"],
            workflow_id=case["workflow"].id,
            adapters=AssemblyAdapters(blender=case["blender"]),
        )
        case["engine"] = engine
        case["workflow"] = created.workflow
        case["tasks"] = created.tasks
        return engine

    engine = restart()
    _approve_pending(case, result.pending_approval_id, comment="Restarted concept fixture approval")
    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.BLOCKED and result.pending_approval_id

    engine = restart()
    _approve_pending(case, result.pending_approval_id, comment="Restarted source fixture approval")
    result = engine.run_workflow(case["workflow"].id)
    assert result.status == WorkflowStatus.FAILED
    assert case["blender"].calls == 1
    revisions = AssetRevisionRepository(case["db"]).list_by_workflow(case["workflow"].id)
    assert len(revisions) == 1 and revisions[0].revision_number == 1


def test_exact_input_retry_reuses_revision_and_conflicting_retry_fails_closed(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    same = create_local_assembly_workflow(
        engine,
        case["project_id"],
        case["spec"],
        case["profile"],
        case["package"].package_dir,
        expected_provenance_sha256=case["package"].retained_provenance_sha256,
        concept_image=case["concept"],
        concept_provenance=case["concept_provenance"],
        workflow_id=case["workflow"].id,
        adapters=AssemblyAdapters(blender=case["blender"]),
    )
    assert same.revision.revision_number == 1
    assert len(same.tasks) == len(case["tasks"])

    case["concept"].write_bytes(_concept_png((90, 60, 30)))
    with pytest.raises(ValidationError, match="conflicting pinned inputs"):
        create_local_assembly_workflow(
            engine,
            case["project_id"],
            case["spec"],
            case["profile"],
            case["package"].package_dir,
            expected_provenance_sha256=case["package"].retained_provenance_sha256,
            concept_image=case["concept"],
            concept_provenance=case["concept_provenance"],
            workflow_id=case["workflow"].id,
        )

    orphan_id = "fixture-assembly-orphan"
    orphan = Workflow(orphan_id, case["project_id"], "Orphan test")
    engine.wf_repo.save(orphan)
    case["concept"].write_bytes(_concept_png())
    AssetRevisionRepository(engine.db).allocate_revision(
        case["spec"].asset_id,
        orphan_id,
        case["revision"].spec_hash,
        concept_hash=compute_sha256(case["concept"]),
        raw_glb_hash=case["package"].retained_glb_sha256,
        profile_id=case["profile"].profile_id,
        profile_version=case["profile"].version,
    )
    with pytest.raises(ValidationError, match="orphaned or incomplete"):
        create_local_assembly_workflow(
            engine,
            case["project_id"],
            case["spec"],
            case["profile"],
            case["package"].package_dir,
            expected_provenance_sha256=case["package"].retained_provenance_sha256,
            concept_image=case["concept"],
            concept_provenance=case["concept_provenance"],
            workflow_id=orphan_id,
        )


def test_existing_json_artifact_must_match_the_requested_payload(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    workflow = case["workflow"]
    task = next(t for t in case["tasks"] if t.task_type.endswith("prepare"))
    handlers = _ASSEMBLY_ROUTERS[case["engine"].handler_registry].by_workflow[workflow.id]
    relative = ".gamefactory/assets/test_assembly_tank/r001/reports/exact-retry.json"
    first = handlers._create_json_artifact(
        workflow, task, "assembly-evidence-boundary", relative, {"attempt": 1}
    )
    assert first.content_hash == compute_sha256(case["root"] / relative)
    with pytest.raises(ArtifactError, match="conflicts with a prior artifact"):
        handlers._create_json_artifact(
            workflow, task, "assembly-evidence-boundary", relative, {"attempt": 2}
        )


def test_godot_output_contract_includes_each_capture_artifact_and_exact_bytes() -> None:
    capture = b"actual verifier PNG output bytes"
    result = AssemblyRuntimeResult(
        status="PASS",
        observation={},
        artifacts={
            "runtime-request.json": b"{}",
            "runtime-observation.json": b"{}",
            "asset_runtime_harness_v07.gd": b"extends SceneTree",
            "front.png": capture,
        },
        findings=[],
        request_digest="a" * 64,
        processed_glb_sha256="b" * 64,
        captures={"front": capture},
    )
    assert _runtime_artifact_payloads(result, ("front",))["front.png"] == capture

    mismatch = AssemblyRuntimeResult(
        status=result.status,
        observation=result.observation,
        artifacts={**result.artifacts, "front.png": b"different bytes"},
        findings=result.findings,
        request_digest=result.request_digest,
        processed_glb_sha256=result.processed_glb_sha256,
        captures=result.captures,
    )
    with pytest.raises(ArtifactError, match="disagree for review capture"):
        _runtime_artifact_payloads(mismatch, ("front",))


def test_runtime_metadata_retention_uses_distinct_files_from_adapter_outputs(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    workflow = case["workflow"]
    task = next(t for t in case["tasks"] if t.task_type.endswith("godot"))
    handlers = _ASSEMBLY_ROUTERS[case["engine"].handler_registry].by_workflow[workflow.id]
    attempt_dir = (
        case["root"]
        / ".gamefactory"
        / "assets"
        / "test_assembly_tank"
        / "r001"
        / "runtime"
        / "fixture-a1"
    )
    attempt_dir.mkdir(parents=True)
    adapter_outputs = {
        "runtime-request.json": b'{"source":"adapter"}',
        "runtime-observation.json": b'{"source":"adapter"}',
        "asset_runtime_harness_v07.gd": b"extends SceneTree # adapter output",
    }
    for name, payload in adapter_outputs.items():
        (attempt_dir / name).write_bytes(payload)

    retained = {}
    for name, payload in adapter_outputs.items():
        role = {
            "runtime-request.json": "assembly-runtime-request",
            "runtime-observation.json": "assembly-runtime-observation",
            "asset_runtime_harness_v07.gd": "assembly-runtime-harness",
        }[name]
        row = handlers._bytes_artifact(
            workflow,
            task,
            role,
            f".gamefactory/assets/test_assembly_tank/r001/runtime/fixture-a1/retained/{name}",
            payload,
        )
        retained[name] = case["root"] / row.relative_path
        assert retained[name].read_bytes() == payload
        assert (attempt_dir / name).read_bytes() == payload
    assert all(path.parent != attempt_dir for path in retained.values())


@pytest.mark.parametrize(
    ("concept_bytes", "provenance_bytes", "message"),
    [
        (_concept_png()[:20] + b"not-a-complete-png", None, "fully decoded as a PNG"),
        (_concept_png(), b"{" + b" " * (1024 * 1024), "Concept provenance exceeds"),
        (_concept_png(), b"[1, 2, 3]", "JSON object"),
        (_concept_png(), b'{"x": NaN}', "valid JSON"),
    ],
    ids=("truncated-png", "oversized-provenance", "non-object-provenance", "nonfinite-provenance"),
)
def test_invalid_concept_or_provenance_is_rejected_before_database_allocation(
    assembly_case: dict[str, Any],
    concept_bytes: bytes,
    provenance_bytes: bytes | None,
    message: str,
) -> None:
    case = assembly_case
    case["concept"].write_bytes(concept_bytes)
    if provenance_bytes is not None:
        case["concept_provenance"].write_bytes(provenance_bytes)
    workflow_id = "invalid-concept-input-workflow"
    with pytest.raises(ValidationError, match=message):
        create_local_assembly_workflow(
            case["engine"],
            case["project_id"],
            case["spec"],
            case["profile"],
            case["package"].package_dir,
            expected_provenance_sha256=case["package"].retained_provenance_sha256,
            concept_image=case["concept"],
            concept_provenance=case["concept_provenance"],
            workflow_id=workflow_id,
        )
    assert case["engine"].wf_repo.get(workflow_id) is None
    assert AssetRevisionRepository(case["db"]).list_by_workflow(workflow_id) == []


def test_source_sidecar_change_after_human_gate_blocks_dispatch(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    result = engine.run_workflow(case["workflow"].id)
    assert result.pending_approval_id
    _approve_pending(case, result.pending_approval_id, comment="Fixture concept approval")
    result = engine.run_workflow(case["workflow"].id)
    assert result.pending_approval_id
    # A pending human request is not authority for later-edited retained bytes.
    sidecar = case["package"].retained_provenance_path
    sidecar.write_bytes(sidecar.read_bytes() + b" ")
    blocked = engine.run_workflow(case["workflow"].id)
    assert blocked.status == WorkflowStatus.BLOCKED
    assert case["blender"].calls == 0
    assert _table_count(case["db"], "cost_ledger") == 0


def test_profile_document_mutation_after_creation_invalidates_the_pinned_workflow(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    result = case["engine"].run_workflow(case["workflow"].id)
    assert result.pending_approval_id
    _approve_pending(case, result.pending_approval_id, comment="Fixture concept approval")
    processing = case["profile"].document.processing
    processing.max_materials += 1
    with pytest.raises(ValidationError, match="profile document does not match"):
        case["engine"].run_workflow(case["workflow"].id)
    assert case["blender"].calls == 0


def test_changed_source_starts_a_new_revision_and_workflow_router_keeps_bindings(
    assembly_case: dict[str, Any], tmp_path: Path
) -> None:
    case = assembly_case
    source2 = _generate_valid_assembly_glb(tmp_path / "source-two.glb")
    document, binary = _read_glb(source2.read_bytes())
    document["asset"]["generator"] = "fixture changed source identity"
    _write_glb(source2, document, binary)
    package2 = ingest_assembly_source(
        source_glb_path=source2,
        managed_root=case["root"],
        relative_package_dir="sources/assembly-two",
        spec=case["spec"],
        authoring_tool_name="Fixture Blender",
        authoring_tool_version="5.2.1",
        source_front="-Z",
        actor="test-fixture-author",
        reason="A distinct authored revision for workflow routing test",
    )
    second = create_local_assembly_workflow(
        case["engine"],
        case["project_id"],
        case["spec"],
        case["profile"],
        package2.package_dir,
        expected_provenance_sha256=package2.retained_provenance_sha256,
        concept_image=case["concept"],
        concept_provenance=case["concept_provenance"],
        workflow_id="fixture-assembly-wf-second",
        adapters=AssemblyAdapters(blender=case["blender"]),
    )
    assert second.revision.revision_number == 2
    assert second.revision.raw_glb_hash == package2.retained_glb_sha256
    assert case["revision"].revision_number == 1
    assert case["revision"].raw_glb_hash == case["package"].retained_glb_sha256

    first_result = case["engine"].run_workflow(case["workflow"].id)
    second_result = case["engine"].run_workflow(second.workflow.id)
    assert first_result.pending_approval_id
    assert second_result.pending_approval_id
    first_artifacts = case["engine"].art_repo.list_by_workflow(case["workflow"].id)
    second_artifacts = case["engine"].art_repo.list_by_workflow(second.workflow.id)
    assert any(a.content_hash == case["package"].retained_glb_sha256 for a in first_artifacts)
    assert any(a.content_hash == package2.retained_glb_sha256 for a in second_artifacts)


def test_stale_process_worker_cannot_publish_after_newer_attempt_claim(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    engine: WorkflowEngine = case["engine"]
    process_task = next(t for t in case["tasks"] if t.task_type.endswith("_process"))
    router = _ASSEMBLY_ROUTERS[engine.handler_registry]
    handlers = router.by_workflow[case["workflow"].id]
    handlers.adapters = AssemblyAdapters(blender=RacingBlender(engine, process_task.id))

    result = engine.run_workflow(case["workflow"].id)
    assert result.pending_approval_id
    _approve_pending(case, result.pending_approval_id, comment="Fixture concept approval")
    result = engine.run_workflow(case["workflow"].id)
    assert result.pending_approval_id
    _approve_pending(case, result.pending_approval_id, comment="Fixture source approval")
    with pytest.raises(ValueError, match="no longer owns the latest running task attempt"):
        engine.run_workflow(case["workflow"].id)
    latest = engine.exec_repo.get_latest_attempt(process_task.id)
    assert latest is not None
    assert latest.id == "EXEC-newer-racing-attempt"
    assert latest.status == ExecutionStatus.RUNNING
    assert engine.art_repo.list_by_task(process_task.id) == []
    assert case["revision"].processed_glb_hash is None


def _seed_successful_runtime_context(case: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    engine: WorkflowEngine = case["engine"]
    workflow = engine.wf_repo.get(case["workflow"].id)
    assert workflow is not None
    task_by_type = {task.task_type: task for task in engine.task_repo.list_by_workflow(workflow.id)}
    process = task_by_type["asset_v07_assembly_process"]
    validate = task_by_type["asset_v07_assembly_validate"]
    godot = task_by_type["asset_v07_assembly_godot"]
    executions = {
        "process": Execution("EXEC-process-1", process.id, 1, status=ExecutionStatus.COMPLETED),
        "validate": Execution("EXEC-validate-1", validate.id, 1, status=ExecutionStatus.COMPLETED),
        "godot": Execution("EXEC-godot-1", godot.id, 1, status=ExecutionStatus.COMPLETED),
    }
    for execution in executions.values():
        engine.exec_repo.save(execution)

    root = case["root"]
    revision = case["revision"]
    revision_number = revision.revision_number
    processed_bytes = case["package"].retained_glb_path.read_bytes()
    processed_sha256 = case["package"].retained_glb_sha256
    process_dir = (
        root
        / ".gamefactory"
        / "assets"
        / case["spec"].asset_id
        / f"r{revision_number:03d}"
        / "process"
        / "EXEC-process-1-a1"
    )
    process_dir.mkdir(parents=True)
    (process_dir / "processed.glb").write_bytes(processed_bytes)
    process_report = {
        "status": "SUCCESS",
        "source_glb_sha256": case["package"].retained_glb_sha256,
        "provenance_sha256": case["package"].retained_provenance_sha256,
        "output_glb_sha256": processed_sha256,
        "processing_script_sha256": compute_sha256(AssemblyProcessor.get_script_path()),
        "spec_fingerprint": revision.spec_hash,
    }
    (process_dir / "blender-report.json").write_text(json.dumps(process_report), encoding="utf-8")
    processed = engine.artifact_mgr.register_file_artifact(
        workflow.id,
        process.id,
        "processed-assembly-glb",
        "fixture",
        (process_dir / "processed.glb").relative_to(root).as_posix(),
    )
    process_row = engine.artifact_mgr.register_file_artifact(
        workflow.id,
        process.id,
        "assembly-blender-report",
        "fixture",
        (process_dir / "blender-report.json").relative_to(root).as_posix(),
    )
    engine.art_repo.save(processed)
    engine.art_repo.save(process_row)

    profile_hash = _json_hash(case["profile"].document.model_dump(mode="json"))
    validation_report = {
        "passed": True,
        "source_glb_sha256": case["package"].retained_glb_sha256,
        "processed_glb_sha256": processed_sha256,
        "profile_document_sha256": profile_hash,
        "preservation_passed": True,
    }
    validation_path = (
        root
        / ".gamefactory"
        / "assets"
        / case["spec"].asset_id
        / f"r{revision_number:03d}"
        / "validation"
        / "EXEC-validate-1-a1.json"
    )
    validation_path.parent.mkdir(parents=True)
    validation_path.write_text(json.dumps(validation_report), encoding="utf-8")
    validation_row = engine.artifact_mgr.register_file_artifact(
        workflow.id,
        validate.id,
        "assembly-validation-report",
        "fixture",
        validation_path.relative_to(root).as_posix(),
    )
    engine.art_repo.save(validation_row)
    revision.processed_glb_hash = processed_sha256
    revision.validation_report_hash = validation_row.content_hash
    AssetRevisionRepository(engine.db).save(revision)

    captures: dict[str, str] = {}
    review_dir = (
        root
        / ".gamefactory"
        / "assets"
        / case["spec"].asset_id
        / f"r{revision_number:03d}"
        / "review"
        / "EXEC-godot-1-a1"
    )
    review_dir.mkdir(parents=True)
    for view in case["profile"].review_views:
        payload = b"fixture capture for " + view.encode()
        path = review_dir / f"{view}.png"
        path.write_bytes(payload)
        capture = engine.artifact_mgr.register_file_artifact(
            workflow.id,
            godot.id,
            "assembly-review-capture",
            "fixture",
            path.relative_to(root).as_posix(),
        )
        engine.art_repo.save(capture)
        captures[view] = capture.content_hash
    runtime_dir = (
        root
        / ".gamefactory"
        / "assets"
        / case["spec"].asset_id
        / f"r{revision_number:03d}"
        / "runtime"
        / "EXEC-godot-1-a1"
    )
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime-index.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "execution_id": executions["godot"].id,
                "attempt_number": 1,
                "processed_glb_sha256": processed_sha256,
                "capture_sha256": captures,
            }
        ),
        encoding="utf-8",
    )
    runtime_row = engine.artifact_mgr.register_file_artifact(
        workflow.id,
        godot.id,
        "assembly-runtime-index",
        "fixture",
        (runtime_dir / "runtime-index.json").relative_to(root).as_posix(),
    )
    engine.art_repo.save(runtime_row)
    return workflow, {"tasks": task_by_type, "executions": executions}


@pytest.mark.parametrize("stage", ("process", "validate", "godot"))
def test_final_review_rejects_a_newer_failed_attempt(
    assembly_case: dict[str, Any], stage: str
) -> None:
    case = assembly_case
    workflow, seeded = _seed_successful_runtime_context(case)
    router = _ASSEMBLY_ROUTERS[case["engine"].handler_registry]
    handlers = router.by_workflow[workflow.id]
    final_task = seeded["tasks"]["asset_v07_assembly_final_review"]
    context = handlers._final_context(workflow, final_task)
    assert context["runtime_execution_id"] == "EXEC-godot-1"
    assert context["runtime_attempt_number"] == 1

    task = seeded["tasks"][
        {
            "process": "asset_v07_assembly_process",
            "validate": "asset_v07_assembly_validate",
            "godot": "asset_v07_assembly_godot",
        }[stage]
    ]
    case["engine"].exec_repo.save(
        Execution(f"EXEC-{stage}-failed", task.id, 2, status=ExecutionStatus.FAILED)
    )
    with pytest.raises(ArtifactError, match="latest successful"):
        handlers._final_context(workflow, final_task)


def test_cold_export_rejects_fixture_approvals_without_workflow_receipts(
    assembly_case: dict[str, Any],
) -> None:
    case = assembly_case
    initial = case["engine"].run_workflow(case["workflow"].id)
    assert initial.status == WorkflowStatus.BLOCKED and initial.pending_approval_id
    workflow, _ = _seed_successful_runtime_context(case)
    engine: WorkflowEngine = case["engine"]
    task_rows = engine.task_repo.list_by_workflow(workflow.id)
    human_types = {
        "asset_v07_assembly_concept_review": "concept_review",
        "asset_v07_assembly_source_review": "source_review",
        "asset_v07_assembly_final_review": "final_visual_review",
    }
    for task_type, approval_type in human_types.items():
        task = next(row for row in task_rows if row.task_type == task_type)
        inputs = engine.approval_inputs(workflow, task, CostClass.LOCAL)
        request = ApprovalService.create_request(
            workflow.id,
            task.id,
            approval_type,
            "Explicit isolated workflow test approval",
            CostClass.LOCAL,
            inputs,
        )
        approved = ApprovalService.approve(
            request,
            "test-fixture-human-operator",
            "Approved for this isolated test only",
            current_inputs=inputs,
        )
        engine.app_repo.save(approved)

    evidence_task = next(row for row in task_rows if row.task_type.endswith("evidence"))
    for row in task_rows:
        if row.id != evidence_task.id:
            engine.task_repo.update_status(row.id, TaskStatus.COMPLETED)

    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.FAILED
    assert result.error_message
    persisted = engine.wf_repo.get(workflow.id)
    assert persisted is not None and persisted.status == WorkflowStatus.FAILED
    evidence_rows = [
        row
        for row in engine.art_repo.list_by_task(evidence_task.id)
        if row.artifact_type in {"assembly-evidence-boundary", "assembly-evidence-manifest"}
    ]
    assert evidence_rows == []


def test_workflow_has_an_explicit_cold_export_boundary(
    assembly_case: dict[str, Any],
) -> None:
    # The last registered stage invokes the cold export and must pass before completion.
    case = assembly_case
    task = next(t for t in case["tasks"] if t.task_type.endswith("evidence"))
    assert task.parameters["graph_version"] == "0.7.0-local-assembly"
    assert not any(
        name in task.parameters
        for name in ("provider", "cost", "reservation", "paid_request_snapshot_sha256")
    )
    assert case["revision"].processed_glb_hash is None
    assert case["workflow"].status == WorkflowStatus.PENDING
