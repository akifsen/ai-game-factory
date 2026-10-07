"""Regression coverage for persisted Factory manifest reconstruction on resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import TaskRepository, WorkflowRepository
from gamefactory.cli.factory_commands import saved_factory_manifests
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest
from gamefactory.core.domain.models import CostClass, Task, TaskStatus, Workflow, WorkflowStatus
from gamefactory.workflows.factory_workflow import build_workflow


def _init_db(root: Path) -> tuple[Database, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    _, cfg, _ = ConfigLoader.init_project(root, project_name="Manifest reconstruction")
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    return db, cfg.project.id


def _persist_manifest(root: Path, db: Database, manifest: FactoryWorkflowManifest) -> None:
    workflow, tasks = build_workflow(manifest)
    WorkflowRepository(db).save(workflow)
    task_repo = TaskRepository(db)
    for task in tasks:
        task_repo.save(task)


def _writing_manifest(project_id: str, workflow_id: str) -> FactoryWorkflowManifest:
    return FactoryWorkflowManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": "factory-workflow-1.0.0",
                "workflow_id": workflow_id,
                "project_id": project_id,
                "name": "Writing workflow",
                "require_human_game_acceptance": True,
                "tasks": [
                    {
                        "task_id": "writer",
                        "name": "Write player controller",
                        "family": "feature",
                        "kind": "code",
                        "executor_id": "executor.local",
                        "agent_id": "agent.local",
                        "objective": "Propose a bounded script update",
                        "prompt_template_id": "feature",
                        "prompt_template_version": "1.0.0",
                        "game_write": True,
                        "output_scopes": ["scripts/player.gd"],
                        "expected_artifacts": ["scripts/player.gd"],
                        "required_gates": ["code"],
                    }
                ],
            },
            allow_nan=False,
        )
    )


def _nonwriting_manifest(
    project_id: str,
    workflow_id: str,
    *,
    require_human_game_acceptance: bool,
) -> FactoryWorkflowManifest:
    return FactoryWorkflowManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": "factory-workflow-1.0.0",
                "workflow_id": workflow_id,
                "project_id": project_id,
                "name": "Advisory workflow",
                "require_human_game_acceptance": require_human_game_acceptance,
                "tasks": [
                    {
                        "task_id": "advisor",
                        "name": "Advise on design",
                        "family": "design",
                        "kind": "design",
                        "executor_id": "executor.local",
                        "agent_id": "agent.local",
                        "objective": "Propose design notes without writing game files",
                        "prompt_template_id": "design",
                        "prompt_template_version": "1.0.0",
                        "game_write": False,
                    }
                ],
            },
            allow_nan=False,
        )
    )


def test_saved_factory_manifests_reconstructs_game_write_workflow(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id = _init_db(root)
    workflow_id = "WF-FACTORY-7356d61d"
    manifest = _writing_manifest(project_id, workflow_id)
    _persist_manifest(root, db, manifest)

    rebuilt = saved_factory_manifests(root, db)

    assert len(rebuilt) == 1
    assert rebuilt[0].workflow_id == workflow_id
    assert rebuilt[0].sha256 == manifest.sha256
    assert rebuilt[0].require_human_game_acceptance is True
    assert rebuilt[0].tasks[0].game_write is True


@pytest.mark.parametrize("require_acceptance", [False, True])
def test_saved_factory_manifests_reconstructs_nonwriting_workflow(
    tmp_path: Path, require_acceptance: bool
) -> None:
    root = tmp_path / "project"
    db, project_id = _init_db(root)
    workflow_id = "wf-readonly-manifest"
    manifest = _nonwriting_manifest(
        project_id, workflow_id, require_human_game_acceptance=require_acceptance
    )
    _persist_manifest(root, db, manifest)

    rebuilt = saved_factory_manifests(root, db)

    assert len(rebuilt) == 1
    assert rebuilt[0].sha256 == manifest.sha256
    assert rebuilt[0].require_human_game_acceptance is require_acceptance


def test_saved_factory_manifests_rejects_tampered_manifest_hash(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id = _init_db(root)
    manifest = _writing_manifest(project_id, "wf-tampered-hash")
    workflow, tasks = build_workflow(manifest)
    WorkflowRepository(db).save(workflow)
    task_repo = TaskRepository(db)
    for task in tasks:
        if task.task_type == "factory_provider_task":
            params = dict(task.parameters)
            params["manifest_sha256"] = "f" * 64
            task = Task(
                task.id,
                task.workflow_id,
                task.name,
                task.task_type,
                task.cost_class,
                status=task.status,
                parameters=params,
            )
        task_repo.save(task)

    with pytest.raises(ValidationError, match="cannot be reconstructed exactly"):
        saved_factory_manifests(root, db)


def test_saved_factory_manifests_rejects_malformed_factory_spec(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id = _init_db(root)
    workflow = Workflow("wf-bad-spec", project_id, "Broken", WorkflowStatus.BLOCKED)
    WorkflowRepository(db).save(workflow)
    TaskRepository(db).save(
        Task(
            "writer",
            workflow.id,
            "Broken writer",
            "factory_provider_task",
            CostClass.LOCAL,
            status=TaskStatus.PENDING,
            parameters={
                "factory_spec": {"task_id": "writer", "kind": "not-a-real-kind"},
                "manifest_sha256": "a" * 64,
            },
        )
    )

    with pytest.raises(ValidationError, match="cannot be reconstructed exactly"):
        saved_factory_manifests(root, db)
