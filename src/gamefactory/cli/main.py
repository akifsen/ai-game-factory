"""Command line interface for local Factory Core workflows."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from gamefactory import __version__
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.external.meshy_cli import MeshyAssetGenerationProvider, MeshyCliRunner
from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.capabilities.registry import CapabilityRegistry
from gamefactory.cli.exit_codes import (
    EXIT_APPROVAL_BLOCKED,
    EXIT_CONFIG_ERROR,
    EXIT_INTERNAL_ERROR,
    EXIT_SUCCESS,
    EXIT_TOOL_UNAVAILABLE,
    EXIT_WORKFLOW_FAILURE,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification, spec_fingerprint
from gamefactory.core.domain.errors import (
    ConfigurationError,
    FactoryError,
    ProviderUnavailable,
    ValidationError,
)
from gamefactory.core.domain.models import AuditEvent, CostClass, generate_id
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.redaction import redactor
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    create_asset_production_workflow,
    register_asset_production_handlers,
)
from gamefactory.workflows.definitions import (
    create_demo_workflow,
    create_failure_workflow,
    create_paid_safety_workflow,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.godot_capture import (
    create_godot_capture_workflow,
    register_godot_capture_handlers,
)
from gamefactory.workflows.godot_verification import (
    create_godot_verification_workflow,
    register_godot_handlers,
)
from gamefactory.workflows.ports import AssetGenerationProvider


def _add_common(parser: argparse.ArgumentParser, *, nested: bool = False) -> None:
    default = argparse.SUPPRESS if nested else None
    parser.add_argument(
        "-p", "--project", default=default, help="project directory (defaults to current directory)"
    )
    parser.add_argument("--godot-path", default=default, help="explicit Godot executable path")
    parser.add_argument("--blender-path", default=default, help="explicit Blender executable path")
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if nested else False,
        dest="as_json",
        help="emit machine-readable JSON",
    )


_JSON_ERRORS = False


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if _JSON_ERRORS:
            print(
                json.dumps({"error": "USAGE_ERROR", "message": redactor.redact_text(message)}),
                file=sys.stderr,
            )
        else:
            self.print_usage(sys.stderr)
            print(f"{self.prog}: error: {message}", file=sys.stderr)
        self.exit(EXIT_CONFIG_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="gamefactory", description="Local-first game development workflow orchestration"
    )
    parser.add_argument("--version", action="version", version=f"gamefactory {__version__}")
    _add_common(parser)
    commands = parser.add_subparsers(dest="command")

    for name in ("doctor", "init", "status", "approvals", "artifacts"):
        cmd = commands.add_parser(name)
        _add_common(cmd, nested=True)
        if name in ("approvals", "artifacts"):
            cmd.add_argument("--workflow")

    report = commands.add_parser("report")
    _add_common(report, nested=True)
    report.add_argument("--workflow", required=True)

    run = commands.add_parser("run", help="run a built-in demonstrator workflow")
    _add_common(run, nested=True)
    run.add_argument(
        "kind", choices=("demo", "failure", "paid-safety", "godot-verify", "godot-capture")
    )
    run.add_argument("--scenario", help="strict Godot scenario JSON")

    resume = commands.add_parser("resume")
    _add_common(resume, nested=True)
    resume.add_argument("workflow_id")

    retry = commands.add_parser("retry")
    _add_common(retry, nested=True)
    retry.add_argument("workflow_id")
    retry.add_argument("task_id")

    for name in ("approve", "reject"):
        cmd = commands.add_parser(name)
        _add_common(cmd, nested=True)
        cmd.add_argument("approval_id")
        cmd.add_argument("--comment")
        cmd.add_argument(
            "--actor", default=getpass.getuser(), help="name recorded for the decision"
        )

    inspect = commands.add_parser("inspect")
    _add_common(inspect, nested=True)
    inspect.add_argument("workflow_id")

    asset_create = commands.add_parser(
        "asset-create", help="create a gated static-prop production workflow"
    )
    _add_common(asset_create, nested=True)
    asset_create.add_argument("--spec", required=True, help="strict asset-spec-0.4.0 YAML or JSON")
    asset_create.add_argument("--concept", required=True, help="concept PNG")
    asset_create.add_argument("--provenance", required=True, help="hashed concept provenance JSON")
    asset_create.add_argument("--provider", choices=("fake", "meshy"), default="fake")
    asset_create.add_argument(
        "--concept-source-type", choices=("imported", "local_generation"), default="imported"
    )
    asset_create.add_argument("--dry-run", action="store_true")
    asset_create.add_argument(
        "--budget-reservation",
        type=float,
        help="maximum budget reservation when provider cost is UNKNOWN; not a cost estimate",
    )
    return parser


def _resolve_root(project: str | None) -> Path:
    target = Path(project or Path.cwd()).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        raise ConfigurationError(f"Project directory does not exist: {target}")
    return ConfigLoader.find_project_root(target) or target


def _db(root: Path) -> Database:
    assert_managed_directory(root, ".gamefactory")
    state = assert_managed_directory(root, ".gamefactory/state")
    if not state.is_dir():
        raise ConfigurationError(
            f"Factory project is not initialized at {root}; run 'gamefactory init'."
        )
    db_path = PathGuard(root).resolve_safe_path(".gamefactory/state/factory.db")
    db = Database(db_path)
    MigrationRunner(db).apply_all()
    return db


def _load_runtime(root: Path, godot_path: str | None, blender_path: str | None) -> tuple[Any, Any]:
    cfg = ConfigLoader.load_config(root)
    godot = godot_path if godot_path is not None else cfg.engine.executable_path
    blender = blender_path if blender_path is not None else cfg.dcc.blender_path
    return cfg, CapabilityRegistry().discover(root, godot, blender)


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _emit(payload: Any, as_json: bool, human: str | None = None, stream: Any = None) -> None:
    out = stream or sys.stdout
    safe_payload = redactor.redact_data(_jsonable(payload))
    if as_json:
        print(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")), file=out)
    elif human is not None:
        print(redactor.redact_text(human), file=out)
    else:
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2), file=out)


def _result_payload(result: Any) -> dict[str, Any]:
    payload = {
        "workflow_id": result.workflow_id,
        "status": result.status.value,
        "pending_approval_id": result.pending_approval_id,
        "completed_tasks": result.completed_tasks,
        "failed_tasks": result.failed_tasks,
        "blocked_tasks": result.blocked_tasks,
        "error_message": result.error_message,
    }
    if getattr(result, "error_code", None) is not None:
        payload["error_code"] = result.error_code
    return payload


def _result_code(result: Any) -> int:
    status = result.status.value
    if status == "BLOCKED":
        return EXIT_APPROVAL_BLOCKED
    if status == "FAILED":
        return EXIT_WORKFLOW_FAILURE
    return EXIT_SUCCESS


def _doctor(
    root: Path, godot_path: str | None, blender_path: str | None
) -> tuple[dict[str, Any], int]:
    cfg = None
    config_status: dict[str, Any]
    config_path = root / ".gamefactory" / "factory.yml"
    try:
        if not config_path.exists():
            cfg = None
            user_config = ConfigLoader._load_user_config()
            user_engine = user_config.get("engine", {})
            user_dcc = user_config.get("dcc", {})
            capabilities = CapabilityRegistry().discover(
                root,
                godot_path if godot_path is not None else user_engine.get("executable_path"),
                blender_path if blender_path is not None else user_dcc.get("blender_path"),
            )
            config_status = {
                "status": "NOT_INITIALIZED",
                "project_root": str(root),
                "message": "Run 'gamefactory init' inside an existing game project.",
                "user_config": "LOADED" if user_config else "DEFAULTS",
            }
        else:
            cfg, capabilities = _load_runtime(root, godot_path, blender_path)
            config_status = {
                "status": "OK",
                "project_root": str(root),
                "project_id": cfg.project.id,
            }
    except (ConfigurationError, OSError) as exc:
        cfg = None
        capabilities = CapabilityRegistry().discover(root, godot_path, blender_path)
        config_status = {"status": "ERROR", "message": str(exc)}
    storage: dict[str, Any]
    try:
        with tempfile.TemporaryDirectory(prefix="gamefactory-doctor-") as temporary:
            test_db = Database(Path(temporary) / "doctor.db")
            MigrationRunner(test_db).apply_all()
            test_db.connect().close()
        storage = {"status": "OK", "message": "SQLite create, migrate, and reopen succeeded"}
    except Exception as exc:
        storage = {"status": "ERROR", "message": f"Local storage check failed: {exc}"}
    entries = {key: entry.to_dict() for key, entry in capabilities.items()}
    payload = {
        "project_root": str(root),
        "configuration": config_status,
        "storage": storage,
        "capabilities": entries,
    }
    bad_explicit = any(
        entry["status"] == "MISCONFIGURED"
        for key, entry in entries.items()
        if key in {"engine.godot.detect", "dcc.blender.detect"}
    )
    code = (
        EXIT_CONFIG_ERROR
        if config_status["status"] == "ERROR"
        else EXIT_TOOL_UNAVAILABLE
        if bad_explicit or storage["status"] == "ERROR"
        else EXIT_SUCCESS
    )
    return payload, code


def _status(root: Path, godot_path: str | None, blender_path: str | None) -> dict[str, Any]:
    cfg, capabilities = _load_runtime(root, godot_path, blender_path)
    db = _db(root)
    workflows = WorkflowRepository(db).list_by_project(cfg.project.id)
    approvals = ApprovalRepository(db).list_pending()
    executions: list[Any] = []
    for workflow in workflows:
        for task in TaskRepository(db).list_by_workflow(workflow.id):
            executions.extend(ExecutionRepository(db).list_by_task(task.id))
    godot_inspection = GodotAdapter().inspect_project(root)
    return {
        "project": {
            "id": cfg.project.id,
            "name": cfg.project.name,
            "root": str(root),
            "engine": cfg.engine.type,
            "targets": cfg.targets,
            "godot_project": godot_inspection.is_project,
            "main_scene": godot_inspection.main_scene,
        },
        "capabilities": {k: v.to_dict() for k, v in capabilities.items()},
        "workflows": [_jsonable(w) for w in workflows],
        "pending_approvals": [_jsonable(a) for a in approvals],
        "recent_executions": [_jsonable(e) for e in executions[-20:]],
    }


def _inspect(db: Database, workflow_id: str) -> dict[str, Any]:
    wf = WorkflowRepository(db).get(workflow_id)
    if wf is None:
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    task_repo = TaskRepository(db)
    tasks = task_repo.list_by_workflow(workflow_id)
    executions = [e for task in tasks for e in ExecutionRepository(db).list_by_task(task.id)]
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    evidence = [e for task in tasks for e in EvidenceRepository(db).list_by_task(task.id)]
    gates = [g for task in tasks for g in QualityGateRepository(db).list_by_task(task.id)]
    approvals = ApprovalRepository(db).list_by_workflow(workflow_id)
    asset_intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    return {
        "workflow": _jsonable(wf),
        "tasks": [_jsonable(t) for t in tasks],
        "executions": [_jsonable(e) for e in executions],
        "artifacts": [_jsonable(a) for a in artifacts],
        "evidence": [_jsonable(e) for e in evidence],
        "gates": [_jsonable(g) for g in gates],
        "approvals": [_jsonable(a) for a in approvals],
        "paid_provider_invocations": ProviderInvocationRepository(db).count(workflow_id)
        + sum(1 for intent in asset_intents if intent.external_task_id),
        "paid_provider_invocations_unknown": any(
            intent.status in {"SUBMITTING", "UNCERTAIN"} and not intent.external_task_id
            for intent in asset_intents
        ),
        "paid_provider_operations": [_jsonable(item) for item in asset_intents],
    }


def _report(root: Path, db: Database, workflow_id: str) -> tuple[dict[str, Any], int, str]:
    """Point at the static review snapshot. This command does not approve anything."""
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is not None and workflow.name.startswith("Asset production:"):
        from gamefactory.workflows.asset_evidence import export_asset_evidence_bundle

        base = root / ".gamefactory" / "reports" / workflow_id
        base.mkdir(parents=True, exist_ok=True)
        number = 1
        while (base / f"snapshot-{number:03d}").exists():
            number += 1
        bundle = export_asset_evidence_bundle(
            root, db, workflow_id, base / f"snapshot-{number:03d}"
        )
        return (
            {
                "workflow_id": workflow_id,
                "bundle": str(bundle),
                "manifest": str(bundle / "manifest.json"),
                "review_html": str(bundle / "index.html"),
            },
            EXIT_SUCCESS,
            f"Asset evidence bundle: {bundle}",
        )
    pages = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-review-html"
    ]
    if not pages:
        raise ConfigurationError(f"No capture review page is recorded for {workflow_id}")
    page = max(pages, key=lambda item: item.created_at)
    path = (root / page.relative_path).resolve()
    path.relative_to(root.resolve())
    receipts = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "godot-review-receipt"
    ]
    payload = {
        "workflow_id": workflow_id,
        "review_html": str(path),
        "report_sha256": page.content_hash,
        "human_review": "RECEIPT_PRESENT" if receipts else "PENDING",
        "note": "The HTML file is a snapshot and does not itself grant approval.",
    }
    return payload, EXIT_SUCCESS, f"Review page: {path}"


def _run_workflow(
    root: Path,
    db: Database,
    kind: str,
    scenario_path: str | None = None,
    godot_path: str | None = None,
) -> Any:
    cfg = ConfigLoader.load_config(root)
    if kind in {"godot-verify", "godot-capture"}:
        if not scenario_path:
            raise ConfigurationError(f"run {kind} requires --scenario <scenario.json>")
        configured = godot_path if godot_path is not None else cfg.engine.executable_path
        if configured is None:
            configured = os.environ.get("GAMEFACTORY_GODOT_PATH")
        adapter = GodotAdapter()
        if configured is None:
            configured = adapter.find_candidate_executable()
        if configured is None:
            raise ProviderUnavailable("Godot executable was not found", provider="godot")
        executable = Path(configured).expanduser().resolve(strict=False)
        if not executable.is_file():
            raise ProviderUnavailable(
                f"Configured Godot executable does not exist: {executable}", provider="godot"
            )
        scenario = Path(scenario_path).expanduser()
        if not scenario.is_absolute():
            scenario = root / scenario
        creator = (
            create_godot_capture_workflow
            if kind == "godot-capture"
            else create_godot_verification_workflow
        )
        workflow, tasks = creator(cfg.project.id, root, executable, scenario)
        engine = _engine(root, db)
        engine.register_workflow(workflow, tasks)
        return engine.run_workflow(workflow.id)
    if scenario_path:
        raise ConfigurationError(
            "--scenario is only valid with 'run godot-verify' or 'run godot-capture'"
        )
    creators = {
        "demo": create_demo_workflow,
        "failure": create_failure_workflow,
        "paid-safety": create_paid_safety_workflow,
    }
    workflow, tasks = creators[kind](cfg.project.id)
    engine = _engine(root, db)
    engine.register_workflow(workflow, tasks)
    return engine.run_workflow(workflow.id)


def _create_asset_workflow(
    root: Path, db: Database, args: argparse.Namespace
) -> tuple[dict[str, Any], int, str]:
    cfg = ConfigLoader.load_config(root)
    spec_path = Path(args.spec).expanduser()
    if not spec_path.is_absolute():
        spec_path = root / spec_path
    specification = parse_asset_specification(spec_path.resolve(strict=True))
    concept = Path(args.concept).expanduser()
    if not concept.is_absolute():
        concept = root / concept
    provenance = Path(args.provenance).expanduser()
    if not provenance.is_absolute():
        provenance = root / provenance
    concept = concept.resolve(strict=True)
    provenance = provenance.resolve(strict=True)
    from gamefactory.adapters.images.concept_ingest import ingest_concept_image

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="gamefactory-asset-dry-run-") as temp:
            ingest_concept_image(
                concept,
                Path(temp) / "concept.png",
                spec_fingerprint(specification),
                sidecar_provenance_path=provenance,
                source_type=args.concept_source_type,
            )
        capabilities = CapabilityRegistry().discover(
            root,
            args.godot_path if args.godot_path is not None else cfg.engine.executable_path,
            args.blender_path if args.blender_path is not None else cfg.dcc.blender_path,
        )
        reservation_preview = (
            None
            if args.provider == "fake"
            else args.budget_reservation
            if args.budget_reservation is not None
            else cfg.policies.max_operation_cost
        )
        payload = {
            "dry_run": True,
            "asset_id": specification.asset_id,
            "specification": specification.model_dump(mode="json"),
            "specification_hash": spec_fingerprint(specification),
            "concept_sha256": hashlib.sha256(concept.read_bytes()).hexdigest(),
            "concept_state": "VALIDATED_PENDING_HUMAN_APPROVAL",
            "provider": args.provider,
            "operation": "image-to-3d" if args.provider == "meshy" else "fake-image-to-3d",
            "estimated_cost": "UNKNOWN" if args.provider == "meshy" else 5.0,
            "budget_reservation": reservation_preview,
            "required_approvals": ["concept_review", "paid_generation", "final_visual_review"],
            "expected_steps": [
                "validate and retain specification, concept, and provenance",
                "human concept approval",
                "separately approved provider generation and raw GLB retention",
                "Blender processing and independent GLB validation",
                "real Godot import, physics collision check, and rendered captures",
                "human final runtime visual review",
            ],
            "readiness": {
                "image_generation": capabilities["image.generate"].status.value,
                "godot": capabilities["engine.godot.import"].status.value,
                "blender": capabilities["dcc.blender.process"].status.value,
                "meshy_cli": capabilities["asset.3d.generate"].status.value,
            },
            "paid_provider_invocations": 0,
            "workflow_state_mutated": False,
            "api_requests": 0,
        }
        return (
            payload,
            EXIT_SUCCESS,
            "Dry run passed; no workflow state or provider request was created.",
        )
    if args.provider == "fake":
        estimate = 5.0
        reservation = None
    else:
        estimate = None
        reservation = args.budget_reservation
        if reservation is None:
            reservation = cfg.policies.max_operation_cost
        if reservation <= 0:
            raise ConfigurationError(
                "Meshy estimate is UNKNOWN and policy max_operation_cost is zero; supply a positive --budget-reservation"
            )
    engine = _engine(
        root, db, args.godot_path, args.blender_path, asset_provider_name=args.provider
    )
    workflow, tasks = create_asset_production_workflow(
        cfg.project.id,
        root,
        specification,
        concept,
        provenance,
        provider_name=args.provider,
        provider_estimate=estimate,
        budget_reservation=reservation,
        concept_source_type=args.concept_source_type,
        revision_repository=AssetRevisionRepository(db),
    )
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    payload = _result_payload(result)
    payload.update(
        {
            "asset_id": specification.asset_id,
            "revision": tasks[0].parameters["revision_number"],
            "provider": args.provider,
            "operation": "image-to-3d" if args.provider == "meshy" else "fake-image-to-3d",
            "estimated_cost": "UNKNOWN" if estimate is None else estimate,
            "budget_reservation": reservation,
        }
    )
    message = f"Asset workflow {workflow.id} is {result.status.value}; asset={specification.asset_id} revision=r{tasks[0].parameters['revision_number']:03d}"
    if result.pending_approval_id:
        approval = ApprovalRepository(db).get(result.pending_approval_id)
        message += f"; {approval.approval_type if approval else 'approval'} ID={result.pending_approval_id}"
        if approval and approval.approval_type == "paid_generation":
            concept_artifact = next(
                (
                    a
                    for a in ArtifactRepository(db).list_by_workflow(workflow.id)
                    if a.artifact_type == "asset-concept"
                ),
                None,
            )
            message += f'; provider={args.provider}; operation=image-to-3d; estimate={"UNKNOWN" if estimate is None else estimate}; concept_sha256={concept_artifact.content_hash if concept_artifact else "missing"}; resume: gamefactory --project "{root}" resume {workflow.id}'
        else:
            message += f"; review the concept artifact, then approve {result.pending_approval_id} and resume {workflow.id}"
    return payload, _result_code(result), message


def _asset_provider_for_workflow(db: Database, workflow_id: str) -> str | None:
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    for task in tasks:
        if task.task_type == "asset_paid_generation":
            value = task.parameters.get("provider")
            return str(value) if value in {"fake", "meshy"} else None
    return None


def _asset_approval_checkpoint(root: Path, db: Database, approval_id: str) -> dict[str, Any] | None:
    approval = ApprovalRepository(db).get(approval_id)
    if approval is None:
        return None
    task = TaskRepository(db).get(approval.task_id)
    if task is None or task.task_type not in {
        "asset_concept_review",
        "asset_paid_generation",
        "asset_final_review",
    }:
        return None
    params = task.parameters
    concept = next(
        (
            item
            for item in ArtifactRepository(db).list_by_workflow(approval.workflow_id)
            if item.artifact_type == "asset-concept"
        ),
        None,
    )
    cfg = ConfigLoader.load_config(root)
    capabilities = CapabilityRegistry().discover(
        root, cfg.engine.executable_path, cfg.dcc.blender_path
    )
    provider_capability = capabilities.get("asset.3d.generate")
    return {
        "asset_id": params.get("asset_id"),
        "revision": params.get("revision_number"),
        "approval_type": approval.approval_type,
        "approval_id": approval.id,
        "concept_path": concept.relative_path if concept else None,
        "concept_sha256": concept.content_hash if concept else None,
        "provider": params.get("provider"),
        "provider_status": provider_capability.status.value
        if provider_capability
        else "NOT_VERIFIED",
        "operation": "image-to-3d",
        "estimate": params.get("provider_estimate")
        if params.get("provider_estimate") is not None
        else "UNKNOWN",
        "budget_reservation": params.get("budget_reservation"),
        "resume_command": f'gamefactory --project "{root}" resume {approval.workflow_id}',
    }


def _engine(
    root: Path,
    db: Database,
    godot_path: str | None = None,
    blender_path: str | None = None,
    *,
    asset_provider_name: str | None = None,
    allow_paid_calls: bool = False,
) -> WorkflowEngine:
    """Build the legacy fake-provider engine or one explicitly selected asset provider."""
    config = ConfigLoader.load_config(root)
    policy = config.policies
    policy_engine = PolicyEngine(
        PolicyRule(
            require_approval_for_paid=policy.paid_operations_require_approval,
            require_approval_for_destructive=policy.destructive_operations_require_approval,
            require_approval_for_repo_write=policy.require_approval_for_repo_write,
            require_approval_for_process_execution=policy.require_approval_for_process_execution,
            max_operation_cost=policy.max_operation_cost,
            project_budget=policy.project_budget,
        )
    )
    intent_repo = ProviderOperationIntentRepository(db)
    asset_provider: AssetGenerationProvider
    if asset_provider_name == "meshy":
        asset_provider = MeshyAssetGenerationProvider(
            MeshyCliRunner(), intent_repo=intent_repo, allow_paid_calls=allow_paid_calls
        )
    elif asset_provider_name == "fake":
        asset_provider = FakeAssetGenerationProvider(
            name="fake", cost_class=CostClass.PAID, intent_repo=intent_repo
        )
    else:
        asset_provider = FakeAssetGenerationProvider()
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=policy_engine,
        asset_provider=asset_provider,
    )
    register_godot_handlers(
        engine.handler_registry,
        root,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.process_runner,
    )
    register_godot_capture_handlers(
        engine.handler_registry,
        root,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.process_runner,
    )
    if asset_provider_name is not None:
        asset_handlers = AssetProductionHandlers(
            root=root,
            artifacts=engine.art_repo,
            revisions=AssetRevisionRepository(db),
            approvals=engine.app_repo,
            intents=intent_repo,
            evidence=engine.evi_repo,
            gates=engine.gate_repo,
            executions=engine.exec_repo,
            artifact_manager=engine.artifact_mgr,
            provider=asset_provider,
            blender_path=blender_path or ConfigLoader.load_config(root).dcc.blender_path,
            godot_path=godot_path or ConfigLoader.load_config(root).engine.executable_path,
            runner=engine.process_runner,
        )
        register_asset_production_handlers(engine.handler_registry, asset_handlers)
    return engine


def _list_project_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, child_dirs, filenames in os.walk(root):
        child_dirs[:] = [
            name for name in child_dirs if name not in {".git", ".godot", ".gamefactory"}
        ]
        files.extend(Path(directory) / filename for filename in filenames)
    return files


def _init(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    # Discovery starts at a subdirectory and anchors initialization at the game root.
    discovered = ConfigLoader.find_project_root(root)
    target = discovered or root
    # Gather read-only project metadata before creating any Factory-owned files.
    inspection = GodotAdapter().inspect_project(target)
    project_name = inspection.project_name if inspection.is_project else target.name
    project_files = _list_project_files(target)
    script_count = sum(path.suffix == ".gd" for path in project_files)
    scene_count = sum(path.suffix == ".tscn" for path in project_files)
    test_indicators = [
        str(path.relative_to(target))
        for path in project_files
        if path.name.lower().startswith("test_")
        or path.name.lower().endswith(".test.gd")
        or any(part.lower() in {"test", "tests"} for part in path.relative_to(target).parts[:-1])
    ]
    has_exports = (target / "export_presets.cfg").is_file()
    git_root = next(
        (parent for parent in (target, *target.parents) if (parent / ".git").exists()), None
    )
    user_config = ConfigLoader._load_user_config()
    user_engine = user_config.get("engine", {})
    configured_godot = (
        args.godot_path if args.godot_path is not None else user_engine.get("executable_path")
    )
    if configured_godot is None and (target / ".gamefactory" / "factory.yml").is_file():
        configured_godot = ConfigLoader.load_config(target).engine.executable_path
    godot_detection = GodotAdapter().detect_engine(configured_godot)
    actual_root, config, existed = ConfigLoader.init_project(
        target,
        project_name=project_name,
        custom_godot_path=args.godot_path,
        custom_blender_path=args.blender_path,
    )
    discovery = {
        "schema_version": 1,
        "project_root": str(actual_root),
        "project": {
            "name": project_name,
            "engine": "godot" if inspection.is_project else config.engine.type,
        },
        "godot": {
            "project_detected": inspection.is_project,
            "version": godot_detection.version,
            "status": godot_detection.status,
            "main_scene": inspection.main_scene,
        },
        "counts": {
            "scripts": script_count,
            "scenes": scene_count,
            "test_files": len(test_indicators),
        },
        "test_indicators": test_indicators,
        "exports_configured": has_exports,
        "git": {
            "repository_detected": git_root is not None,
            "root": str(git_root) if git_root else None,
        },
    }
    discovery_path = PathGuard(actual_root).resolve_safe_path(".gamefactory/discovery.json")
    try:
        with discovery_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(discovery, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        pass
    return {
        "project_root": str(actual_root),
        "project_id": config.project.id,
        "project_name": config.project.name,
        "already_initialized": existed,
        "godot_project": inspection.is_project,
        "main_scene": inspection.main_scene,
        "scripts": script_count,
        "scenes": scene_count,
        "test_files": len(test_indicators),
        "exports": has_exports,
        "git": git_root is not None,
        "discovery_file": str(discovery_path),
    }


def _dispatch(args: argparse.Namespace) -> tuple[Any, int, str | None]:
    root = _resolve_root(args.project)
    if args.command == "doctor":
        payload, code = _doctor(root, args.godot_path, args.blender_path)
        return payload, code, None
    if args.command == "init":
        payload = _init(root, args)
        return (
            payload,
            EXIT_SUCCESS,
            f"Initialized {payload['project_name']} at {payload['project_root']}"
            + (" (already initialized)" if payload["already_initialized"] else ""),
        )
    if args.command in {
        "status",
        "approvals",
        "artifacts",
        "run",
        "resume",
        "retry",
        "approve",
        "reject",
        "inspect",
        "report",
        "asset-create",
    }:
        # Explicit path overrides are passed through detection; no failed explicit path falls back.
        db = _db(root)
        if args.command == "asset-create":
            return _create_asset_workflow(root, db, args)
        if args.command == "status":
            return _status(root, args.godot_path, args.blender_path), EXIT_SUCCESS, None
        if args.command == "run":
            ConfigLoader.load_config(root)
            result = _run_workflow(
                root, db, args.kind, getattr(args, "scenario", None), args.godot_path
            )
            payload = _result_payload(result)
            message = f"Workflow {result.workflow_id}: {result.status.value}"
            if result.pending_approval_id:
                message += (
                    f"; approve with gamefactory approve {result.pending_approval_id}, then "
                    f"gamefactory resume {result.workflow_id}"
                )
            if result.error_message:
                message += f"; {result.error_message}"
            return payload, _result_code(result), message
        if args.command == "resume":
            provider_name = _asset_provider_for_workflow(db, args.workflow_id)
            result = _engine(
                root,
                db,
                args.godot_path,
                args.blender_path,
                asset_provider_name=provider_name,
                allow_paid_calls=True,
            ).run_workflow(args.workflow_id)
            payload = _result_payload(result)
            message = f"Workflow {result.workflow_id}: {result.status.value}"
            if result.pending_approval_id:
                checkpoint = _asset_approval_checkpoint(root, db, result.pending_approval_id)
                if checkpoint is not None:
                    payload["asset_checkpoint"] = checkpoint
                    message += (
                        f"; {checkpoint['approval_type']} approval {checkpoint['approval_id']}"
                        f"; asset={checkpoint['asset_id']} revision=r{int(checkpoint['revision']):03d}"
                        f"; concept={checkpoint['concept_path']} sha256={checkpoint['concept_sha256']}"
                        f"; provider={checkpoint['provider']} status={checkpoint['provider_status']}"
                        f"; operation={checkpoint['operation']} estimate={checkpoint['estimate']}"
                        f"; budget reservation={checkpoint['budget_reservation']}"
                        f"; resume: {checkpoint['resume_command']}"
                    )
                else:
                    message += f"; approve with gamefactory approve {result.pending_approval_id}, then gamefactory resume {result.workflow_id}"
            return (
                payload,
                _result_code(result),
                message,
            )
        if args.command == "retry":
            provider_name = _asset_provider_for_workflow(db, args.workflow_id)
            result = _engine(
                root,
                db,
                args.godot_path,
                args.blender_path,
                asset_provider_name=provider_name,
                allow_paid_calls=True,
            ).retry_task(args.workflow_id, args.task_id)
            return (
                _result_payload(result),
                _result_code(result),
                f"Workflow {result.workflow_id}: {result.status.value}",
            )
        if args.command in {"approve", "reject"}:
            repo = ApprovalRepository(db)
            approval = repo.get(args.approval_id)
            if approval is None:
                raise ConfigurationError(f"Approval not found: {args.approval_id}")
            current_inputs = None
            if args.command == "approve":
                provider_name = _asset_provider_for_workflow(db, approval.workflow_id)
                engine = _engine(
                    root, db, args.godot_path, args.blender_path, asset_provider_name=provider_name
                )
                workflow = engine.wf_repo.get(approval.workflow_id)
                task = engine.task_repo.get(approval.task_id)
                if workflow is None or task is None:
                    raise ConfigurationError(
                        f"Approval '{approval.id}' is not bound to a current workflow task"
                    )
                current_inputs = engine.approval_inputs(workflow, task)
            decided = (
                ApprovalService.approve(
                    approval, args.actor, args.comment, current_inputs=current_inputs
                )
                if args.command == "approve"
                else ApprovalService.reject(approval, args.actor, args.comment)
            )
            event = AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Approval",
                entity_id=decided.id,
                action=decided.status.value,
                actor=decided.actor or "operator",
                previous_state="PENDING",
                new_state=decided.status.value,
                details={
                    "workflow_id": decided.workflow_id,
                    "task_id": decided.task_id,
                    "comment": decided.comment,
                },
            )
            if not repo.decide_if_pending(decided, event):
                raise ValidationError(
                    f"Approval '{decided.id}' is no longer pending; inspect its current status."
                )
            return (
                _jsonable(decided),
                EXIT_SUCCESS,
                f"Approval {decided.id}: {decided.status.value}",
            )
        if args.command == "approvals":
            repo = ApprovalRepository(db)
            approval_values = (
                repo.list_pending(args.workflow) if args.workflow else repo.list_pending()
            )
            records = [_jsonable(value) for value in approval_values]
            human = (
                "\n".join(
                    f"{v['id']}  {v['status']}  {v['approval_type']} ({v['cost_class']})  "
                    f"workflow={v['workflow_id']} task={v['task_id']} requested={v['requested_at']}\n  "
                    f"{v['reason']}  artifacts={','.join(v['artifact_ids'])}"
                    for v in records
                )
                or "No pending approvals."
            )
            return records, EXIT_SUCCESS, human
        if args.command == "report":
            return _report(root, db, args.workflow)
        if args.command == "artifacts":
            cfg = ConfigLoader.load_config(root)
            workflows = (
                [args.workflow]
                if args.workflow
                else [w.id for w in WorkflowRepository(db).list_by_project(cfg.project.id)]
            )
            artifact_values = [
                item
                for wf_id in workflows
                for item in ArtifactRepository(db).list_by_workflow(wf_id)
            ]
            records = [_jsonable(value) for value in artifact_values]
            human = (
                "\n".join(
                    f"{v['id']}  {v['artifact_type']}  {v['relative_path']}  producer={v['producer']}  "
                    f"workflow={v['workflow_id']} task={v['task_id']} validation={v['validation_state']} "
                    f"created={v['created_at']}"
                    for v in records
                )
                or "No artifacts found."
            )
            return records, EXIT_SUCCESS, human
        if args.command == "inspect":
            return _inspect(db, args.workflow_id), EXIT_SUCCESS, None
    raise ConfigurationError("Choose a command. Run 'gamefactory --help' for available commands.")


def main(argv: Sequence[str] | None = None) -> int:
    global _JSON_ERRORS
    for output in (sys.stdout, sys.stderr):
        reconfigure = getattr(output, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    _JSON_ERRORS = "--json" in (list(argv) if argv is not None else sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload, code, human = _dispatch(args)
        _emit(payload, args.as_json, human)
        return code
    except FactoryError as exc:
        code_by_error = {
            "RECONCILIATION_REQUIRED": EXIT_APPROVAL_BLOCKED,
            "LOCK_ERROR": EXIT_APPROVAL_BLOCKED,
            "APPROVAL_REQUIRED": EXIT_APPROVAL_BLOCKED,
            "PROVIDER_UNAVAILABLE": EXIT_TOOL_UNAVAILABLE,
            "TOOL_EXECUTION_ERROR": EXIT_TOOL_UNAVAILABLE,
            "WORKFLOW_ERROR": EXIT_WORKFLOW_FAILURE,
        }
        code = code_by_error.get(getattr(exc, "code", ""), EXIT_CONFIG_ERROR)
        _emit(
            {
                "error": getattr(exc, "code", exc.__class__.__name__),
                "message": str(exc),
                "details": {},
            },
            args.as_json,
            f"Error: {exc}",
            sys.stderr,
        )
        return code
    except (OSError, ValueError) as exc:
        _emit(
            {"error": exc.__class__.__name__, "message": str(exc), "details": {}},
            args.as_json,
            f"Error: {exc}",
            sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    except sqlite3.OperationalError as exc:
        raw = str(exc).lower()
        if "locked" in raw or "busy" in raw:
            advice = "Local database is busy or locked. Wait for the active Factory command to finish, then retry."
        elif "readonly" in raw or "read-only" in raw:
            advice = (
                "Local database is read-only. Check write access to .gamefactory/state and retry."
            )
        else:
            advice = f"Local database operation failed: {exc}"
        _emit(
            {"error": "DATABASE_ERROR", "message": advice, "details": {}},
            args.as_json,
            f"Error: {advice}",
            sys.stderr,
        )
        return EXIT_INTERNAL_ERROR
    except Exception as exc:
        # No tracebacks in ordinary CLI output; diagnostics are available with the error class.
        _emit(
            {"error": exc.__class__.__name__, "message": str(exc), "details": {}},
            args.as_json,
            f"Internal error: {exc}",
            sys.stderr,
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
