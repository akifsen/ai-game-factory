"""Deterministic fake-process integration for the Godot workflow boundary.

These tests exercise Factory policy, durable task execution, artifacts, gates,
and recovery without claiming that a fake runner is a real engine test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.engines.godot_contracts import SCHEMA_VERSION
from gamefactory.adapters.engines.godot_staging import GodotStager
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.errors import ReconciliationRequired, ValidationError
from gamefactory.core.domain.models import (
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    GateStatus,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.godot_verification import (
    create_godot_verification_workflow,
    register_godot_handlers,
)

PROJECT = Path(__file__).parents[2]
FIXTURE = PROJECT / "examples" / "godot-verification"


class FakeGodotRunner:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls: list[list[str]] = []
        self.requests: list[Any] = []
        self.previous_report: dict[str, Any] | None = None

    def run(self, request: Any) -> Any:
        from gamefactory.core.execution.process_runner import CommandResult

        args = [str(item) for item in request.args]
        self.calls.append(args)
        self.requests.append(request)
        if "--version" in args:
            if self.outcome == "incomplete-cleanup":
                return CommandResult(
                    0,
                    "4.fake.test\n",
                    "",
                    pid=4321,
                    cleanup_completed=False,
                    cleanup_status="unconfirmed",
                )
            return CommandResult(0, "4.fake.test\n", "", duration_seconds=0.01)
        if "--help" in args:
            return CommandResult(
                0,
                "--headless --path --import --script --display-driver --rendering-driver "
                "--rendering-method --windowed --resolution -- user-provided arguments",
                "",
                duration_seconds=0.01,
            )
        if "--import" in args:
            code = 1 if self.outcome == "import-failure" else 0
            return CommandResult(
                code, "", "ERROR: controlled import failure" if code else "", duration_seconds=0.01
            )
        if "--script" not in args:
            return CommandResult(2, "", "unexpected fake Godot argv", duration_seconds=0.01)
        if "--captures" in args:
            return self._capture(args)
        if self.outcome == "runtime-failure":
            return CommandResult(2, "", "controlled runtime failure", duration_seconds=0.01)
        request_path = Path(args[args.index("--request") + 1])
        output_path = Path(args[args.index("--output") + 1])
        runtime_request = json.loads(request_path.read_text(encoding="utf-8"))
        if self.outcome == "missing-report":
            return CommandResult(0, "", "", duration_seconds=0.01)
        report = (
            self.previous_report
            if self.outcome == "stale-previous-attempt" and self.previous_report is not None
            else self._report(runtime_request)
        )
        report = json.loads(json.dumps(report))
        if self.outcome == "stale-execution":
            report["execution_id"] = "EXEC-STALE"
        if self.outcome == "completed-missing-snapshot":
            report["snapshots"].pop()
        if self.outcome == "self-declared-pass":
            report["snapshots"][-1]["state"]["player_hp"] = 99
            report["status"] = "PASS"
            report["expected"] = {"player_hp": 99}
            report["actual"] = {"player_hp": 99}
        if self.outcome == "wrong-state":
            report["snapshots"][-1]["state"]["player_hp"] += 1
        if self.outcome == "malformed-report":
            output_path.write_text('{"schema_version":', encoding="utf-8")
            return CommandResult(0, "fake runtime finished", "", duration_seconds=0.01)
        output_path.write_text(json.dumps(report), encoding="utf-8")
        if self.outcome == "pass-stdout-nonzero":
            self.previous_report = json.loads(json.dumps(report))
            return CommandResult(1, "PASS", "controlled nonzero exit", duration_seconds=0.01)
        if self.outcome == "truncated-output":
            return CommandResult(
                0,
                "fake runtime finished",
                "",
                duration_seconds=0.01,
                stdout_truncated=True,
            )
        return CommandResult(0, "fake runtime finished", "", duration_seconds=0.01)

    def _capture(self, args: list[str]) -> Any:
        from io import BytesIO

        from PIL import Image, ImageDraw

        from gamefactory.adapters.engines.godot_image import LAYOUTS
        from gamefactory.core.execution.process_runner import CommandResult

        request = json.loads(Path(args[args.index("--request") + 1]).read_text(encoding="utf-8"))
        output_path = Path(args[args.index("--output") + 1])
        capture_dir = Path(args[args.index("--captures") + 1])
        width = int(request["viewport_width"])
        height = int(request["viewport_height"])
        hp, enemies, score = 100, 3, 0
        action_by_tick: dict[int, list[dict[str, Any]]] = {}
        for action in request["actions"]:
            action_by_tick.setdefault(int(action["tick"]), []).append(action)
        snapshots = []
        current = 0
        for tick in request["snapshots"]:
            tick = int(tick)
            for next_tick in range(current + 1, tick + 1):
                for action in action_by_tick.get(next_tick, []):
                    if action["action"] == "apply_damage":
                        hp = max(0, hp - int(action["amount"]))
                    elif enemies:
                        enemies -= 1
                        score += 100
            if tick == 0:
                hp, enemies, score = 100, 3, 0
            state = {"player_hp": hp, "enemies_remaining": enemies, "score": score}
            snapshots.append({"tick": tick, "state": state})
            current = tick
        display = "headless" if self.outcome == "dummy-renderer" else "windows"
        captures = []
        for index, snapshot in enumerate(snapshots):
            point = request["captures"][index]
            state = snapshot["state"]
            painted = state
            if self.outcome == "hud-mutation":
                painted = {**state, "player_hp": 100}
            if self.outcome == "previous-frame" and index == len(snapshots) - 1:
                painted = snapshots[0]["state"]
            image = Image.new(
                "RGBA",
                (100, 100) if self.outcome == "wrong-resolution" else (width, height),
                (31, 36, 46, 255),
            )
            if self.outcome != "wrong-resolution":
                draw = ImageDraw.Draw(image)
                bar_x, bar_y, bar_w, bar_h = LAYOUTS[(width, height)]["bar"]
                draw.rectangle(
                    (bar_x, bar_y, bar_x + bar_w - 1, bar_y + bar_h - 1), fill=(51, 51, 56, 255)
                )
                filled = int(bar_w * int(painted["player_hp"]) / 100)
                if filled > 0:
                    draw.rectangle(
                        (bar_x, bar_y, bar_x + filled - 1, bar_y + bar_h - 1),
                        fill=(51, 191, 71, 255),
                    )
                for enemy_index, (ex, ey, ew, eh) in enumerate(LAYOUTS[(width, height)]["enemies"]):
                    if enemy_index < int(painted["enemies_remaining"]):
                        draw.rectangle((ex, ey, ex + ew - 1, ey + eh - 1), fill=(217, 56, 46, 255))
            raw = BytesIO()
            image.save(raw, format="PNG")
            payload = raw.getvalue()
            if self.outcome == "truncated-png":
                payload = b"\x89PNG\r\n\x1a\n" + b"truncated"
            if self.outcome != "no-png":
                (capture_dir / f"{point['id']}.png").write_bytes(payload)
            captures.append(
                {
                    "id": point["id"],
                    "tick": snapshot["tick"],
                    "state": state,
                    "file": f"{point['id']}.png",
                }
            )
        if self.outcome == "duplicate-capture-id" and captures:
            captures[-1]["id"] = captures[0]["id"]
        report = {
            "schema_version": "0.3.0",
            "harness_version": "0.3.0",
            "execution_id": "EXEC-STALE"
            if self.outcome == "stale-execution"
            else request["execution_id"],
            "scenario_id": request["scenario_id"],
            "scenario_sha256": request["scenario_sha256"],
            "completed_tick": request["max_tick"],
            "snapshots": snapshots,
            "captures": captures,
            "renderer": {
                "rendering_method": "gl_compatibility",
                "rendering_driver": "opengl3",
                "adapter_name": "Fake Adapter",
                "adapter_vendor": "Test",
                "display_server": display,
                "os_name": "Test",
                "viewport_width": width,
                "viewport_height": height,
                "source": "engine_api",
            },
            "completion": "COMPLETED",
            "error": None,
        }
        output_path.write_text(json.dumps(report), encoding="utf-8")
        return CommandResult(
            0,
            "OpenGL API 3.3.0 Compatibility - Using Device: Fake Adapter\n",
            "",
            duration_seconds=0.01,
        )

    @staticmethod
    def _report(request: dict[str, Any]) -> dict[str, Any]:
        hp, enemies, score = 100, 3, 0
        action_by_tick: dict[int, list[dict[str, Any]]] = {}
        for action in request["actions"]:
            action_by_tick.setdefault(action["tick"], []).append(action)
        snapshots = []
        current = 0
        for tick in request["snapshots"]:
            for next_tick in range(current + 1, tick + 1):
                for action in action_by_tick.get(next_tick, []):
                    if action["action"] == "apply_damage":
                        hp = max(0, hp - action["amount"])
                    elif enemies:
                        enemies -= 1
                        score += 100
            if tick == 0:
                hp, enemies, score = 100, 3, 0
            snapshots.append(
                {
                    "tick": tick,
                    "state": {"player_hp": hp, "enemies_remaining": enemies, "score": score},
                }
            )
            current = tick
        return {
            "schema_version": SCHEMA_VERSION,
            "execution_id": request["execution_id"],
            "scenario_id": request["scenario_id"],
            "scenario_sha256": request["scenario_sha256"],
            "completed_tick": request["max_tick"],
            "snapshots": snapshots,
            "completion": "COMPLETED",
            "error": None,
        }


def _project(tmp_path: Path) -> tuple[Path, Database, str, Path, Path]:
    root = tmp_path / "Godot Ω game"
    shutil.copytree(FIXTURE, root)
    ConfigLoader.init_project(root)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    cfg = ConfigLoader.load_config(root)
    ProjectRepository(db).save(
        __import__("gamefactory.core.domain.models", fromlist=["Project"]).Project(
            id=cfg.project.id,
            name=cfg.project.name,
            engine_type=cfg.engine.type,
            root_path=str(root),
        )
    )
    executable = root / "fake-godot.exe"
    executable.write_bytes(b"fake engine executable for path/hash binding")
    scenario = root / "scenario.json"
    shutil.copy2(FIXTURE / "scenario.json", scenario)
    return root, db, cfg.project.id, executable, scenario


def _workflow(
    root: Path,
    db: Database,
    project_id: str,
    exe: Path,
    scenario: Path,
    runner: FakeGodotRunner,
    *,
    require_approval: bool = False,
) -> tuple[WorkflowEngine, Workflow]:
    policy = PolicyEngine(PolicyRule(require_approval_for_process_execution=require_approval))
    engine = WorkflowEngine(root, db, policy_engine=policy, process_runner=runner)
    register_godot_handlers(
        engine.handler_registry,
        root,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        runner,
    )
    workflow, tasks = create_godot_verification_workflow(project_id, root, exe, scenario)
    engine.register_workflow(workflow, tasks)
    return engine, workflow


def _approve(db: Database, approval_id: str) -> None:
    repo = ApprovalRepository(db)
    approval = repo.get(approval_id)
    assert approval is not None
    decided = ApprovalService.approve(approval, "pipeline-test", "approved test inputs")
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


def test_fake_godot_pipeline_passes_independent_assertions_and_no_duplicate_resume(
    tmp_path: Path,
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.COMPLETED
    assert len(runner.calls) == 4
    tasks = TaskRepository(db).list_by_workflow(workflow.id)
    assert [task.task_type for task in tasks] == [
        "godot_execute",
        "godot_validate",
        "record_evidence",
    ]
    report_artifact = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow.id)
        if item.artifact_type == "godot-validation-report"
    )
    report_path = engine.artifact_mgr.path_guard.resolve_safe_path(report_artifact.relative_path)
    validation = json.loads(report_path.read_text(encoding="utf-8"))
    assert validation["status"] == "PASS"
    assert [finding["actual"] for finding in validation["findings"] if finding["tick"] == 90] == [
        40,
        2,
        100,
    ]
    assert all(
        gate.status == GateStatus.PASSED
        for gate in QualityGateRepository(db).list_by_task(tasks[-1].id)
    )
    resumed = WorkflowEngine(root, db, process_runner=runner)
    register_godot_handlers(
        resumed.handler_registry,
        root,
        resumed.artifact_mgr,
        resumed.art_repo,
        resumed.exec_repo,
        resumed.evi_repo,
        resumed.gate_repo,
        runner,
    )
    assert resumed.run_workflow(workflow.id).status == WorkflowStatus.COMPLETED
    assert len(runner.calls) == 4


def test_staging_excludes_plural_secret_files_and_generic_cache_directories(tmp_path: Path) -> None:
    root, _db, _project_id, _exe, scenario = _project(tmp_path)
    (root / "secrets.yml").write_text("token: do-not-copy\n", encoding="utf-8")
    (root / "credentials.toml").write_text("password = 'do-not-copy'\n", encoding="utf-8")
    cache_dir = root / ".cache"
    cache_dir.mkdir()
    (cache_dir / "generated.bin").write_bytes(b"do-not-copy")

    stager = GodotStager(root, root / ".gamefactory" / "scratch")
    manifest, manifest_hash = stager.source_manifest(scenario)
    manifest_paths = {item.relative_path for item in manifest}
    assert "secrets.yml" not in manifest_paths
    assert "credentials.toml" not in manifest_paths
    assert ".cache/generated.bin" not in manifest_paths

    stage, _, _ = stager.create_stage(
        "WF-SECRET-FILTER",
        "EXEC-SECRET-FILTER",
        scenario,
        manifest_hash,
        b"extends SceneTree\nfunc _init(): quit()\n",
    )
    assert not (stage / "secrets.yml").exists()
    assert not (stage / "credentials.toml").exists()
    assert not (stage / ".cache").exists()


@pytest.mark.parametrize(
    ("outcome", "runtime_calls"),
    [("import-failure", 0), ("runtime-failure", 1), ("missing-report", 1), ("stale-execution", 1)],
)
def test_process_and_stale_report_failures_keep_attempt_artifacts(
    tmp_path: Path, outcome: str, runtime_calls: int
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner(outcome)
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.FAILED
    assert sum("--script" in args for args in runner.calls) == runtime_calls
    artifacts = ArtifactRepository(db).list_by_workflow(workflow.id)
    assert any(item.artifact_type == "godot-source-manifest" for item in artifacts)
    if outcome == "import-failure":
        assert any(item.artifact_type == "godot-import-stderr" for item in artifacts)
        process_artifact = next(
            item for item in artifacts if item.artifact_type == "godot-import-process"
        )
        process_path = engine.artifact_mgr.path_guard.resolve_safe_path(
            process_artifact.relative_path
        )
        process_metadata = json.loads(process_path.read_text(encoding="utf-8"))
        assert process_metadata["workflow_id"] == workflow.id
        assert process_metadata["task_id"] == TaskRepository(db).list_by_workflow(workflow.id)[0].id
        assert process_metadata["execution_id"]
        assert process_metadata["attempt_number"] == 1
        assert process_metadata["scenario_fingerprint"]
        assert process_metadata["source_manifest_sha256"]
        assert process_metadata["executable_sha256"]
        assert process_metadata["harness_sha256"]
        assert process_metadata["harness_version"] == "0.2.0"
        assert not any("--script" in args for args in runner.calls)
    if outcome == "stale-execution":
        assert any(item.artifact_type == "godot-validation-report" for item in artifacts)


@pytest.mark.parametrize(
    "outcome",
    [
        "completed-missing-snapshot",
        "self-declared-pass",
        "pass-stdout-nonzero",
        "malformed-report",
        "truncated-output",
    ],
)
def test_malformed_or_self_asserted_runtime_evidence_never_completes_workflow(
    tmp_path: Path, outcome: str
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner(outcome)
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)

    result = engine.run_workflow(workflow.id)

    assert result.status == WorkflowStatus.FAILED
    tasks = TaskRepository(db).list_by_workflow(workflow.id)
    assert tasks[1].status != TaskStatus.COMPLETED
    assert tasks[2].status == TaskStatus.PENDING
    assert any(task.status == TaskStatus.FAILED for task in tasks)
    assert not any(
        gate.status == GateStatus.PASSED and gate.gate_type == "godot-independent-assertions"
        for task in tasks
        for gate in QualityGateRepository(db).list_by_task(task.id)
    )


def test_prior_attempt_report_is_preserved_and_rejected_when_reused_by_retry(
    tmp_path: Path,
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner("pass-stdout-nonzero")
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    first = engine.run_workflow(workflow.id)
    assert first.status == WorkflowStatus.FAILED
    task = TaskRepository(db).list_by_workflow(workflow.id)[0]
    attempt1 = ExecutionRepository(db).get_latest_attempt(task.id)
    first_observation = next(
        artifact
        for artifact in ArtifactRepository(db).list_by_task(task.id)
        if artifact.artifact_type == "godot-runtime-observation"
        and Path(artifact.relative_path).parts[-2] == attempt1.id
    )
    first_path = engine.artifact_mgr.path_guard.resolve_safe_path(first_observation.relative_path)
    first_bytes = first_path.read_bytes()
    first_report = json.loads(first_bytes)
    assert first_report["execution_id"] == attempt1.id

    runner.outcome = "stale-previous-attempt"
    retried = engine.retry_task(workflow.id, task.id)

    assert retried.status == WorkflowStatus.FAILED
    attempts = ExecutionRepository(db).list_by_task(task.id)
    assert len(attempts) == 2 and attempts[1].id != attempts[0].id
    assert first_path.read_bytes() == first_bytes
    second_observations = [
        artifact
        for artifact in ArtifactRepository(db).list_by_task(task.id)
        if artifact.artifact_type == "godot-runtime-observation"
        and Path(artifact.relative_path).parts[-2] == attempts[1].id
    ]
    assert second_observations
    second_path = engine.artifact_mgr.path_guard.resolve_safe_path(
        second_observations[0].relative_path
    )
    assert json.loads(second_path.read_text(encoding="utf-8"))["execution_id"] == attempts[0].id
    task_states = TaskRepository(db).list_by_workflow(workflow.id)
    assert task_states[0].status == TaskStatus.COMPLETED
    assert task_states[1].status == TaskStatus.FAILED
    assert task_states[2].status == TaskStatus.PENDING


@pytest.mark.parametrize("tamper_path", ["observation-bytes", "report-path-escape"])
def test_observation_integrity_or_managed_path_escape_blocks_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper_path: str
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    execute_task = TaskRepository(db).list_by_workflow(workflow.id)[0]
    execute_task_id = execute_task.id
    original_execute_task = engine._execute_task

    def execute_then_tamper(wf: Workflow, task: Any) -> Any:
        result = original_execute_task(wf, task)
        if task.id == execute_task_id:
            observation = next(
                artifact
                for artifact in ArtifactRepository(db).list_by_task(task.id)
                if artifact.artifact_type == "godot-runtime-observation"
            )
            if tamper_path == "observation-bytes":
                path = engine.artifact_mgr.path_guard.resolve_safe_path(observation.relative_path)
                path.write_bytes(path.read_bytes() + b" ")
            else:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE artifacts SET relative_path = ? WHERE id = ?",
                        ("../escaped-observation.json", observation.id),
                    )
        return result

    monkeypatch.setattr(engine, "_execute_task", execute_then_tamper)
    result = engine.run_workflow(workflow.id)

    assert result.status == WorkflowStatus.BLOCKED
    tasks = TaskRepository(db).list_by_workflow(workflow.id)
    assert tasks[0].status == TaskStatus.COMPLETED
    assert tasks[1].status == TaskStatus.BLOCKED
    assert tasks[2].status == TaskStatus.PENDING
    assert runner.calls and sum("--script" in args for args in runner.calls) == 1
    assert not any(
        gate.status == GateStatus.PASSED and gate.gate_type == "godot-independent-assertions"
        for task in tasks
        for gate in QualityGateRepository(db).list_by_task(task.id)
    )


def test_approval_precedes_all_godot_processes_and_changed_inputs_request_new_approval(
    tmp_path: Path,
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner, require_approval=True)
    blocked = engine.run_workflow(workflow.id)
    assert blocked.status == WorkflowStatus.BLOCKED
    assert runner.calls == []
    original_id = blocked.pending_approval_id
    assert original_id
    _approve(db, original_id)
    # A source mutation after approval changes the canonical operation hash.
    with (root / "main.gd").open("a", encoding="utf-8") as game_source:
        game_source.write("\n# post-approval mutation\n")
    changed = engine.run_workflow(workflow.id)
    assert changed.status == WorkflowStatus.BLOCKED
    assert changed.pending_approval_id and changed.pending_approval_id != original_id
    assert runner.calls == []


def test_recovery_with_unknown_child_intent_blocks_in_fresh_cli_processes(tmp_path: Path) -> None:
    """Crash after fsynced intent and before runner return; CLI resume never launches again."""
    root, _db0, _project_id, exe, scenario = _project(tmp_path)
    workflow_id_path = tmp_path / "workflow-id.txt"
    helper = tmp_path / "crash_after_intent.py"
    helper.write_text(
        """import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import ArtifactRepository
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.execution.process_runner import CommandResult
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.godot_verification import create_godot_verification_workflow, register_godot_handlers
class CrashRunner:
    def run(self, request):
        os._exit(71)
