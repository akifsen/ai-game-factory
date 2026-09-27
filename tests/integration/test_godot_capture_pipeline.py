"""Fake-renderer capture workflow: policy, binding, review, and tamper checks.

These tests do not claim a real viewport was rendered.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ProjectRepository,
    TaskRepository,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    AuditEvent,
    Project,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.godot_capture import (
    create_godot_capture_workflow,
    register_godot_capture_handlers,
)
from tests.integration.test_godot_pipeline import FIXTURE, FakeGodotRunner

PROJECT = Path(__file__).parents[2]


def _project(
    tmp_path: Path, scenario_name: str = "visual-scenario.json"
) -> tuple[Path, Database, str, Path, Path]:
    root = tmp_path / "Capture Ω game"
    shutil.copytree(FIXTURE, root)
    ConfigLoader.init_project(root)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    cfg = ConfigLoader.load_config(root)
    ProjectRepository(db).save(
        Project(
            id=cfg.project.id,
            name=cfg.project.name,
            engine_type=cfg.engine.type,
            root_path=str(root),
        )
    )
    executable = root / "fake-godot.exe"
    executable.write_bytes(b"fake engine executable for path/hash binding")
    scenario = root / scenario_name
    shutil.copy2(FIXTURE / scenario_name, scenario)
    return root, db, cfg.project.id, executable, scenario


def _engine(
    root: Path, db: Database, project_id: str, exe: Path, scenario: Path, runner: FakeGodotRunner
) -> WorkflowEngine:
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(PolicyRule(require_approval_for_process_execution=False)),
        process_runner=runner,
    )
    register_godot_capture_handlers(
        engine.handler_registry,
        root,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        runner,
    )
    workflow, tasks = create_godot_capture_workflow(project_id, root, exe, scenario)
    engine.register_workflow(workflow, tasks)
    return engine


def _approve(engine: WorkflowEngine, db: Database, approval_id: str) -> None:
    repo = ApprovalRepository(db)
    approval = repo.get(approval_id)
    assert approval is not None
    workflow = engine.wf_repo.get(approval.workflow_id)
    task = engine.task_repo.get(approval.task_id)
    assert workflow is not None and task is not None
    decided = ApprovalService.approve(
        approval,
        "pipeline-test",
        "test actor, not a human visual review",
        current_inputs=engine.approval_inputs(workflow, task),
    )
    assert repo.decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="pipeline-test",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )


def test_fake_capture_blocks_for_visual_review_and_resume_does_not_rerun(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine = _engine(root, db, project_id, exe, scenario, runner)
    workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
    blocked = engine.run_workflow(workflow_id)
    assert blocked.status == WorkflowStatus.BLOCKED, blocked.error_message
    assert blocked.pending_approval_id
    approval = ApprovalRepository(db).get(blocked.pending_approval_id)
    assert approval is not None and approval.approval_type == "visual_review"
    runtime = next(call for call in runner.calls if "--captures" in call)
    assert "--headless" not in runtime
    assert "--windowed" in runtime
    assert "--rendering-method" in runtime
    assert "gl_compatibility" in runtime
    pages = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-review-html"
    ]
    html = (root / pages[0].relative_path).read_text(encoding="utf-8")
    assert "PENDING" in html and "<script" not in html.lower()
    assert 'src="images/cp-000.png"' in html
    calls_before = len(runner.calls)
    _approve(engine, db, blocked.pending_approval_id)
    completed = engine.run_workflow(workflow_id)
    assert completed.status == WorkflowStatus.COMPLETED
    assert len(runner.calls) == calls_before


def test_hud_mutation_keeps_state_pass_and_fails_visual_gate(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner("hud-mutation")
    engine = _engine(root, db, project_id, exe, scenario, runner)
    workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    report = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-capture-validation"
    )
    payload = json.loads((root / report.relative_path).read_text(encoding="utf-8"))
    assert payload["state"]["status"] == "PASS"
    assert payload["status"] == "FAIL"
    assert any(
        item["status"] == "FAIL" and "health-tail" in str(item["id"])
        for item in payload["region_checks"]
    )
    assert ApprovalRepository(db).list_by_workflow(workflow_id) == []


def test_missing_png_and_dummy_renderer_are_not_passes(tmp_path: Path) -> None:
    for outcome in (
        "no-png",
        "dummy-renderer",
        "truncated-png",
        "wrong-resolution",
        "stale-execution",
        "previous-frame",
    ):
        root, db, project_id, exe, scenario = _project(tmp_path / outcome)
        engine = _engine(root, db, project_id, exe, scenario, FakeGodotRunner(outcome))
        workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
        result = engine.run_workflow(workflow_id)
        assert result.status == WorkflowStatus.FAILED
        assert ApprovalRepository(db).list_pending(workflow_id) == []


def test_reject_does_not_start_another_capture(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine = _engine(root, db, project_id, exe, scenario, runner)
    workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
    blocked = engine.run_workflow(workflow_id)
    approval = ApprovalRepository(db).get(blocked.pending_approval_id or "")
    assert approval is not None
    rejected = ApprovalService.reject(approval, "pipeline-test", "not accepted")
    assert ApprovalRepository(db).decide_if_pending(
        rejected,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=rejected.id,
            action="REJECTED",
            actor="pipeline-test",
            previous_state="PENDING",
            new_state="REJECTED",
        ),
    )
    calls = len(runner.calls)
    failed = engine.run_workflow(workflow_id)
    assert failed.status == WorkflowStatus.FAILED
    assert len(runner.calls) == calls


def test_tampered_png_cannot_be_approved_or_completed(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    engine = _engine(root, db, project_id, exe, scenario, FakeGodotRunner())
    workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
    blocked = engine.run_workflow(workflow_id)
    png = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-capture-png" and item.relative_path.endswith("cp-090.png")
    )
    target = root / png.relative_path
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(ArtifactError):
        _approve(engine, db, blocked.pending_approval_id or "")
    approval = ApprovalRepository(db).get(blocked.pending_approval_id or "")
    assert approval is not None and approval.status.value == "PENDING"


def test_linux_capture_allowlists_display_and_refuses_headless_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _db, _project_id, exe, scenario = _project(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    from gamefactory.adapters.engines.godot_capture_contracts import (
        capture_fingerprint,
        load_capture_scenario,
    )
    from gamefactory.adapters.engines.godot_execution import GodotExecutor

    loaded = load_capture_scenario(scenario)
    fingerprint = capture_fingerprint(loaded)
    runner = FakeGodotRunner()
    executor = GodotExecutor(runner)
    stage = root / "stage"
    stage.mkdir()
    scratch = root / "scratch"
    scratch.mkdir()
    with pytest.raises(ValidationError, match="DISPLAY"):
        executor.run_capture(
            exe, stage, scratch, loaded, "EXEC-test", fingerprint, "source", "harness", 5, 5
        )
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XAUTHORITY", str(root / "xauth"))
    monkeypatch.setenv("SECRET_TOKEN", "do-not-pass")
    (root / "xauth").write_text("cookie", encoding="utf-8")
    executor.run_capture(
        exe,
        stage,
        scratch,
        loaded,
        "EXEC-test",
        capture_fingerprint(loaded),
        "source",
        "harness",
        5,
        5,
    )
    runtime = next(
        item for item in runner.requests if "--captures" in [str(part) for part in item.args]
    )
    env = runtime.env_overrides
    assert env["DISPLAY"] == ":99"
    assert env["XAUTHORITY"] == str(root / "xauth")
    assert "SECRET_TOKEN" not in env
    argv = [str(part) for part in runtime.args]
    assert "--display-driver" in argv and "x11" in argv
    assert "--headless" not in argv


def test_portrait_profile_is_a_separate_size(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path, "visual-scenario-portrait.json")
    engine = _engine(root, db, project_id, exe, scenario, FakeGodotRunner())
    workflow_id = engine.wf_repo.list_by_project(project_id)[-1].id
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    png = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-capture-png"
    )
    from gamefactory.adapters.engines.godot_image import decode_png

    image = decode_png((root / png.relative_path).read_bytes(), 720, 1280)
    assert (image.width, image.height) == (720, 1280)
    html = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-review-html"
    )
    page = (root / html.relative_path).read_text(encoding="utf-8")
    assert "portrait" in page
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    assert tasks[0].status.value == "COMPLETED"
    assert tasks[2].status.value == "BLOCKED"
