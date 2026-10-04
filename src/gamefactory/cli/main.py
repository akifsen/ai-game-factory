"""Command line interface for local Factory Core workflows."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
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
    AuditLogRepository,
    ConceptVersionRepository,
    CostLedgerRepository,
    EvidenceRepository,
    ExecutionRepository,
    PaidRequestSnapshotRepository,
    ProductionReadinessRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
    _insert_audit_event,
    _insert_ledger_entry,
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
from gamefactory.cli.factory_commands import (
    add_factory_commands,
    dispatch_factory_command,
    load_factory_registries,
    saved_factory_manifests,
)
from gamefactory.cli.plan_command import run_plan_command
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.accounting.ledger import (
    EntryType,
    LedgerEntry,
    plan_settlement,
    signed_amount,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_any_asset_specification, spec_fingerprint
from gamefactory.core.domain.errors import (
    ConfigurationError,
    FactoryError,
    ProviderUnavailable,
    ReconciliationStateChangedError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    AuditEvent,
    CostClass,
    ExecutionStatus,
    Task,
    TaskStatus,
    generate_id,
    utc_now_iso,
)
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
from gamefactory.workflows.production_readiness import DefaultReadinessProbes, ReadinessProbes
from gamefactory.workflows.recovery import RECOVERY_POLICY, classify


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

    plan_cmd = commands.add_parser(
        "plan",
        help="plan a static_prop asset-spec JSON via Codex (pilot; does not create assets)",
    )
    _add_common(plan_cmd, nested=True)
    plan_cmd.add_argument(
        "request",
        help="natural-language description of one Tide Bastion static prop",
    )
    plan_cmd.add_argument(
        "--output",
        required=True,
        help="write asset-spec-0.4.0 JSON (refuses to overwrite an existing file)",
    )
    plan_cmd.add_argument("--codex-path", help="explicit Codex CLI executable path")
    plan_cmd.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Codex subprocess timeout in seconds (default: 180)",
    )

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

    for name in ("approve", "reject", "request-changes"):
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
        "asset-create", help="create a gated profile-driven asset production workflow"
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

    asset = commands.add_parser("asset", help="profile-aware asset commands")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    asset_profiles = asset_commands.add_parser("profiles", help="list built-in asset profiles")
    _add_common(asset_profiles, nested=True)
    asset_create_alias = asset_commands.add_parser(
        "create", help="create a gated profile-driven asset production workflow"
    )
    _add_asset_create_arguments(asset_create_alias)
    asset_register = asset_commands.add_parser(
        "register-source",
        help="write the source registration for an operator-authored assembly GLB",
    )
    _add_common(asset_register, nested=True)
    asset_register.add_argument("--spec", required=True, help="asset-spec-0.7.0 file")
    asset_register.add_argument("--source", required=True, help="operator-authored GLB")
    asset_register.add_argument(
        "--source-front",
        required=True,
        choices=("-Z", "+Z"),
        help=(
            "front axis of the source file, written as --source-front=-Z or "
            "--source-front=+Z; +Z is what Blender's glTF export produces"
        ),
    )
    asset_register.add_argument("--authoring-tool", required=True, help="e.g. blender")
    asset_register.add_argument("--authoring-tool-version", required=True, help="e.g. 4.0.2")
    asset_register.add_argument("--actor", required=True, help="registering operator")
    asset_register.add_argument("--reason", required=True, help="why this source is registered")
    asset_register.add_argument("--output", required=True, help="registration JSON to write")
    asset_assemble = asset_commands.add_parser(
        "assemble",
        help="create a V0.7 assembly workflow from a registered operator source (no provider)",
    )
    _add_common(asset_assemble, nested=True)
    asset_assemble.add_argument("--spec", required=True, help="asset-spec-0.7.0 file")
    asset_assemble.add_argument("--source", required=True, help="operator-authored GLB")
    asset_assemble.add_argument("--registration", required=True, help="source registration")
    asset_assemble.add_argument(
        "--dry-run", action="store_true", help="validate inputs without creating a workflow"
    )
    asset_reuse = asset_commands.add_parser(
        "reuse",
        help="create a static_prop reuse workflow from an external/manual GLB (no provider)",
    )
    _add_common(asset_reuse, nested=True)
    asset_reuse.add_argument("--spec", required=True, help="asset-spec-0.4.0 static_prop file")
    asset_reuse.add_argument("--source", required=True, help="existing external .glb")
    asset_reuse.add_argument(
        "--provenance", required=True, help="existing-external-source-provenance JSON"
    )
    asset_install = asset_commands.add_parser(
        "install", help="install a reviewed asset revision into the Godot project"
    )
    _add_common(asset_install, nested=True)
    asset_install.add_argument("--source-workflow", required=True)
    asset_install.add_argument("--revision", required=True)
    asset_install.add_argument(
        "--replace-baseline-sha256", action="append", default=[], metavar="PATH=SHA256"
    )
    asset_reuse.add_argument(
        "--dry-run", action="store_true", help="validate inputs without creating a workflow"
    )
    asset_inspect = asset_commands.add_parser("inspect", help="show one asset revision")
    _add_common(asset_inspect, nested=True)
    asset_inspect.add_argument("asset_id")

    asset_concept = asset_commands.add_parser("concept", help="manage concept versions")
    asset_concept_commands = asset_concept.add_subparsers(dest="concept_command", required=True)
    asset_concept_replace = asset_concept_commands.add_parser(
        "replace", help="replace concept before paid production"
    )
    _add_common(asset_concept_replace, nested=True)
    asset_concept_replace.add_argument("--workflow", required=True, help="workflow ID")
    asset_concept_replace.add_argument("--concept", required=True, help="new concept PNG")
    asset_concept_replace.add_argument(
        "--provenance", required=True, help="new concept provenance JSON"
    )
    asset_concept_replace.add_argument("--actor", required=True, help="actor replacing concept")
    asset_concept_replace.add_argument("--reason", required=True, help="reason for replacement")
    asset_concept_replace.add_argument(
        "--concept-source-type",
        choices=("imported", "local_generation"),
        default="imported",
        help="source type for new concept",
    )
    asset_concept_replace.add_argument(
        "--dry-run", action="store_true", help="dry run without changes"
    )

    accounting = commands.add_parser("accounting", help="cost ledger and reconciliation commands")
    accounting_commands = accounting.add_subparsers(dest="accounting_command", required=True)

    ledger_cmd = accounting_commands.add_parser(
        "ledger", help="read-only listing of workflow ledger entries"
    )
    _add_common(ledger_cmd, nested=True)
    ledger_cmd.add_argument("--workflow", required=True, help="workflow ID")

    reconcile_cmd = accounting_commands.add_parser(
        "reconcile", help="reconcile paid operations against provider evidence"
    )
    _add_common(reconcile_cmd, nested=True)
    reconcile_cmd.add_argument("--workflow", required=True, help="workflow ID")
    reconcile_cmd.add_argument("--task", help="optional task ID to limit reconciliation")
    reconcile_cmd.add_argument(
        "--apply", action="store_true", help="apply reconciliation to ledger"
    )
    reconcile_cmd.add_argument("--dry-run", action="store_true", help="explicit dry-run (default)")
    reconcile_cmd.add_argument(
        "--actor", help="actor applying reconciliation (required with --apply)"
    )
    reconcile_cmd.add_argument("--reason", help="reason for reconciliation (required with --apply)")
    reconcile_cmd.add_argument(
        "--plan-hash",
        help="plan hash from a reviewed dry-run; --apply refuses if the current plan differs",
    )

    recovery = commands.add_parser(
        "recovery", help="evidence-driven failure inspection and recovery"
    )
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)

    recovery_inspect = recovery_commands.add_parser(
        "inspect", help="read-only inspection of failed/blocked tasks"
    )
    _add_common(recovery_inspect, nested=True)
    recovery_inspect.add_argument("workflow_id", help="workflow ID")

    recovery_reclassify = recovery_commands.add_parser(
        "reclassify", help="reclassify pre-launch tool failures as retryable"
    )
    _add_common(recovery_reclassify, nested=True)
    recovery_reclassify.add_argument(
        "--execution", required=True, help="execution ID to reclassify"
    )
    recovery_reclassify.add_argument(
        "--apply", action="store_true", help="apply reclassification to database"
    )
    recovery_reclassify.add_argument(
        "--dry-run", action="store_true", help="explicit dry-run (default)"
    )
    recovery_reclassify.add_argument(
        "--actor", help="actor applying reclassification (required with --apply)"
    )
    recovery_reclassify.add_argument(
        "--reason", help="reason for reclassification (required with --apply)"
    )

    add_factory_commands(commands)

    return parser


def _add_asset_create_arguments(parser: argparse.ArgumentParser) -> None:
    _add_common(parser, nested=True)
    parser.add_argument("--spec", required=True, help="strict asset specification YAML or JSON")
    parser.add_argument("--concept", required=True, help="concept PNG")
    parser.add_argument("--provenance", required=True, help="hashed concept provenance JSON")
    parser.add_argument("--provider", choices=("fake", "meshy"), default="fake")
    parser.add_argument(
        "--concept-source-type", choices=("imported", "local_generation"), default="imported"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--budget-reservation",
        type=float,
        help="maximum budget reservation when provider cost is UNKNOWN; not a cost estimate",
    )


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
    from gamefactory.core.domain.asset_profiles import builtin_registry

    entries = {key: entry.to_dict() for key, entry in capabilities.items()}

    def detected_path(key: str) -> str | None:
        value = entries.get(key, {}).get("details", {}).get("executable_path")
        return str(value) if value else None

    # Same lower-level probes as the pre-spend gate; informational only (the exit
    # code is unchanged) and never submits provider work.
    readiness = _readiness_probes_factory()
    summary = getattr(readiness, "capability_summary", None)
    production_readiness = (
        summary(root, detected_path("dcc.blender.detect"), detected_path("engine.godot.detect"))
        if callable(summary)
        else {}
    )
    payload = {
        "project_root": str(root),
        "configuration": config_status,
        "storage": storage,
        "capabilities": entries,
        "asset_profiles": builtin_registry().availability(),
        "production_readiness": production_readiness,
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
    ledger_repo = CostLedgerRepository(db)
    project_spend = ledger_repo.project_net(cfg.project.id)
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
        "accounting": {
            "project_committed_spend": project_spend,
            "project_spend": project_spend,
        },
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
    ledger_repo = CostLedgerRepository(db)
    ledger_entries = ledger_repo.list_by_workflow(workflow_id)
    project_spend = ledger_repo.project_net(wf.project_id)
    op_summaries: dict[str, Any] = {}
    for task in tasks:
        acct = ledger_repo.operation_account(task.id)
        if (
            task.cost_class in (CostClass.PAID, CostClass.METERED, CostClass.EXPENSIVE)
            or acct.reserved_total > 0
            or acct.settled
        ):
            op_summaries[task.id] = acct.to_dict()
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
        "accounting": {
            "project_committed_spend": project_spend,
            "project_spend": project_spend,
            "ledger_entries": [_jsonable(e) for e in ledger_entries],
            "operation_accounts": op_summaries,
            "operation_summaries": op_summaries,
        },
    }


def _db_existing_readonly(root: Path) -> Database:
    """Open initialized state for pure inspection: no migrations, no created files.

    Opening through :func:`_db` migrates the schema, so an inspection of a live
    database written by an older release would change it. Read-only inspection
    therefore requires a database already at the current schema and refuses an
    older one instead of silently migrating it.
    """
    from gamefactory.adapters.persistence.migrations import MIGRATIONS
    from gamefactory.adapters.persistence.read_only_database import ReadOnlyDatabase

    assert_managed_directory(root, ".gamefactory")
    state = assert_managed_directory(root, ".gamefactory/state")
    if not state.is_dir():
        raise ConfigurationError(
            f"Factory project is not initialized at {root}; run 'gamefactory init'."
        )
    db_path = PathGuard(root).resolve_safe_path(".gamefactory/state/factory.db")
    if not db_path.is_file():
        raise ConfigurationError(f"Factory database is missing: {db_path}")
    db = ReadOnlyDatabase(db_path)
    supported = max(version for version, _name, _fn in MIGRATIONS)
    try:
        with db.transaction() as conn:
            applied = {
                row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
    except sqlite3.OperationalError as exc:
        raise ConfigurationError(f"Factory database has no migration history: {db_path}") from exc
    current = max(applied, default=0)
    if current != supported:
        raise ConfigurationError(
            f"Factory database schema is version {current}; this CLI reads version {supported}. "
            "Inspection is read-only and will not migrate it. Back it up and follow the operator "
            "procedure in docs/architecture/accounting.md, or run any mutating command to migrate."
        )
    return db


def _accounting_ledger(db: Database, workflow_id: str) -> tuple[dict[str, Any], int, str | None]:
    wf = WorkflowRepository(db).get(workflow_id)
    if wf is None:
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    ledger_repo = CostLedgerRepository(db)
    entries = ledger_repo.list_by_workflow(workflow_id)
    payload = {
        "workflow_id": workflow_id,
        "project_id": wf.project_id,
        "entry_count": len(entries),
        "entries": [_jsonable(e) for e in entries],
    }
    lines = [f"Cost Ledger for workflow {workflow_id}:"]
    if not entries:
        lines.append("  (no ledger entries)")
    else:
        for e in entries:
            lines.append(
                f"  [{e.created_at}] {e.entry_type.value:<10} amount={e.amount:.2f} {e.cost_unit} "
                f"task={e.task_id} source={e.source} reason={e.reason}"
            )
    return payload, EXIT_SUCCESS, "\n".join(lines)


_RECONCILED_COST_CLASSES = (CostClass.PAID, CostClass.METERED, CostClass.EXPENSIVE)

# Only asset paid generation journals a durable provider intent (claim_intent) before
# any provider contact, and has done so since the task type was introduced. For every
# other paid/metered/expensive task type (e.g. legacy ``paid_generation``) the absence
# of an intent says nothing about whether the provider was called or charged.
_INTENT_JOURNALED_TASK_TYPES = frozenset({"asset_paid_generation"})


def _no_intent_release_decision(
    task: Task, executions: list[Any], workflow_invocations: int, held: float
) -> tuple[bool, bool, str]:
    """Decide whether a hold without a durable intent may be released.

    Returns ``(eligible, manual_evidence_required, reason)``. RELEASE is only allowed
    when the intent-before-contact invariant applies AND every record supports that
    no provider request was ever sent.
    """
    if task.task_type not in _INTENT_JOURNALED_TASK_TYPES:
        return (
            False,
            True,
            f"No durable provider intent; task type '{task.task_type}' does not journal an "
            "intent before provider contact, so a missing intent is not evidence of no "
            "submission. Not eligible: manual provider evidence required",
        )
    # claim_execution creates the RUNNING attempt atomically with the hold, so a RUNNING
    # attempt (not the task status) is the in-flight evidence.
    if any(e.status == ExecutionStatus.RUNNING for e in executions):
        return False, True, "Execution is in-flight; provider evidence is required"
    if any(e.external_op_id for e in executions):
        return (
            False,
            True,
            "An execution records an external operation id; provider evidence is required",
        )
    if workflow_invocations > 0:
        return (
            False,
            True,
            "Provider invocations are recorded for this workflow; provider evidence is required",
        )
    if task.status == TaskStatus.COMPLETED or any(
        e.status == ExecutionStatus.COMPLETED for e in executions
    ):
        return (
            False,
            True,
            "A paid attempt completed without a durable intent; provider evidence is required",
        )
    if held <= 0:
        return False, False, "No intent and no held reservation; nothing to release"
    return (
        True,
        False,
        "No durable intent for asset paid generation (the intent is journaled before any "
        "provider contact), no in-flight attempt, external operation id or recorded "
        "invocation; eligible to release reservation",
    )


def _reconcile_plan(
    db: Database,
    conn: sqlite3.Connection,
    wf: Any,
    task_filter: str | None,
    actor: str | None,
    reason: str | None,
) -> tuple[list[dict[str, Any]], list[LedgerEntry], str]:
    """Evaluate reconciliation from one connection so every read shares one snapshot.

    Returns the per-operation payloads, the ledger entries that would be appended,
    and a fingerprint of the plan (independent of generated ids and timestamps).
    """
    tasks = TaskRepository(db).list_by_workflow(wf.id, conn)
    if task_filter:
        tasks = [t for t in tasks if t.id == task_filter]
        if not tasks:
            raise ConfigurationError(f"Task not found in workflow {wf.id}: {task_filter}")

    ledger_repo = CostLedgerRepository(db)
    intent_repo = ProviderOperationIntentRepository(db)
    exec_repo = ExecutionRepository(db)
    workflow_invocations = ProviderInvocationRepository(db).count(wf.id, conn)

    op_payloads: list[dict[str, Any]] = []
    planned: list[LedgerEntry] = []
    for task in tasks:
        entries = ledger_repo.list_by_task(task.id, conn)
        if task.cost_class not in _RECONCILED_COST_CLASSES and not entries:
            continue

        account = ledger_repo.operation_account(task.id, conn)
        intent = intent_repo.get_by_task(task.id, conn)
        task_executions = exec_repo.list_by_task(task.id, conn)
        recorded_cost = sum(e.cost for e in task_executions)
        held = account.held
        settled = account.settled
        provider_actual = intent.actual_cost if intent else None

        eligible = False
        manual_evidence = False
        proposed: list[LedgerEntry] = []

        if settled:
            eligibility_reason = "Operation is already settled"
        elif intent is not None:
            if intent.status in ("SUCCEEDED", "FAILED") and intent.actual_cost is not None:
                eligible = True
                eligibility_reason = (
                    f"Terminal intent status {intent.status} with verified actual cost "
                    f"{intent.actual_cost}"
                )
                proposed = plan_settlement(
                    account,
                    intent.actual_cost,
                    source="reconciliation",
                    actor=actor or "reconciliation",
                    reason=reason or f"Reconciliation to verified actual cost {intent.actual_cost}",
                    intent_id=intent.id,
                    request_fingerprint=intent.request_fingerprint,
                    cost_unit=intent.cost_unit,
                )
            elif intent.status in ("SUCCEEDED", "FAILED"):
                manual_evidence = True
                eligibility_reason = (
                    "Provider actual cost is unknown; provider evidence is required"
                )
            else:
                manual_evidence = True
                eligibility_reason = (
                    f"Intent status is {intent.status}; provider evidence is required"
                )
        else:
            eligible, manual_evidence, eligibility_reason = _no_intent_release_decision(
                task, task_executions, workflow_invocations, held
            )
            if eligible:
                proposed = [
                    LedgerEntry(
                        id=generate_id("LEDGER"),
                        project_id=wf.project_id,
                        workflow_id=wf.id,
                        task_id=task.id,
                        entry_type=EntryType.RELEASE,
                        amount=held,
                        cost_unit=account.cost_unit,
                        reason=reason
                        or "Release reservation via reconciliation: no provider submission",
                        source="reconciliation_no_submission",
                        actor=actor or "reconciliation",
                        created_at=utc_now_iso(),
                    )
                ]

        planned.extend(proposed)
        op_payloads.append(
            {
                "task_id": task.id,
                "task": task.id,
                "task_type": task.task_type,
                "intent_id": intent.id if intent else None,
                "intent_status": intent.status if intent else None,
                "external_task_id": intent.external_task_id if intent else None,
                "reservation_held": held,
                "held": held,
                "reservation": held,
                "recorded_cost": recorded_cost,
                "legacy_execution_cost": recorded_cost,
                "provider_actual": provider_actual,
                "actual_cost": provider_actual,
                "settled": settled,
                "eligible": eligible,
                "eligibility": "YES" if eligible else "NO",
                "manual_evidence_required": manual_evidence,
                "reason": eligibility_reason,
                "proposed_entries": [_jsonable(e) for e in proposed],
                "net_change": sum(signed_amount(e) for e in proposed),
            }
        )

    material = [
        {
            "task_id": op["task_id"],
            "intent_id": op["intent_id"],
            "intent_status": op["intent_status"],
            "external_task_id": op["external_task_id"],
            "provider_actual": op["provider_actual"],
            "held": op["held"],
            "settled": op["settled"],
            "eligible": op["eligible"],
            "reason": op["reason"],
            "proposed": [
                [pe["entry_type"], pe["amount"], pe["cost_unit"]] for pe in op["proposed_entries"]
            ],
        }
        for op in op_payloads
    ]
    plan_hash = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return op_payloads, planned, plan_hash


def _accounting_reconcile(
    db: Database, args: argparse.Namespace
) -> tuple[dict[str, Any], int, str | None]:
    apply_mode = getattr(args, "apply", False)
    actor = getattr(args, "actor", None)
    reason = getattr(args, "reason", None)
    expected_plan_hash = getattr(args, "plan_hash", None)
    if apply_mode:
        if not actor or not actor.strip():
            raise ValidationError("--apply requires non-empty --actor")
        if not reason or not reason.strip():
            raise ValidationError("--apply requires non-empty --reason")

    wf = WorkflowRepository(db).get(args.workflow)
    if wf is None:
        raise ConfigurationError(f"Workflow not found: {args.workflow}")
    task_filter = getattr(args, "task", None)
    ledger_repo = CostLedgerRepository(db)

    # Preview: one read transaction, so the plan and the balance share a snapshot.
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        op_payloads, planned, plan_hash = _reconcile_plan(db, conn, wf, task_filter, actor, reason)
        spend_before = ledger_repo.project_net(wf.project_id, conn)
    finally:
        conn.rollback()
        conn.close()
    total_net_change = sum(signed_amount(e) for e in planned)
    spend_after = spend_before + total_net_change

    if apply_mode:
        expected = expected_plan_hash or plan_hash
        applied_ops: list[dict[str, Any]] = []
        applied_entries: list[LedgerEntry] = []
        if planned:
            with db.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # Re-evaluate and re-read the balance under the write lock; this is the
                # plan that is applied and the balance the audit event reports.
                applied_ops, applied_entries, fresh_hash = _reconcile_plan(
                    db, conn, wf, task_filter, actor, reason
                )
                if applied_entries and fresh_hash != expected:
                    raise ReconciliationStateChangedError(
                        "Reconciliation state changed since the preview (intent, actual "
                        "cost, reservation or eligibility); nothing was applied. "
                        "Re-run the dry-run and review the new plan.",
                        details={"expected_plan_hash": expected, "current_plan_hash": fresh_hash},
                    )
                if not applied_entries:
                    raise ReconciliationStateChangedError(
                        "Reconciliation state changed since the preview: the previewed "
                        "operations are no longer eligible; nothing was applied. "
                        "Re-run the dry-run.",
                        details={"expected_plan_hash": expected, "current_plan_hash": fresh_hash},
                    )
                if applied_entries:
                    spend_before = ledger_repo.project_net(wf.project_id, conn)
                    for entry in applied_entries:
                        _insert_ledger_entry(conn, entry)
                    spend_after = ledger_repo.project_net(wf.project_id, conn)
                    total_net_change = spend_after - spend_before
                    planned_net = sum(signed_amount(e) for e in applied_entries)
                    if not math.isclose(total_net_change, planned_net, abs_tol=1e-9):
                        raise ReconciliationStateChangedError(
                            "Ledger balance moved inside the reconciliation transaction; "
                            "nothing was applied.",
                            details={"planned_net": planned_net, "observed": total_net_change},
                        )
                    audit = AuditEvent(
                        id=generate_id("AUDIT"),
                        entity_type="Workflow",
                        entity_id=wf.id,
                        action="ACCOUNTING_RECONCILED",
                        actor=actor or "reconciliation",
                        timestamp=utc_now_iso(),
                        details={
                            "workflow_id": wf.id,
                            "project_id": wf.project_id,
                            "spend_before": spend_before,
                            "spend_after": spend_after,
                            "net_change": total_net_change,
                            "plan_hash": fresh_hash,
                            "reconciled_tasks": sorted({e.task_id for e in applied_entries}),
                            "ledger_entry_ids": [e.id for e in applied_entries],
                            "reason": reason,
                        },
                    )
                    _insert_audit_event(conn, audit)

        if not applied_entries:
            payload = {
                "workflow_id": wf.id,
                "project_id": wf.project_id,
                "dry_run": False,
                "applied": False,
                "reconciled": False,
                "operations": op_payloads,
                "plan_hash": plan_hash,
                "spend_before": spend_before,
                "project_spend_before": spend_before,
                "spend_after": spend_before,
                "project_spend_after": spend_before,
                "net_change": 0.0,
                "message": "Nothing to reconcile",
            }
            return payload, EXIT_SUCCESS, "Nothing to reconcile."

        payload = {
            "workflow_id": wf.id,
            "project_id": wf.project_id,
            "dry_run": False,
            "applied": True,
            "reconciled": True,
            "operations": applied_ops,
            "plan_hash": expected,
            "applied_entries": [_jsonable(e) for e in applied_entries],
            "spend_before": spend_before,
            "project_spend_before": spend_before,
            "spend_after": spend_after,
            "project_spend_after": spend_after,
            "net_change": total_net_change,
        }
        reconciled_count = len({e.task_id for e in applied_entries})
        human_msg = (
            f"Applied reconciliation to {reconciled_count} operation(s). "
            f"Project committed spend: {spend_before:.2f} -> {spend_after:.2f}"
        )
        return payload, EXIT_SUCCESS, human_msg

    payload = {
        "workflow_id": wf.id,
        "project_id": wf.project_id,
        "dry_run": True,
        "applied": False,
        "reconciled": False,
        "operations": op_payloads,
        "plan_hash": plan_hash,
        "spend_before": spend_before,
        "project_spend_before": spend_before,
        "spend_after": spend_after,
        "project_spend_after": spend_after,
        "net_change": total_net_change,
    }
    human_lines = [
        f"Reconciliation preview for workflow {wf.id}:",
        f"  Project spend before: {spend_before:.2f}",
        f"  Project spend after:  {spend_after:.2f} (net change: {total_net_change:+.2f})",
        f"  Plan hash: {plan_hash}",
        "",
    ]
    for op in op_payloads:
        human_lines.append(f"Task: {op['task_id']}")
        human_lines.append(f"  Reservation held: {op['reservation_held']:.2f}")
        human_lines.append(f"  Legacy recorded cost: {op['recorded_cost']:.2f}")
        human_lines.append(f"  Provider actual: {op['provider_actual']}")
        human_lines.append(f"  Settled: {op['settled']}")
        eligibility = op["eligibility"]
        if op["manual_evidence_required"]:
            eligibility += ", manual evidence required"
        human_lines.append(f"  Eligible: {eligibility} ({op['reason']})")
        if op["proposed_entries"]:
            human_lines.append("  Proposed entries:")
            for pe in op["proposed_entries"]:
                human_lines.append(f"    {pe['entry_type']} {pe['amount']:.2f} ({pe['reason']})")
        human_lines.append(f"  Net change: {op['net_change']:+.2f}")
        human_lines.append("")
    human_lines.append(
        "Dry run complete; no ledger entries written. Run with --apply --actor <A> "
        f"--reason <R> --plan-hash {plan_hash} to apply exactly this plan."
    )
    return payload, EXIT_SUCCESS, "\n".join(human_lines)


def _recovery_inspect(
    root: Path, db: Database, workflow_id: str
) -> tuple[dict[str, Any], int, str | None]:
    wf = WorkflowRepository(db).get(workflow_id)
    if wf is None:
        raise ConfigurationError(f"Workflow not found: {workflow_id}")

    task_repo = TaskRepository(db)
    exec_repo = ExecutionRepository(db)
    intent_repo = ProviderOperationIntentRepository(db)
    audit_repo = AuditLogRepository(db)
    artifact_repo = ArtifactRepository(db)

    tasks = task_repo.list_by_workflow(workflow_id)
    all_artifacts = artifact_repo.list_by_workflow(workflow_id)

    task_reports: list[dict[str, Any]] = []

    for task in tasks:
        executions = exec_repo.list_by_task(task.id)
        latest_exec = executions[-1] if executions else None

        include_task = task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED) or (
            latest_exec is not None
            and latest_exec.status in (ExecutionStatus.FAILED, ExecutionStatus.UNCERTAIN)
        )
        if not include_task:
            continue

        intent = intent_repo.get_by_task(task.id)
        audits = audit_repo.list_by_entity("Task", task.id)
        if latest_exec is not None:
            audits = audits + audit_repo.list_by_entity("Execution", latest_exec.id)

        task_artifacts = [a for a in all_artifacts if a.task_id == task.id]

        assessment = classify(task, latest_exec, intent, audits, task_artifacts, root)

        latest_exec_info = (
            {
                "id": latest_exec.id,
                "attempt": latest_exec.attempt_number,
                "status": latest_exec.status.value,
                "retryable": bool(latest_exec.retryable),
                "pid": latest_exec.pid,
                "exit_code": latest_exec.exit_code,
                "stdout_present": bool(latest_exec.stdout),
                "stderr_present": bool(latest_exec.stderr),
                "error_message": latest_exec.error_message,
            }
            if latest_exec is not None
            else None
        )

        provider_intent_info = (
            {
                "id": intent.id,
                "status": intent.status,
                "external_task_id": intent.external_task_id,
            }
            if intent is not None
            else None
        )

        task_reports.append(
            {
                "task_id": task.id,
                "task_type": task.task_type,
                "task_status": task.status.value,
                "latest_execution": latest_exec_info,
                "attempts_used": len(executions),
                "max_retries": task.max_retries,
                "classification": assessment.category.value,
                "category": assessment.category.value,
                "evidence": assessment.evidence,
                "eligible_for_reclassification": assessment.eligible_for_reclassification,
                "refusal_reasons": assessment.refusal_reasons,
                "provider_safety": assessment.provider_safety,
                "provider_intent": provider_intent_info,
                "registered_artifacts": [
                    {
                        "id": a.id,
                        "type": a.artifact_type,
                        "sha256": a.content_hash,
                    }
                    for a in task_artifacts
                ],
                "safe_actions": assessment.safe_actions,
            }
        )

    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "tasks": task_reports,
    }
    if len(task_reports) == 1:
        payload["classification"] = task_reports[0]["classification"]
        payload["category"] = task_reports[0]["category"]
        payload["safe_actions"] = task_reports[0]["safe_actions"]

    lines = [f"Recovery inspection for workflow {workflow_id}:"]
    if not task_reports:
        lines.append("  No failed or blocked tasks found.")
    else:
        for t in task_reports:
            lines.append(f"Task: {t['task_id']} ({t['task_type']}) - Status: {t['task_status']}")
            lines.append(f"  Classification: {t['classification']}")
            lines.append(f"  Attempts: {t['attempts_used']}/{t['max_retries'] + 1}")
            if t["latest_execution"]:
                le = t["latest_execution"]
                lines.append(
                    f"  Latest Execution: {le['id']} (attempt {le['attempt']}, status={le['status']}, retryable={le['retryable']})"
                )
                if le["error_message"]:
                    lines.append(f"  Error: {le['error_message']}")
            if t["provider_intent"]:
                pi = t["provider_intent"]
                lines.append(
                    f"  Provider Intent: {pi['id']} status={pi['status']} external_id={pi['external_task_id']}"
                )
            lines.append("  Safe Actions:")
            for act in t["safe_actions"]:
                lines.append(f"    - {act}")
            lines.append("")
    return payload, EXIT_SUCCESS, "\n".join(lines)


def _recovery_reclassify(
    root: Path, db: Database, args: argparse.Namespace
) -> tuple[dict[str, Any], int, str | None]:
    execution_id = args.execution
    apply_mode = getattr(args, "apply", False)
    actor = getattr(args, "actor", None)
    reason = getattr(args, "reason", None)

    if apply_mode:
        if not actor or not actor.strip():
            raise ValidationError("--apply requires non-empty --actor")
        if not reason or not reason.strip():
            raise ValidationError("--apply requires non-empty --reason")

    exec_repo = ExecutionRepository(db)
    execution = exec_repo.get(execution_id)
    if execution is None:
        raise ConfigurationError(f"Execution not found: {execution_id}")

    task_repo = TaskRepository(db)
    task = task_repo.get(execution.task_id)
    if task is None:
        raise ConfigurationError(f"Task not found for execution: {execution_id}")

    latest_attempt = exec_repo.get_latest_attempt(task.id)
    is_latest = latest_attempt is not None and latest_attempt.id == execution.id

    intent_repo = ProviderOperationIntentRepository(db)
    intent = intent_repo.get_by_task(task.id)

    audit_repo = AuditLogRepository(db)
    audits = audit_repo.list_by_entity("Task", task.id) + audit_repo.list_by_entity(
        "Execution", execution.id
    )

    artifact_repo = ArtifactRepository(db)
    all_artifacts = artifact_repo.list_by_workflow(task.workflow_id)
    task_artifacts = [a for a in all_artifacts if a.task_id == task.id]

    assessment = classify(task, execution, intent, audits, task_artifacts, root)

    # Refuse if the execution is not the latest attempt of its task or the task is not FAILED
    if not is_latest:
        assessment.eligible_for_reclassification = False
        assessment.refusal_reasons.append("Execution is not the latest attempt for the task")

    if task.status != TaskStatus.FAILED:
        assessment.eligible_for_reclassification = False
        assessment.refusal_reasons.append(f"Task status is {task.status.value}, expected FAILED")

    all_attempts = exec_repo.list_by_task(task.id)
    attempts_used = len(all_attempts)
    retries_remaining = max(0, task.max_retries + 1 - attempts_used)
    retry_limit_status = {
        "attempts_used": attempts_used,
        "max_retries": task.max_retries,
        "retries_remaining": retries_remaining,
    }

    proposed_mutation = {
        "execution_id": execution.id,
        "field": "retryable",
        "before": 1 if execution.retryable else 0,
        "after": 1,
    }

    audit_preview = {
        "action": "RECOVERY_RECLASSIFIED",
        "entity": "Execution",
        "actor": actor or "<actor>",
        "details": {
            "policy": RECOVERY_POLICY,
            "category": assessment.category.value,
            "evidence": assessment.evidence,
            "reason": reason or "<reason>",
            "before": 0,
            "after": 1,
            "task_id": task.id,
            "workflow_id": task.workflow_id,
        },
    }

    if not apply_mode:
        payload = {
            "execution_id": execution.id,
            "eligible": assessment.eligible_for_reclassification,
            "eligibility": "YES" if assessment.eligible_for_reclassification else "NO",
            "reasons": assessment.refusal_reasons,
            "evidence": assessment.evidence,
            "policy": RECOVERY_POLICY,
            "proposed_mutation": proposed_mutation,
            "audit_event_preview": audit_preview,
            "provider_safety_implications": assessment.provider_safety,
            "retry_limit_status": retry_limit_status,
            "dry_run": True,
            "applied": False,
            "mutated": False,
        }
        human_lines = [
            f"Recovery reclassify preview for execution {execution.id}:",
            f"  Eligible: {'YES' if assessment.eligible_for_reclassification else 'NO'}",
            f"  Category: {assessment.category.value}",
            f"  Policy: {RECOVERY_POLICY}",
        ]
        if assessment.refusal_reasons:
            human_lines.append("  Refusal reasons:")
            for r in assessment.refusal_reasons:
                human_lines.append(f"    - {r}")
        human_lines.append(
            "Dry run complete; no database mutations made. Run with --apply --actor <A> --reason <R> to execute."
        )
        return payload, EXIT_SUCCESS, "\n".join(human_lines)

    # Apply mode:
    if not assessment.eligible_for_reclassification:
        payload = {
            "execution_id": execution.id,
            "eligible": False,
            "eligibility": "NO",
            "reasons": assessment.refusal_reasons,
            "evidence": assessment.evidence,
            "policy": RECOVERY_POLICY,
            "proposed_mutation": proposed_mutation,
            "audit_event_preview": audit_preview,
            "provider_safety_implications": assessment.provider_safety,
            "retry_limit_status": retry_limit_status,
            "dry_run": False,
            "applied": False,
            "mutated": False,
        }
        human = f"Reclassification refused: {'; '.join(assessment.refusal_reasons)}"
        return payload, EXIT_WORKFLOW_FAILURE, human

    assert actor is not None and reason is not None
    audit_event = AuditEvent(
        id=generate_id("AUDIT"),
        entity_type="Execution",
        entity_id=execution.id,
        action="RECOVERY_RECLASSIFIED",
        actor=actor,
        previous_state="0",
        new_state="1",
        details={
            "policy": RECOVERY_POLICY,
            "category": assessment.category.value,
            "evidence": assessment.evidence,
            "reason": reason,
            "before": 0,
            "after": 1,
            "task_id": task.id,
            "workflow_id": task.workflow_id,
        },
    )

    mutated = exec_repo.reclassify_retryable(execution.id, audit_event)
    if not mutated:
        payload = {
            "execution_id": execution.id,
            "eligible": False,
            "eligibility": "NO",
            "reasons": [
                "Execution could not be mutated (it may no longer be FAILED or retryable=0)"
            ],
            "evidence": assessment.evidence,
            "policy": RECOVERY_POLICY,
            "proposed_mutation": proposed_mutation,
            "audit_event_preview": audit_preview,
            "provider_safety_implications": assessment.provider_safety,
            "retry_limit_status": retry_limit_status,
            "dry_run": False,
            "applied": False,
            "mutated": False,
        }
        return payload, EXIT_WORKFLOW_FAILURE, "Reclassification failed; execution was not updated."

    payload = {
        "execution_id": execution.id,
        "eligible": True,
        "eligibility": "YES",
        "reasons": [],
        "evidence": assessment.evidence,
        "policy": RECOVERY_POLICY,
        "proposed_mutation": proposed_mutation,
        "audit_event_preview": audit_preview,
        "provider_safety_implications": assessment.provider_safety,
        "retry_limit_status": retry_limit_status,
        "dry_run": False,
        "applied": True,
        "mutated": True,
    }
    human = f"Successfully reclassified execution {execution.id} as retryable (0 -> 1)."
    return payload, EXIT_SUCCESS, human


def _report(root: Path, db: Database, workflow_id: str) -> tuple[dict[str, Any], int, str]:
    """Point at the static review snapshot. This command does not approve anything."""
    workflow = WorkflowRepository(db).get(workflow_id)
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    from gamefactory.workflows.asset_reuse import (
        build_static_prop_reuse_report,
        workflow_uses_existing_external_reuse,
    )

    if workflow is not None and workflow_uses_existing_external_reuse(tasks):
        payload, human = build_static_prop_reuse_report(root, db, workflow_id)
        return payload, EXIT_SUCCESS, human
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
    specification = parse_any_asset_specification(spec_path.resolve(strict=True))
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
            "profile": specification.bound_profile().profile_id,
            "profile_version": specification.bound_profile().qualified,
            "provider": args.provider,
            "operation": "image-to-3d" if args.provider == "meshy" else "fake-image-to-3d",
            "estimated_cost": "UNKNOWN" if estimate is None else estimate,
            "budget_reservation": reservation,
        }
    )
    message = (
        f"Asset workflow {workflow.id} is {result.status.value}; "
        f"asset={specification.asset_id} revision=r{tasks[0].parameters['revision_number']:03d} "
        f"profile={specification.bound_profile().qualified}"
    )
    if result.pending_approval_id:
        approval = ApprovalRepository(db).get(result.pending_approval_id)
        message += f"; {approval.approval_type if approval else 'approval'} ID={result.pending_approval_id}"
        if approval and approval.approval_type == "paid_generation":
            checkpoint = _asset_approval_checkpoint(root, db, result.pending_approval_id)
            if checkpoint and "human_block" in checkpoint:
                message += f"\n\n{checkpoint['human_block']}"
            else:
                active_concept_ver = ConceptVersionRepository(db).active_for_workflow(workflow.id)
                if active_concept_ver is not None:
                    concept_artifact = ArtifactRepository(db).get(active_concept_ver.artifact_id)
                else:
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


def _cli_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _load_v07_spec(root: Path, value: str) -> Any:
    from gamefactory.core.domain.asset_contracts import AssetSpecificationV07

    specification = parse_any_asset_specification(_cli_path(root, value).resolve(strict=True))
    if not isinstance(specification, AssetSpecificationV07):
        raise ConfigurationError("Assembly commands require an asset-spec-0.7.0 specification")
    return specification


def _register_assembly_source(root: Path, args: argparse.Namespace) -> tuple[Any, int, str]:
    from gamefactory.workflows.assembly_production import build_source_registration

    specification = _load_v07_spec(root, args.spec)
    source = _cli_path(root, args.source).resolve(strict=True)
    registration = build_source_registration(
        specification,
        source,
        source_front=args.source_front,
        authoring_tool=args.authoring_tool,
        authoring_tool_version=args.authoring_tool_version,
        actor=args.actor,
        reason=args.reason,
    )
    output = _cli_path(root, args.output)
    if output.exists():
        raise ConfigurationError(f"Refusing to overwrite existing registration: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(registration, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return (
        {"registration": str(output), **registration},
        EXIT_SUCCESS,
        f"Source registration written: {output} (source_front={args.source_front}, "
        f"sha256={registration['artifact_sha256']})",
    )


def _create_assembly_workflow(
    root: Path, db: Database, args: argparse.Namespace
) -> tuple[dict[str, Any], int, str]:
    from gamefactory.core.domain.assembly_source import parse_source_registration
    from gamefactory.workflows.assembly_production import create_assembly_workflow

    cfg = ConfigLoader.load_config(root)
    specification = _load_v07_spec(root, args.spec)
    source = _cli_path(root, args.source).resolve(strict=True)
    registration = _cli_path(root, args.registration).resolve(strict=True)
    profile = specification.bound_profile()
    if args.dry_run:
        record = parse_source_registration(registration)
        record.check_against_spec(specification)
        record.check_artifact(source.read_bytes())
        payload = {
            "dry_run": True,
            "asset_id": specification.asset_id,
            "profile": profile.qualified,
            "geometry_mode": profile.geometry_mode,
            "source_kind": specification.source_kind,
            "source_front": record.source_front,
            "normalization": "180 deg about +Y at the root"
            if record.source_front == "+Z"
            else "none",
            "specification_hash": spec_fingerprint(specification),
            "source_sha256": record.artifact_sha256,
            "required_approvals": ["final_visual_review"],
            "paid_provider_invocations": 0,
            "workflow_state_mutated": False,
        }
        return payload, EXIT_SUCCESS, "Dry run passed; no workflow state was created."
    engine = _engine(root, db, args.godot_path, args.blender_path, asset_provider_name="local")
    workflow, tasks = create_assembly_workflow(
        cfg.project.id,
        root,
        specification,
        source,
        registration,
        revision_repository=AssetRevisionRepository(db),
    )
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    payload = _result_payload(result)
    payload.update(
        {
            "asset_id": specification.asset_id,
            "revision": tasks[0].parameters["revision_number"],
            "profile": profile.profile_id,
            "profile_version": profile.qualified,
            "source_kind": specification.source_kind,
            "source_front": tasks[0].parameters["source_front"],
            "paid_provider_invocations": 0,
        }
    )
    message = (
        f"Asset workflow {workflow.id} is {result.status.value}; "
        f"asset={specification.asset_id} revision=r{tasks[0].parameters['revision_number']:03d} "
        f"profile={profile.qualified} source=local_operator_assembly"
    )
    if result.pending_approval_id:
        message += f"; final_visual_review ID={result.pending_approval_id}"
    if result.error_message:
        message += f"; {result.error_message}"
    return payload, _result_code(result), message


def _create_reuse_workflow(
    root: Path, db: Database | None, args: argparse.Namespace
) -> tuple[dict[str, Any], int, str]:
    from gamefactory.core.domain.asset_contracts import AssetSpecification
    from gamefactory.workflows.asset_reuse import validate_static_prop_reuse_inputs

    spec_path = _cli_path(root, args.spec).resolve(strict=True)
    specification = parse_any_asset_specification(spec_path)
    if not isinstance(specification, AssetSpecification):
        raise ConfigurationError("Reuse requires an asset-spec-0.4.0 static_prop specification")
    if args.dry_run:
        validated = validate_static_prop_reuse_inputs(
            root, specification, args.source, args.provenance
        )
        payload = {
            "dry_run": True,
            "asset_id": validated["asset_id"],
            "profile": validated["profile"],
            "source_mode": validated["source_mode"],
            "original_provider": validated["original_provider"],
            "specification_hash": validated["specification_hash"],
            "source_sha256": validated["source_sha256"],
            "required_approvals": ["final_visual_review"],
            "paid_provider_invocations": 0,
            "workflow_state_mutated": False,
            "generated_by_this_workflow": False,
            "paid_by_this_workflow": False,
        }
        return payload, EXIT_SUCCESS, "Dry run passed; no workflow state was created."
    if db is None:
        raise ConfigurationError("Reuse workflow creation requires initialized project state")
    from gamefactory.workflows.asset_reuse import create_static_prop_reuse_workflow

    cfg = ConfigLoader.load_config(root)
    validated = validate_static_prop_reuse_inputs(root, specification, args.source, args.provenance)
    source = Path(validated["source_glb"])
    provenance = Path(validated["source_provenance"])
    engine = _engine(root, db, args.godot_path, args.blender_path, asset_provider_name="local")
    profile = specification.bound_profile()
    workflow, tasks = create_static_prop_reuse_workflow(
        cfg.project.id,
        root,
        specification,
        source,
        provenance,
        revision_repository=AssetRevisionRepository(db),
    )
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    payload = _result_payload(result)
    payload.update(
        {
            "asset_id": specification.asset_id,
            "revision": tasks[0].parameters["revision_number"],
            "profile": profile.qualified,
            "source_mode": "existing_external",
            "paid_provider_invocations": 0,
        }
    )
    message = (
        f"Asset reuse workflow {workflow.id} is {result.status.value}; "
        f"asset={specification.asset_id} revision=r{tasks[0].parameters['revision_number']:03d} "
        f"profile={profile.qualified} source=existing_external"
    )
    if result.pending_approval_id:
        message += f"; final_visual_review ID={result.pending_approval_id}"
    if result.error_message:
        message += f"; {result.error_message}"
    return payload, _result_code(result), message


def _asset_provider_for_workflow(db: Database, workflow_id: str) -> str | None:
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    if any(task.task_type in {"asset_assembly_prepare", "asset_reuse_prepare"} for task in tasks):
        return "local"
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
    active_concept_ver = ConceptVersionRepository(db).active_for_workflow(approval.workflow_id)
    if active_concept_ver is not None:
        concept = ArtifactRepository(db).get(active_concept_ver.artifact_id)
    else:
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
    checkpoint: dict[str, Any] = {
        "asset_id": params.get("asset_id"),
        "revision": params.get("revision_number"),
        "profile_id": params.get("profile_id"),
        "profile_version": params.get("profile_version"),
        "profile_qualified": params.get("profile_qualified"),
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
    if params.get("source_kind") == "local_operator_assembly":
        # Operator assemblies never involve a provider, a concept or money.
        checkpoint.update(
            {
                "provider": None,
                "provider_status": "NOT_USED",
                "operation": "local_operator_assembly",
                "estimate": 0,
                "budget_reservation": None,
                "source_sha256": params.get("source_glb_hash"),
                "source_front": params.get("source_front"),
                "paid": False,
            }
        )
    if params.get("source_mode") == "existing_external":
        checkpoint.update(
            {
                "provider": None,
                "provider_status": "NOT_USED",
                "operation": "existing_external_reuse",
                "estimate": 0,
                "budget_reservation": None,
                "source_sha256": params.get("source_glb_hash"),
                "original_provider": params.get("original_provider"),
                "paid": False,
                "generated_by_this_workflow": False,
            }
        )
    if approval.approval_type == "paid_generation":
        snap_record = PaidRequestSnapshotRepository(db).get_active_for_workflow(
            approval.workflow_id
        )
        readiness_record = ProductionReadinessRepository(db).get_active_for_workflow(
            approval.workflow_id
        )
        snapshot_sha = (
            approval.paid_request_snapshot_hash
            or params.get("paid_request_snapshot_sha256")
            or (snap_record.snapshot_sha256 if snap_record else None)
        )
        snapshot_request: dict[str, Any] = {}
        if "paid_request_snapshot" in params and isinstance(params["paid_request_snapshot"], dict):
            snapshot_request = params["paid_request_snapshot"].get("request", {})
        elif snap_record:
            try:
                snap_content = json.loads(snap_record.canonical_json)
                snapshot_request = snap_content.get("request", {})
            except Exception:
                pass
        readiness_sha = params.get("production_readiness_report_sha256") or (
            readiness_record.report_sha256 if readiness_record else None
        )
        readiness_result = readiness_record.result if readiness_record else "UNKNOWN"
        estimated_cost = (
            params.get("provider_estimate")
            if params.get("provider_estimate") is not None
            else "UNKNOWN"
        )
        reservation = float(params.get("cost", params.get("budget_reservation", 0.0)))
        policy_ceiling = {
            "max_operation_cost": cfg.policies.max_operation_cost,
            "project_budget": cfg.policies.project_budget,
        }

        checkpoint["snapshot_sha256"] = snapshot_sha
        checkpoint["snapshot_request"] = snapshot_request
        checkpoint["readiness_report_sha256"] = readiness_sha
        checkpoint["readiness_result"] = readiness_result
        checkpoint["estimated_cost"] = estimated_cost
        checkpoint["reservation"] = reservation
        checkpoint["policy_ceiling"] = policy_ceiling

        req_lines = [f"  {k}: {v}" for k, v in snapshot_request.items()]
        req_block = "\n".join(req_lines) if req_lines else "  (none)"
        rev_num = params.get("revision_number", 1)
        human_block = (
            "PAID GENERATION APPROVAL REQUIRED\n"
            f"Asset: {params.get('asset_id')}\n"
            f"Revision: r{int(rev_num):03d}\n"
            f"Provider: {params.get('provider')}\n"
            "Operation: image-to-3d\n"
            f"Request Snapshot:\n{req_block}\n"
            f"Request SHA-256: {snapshot_sha}\n"
            f"Estimated Cost: {estimated_cost}\n"
            f"Reservation: {reservation}\n"
            f"Policy Ceiling: max_operation_cost={cfg.policies.max_operation_cost}, project_budget={cfg.policies.project_budget}\n"
            f"Readiness: {readiness_result} ({readiness_sha})\n"
            "Approval: PENDING"
        )
        checkpoint["human_block"] = human_block

    return checkpoint


def _engine(
    root: Path,
    db: Database,
    godot_path: str | None = None,
    blender_path: str | None = None,
    *,
    asset_provider_name: str | None = None,
    allow_paid_calls: bool = False,
    factory_registries: tuple[Any, Any, Any] | None = None,
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
    elif asset_provider_name == "local":
        from gamefactory.workflows.assembly_production import LocalAssemblyNoProvider

        asset_provider = LocalAssemblyNoProvider()
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
    # Register the complete local runtime surface on every process entry point,
    # including resume/approval. This keeps authorization policy and handlers
    # consistent across initial execution and recovery.
    from gamefactory.workflows.gameplay_quality import register_gameplay_quality_handlers
    from gamefactory.workflows.project_operations import register_project_operation_handlers

    register_gameplay_quality_handlers(
        engine.handler_registry,
        root,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.process_runner,
    )
    register_project_operation_handlers(
        engine.handler_registry,
        root,
        engine.art_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        engine.process_runner,
    )
    from gamefactory.workflows.asset_installation import register_asset_installation_handlers

    register_asset_installation_handlers(
        engine.handler_registry,
        root,
        db,
        engine.art_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
    )
    if factory_registries is None:
        factory_registries = load_factory_registries(root, saved_factory_manifests(root, db))[:3]
    from gamefactory.workflows.factory_workflow import register_factory_handlers

    executors, agents, gates = factory_registries
    register_factory_handlers(
        engine.handler_registry,
        project_root=root,
        db=db,
        executor_registry=executors,
        agent_registry=agents,
        gate_registry=gates,
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
            readiness_probes=_readiness_probes_factory(),
        )
        register_asset_production_handlers(engine.handler_registry, asset_handlers)
    return engine


def _readiness_probes_factory() -> ReadinessProbes:
    """Real pre-spend readiness probes; tests replace this factory by monkeypatching."""
    return DefaultReadinessProbes()


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


def _doctor_text(payload: dict[str, Any]) -> str:
    lines = ["Asset Profiles"]
    for row in payload.get("asset_profiles", []):
        if row["status"] == "AVAILABLE":
            lines.append(f"✓ {row['qualified']}")
        else:
            lines.append(f"{row['profile_id']:<18} UNSUPPORTED")
    lines.append("")
    lines.append("Tools")
    capabilities = payload.get("capabilities", {})

    def present(name: str) -> bool:
        status = str(capabilities.get(name, {}).get("status", "UNAVAILABLE"))
        return status in {"AVAILABLE", "NOT_VERIFIED"}

    lines.append(f"{'✓' if present('dcc.blender.detect') else '○'} Blender")
    lines.append(f"{'✓' if present('engine.godot.detect') else '○'} Godot")
    meshy = capabilities.get("asset.3d.meshy", {})
    meshy_status = str(meshy.get("status", "UNAVAILABLE"))
    if meshy_status == "AVAILABLE":
        lines.append("○ Meshy — configured / paid approval required")
    elif meshy_status == "MISCONFIGURED":
        lines.append("○ Meshy — not configured / paid approval required")
    else:
        lines.append("○ Meshy — unavailable / paid approval required")
    readiness = payload.get("production_readiness") or {}
    if readiness:
        lines.append("")
        lines.append("Production Readiness Capabilities")
        for key, label in (
            ("provider", "Provider"),
            ("blender", "Blender"),
            ("godot", "Godot"),
            ("workspace", "Workspace"),
        ):
            row = readiness.get(key, {})
            lines.append(f"{label:<10} {row.get('status', 'UNKNOWN'):<14} {row.get('reason', '')}")
    return "\n".join(lines)


def _profile_catalog_text(rows: list[dict[str, str]]) -> str:
    lines = ["Asset Profiles", ""]
    for row in rows:
        if row["status"] == "AVAILABLE":
            lines.append(
                f"{row['profile_id']:<18} AVAILABLE {row.get('geometry_mode', '')}".rstrip()
            )
        else:
            lines.append(f"{row['profile_id']:<18} UNSUPPORTED")
    return "\n".join(lines)


def _asset_inspect(db: Database, asset_id: str) -> dict[str, Any]:
    revision = AssetRevisionRepository(db).get_latest(asset_id)
    if revision is None:
        raise ConfigurationError(f"No stored revision for asset {asset_id}")
    workflow = WorkflowRepository(db).get(revision.workflow_id)
    if workflow is None:
        raise ConfigurationError(f"Workflow for asset {asset_id} is missing")
    tasks = TaskRepository(db).list_by_workflow(revision.workflow_id)
    prepare = next(
        (
            task
            for task in tasks
            if task.task_type in {"asset_prepare", "asset_assembly_prepare", "asset_reuse_prepare"}
        ),
        None,
    )
    if prepare is None:
        raise ConfigurationError(f"Asset {asset_id} has no specification task")
    specification = parse_any_asset_specification(prepare.parameters["specification"])
    profile = specification.bound_profile()
    current = next(
        (task for task in tasks if task.status.value not in {"COMPLETED", "SKIPPED"}),
        None,
    )
    if prepare.task_type == "asset_reuse_prepare":
        params = prepare.parameters
        return {
            "asset_id": asset_id,
            "revision": revision.revision_id,
            "profile": profile.profile_id,
            "profile_version": profile.qualified,
            "workflow": workflow.id,
            "workflow_status": workflow.status.value,
            "current_gate": current.task_type if current is not None else workflow.status.value,
            "source_mode": "existing_external",
            "original_provider": params.get("original_provider"),
            "source_sha256": params.get("source_glb_hash"),
            "source_provenance_hash": params.get("source_provenance_hash"),
            "specification_hash": params.get("specification_hash"),
            "paid": False,
            "concept_versions": [],
        }
    if prepare.task_type == "asset_assembly_prepare":
        params = prepare.parameters
        return {
            "asset_id": asset_id,
            "revision": revision.revision_id,
            "profile": profile.profile_id,
            "profile_version": profile.qualified,
            "workflow": workflow.id,
            "workflow_status": workflow.status.value,
            "current_gate": current.task_type if current is not None else workflow.status.value,
            "source_kind": "local_operator_assembly",
            "source_sha256": params.get("source_glb_hash"),
            "source_front": params.get("source_front"),
            "paid": False,
            "concept_versions": [],
        }
    cv_repo = ConceptVersionRepository(db)
    rows = cv_repo.list_for_revision(asset_id, revision.revision_number)
    if rows:
        concept_versions = [
            {
                "version": r.version,
                "status": r.status,
                "sha256": r.content_hash,
                "created_at": r.created_at,
                "actor": r.actor,
            }
            for r in rows
        ]
    else:
        artifacts = ArtifactRepository(db).list_by_workflow(revision.workflow_id)
        concept_arts = [a for a in artifacts if a.artifact_type == "asset-concept"]
        concept_sha = (
            concept_arts[0].content_hash if concept_arts else (revision.concept_hash or "")
        )
        created_at = concept_arts[0].created_at if concept_arts else revision.created_at
        concept_versions = [
            {
                "version": 1,
                "status": "ACTIVE",
                "sha256": concept_sha,
                "created_at": created_at,
                "actor": "system",
            }
        ]

    return {
        "asset_id": asset_id,
        "revision": revision.revision_id,
        "profile": profile.profile_id,
        "profile_version": profile.qualified,
        "workflow": workflow.id,
        "workflow_status": workflow.status.value,
        "current_gate": current.task_type if current is not None else workflow.status.value,
        "concept_versions": concept_versions,
    }


def _asset_inspect_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Asset: {payload['asset_id']}",
        f"Revision: {payload['revision']}",
        f"Profile: {payload['profile']}",
        f"Profile Version: {payload['profile_version']}",
        f"Workflow: {payload['workflow']}",
        f"Current Gate: {payload['current_gate']}",
    ]
    if payload.get("source_kind") == "local_operator_assembly":
        lines.append(
            f"Source: local_operator_assembly sha256={payload['source_sha256']} "
            f"source_front={payload['source_front']} paid=false"
        )
    if payload.get("source_mode") == "existing_external":
        lines.append(
            f"Source: existing_external sha256={payload['source_sha256']} "
            f"provider={payload.get('original_provider')} paid=false (no new provider spend)"
        )
    if "concept_versions" in payload and payload["concept_versions"]:
        lines.append("Concept Versions:")
        for cv in payload["concept_versions"]:
            lines.append(
                f"  v{cv['version']} [{cv['status']}] sha256={cv['sha256']} actor={cv['actor']} ({cv['created_at']})"
            )
    return "\n".join(lines)


def _asset_resume_checkpoint_suffix(checkpoint: dict[str, Any]) -> str:
    base = (
        f"; {checkpoint['approval_type']} approval {checkpoint['approval_id']}"
        f"; asset={checkpoint['asset_id']} revision=r{int(checkpoint['revision']):03d}"
        f" profile={checkpoint.get('profile_qualified') or 'unbound'}"
    )
    if checkpoint.get("operation") == "existing_external_reuse":
        return (
            f"{base}"
            f"; source=existing_external sha256={checkpoint.get('source_sha256')}"
            f" original_provider={checkpoint.get('original_provider')}"
            f"; provider=None status={checkpoint['provider_status']}"
            f"; operation={checkpoint['operation']} estimate={checkpoint['estimate']}"
            f"; paid=false"
            f"; resume: {checkpoint['resume_command']}"
        )
    return (
        f"{base}"
        f"; concept={checkpoint['concept_path']} sha256={checkpoint['concept_sha256']}"
        f"; provider={checkpoint['provider']} status={checkpoint['provider_status']}"
        f"; operation={checkpoint['operation']} estimate={checkpoint['estimate']}"
        f"; budget reservation={checkpoint['budget_reservation']}"
        f"; resume: {checkpoint['resume_command']}"
    )


def _asset_concept_replace(root: Path, db: Database, args: argparse.Namespace) -> dict[str, Any]:
    provider_name = _asset_provider_for_workflow(db, args.workflow)
    engine = _engine(
        root,
        db,
        getattr(args, "godot_path", None),
        getattr(args, "blender_path", None),
        asset_provider_name=provider_name,
        allow_paid_calls=True,
    )
    from gamefactory.workflows.concept_versions import replace_concept

    concept_path = Path(args.concept)
    if not concept_path.is_absolute():
        concept_path = root / concept_path
    prov_path = Path(args.provenance)
    if not prov_path.is_absolute():
        prov_path = root / prov_path

    return replace_concept(
        root,
        db,
        engine,
        args.workflow,
        concept_path,
        prov_path,
        actor=args.actor,
        reason=args.reason,
        concept_source_type=getattr(args, "concept_source_type", "imported"),
        dry_run=getattr(args, "dry_run", False),
    )


def _asset_concept_replace_text(payload: dict[str, Any]) -> str:
    if payload.get("dry_run"):
        return (
            f"[DRY RUN] Concept replacement planned for asset {payload['asset_id']} (revision r{payload['revision']:03d}):\n"
            f"  Version: v{payload['old_version']} -> v{payload['new_version']}\n"
            f"  Concept SHA-256: {payload['old_concept_sha256']} -> {payload['new_concept_sha256']}\n"
            f"  Reopened tasks: {', '.join(payload['reopened_tasks'])}\n"
            f"  No changes applied."
        )
    return (
        f"Concept replaced for asset {payload['asset_id']} (revision r{payload['revision']:03d}):\n"
        f"  Version: v{payload['old_version']} -> v{payload['new_version']}\n"
        f"  Concept SHA-256: {payload['old_concept_sha256']} -> {payload['new_concept_sha256']}\n"
        f"  Approval: {payload['new_approval_id']} (PENDING)\n"
        f"\nNext steps:\n"
        f"  gamefactory approve {payload['new_approval_id']}\n"
        f"  gamefactory resume {payload['workflow_id']}"
    )


def _dispatch(args: argparse.Namespace) -> tuple[Any, int, str | None]:
    root = _resolve_root(args.project)
    custom_commands = {
        "new",
        "discover",
        "factory",
        "test-game",
        "performance-review",
        "build",
        "release",
        "editor",
        "run-scene",
        "operation",
    }
    if args.command in custom_commands:
        read_only_factory_command = args.command == "factory" and (
            args.factory_command == "manifest"
            and args.manifest_action in {"create", "preflight"}
            or args.factory_command == "providers"
        )
        requires_db = (
            args.command
            in {
                "test-game",
                "performance-review",
                "factory",
                "build",
                "release",
                "editor",
                "run-scene",
                "operation",
            }
            and not read_only_factory_command
        )
        # The branch above keeps manifest authoring and provider configuration
        # free of database migrations; stateful actions initialize explicitly.
        db = _db(root) if requires_db else None
        dispatched = dispatch_factory_command(args, root, db)
        if dispatched is not None:
            return dispatched
    if args.command == "doctor":
        payload, code = _doctor(root, args.godot_path, args.blender_path)
        return payload, code, _doctor_text(payload)
    if args.command == "plan":
        return run_plan_command(root, args)
    if args.command == "asset" and args.asset_command == "register-source":
        return _register_assembly_source(root, args)
    if args.command == "asset" and args.asset_command == "profiles":
        from gamefactory.core.domain.asset_profiles import builtin_registry

        registry = builtin_registry()
        rows = registry.availability()
        modes = {p.qualified: p.geometry_mode for p in registry.available_v07}
        for row in rows:
            if row["qualified"] in modes:
                row["geometry_mode"] = modes[row["qualified"]]
        return {"asset_profiles": rows}, EXIT_SUCCESS, _profile_catalog_text(rows)
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
        "request-changes",
        "inspect",
        "report",
        "asset-create",
        "asset",
        "accounting",
        "recovery",
    }:
        if (
            args.command == "asset"
            and getattr(args, "asset_command", None) == "reuse"
            and getattr(args, "dry_run", False)
        ):
            return _create_reuse_workflow(root, None, args)
        # Explicit path overrides are passed through detection; no failed explicit path falls back.
        read_only = (args.command == "recovery" and args.recovery_command == "inspect") or (
            args.command == "accounting" and args.accounting_command == "ledger"
        )
        db = _db_existing_readonly(root) if read_only else _db(root)
        if args.command == "recovery":
            if args.recovery_command == "inspect":
                return _recovery_inspect(root, db, args.workflow_id)
            if args.recovery_command == "reclassify":
                return _recovery_reclassify(root, db, args)
        if args.command == "accounting":
            if args.accounting_command == "ledger":
                return _accounting_ledger(db, args.workflow)
            if args.accounting_command == "reconcile":
                return _accounting_reconcile(db, args)
        if args.command == "asset-create" or (
            args.command == "asset" and args.asset_command == "create"
        ):
            return _create_asset_workflow(root, db, args)
        if args.command == "asset" and args.asset_command == "assemble":
            return _create_assembly_workflow(root, db, args)
        if args.command == "asset" and args.asset_command == "reuse":
            return _create_reuse_workflow(root, db, args)
        if args.command == "asset" and args.asset_command == "install":
            from gamefactory.workflows.asset_installation import build_asset_installation_workflow

            replacements: dict[str, str] = {}
            for item in args.replace_baseline_sha256:
                path, separator, digest = item.partition("=")
                if not separator or not path or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValidationError("--replace-baseline-sha256 must be PATH=lowercase-SHA256")
                replacements[path] = digest
            cfg = ConfigLoader.load_config(root)
            workflow, tasks = build_asset_installation_workflow(
                cfg.project.id,
                root,
                args.source_workflow,
                args.revision,
                db=db,
                replace_baseline_sha256=replacements or None,
            )
            engine = _engine(root, db, args.godot_path, args.blender_path)
            engine.register_workflow(workflow, tasks)
            result = engine.run_workflow(workflow.id)
            payload = _result_payload(result)
            return (
                payload,
                _result_code(result),
                f"Asset installation {workflow.id}: {result.status.value}",
            )
        if args.command == "asset" and args.asset_command == "inspect":
            payload = _asset_inspect(db, args.asset_id)
            return payload, EXIT_SUCCESS, _asset_inspect_text(payload)
        if args.command == "asset" and args.asset_command == "concept":
            if getattr(args, "concept_command", None) == "replace":
                payload = _asset_concept_replace(root, db, args)
                return payload, EXIT_SUCCESS, _asset_concept_replace_text(payload)
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
                    if (
                        checkpoint.get("approval_type") == "paid_generation"
                        and "human_block" in checkpoint
                    ):
                        message += f"\n\n{checkpoint['human_block']}"
                    else:
                        message += _asset_resume_checkpoint_suffix(checkpoint)
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
        if args.command in {"approve", "reject", "request-changes"}:
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
                approval_workflow = engine.wf_repo.get(approval.workflow_id)
                task = engine.task_repo.get(approval.task_id)
                if approval_workflow is None or task is None:
                    raise ConfigurationError(
                        f"Approval '{approval.id}' is not bound to a current workflow task"
                    )
                current_inputs = engine.approval_inputs(approval_workflow, task)
            if args.command == "approve":
                decided = ApprovalService.approve(
                    approval, args.actor, args.comment, current_inputs=current_inputs
                )
            elif args.command == "reject":
                decided = ApprovalService.reject(approval, args.actor, args.comment)
            else:
                decided = ApprovalService.request_changes(approval, args.actor, args.comment)
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
            "RECONCILIATION_STATE_CHANGED": EXIT_APPROVAL_BLOCKED,
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