root, exe, scenario, out = map(Path, sys.argv[1:])
db = Database(root / '.gamefactory' / 'state' / 'factory.db')
cfg = ConfigLoader.load_config(root)
engine = WorkflowEngine(root, db, policy_engine=PolicyEngine(PolicyRule()), process_runner=CrashRunner())
register_godot_handlers(engine.handler_registry, root, engine.artifact_mgr, engine.art_repo, engine.exec_repo, engine.evi_repo, engine.gate_repo, engine.process_runner)
workflow, tasks = create_godot_verification_workflow(cfg.project.id, root, exe, scenario)
engine.register_workflow(workflow, tasks)
out.write_text(workflow.id, encoding='utf-8')
engine.run_workflow(workflow.id)
""",
        encoding="utf-8",
    )
    crashed = subprocess.run(
        [sys.executable, str(helper), str(root), str(exe), str(scenario), str(workflow_id_path)],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert crashed.returncode == 71
    workflow_id = workflow_id_path.read_text(encoding="utf-8")
    for _ in range(2):
        resumed = subprocess.run(
            [
                sys.executable,
                "-m",
                "gamefactory",
                "--json",
                "--project",
                str(root),
                "resume",
                workflow_id,
            ],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert resumed.returncode == 3
        assert json.loads(resumed.stderr)["error"] == "RECONCILIATION_REQUIRED"
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    assert tasks[0].status == TaskStatus.BLOCKED
    attempts = ExecutionRepository(db).list_by_task(tasks[0].id)
    assert len(attempts) == 1 and attempts[0].status == ExecutionStatus.UNCERTAIN
    assert any(
        item.artifact_type == "godot-process-journal"
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
    )


@pytest.mark.parametrize("terminal_receipt", [False, True])
def test_known_terminal_or_no_intent_recovery_allows_only_explicit_new_attempt(
    tmp_path: Path, terminal_receipt: bool
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    task = TaskRepository(db).list_by_workflow(workflow.id)[0]
    old = Execution(id="EXEC-RECOVERY-001", task_id=task.id, attempt_number=1)
    assert TaskRepository(db).claim_execution(task.id, old, project_id, 500.0)
    if terminal_receipt:
        import_folder = (
            Path(".gamefactory") / "artifacts" / "godot" / workflow.id / task.id / old.id
        )
        for name, phase, body in (
            ("version.json", "version", {"argv": [str(exe), "--version"]}),
            # ProcessRunner proved no child PID was created. Cleanup therefore
            # was not applicable, but this is still a conclusive terminal receipt.
            (
                "version-terminal.json",
                "version-terminal",
                {"exit_code": None, "pid": None, "cleanup_completed": False},
            ),
        ):
            payload = json.dumps(
                {
                    "workflow_id": workflow.id,
                    "task_id": task.id,
                    "execution_id": old.id,
                    "attempt_number": 1,
                    "phase": phase,
                    "process": body,
                }
            )
            artifact = engine.artifact_mgr.create_text_artifact(
                workflow.id,
                task.id,
                "godot-process-journal",
                "test",
                (import_folder / name).as_posix(),
                payload,
            )
            ArtifactRepository(db).save(artifact)
    recovered = engine.run_workflow(workflow.id)
    assert recovered.status == WorkflowStatus.FAILED
    assert ExecutionRepository(db).get_latest_attempt(task.id).retryable
    assert runner.calls == []  # Recovery never reuses or replays the old attempt.
    completed = engine.retry_task(workflow.id, task.id)
    assert completed.status == WorkflowStatus.COMPLETED
    attempts = ExecutionRepository(db).list_by_task(task.id)
    assert [item.attempt_number for item in attempts] == [1, 2]
    assert attempts[0].status == ExecutionStatus.FAILED
    assert attempts[1].status == ExecutionStatus.COMPLETED
    assert len(runner.calls) == 4


def test_metered_conservative_handler_never_uses_safe_terminal_retry_path(tmp_path: Path) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner()
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    task = TaskRepository(db).list_by_workflow(workflow.id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET cost_class = ? WHERE id = ?",
            (CostClass.METERED.value, task.id),
        )
    old = Execution(id="EXEC-METERED-RECOVERY", task_id=task.id, attempt_number=1)
    assert TaskRepository(db).claim_execution(task.id, old, project_id, 500.0)

    with pytest.raises(ReconciliationRequired):
        engine.run_workflow(workflow.id)

    assert TaskRepository(db).get(task.id).status == TaskStatus.BLOCKED
    assert ExecutionRepository(db).get_latest_attempt(task.id).status == ExecutionStatus.UNCERTAIN
    assert runner.calls == []


def test_terminal_journal_persistence_failure_preserves_unknown_child_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, project_id, exe, scenario = _project(tmp_path)
    runner = FakeGodotRunner("incomplete-cleanup")
    engine, workflow = _workflow(root, db, project_id, exe, scenario, runner)
    original_save = engine.art_repo.save
    failed_paths: list[str] = []

    def fail_terminal_journal(artifact: Any) -> None:
        if (
            artifact.artifact_type == "godot-process-journal"
            and artifact.relative_path.endswith("version-terminal.json")
        ) or artifact.relative_path.endswith("version-process.json"):
            failed_paths.append(artifact.relative_path)
            raise OSError("controlled process artifact persistence failure")
        original_save(artifact)

    monkeypatch.setattr(engine.art_repo, "save", fail_terminal_journal)
    result = engine.run_workflow(workflow.id)

    assert result.status == WorkflowStatus.BLOCKED
    assert any(path.endswith("version-terminal.json") for path in failed_paths)
    assert any(path.endswith("version-process.json") for path in failed_paths)
    task = TaskRepository(db).list_by_workflow(workflow.id)[0]
    assert task.status == TaskStatus.BLOCKED
    assert ExecutionRepository(db).get_latest_attempt(task.id).status == ExecutionStatus.UNCERTAIN
    with pytest.raises(ValidationError):
        engine.retry_task(workflow.id, task.id)
    with pytest.raises(ReconciliationRequired):
        engine.run_workflow(workflow.id)
    assert len(runner.calls) == 1
    assert len(ExecutionRepository(db).list_by_task(task.id)) == 1
