"""Static_prop external reuse workflow (SQLite-backed, no provider)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    CostLedgerRepository,
    ProjectRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import Project, WorkflowStatus
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.assembly_production import LocalAssemblyNoProvider
from gamefactory.workflows.asset_evidence import export_asset_evidence_bundle
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    register_asset_production_handlers,
)
from gamefactory.workflows.asset_reuse import (
    create_static_prop_reuse_workflow,
    resolve_reuse_input_path,
    verify_retained_provenance_artifact,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import RegisteredTaskHandler, TaskHandlerResult

PROJECT = Path(__file__).resolve().parents[2]
SPEC = PROJECT / "src/gamefactory/resources/specs/prop_energy_crate_01.yml"
PICKUP_SPEC = PROJECT / "src/gamefactory/resources/specs/pickup_energy_cell_01.yml"


def _project(tmp_path: Path) -> tuple[Path, Database]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="T"\n', encoding="utf-8"
    )
    factory_dir = root / ".gamefactory"
    factory_dir.mkdir(parents=True, exist_ok=True)
    (factory_dir / "factory.yml").write_text(
        "schema_version: '0.1.0'\nproject:\n  id: reuse\n  name: Reuse test\n  version: '0.1.0'\n"
        "engine:\n  type: godot\n",
        encoding="utf-8",
    )
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="reuse", name="Reuse test", engine_type="godot", root_path=str(root))
    )
    return root, db


def _provenance(source_hash: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "existing-external-source-provenance-1.0",
        "source_mode": "existing_external",
        "source_sha256": source_hash,
        "original_provider": "meshy",
        "generated_by_this_workflow": False,
        "paid_by_this_workflow": False,
    }
    payload.update(extra)
    return payload


def _inputs(root: Path, *, external: bool = False) -> tuple[object, Path, Path, bytes]:
    spec = parse_asset_specification(SPEC)
    glb_bytes = create_box_glb(
        width_m=spec.dimensions.width_m,
        depth_m=spec.dimensions.depth_m,
        height_m=spec.dimensions.height_m,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
    )
    if external:
        source = tmp_path_external_dir(root) / "manual_chest.glb"
        source.parent.mkdir(parents=True, exist_ok=True)
    else:
        source = root / "inputs" / "source.glb"
        source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(glb_bytes)
    provenance = source.with_suffix(".provenance.json")
    provenance.write_text(
        json.dumps(_provenance(hashlib.sha256(glb_bytes).hexdigest())), encoding="utf-8"
    )
    return spec, source, provenance, glb_bytes


def tmp_path_external_dir(root: Path) -> Path:
    return root.parent / "outside"


def _install_stubs(handlers: AssetProductionHandlers) -> None:
    def process(workflow, task, execution):
        raw = next(
            a
            for a in handlers.artifacts.list_by_workflow(workflow.id)
            if a.artifact_type == "asset-raw-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(raw)
        path = handlers._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        path.write_bytes((handlers.root / raw.relative_path).read_bytes())
        report = handlers._path(task, f"processing-attempt-{execution.attempt_number}.json")
        report.write_text(json.dumps({"status": "SUCCESS", "exit_code": 0}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "stub process",
            [
                handlers._register(workflow, task, execution, "asset-processed-glb", path),
                handlers._register(workflow, task, execution, "asset-processing-report", report),
            ],
        )

    def validate(workflow, task, execution):
        path = handlers._path(task, f"validation-attempt-{execution.attempt_number}.json")
        path.write_text(json.dumps({"status": "PASS", "passed": True}), encoding="utf-8")
        return TaskHandlerResult(
            1,
            "stub validate",
            [handlers._register(workflow, task, execution, "asset-validation-report", path)],
        )

    def godot(workflow, task, execution):
        path = handlers._path(task, f"runtime-{execution.id}.json")
        path.write_text(
            json.dumps({"status": "PASS", "execution_id": execution.id}), encoding="utf-8"
        )
        ids = [handlers._register(workflow, task, execution, "asset-runtime-observation", path)]
        for angle in ("front", "three_quarter", "side"):
            capture = handlers._path(task, f"{execution.id}-{angle}.png")
            Image.new("RGB", (2, 2), (20, 70, 140)).save(capture)
            ids.append(
                handlers._register(workflow, task, execution, "asset-runtime-capture", capture)
            )
        return TaskHandlerResult(1, "stub godot", ids)

    handlers.process = process
    handlers.validate = validate
    handlers.godot = godot


def _engine(root: Path, db: Database) -> WorkflowEngine:
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(require_approval_for_paid=True, require_approval_for_process_execution=False)
        ),
        asset_provider=LocalAssemblyNoProvider(),  # type: ignore[arg-type]
    )
    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        AssetRevisionRepository(db),
        engine.app_repo,
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        LocalAssemblyNoProvider(),  # type: ignore[arg-type]
    )
    _install_stubs(handlers)
    register_asset_production_handlers(engine.handler_registry, handlers)
    godot_meta = engine.handler_registry._handlers["asset_godot"].metadata
    engine.handler_registry._handlers["asset_godot"] = RegisteredTaskHandler(
        lambda *args, **kwargs: handlers.godot(*args, **kwargs), godot_meta
    )
    return engine


def _create(
    root: Path, db: Database, spec: object, source: Path, provenance: Path
) -> tuple[WorkflowEngine, str]:
    workflow, tasks = create_static_prop_reuse_workflow(
        "reuse", root, spec, source, provenance, revision_repository=AssetRevisionRepository(db)
    )
    engine = _engine(root, db)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    return engine, workflow.id


def test_reuse_graph_has_no_paid_or_concept_stage(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    workflow, tasks = create_static_prop_reuse_workflow(
        "reuse", root, spec, source, provenance, revision_repository=AssetRevisionRepository(db)
    )
    assert [t.task_type for t in tasks] == [
        "asset_reuse_prepare",
        "asset_process",
        "asset_validate",
        "asset_godot",
        "asset_final_review",
        "record_evidence",
    ]
    assert all("cost" not in t.parameters for t in tasks)
    revision = AssetRevisionRepository(db).get(spec.asset_id, 1)
    assert revision is not None and revision.concept_hash is None
    assert revision.raw_glb_hash == hashlib.sha256(source.read_bytes()).hexdigest()


def test_prepare_process_without_provider_or_ledger(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, original = _inputs(root, external=True)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id
    approval = ApprovalRepository(db).get(result.pending_approval_id)
    assert approval is not None and approval.approval_type == "final_visual_review"
    assert source.read_bytes() == original
    assert CostLedgerRepository(db).list_by_workflow(workflow_id) == []
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    types = {a.artifact_type for a in ArtifactRepository(db).list_by_workflow(workflow_id)}
    assert "asset-concept" not in types
    assert "asset-existing-source-provenance" in types and "asset-raw-glb" in types


def test_source_changed_after_creation_stops_prepare(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    workflow, tasks = create_static_prop_reuse_workflow(
        "reuse", root, spec, source, provenance, revision_repository=AssetRevisionRepository(db)
    )
    other = create_box_glb(
        width_m=spec.dimensions.width_m + 0.01,
        depth_m=spec.dimensions.depth_m,
        height_m=spec.dimensions.height_m,
        mesh_name=f"SM_{spec.asset_id}_alt",
        collider_name=f"COL_{spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
    )
    source.write_bytes(other)
    engine = _engine(root, db)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.FAILED
    assert "changed after workflow creation" in (result.error_message or "")


def test_provenance_hash_mismatch_at_creation(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    data = json.loads(provenance.read_text())
    data["source_sha256"] = "0" * 64
    provenance.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="does not match"):
        create_static_prop_reuse_workflow(
            "reuse",
            root,
            spec,
            source,
            provenance,
            revision_repository=AssetRevisionRepository(db),
        )


def test_unsupported_profile_rejected(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    from gamefactory.core.domain.asset_contracts import parse_asset_specification as parse_pickup

    pickup = parse_pickup(PICKUP_SPEC)
    spec, source, provenance, _ = _inputs(root)
    with pytest.raises(ValidationError, match="static_prop@1"):
        create_static_prop_reuse_workflow(
            "reuse",
            root,
            pickup,
            source,
            provenance,
            revision_repository=AssetRevisionRepository(db),
        )


def test_retained_raw_drift_on_process(tmp_path: Path) -> None:
    from gamefactory.workflows.asset_reuse import verify_retained_raw_artifact

    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    engine.run_workflow(workflow_id)
    raw = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-raw-glb"
    )
    path = root / raw.relative_path
    path.write_bytes(path.read_bytes() + b"corrupt")
    handlers = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        LocalAssemblyNoProvider(),  # type: ignore[arg-type]
    )
    prepare = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "asset_reuse_prepare"
    )
    with pytest.raises(ArtifactError, match="integrity failure"):
        verify_retained_raw_artifact(handlers, workflow_id, prepare.parameters["source_glb_hash"])


def _cli(root: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "gamefactory", "--json", "--project", str(root), *arguments],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return result, json.loads(result.stdout.strip() or result.stderr.strip() or "{}")


def test_malformed_glb_rejected_at_creation(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, _source, provenance, _ = _inputs(root)
    bad = root / "inputs" / "bad.glb"
    bad.write_bytes(b"not-a-glb")
    prov = bad.with_suffix(".provenance.json")
    prov.write_text(
        json.dumps(_provenance(hashlib.sha256(b"not-a-glb").hexdigest())), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="preflight|GLB"):
        create_static_prop_reuse_workflow(
            "reuse",
            root,
            spec,
            bad,
            prov,
            revision_repository=AssetRevisionRepository(db),
        )


def test_unsafe_relative_traversal_rejected(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root, external=True)
    outside = source.resolve()
    with pytest.raises(ValidationError, match="traversal|outside|safe"):
        resolve_reuse_input_path(root, Path("..") / outside.parent.name / outside.name)


def test_provenance_file_drift_on_prepare(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    workflow, tasks = create_static_prop_reuse_workflow(
        "reuse", root, spec, source, provenance, revision_repository=AssetRevisionRepository(db)
    )
    provenance.write_text(
        json.dumps(
            _provenance(hashlib.sha256(source.read_bytes()).hexdigest(), original_provider="x")
        ),
        encoding="utf-8",
    )
    engine = _engine(root, db)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.FAILED
    assert "provenance changed" in (result.error_message or "").lower()


def test_retained_provenance_drift_on_process(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    engine.run_workflow(workflow_id)
    prov = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-existing-source-provenance"
    )
    path = root / prov.relative_path
    path.write_bytes(path.read_bytes() + b" ")
    handlers = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        LocalAssemblyNoProvider(),  # type: ignore[arg-type]
    )
    prepare = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "asset_reuse_prepare"
    )
    with pytest.raises(ArtifactError, match="integrity failure"):
        verify_retained_provenance_artifact(
            handlers, workflow_id, prepare.parameters["source_provenance_hash"]
        )


def test_evidence_export_refused_for_reuse(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    engine.run_workflow(workflow_id)
    with pytest.raises(ValidationError, match="existing_external reuse"):
        export_asset_evidence_bundle(root, db, workflow_id, root / "bundle")


def test_cli_report_reuse_pending_review_without_report_dir(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, _ = _inputs(root)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    engine.run_workflow(workflow_id)
    report_dir = root / ".gamefactory" / "reports" / workflow_id
    assert not report_dir.exists()
    proc, payload = _cli(root, "report", "--workflow", workflow_id)
    assert proc.returncode == 0, (proc.stderr, payload)
    assert not report_dir.exists()
    assert payload["workflow_kind"] == "static_prop_reuse"
    assert payload["final_visual_review"]["status"] == "PENDING"
    assert "asset-processed-glb" in payload["managed_artifacts"]
    assert payload["runtime_captures"]
    raw = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-raw-glb"
    )
    (root / raw.relative_path).write_bytes((root / raw.relative_path).read_bytes() + b"x")
    proc_drift, payload_drift = _cli(root, "report", "--workflow", workflow_id)
    assert proc_drift.returncode != 0
    assert "integrity" in (proc_drift.stderr + proc_drift.stdout).lower()


def test_cli_reuse_dry_run_without_init_or_db_mutation(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="T"\n', encoding="utf-8"
    )
    spec_dest = root / "spec.yml"
    spec_dest.write_bytes(SPEC.read_bytes())
    glb_bytes = create_box_glb(include_lod1=True, include_collider=True, include_texture=True)
    source = root / "chest.glb"
    source.write_bytes(glb_bytes)
    provenance = root / "prov.json"
    provenance.write_text(
        json.dumps(_provenance(hashlib.sha256(glb_bytes).hexdigest())), encoding="utf-8"
    )
    assert not (root / ".gamefactory").exists()
    proc, payload = _cli(
        root,
        "asset",
        "reuse",
        "--spec",
        "spec.yml",
        "--source",
        "chest.glb",
        "--provenance",
        "prov.json",
        "--dry-run",
    )
    assert proc.returncode == 0, payload
    assert payload["workflow_state_mutated"] is False
    assert not (root / ".gamefactory").exists()


def test_cli_reuse_dry_run_leaves_existing_db_unchanged(tmp_path: Path) -> None:
    root, _db = _project(tmp_path)
    db_path = root / ".gamefactory/state/factory.db"
    before = db_path.read_bytes()
    spec_dest = root / "spec.yml"
    spec_dest.write_bytes(SPEC.read_bytes())
    glb_bytes = create_box_glb(include_lod1=True, include_collider=True, include_texture=True)
    source = root / "chest.glb"
    source.write_bytes(glb_bytes)
    provenance = root / "prov.json"
    provenance.write_text(
        json.dumps(_provenance(hashlib.sha256(glb_bytes).hexdigest())), encoding="utf-8"
    )
    proc, payload = _cli(
        root,
        "asset",
        "reuse",
        "--spec",
        "spec.yml",
        "--source",
        "chest.glb",
        "--provenance",
        "prov.json",
        "--dry-run",
    )
    assert proc.returncode == 0, payload
    assert db_path.read_bytes() == before


def test_cli_reuse_dry_run_and_create(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="T"\n', encoding="utf-8"
    )
    assert _cli(root, "init")[0].returncode == 0
    spec_dest = root / "spec.yml"
    spec_dest.write_bytes(SPEC.read_bytes())
    glb_bytes = create_box_glb(include_lod1=True, include_collider=True, include_texture=True)
    source = root / "chest.glb"
    source.write_bytes(glb_bytes)
    provenance = root / "prov.json"
    provenance.write_text(
        json.dumps(_provenance(hashlib.sha256(glb_bytes).hexdigest())), encoding="utf-8"
    )
    proc, payload = _cli(
        root,
        "asset",
        "reuse",
        "--spec",
        "spec.yml",
        "--source",
        "chest.glb",
        "--provenance",
        "prov.json",
        "--dry-run",
    )
    assert proc.returncode == 0, payload
    assert payload["workflow_state_mutated"] is False
    assert payload["paid_provider_invocations"] == 0
    proc, payload = _cli(
        root,
        "asset",
        "reuse",
        "--spec",
        "spec.yml",
        "--source",
        "chest.glb",
        "--provenance",
        "prov.json",
    )
    assert proc.returncode in {0, 2, 3}, (proc.stderr, payload)
    workflow_id = payload.get("workflow_id")
    assert workflow_id
    tasks = TaskRepository(Database(root / ".gamefactory/state/factory.db")).list_by_workflow(
        str(workflow_id)
    )
    assert any(t.task_type == "asset_reuse_prepare" for t in tasks)


def _cli_human(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "gamefactory", "--project", str(root), *arguments],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_cli_asset_inspect_and_resume_at_final_visual_review(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, provenance, glb_bytes = _inputs(root)
    engine, workflow_id = _create(root, db, spec, source, provenance)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    source_hash = hashlib.sha256(glb_bytes).hexdigest()
    prepare = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "asset_reuse_prepare"
    )

    proc, payload = _cli(root, "asset", "inspect", spec.asset_id)
    assert proc.returncode == 0, (proc.stderr, payload)
    assert payload == {
        "asset_id": spec.asset_id,
        "revision": "r001",
        "profile": prepare.parameters["profile_id"],
        "profile_version": prepare.parameters["profile_qualified"],
        "workflow": workflow_id,
        "workflow_status": WorkflowStatus.BLOCKED.value,
        "current_gate": "asset_final_review",
        "source_mode": "existing_external",
        "original_provider": "meshy",
        "source_sha256": source_hash,
        "source_provenance_hash": prepare.parameters["source_provenance_hash"],
        "specification_hash": prepare.parameters["specification_hash"],
        "paid": False,
        "concept_versions": [],
    }

    human = _cli_human(root, "asset", "inspect", spec.asset_id)
    assert human.returncode == 0, human.stderr
    assert "existing_external" in human.stdout
    assert "no new provider spend" in human.stdout
    assert "Concept Versions:" not in human.stdout

    shape = json.loads(
        (PROJECT / "tests/fixtures/cli/reuse_final_resume_checkpoint.json").read_text(
            encoding="utf-8"
        )
    )
    proc_resume, resume_payload = _cli(root, "resume", workflow_id)
    assert proc_resume.returncode in {2, 3}, (proc_resume.stderr, resume_payload)
    checkpoint = resume_payload["asset_checkpoint"]
    for key, expected in shape.items():
        assert checkpoint[key] == expected
    assert checkpoint["source_sha256"] == source_hash
    assert checkpoint["original_provider"] == "meshy"
    assert checkpoint["concept_path"] is None
    assert checkpoint["concept_sha256"] is None

    human_resume = _cli_human(root, "resume", workflow_id)
    assert human_resume.returncode in {2, 3}, human_resume.stderr
    assert "concept=None" not in human_resume.stdout
    assert "source=existing_external" in human_resume.stdout
    assert f"sha256={source_hash}" in human_resume.stdout
    assert "paid=false" in human_resume.stdout
