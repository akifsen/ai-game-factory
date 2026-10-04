"""CLI composition for generalized Factory workflows and native Godot operations.

The legacy ``plan`` pilot remains in :mod:`plan_command`; commands here use the
versioned general workflow contract and explicit operator-owned provider config.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol

from gamefactory.adapters.agents.base import ProviderReadiness
from gamefactory.adapters.agents.codex import CodexAgentConfig, CodexAgentProvider
from gamefactory.adapters.agents.openai_media import (
    OpenAIAudioSpeechProvider,
    OpenAIImageProvider,
    OpenAIMediaConfig,
)
from gamefactory.adapters.agents.process_provider import (
    OperatorProcessAgentProvider,
    ProcessProviderConfig,
)
from gamefactory.adapters.engines.godot_staging import _is_reparse
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.generic_operations import GenericOperationRepository
from gamefactory.adapters.projects.discovery import discover_project
from gamefactory.adapters.projects.onboarding import create_godot_project
from gamefactory.agents.registry import AgentRegistry
from gamefactory.cli.exit_codes import (
    EXIT_APPROVAL_BLOCKED,
    EXIT_SUCCESS,
    EXIT_TOOL_UNAVAILABLE,
    EXIT_WORKFLOW_FAILURE,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.agent_contracts import AgentDefinition
from gamefactory.core.domain.errors import ConfigurationError, ValidationError
from gamefactory.core.domain.factory_workflow import (
    FactoryWorkflowManifest,
    WorkflowTaskSpec,
)
from gamefactory.core.domain.models import generate_id
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.factory_workflow import (
    CapabilityExecutor,
    ExecutorRegistry,
    GateExecutorRegistry,
    build_workflow,
    inspect_factory_apply_journal,
    preflight_manifest,
    recover_factory_apply_journal,
)
from gamefactory.workflows.gameplay_quality import (
    create_gameplay_quality_workflow,
)
from gamefactory.workflows.project_operations import (
    build_project_operation_workflow,
)

_CONFIG_RELATIVE = ".gamefactory/providers.json"
_CONFIG_LIMIT = 256_000
_MANIFEST_LIMIT = 1_000_000


class _ConfiguredCapabilityExecutor(CapabilityExecutor, Protocol):
    provider_id: str
    capabilities: frozenset[str]

    def readiness(self) -> ProviderReadiness: ...


def _workflow_code(status: str) -> int:
    if status in {"BLOCKED", "PENDING_APPROVAL"}:
        return EXIT_APPROVAL_BLOCKED
    if status in {"FAILED", "CANCELLED"}:
        return EXIT_WORKFLOW_FAILURE
    return EXIT_SUCCESS


def _preflight_code(status: str) -> int:
    return (
        EXIT_SUCCESS
        if status == "READY"
        else EXIT_APPROVAL_BLOCKED
        if status == "BLOCKED"
        else EXIT_TOOL_UNAVAILABLE
    )


def _validate_declared_outputs(
    game_write: bool, output_scopes: list[str], expected_artifacts: list[str]
) -> None:
    if game_write and (
        not output_scopes or not expected_artifacts or set(output_scopes) != set(expected_artifacts)
    ):
        raise ValidationError(
            "--game-write requires --artifact paths to exactly match --output-scope paths"
        )


def add_factory_commands(commands: Any) -> None:
    """Add new, discovery, generic factory, gameplay and build command groups."""
    new = commands.add_parser("new", help="create and initialize a new Godot project")
    _add_common(new)
    new.add_argument("name")
    new.add_argument("--path", required=True, help="new project directory; parent must exist")
    new.add_argument("--dimension", choices=("2d", "3d"), default="2d")

    discover = commands.add_parser("discover", help="inspect a Godot project without running it")
    _add_common(discover)
    discover.add_argument("--path", default=".")
    discover.add_argument("--include-git", action="store_true")

    factory = commands.add_parser("factory", help="manage general Factory manifests and providers")
    _add_common(factory)
    family = factory.add_subparsers(dest="factory_command", required=True)
    manifest = family.add_parser("manifest", help="create or preflight a strict workflow manifest")
    manifest_action = manifest.add_subparsers(dest="manifest_action", required=True)
    create = manifest_action.add_parser("create", help="write a one-task workflow manifest")
    _add_common(create)
    create.add_argument("--output", required=True)
    create.add_argument("--workflow-name", required=True)
    create.add_argument(
        "--task-id", help="optional stable task identity; defaults to a generated unique id"
    )
    create.add_argument("--task-name", default="Main task")
    create.add_argument(
        "--family",
        choices=("design", "feature", "level", "ui", "image", "audio", "vision"),
        default="feature",
    )
    create.add_argument(
        "--kind", choices=("general", "code", "design", "image", "audio", "vision"), default="code"
    )
    create.add_argument("--executor", required=True)
    create.add_argument("--agent", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="PATH=PURPOSE",
        help="declared project input; repeat as needed",
    )
    create.add_argument("--prompt-template", default="codex.readonly")
    create.add_argument("--prompt-version", default="1.0.0")
    create.add_argument("--capability", action="append", default=[])
    create.add_argument("--allowed-tool", action="append", default=[])
    create.add_argument("--forbidden-tool", action="append", default=[])
    create.add_argument("--output-scope", action="append", default=[])
    create.add_argument("--artifact", action="append", default=[])
    create.add_argument(
        "--gate", action="append", choices=("code", "gameplay", "visual", "performance"), default=[]
    )
    create.add_argument("--acceptance", action="append", default=[])
    create.add_argument("--game-write", action="store_true")
    create.add_argument(
        "--cost-class",
        choices=("LOCAL", "FREE_EXTERNAL", "METERED", "PAID", "EXPENSIVE"),
        required=True,
    )
    create.add_argument("--max-cost", type=float, required=True)
    create.add_argument("--currency", default="USD")
    create.add_argument(
        "--cost-unit", choices=("request", "task", "minute", "token", "artifact"), default="request"
    )
    create.add_argument("--max-tool-calls", type=int, default=0)
    create.add_argument("--network-allowed", action="store_true")
    create.add_argument("--process-execution-allowed", action="store_true")
    create.add_argument("--repository-write-allowed", action="store_true")
    create.add_argument(
        "--parameters", help="bounded project-relative JSON with deterministic gate inputs"
    )
    create.add_argument("--timeout", type=float, default=300)

    preflight = manifest_action.add_parser(
        "preflight", help="check local provider and gate readiness"
    )
    _add_common(preflight)
    preflight.add_argument("path")

    providers = family.add_parser("providers", help="show or configure explicit local providers")
    provider_action = providers.add_subparsers(dest="provider_action", required=True)
    provider_action.add_parser("list", help="report configured status without making remote calls")
    _add_common(provider_action.choices["list"])
    codex = provider_action.add_parser(
        "add-codex", help="configure read-only Codex proposal provider"
    )
    _add_common(codex)
    codex.add_argument("--model", required=True)
    codex.add_argument("--codex-path")
    codex.add_argument(
        "--cost-class", required=True, choices=("FREE_EXTERNAL", "METERED", "PAID", "EXPENSIVE")
    )
    codex.add_argument("--max-cost", type=float, required=True)
    codex.add_argument("--currency", default="USD")
    codex.add_argument(
        "--cost-unit", choices=("request", "task", "minute", "token", "artifact"), default="request"
    )
    codex.add_argument("--replace", action="store_true")
    process = provider_action.add_parser(
        "add-process", help="register an operator-specified provider"
    )
    _add_common(process)
    process.add_argument("--config", required=True, help="strict ProcessProviderConfig JSON")
    process.add_argument("--agent-definition", required=True, help="strict AgentDefinition JSON")
    process.add_argument("--replace", action="store_true")
    for name in ("add-openai-image", "add-openai-speech", "add-openai-vision"):
        media = provider_action.add_parser(
            name,
            help="configure an explicit official OpenAI media provider without making a request",
        )
        _add_common(media)
        media.add_argument(
            "--config",
            required=True,
            help="strict provider config JSON; credential value is never stored",
        )
        if name == "add-openai-speech":
            media.add_argument("--voice", required=True)
        media.add_argument("--replace", action="store_true")

    run = family.add_parser("run", help="preflight and run a versioned workflow manifest")
    _add_common(run)
    run.add_argument("--manifest", required=True)

    test_game = commands.add_parser("test-game", help="execute an allowlisted gameplay scenario")
    _add_common(test_game)
    test_game.add_argument("--scenario", required=True)
    test_game.add_argument("--timeout", type=float, default=120)

    performance = commands.add_parser(
        "performance-review", help="collect real engine performance evidence"
    )
    _add_common(performance)
    performance.add_argument("--scenario", required=True)
    performance.add_argument("--timeout", type=float, default=120)

    for verb in ("editor", "run-scene"):
        operation = commands.add_parser(
            verb,
            help="run the bounded Godot "
            + ("editor" if verb == "editor" else "scene")
            + " operation",
        )
        _add_common(operation)
        operation.add_argument("--executable", required=True)
        if verb == "run-scene":
            operation.add_argument("--scene")
        operation.add_argument("--timeout", type=float, default=300)

    for verb in ("build", "release"):
        operation = commands.add_parser(verb, help=f"run the reviewed Godot {verb} workflow")
        _add_common(operation)
        if verb == "build":
            operation.add_argument("--executable", required=True)
            operation.add_argument("--preset", required=True)
            operation.add_argument("--output-name", required=True)
        else:
            operation.add_argument("--build-attempt", required=True)
        operation.add_argument("--timeout", type=float, default=300)

    operation = commands.add_parser(
        "operation", help="inspect or reconcile provider operation intents"
    )
    _add_common(operation)
    op_action = operation.add_subparsers(dest="operation_action", required=True)
    op_inspect = op_action.add_parser("inspect")
    _add_common(op_inspect)
    op_inspect.add_argument("--workflow")
    op_inspect.add_argument("--project-id")
    op_reconcile = op_action.add_parser("reconcile")
    _add_common(op_reconcile)
    op_reconcile.add_argument("--task", required=True)
    op_reconcile.add_argument("--request-fingerprint", required=True)
    op_reconcile.add_argument(
        "--expected-status",
        required=True,
        choices=("SUBMITTING", "UNCERTAIN", "COMPLETED", "FAILED"),
    )
    op_reconcile.add_argument("--status", required=True, choices=("FAILED", "RECONCILED"))
    op_reconcile.add_argument("--actor", required=True)
    op_reconcile.add_argument("--comment", required=True)
    op_reconcile.add_argument("--evidence-ref", action="append", required=True)
    op_reconcile.add_argument("--external-id")
    op_reconcile.add_argument("--actual-cost", type=float)
    op_reconcile.add_argument("--apply", action="store_true")

    recovery = family.add_parser(
        "recover-apply", help="inspect or safely recover a Factory apply journal"
    )
    _add_common(recovery)
    recovery.add_argument("--journal", required=True, help="project-relative apply journal path")
    recovery.add_argument("--apply", action="store_true", help="apply the verified recovery action")
    recovery.add_argument("--expected-journal-sha256")
    recovery.add_argument("--actor")
    recovery.add_argument("--comment")


def _add_common(parser: Any) -> None:
    """Keep existing CLI project selection and JSON output conventions."""
    parser.add_argument("-p", "--project", default=argparse.SUPPRESS)
    parser.add_argument("--godot-path", default=argparse.SUPPRESS)
    parser.add_argument("--blender-path", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, dest="as_json")


def _read_json(path: Path, limit: int, *, project_root: Path | None = None) -> Any:
    path = path.expanduser()
    if project_root is not None:
        original = path if path.is_absolute() else project_root / path
        _reject_link_chain(original)
        path = PathGuard(project_root).resolve_safe_path(original)
    else:
        path = path.absolute()
        _reject_link_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValidationError(
                    f"JSON input must be a single-link regular file no larger than {limit} bytes"
                )
            data = bytearray()
            while len(data) <= limit:
                block = os.read(descriptor, min(64_000, limit + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
            if len(data) > limit:
                raise ValidationError(f"JSON input exceeds {limit} bytes")
        finally:
            os.close(descriptor)
        return json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid JSON input: {path.name}") from exc


def _write_new_json(path: Path, payload: Any) -> None:
    path = path.expanduser().absolute()
    _reject_link_chain(path.parent)
    if path.exists():
        raise ValidationError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=".gamefactory-cli-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _reject_link_chain(path: Path) -> None:
    current = path.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Path cannot contain a symbolic link or junction")
        current = current.parent


def _provider_document(root: Path) -> dict[str, Any]:
    path = PathGuard(root).resolve_safe_path(_CONFIG_RELATIVE)
    if not path.exists():
        return {"schema_version": "factory-providers-1.0.0", "providers": []}
    payload = _read_json(path, _CONFIG_LIMIT)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "factory-providers-1.0.0"
        or not isinstance(payload.get("providers"), list)
    ):
        raise ValidationError(
            "Provider config must use factory-providers-1.0.0 with a providers list"
        )
    if len(payload["providers"]) > 32:
        raise ValidationError("Provider config exceeds the 32 provider limit")
    return payload


def _save_provider(root: Path, entry: dict[str, Any], *, replace_existing: bool) -> dict[str, Any]:
    target = PathGuard(root).resolve_safe_path(_CONFIG_RELATIVE)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / "providers.lock"
    lock_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except FileExistsError as exc:
        raise ValidationError("Provider configuration is being updated by another process") from exc
    try:
        os.close(lock_fd)
        baseline = _read_json(target, _CONFIG_LIMIT) if target.exists() else None
        payload = (
            _provider_document(root)
            if baseline is not None
            else {"schema_version": "factory-providers-1.0.0", "providers": []}
        )
        providers: list[Any] = list(payload["providers"])
        payload["providers"] = providers
        identity = entry.get("provider_id")
        existing = next(
            (index for index, item in enumerate(providers) if item.get("provider_id") == identity),
            None,
        )
        if existing is not None and not replace_existing:
            raise ValidationError(f"Provider {identity!r} is already configured; use --replace")
        if existing is None and len(providers) >= 32:
            raise ValidationError("Provider config exceeds the 32 provider limit")
        if existing is None:
            providers.append(entry)
        else:
            providers[existing] = entry
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".providers-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            current = _read_json(target, _CONFIG_LIMIT) if target.exists() else None
            if current != baseline:
                raise ValidationError(
                    "Provider config changed while this update was being prepared"
                )
            if baseline is None:
                os.link(temporary, target)
                os.unlink(temporary)
            else:
                os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "provider_id": identity,
            "config_path": _CONFIG_RELATIVE,
            "credential_values_stored": False,
        }
    finally:
        lock_path.unlink(missing_ok=True)


def load_factory_registries(
    root: Path, manifests: tuple[FactoryWorkflowManifest, ...] = ()
) -> tuple[ExecutorRegistry, AgentRegistry, GateExecutorRegistry, list[dict[str, Any]]]:
    """Build explicit provider, agent and trusted-gate registries; perform readiness only."""
    document = _provider_document(root)
    executors, agents, gates = ExecutorRegistry(), AgentRegistry(), GateExecutorRegistry()
    status: list[dict[str, Any]] = []
    for raw in document["providers"]:
        if not isinstance(raw, dict):
            raise ValidationError("Each provider config entry must be an object")
        kind = raw.get("kind")
        definition = AgentDefinition.model_validate_json(
            json.dumps(raw.get("agent_definition"), allow_nan=False)
        )
        provider: _ConfiguredCapabilityExecutor
        if kind == "codex.readonly":
            codex_config = CodexAgentConfig(**raw.get("config", {}))
            provider = CodexAgentProvider(codex_config)
        elif kind == "process":
            process_config = ProcessProviderConfig.model_validate_json(
                json.dumps(raw.get("config"), allow_nan=False)
            )
            provider = OperatorProcessAgentProvider(process_config)
        elif kind == "openai.image":
            image_config = OpenAIMediaConfig.model_validate_json(
                json.dumps(raw.get("config"), allow_nan=False)
            )
            provider = OpenAIImageProvider(image_config, provider_id=raw["provider_id"])
        elif kind == "openai.speech":
            speech_config = OpenAIMediaConfig.model_validate_json(
                json.dumps(raw.get("config"), allow_nan=False)
            )
            provider = OpenAIAudioSpeechProvider(
                speech_config, voice=raw.get("voice", ""), provider_id=raw["provider_id"]
            )
        elif kind == "openai.vision":
            from gamefactory.adapters.agents.openai_vision import (
                OpenAIVisionConfig,
                OpenAIVisionReviewProvider,
            )

            vision_config = OpenAIVisionConfig.model_validate_json(
                json.dumps(raw.get("config"), allow_nan=False)
            )
            provider = OpenAIVisionReviewProvider(vision_config, provider_id=raw["provider_id"])
        else:
            raise ValidationError(f"Unsupported provider kind: {kind!r}")
        provider_id = raw.get("provider_id")
        if not isinstance(provider_id, str) or provider.provider_id != provider_id:
            raise ValidationError("Configured provider id mismatch")
        fingerprint = getattr(provider, "config_fingerprint", None)
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValidationError("Provider did not expose a configuration fingerprint")
        provider_capabilities = provider.capabilities
        executors.register(
            provider_id,
            provider,
            capabilities=set(provider_capabilities),
            config_fingerprint=fingerprint,
        )
        agents.register(definition)
        readiness = provider.readiness()
        status.append(
            {
                "provider_id": provider_id,
                "status": readiness.status.value,
                "capabilities": sorted(provider_capabilities),
                "reason": readiness.reason,
                "credential_env_names": list(readiness.missing_credentials),
                "live_execution_verified": readiness.live_execution_verified,
                "config_fingerprint": fingerprint,
            }
        )
    from gamefactory.cli.factory_gates import register_manifest_gates

    for manifest in manifests:
        status.extend(register_manifest_gates(root, manifest, gates, executors))
    return executors, agents, gates, status


def saved_factory_manifests(root: Path, db: Database) -> tuple[FactoryWorkflowManifest, ...]:
    """Rebuild immutable gate bindings for persisted workflow tasks on resume."""
    from gamefactory.adapters.persistence.repositories import TaskRepository, WorkflowRepository

    workflows = WorkflowRepository(db)
    tasks = TaskRepository(db)
    manifests: list[FactoryWorkflowManifest] = []
    for workflow in workflows.list_by_project(ConfigLoader.load_config(root).project.id):
        specs = []
        expected_hash = None
        for task in tasks.list_by_workflow(workflow.id):
            if task.task_type != "factory_provider_task":
                continue
            spec = task.parameters.get("factory_spec")
            if isinstance(spec, dict):
                specs.append(spec)
                expected_hash = task.parameters.get("manifest_sha256", expected_hash)
        if not specs:
            continue
        base = {
            "schema_version": "factory-workflow-1.0.0",
            "workflow_id": workflow.id,
            "project_id": workflow.project_id,
            "name": workflow.name,
            "tasks": specs,
        }
        matches: list[FactoryWorkflowManifest] = []
        for require_acceptance in (False, True):
            candidate = FactoryWorkflowManifest.model_validate_json(
                json.dumps(
                    {**base, "require_human_game_acceptance": require_acceptance}, allow_nan=False
                )
            )
            if candidate.sha256 == expected_hash:
                matches.append(candidate)
        if len(matches) != 1:
            raise ValidationError(
                f"Saved Factory manifest for workflow {workflow.id} cannot be reconstructed exactly"
            )
        manifests.append(matches[0])
    return tuple(manifests)


def _manifest_template(root: Path, args: Any) -> dict[str, Any]:
    cfg = ConfigLoader.load_config(root)
    task_id = args.task_id or generate_id("TASK")
    inputs = []
    for item in args.input:
        path, separator, purpose = item.partition("=")
        if not separator or not path or not purpose:
            raise ValidationError("--input must be PATH=PURPOSE")
        inputs.append({"path": path, "purpose": purpose})
    task: dict[str, Any] = {
        "task_id": task_id,
        "name": args.task_name,
        "family": args.family,
        "kind": args.kind,
        "executor_id": args.executor,
        "agent_id": args.agent,
        "objective": args.objective,
        "prompt_template_id": args.prompt_template,
        "prompt_template_version": args.prompt_version,
        "dependencies": [],
        "inputs": inputs,
        "capabilities": args.capability,
        "allowed_tools": args.allowed_tool,
        "forbidden_tools": args.forbidden_tool,
        "output_scopes": args.output_scope,
        "expected_artifacts": args.artifact,
        "required_gates": args.gate,
        "acceptance_criteria": args.acceptance,
        "cost": {
            "cost_class": args.cost_class,
            "max_amount": args.max_cost,
            "currency": args.currency,
            "unit": args.cost_unit,
            "approval_required": args.cost_class not in {"LOCAL", "FREE_EXTERNAL"},
        },
        "tool_constraints": {
            "allowed_tools": args.allowed_tool,
            "forbidden_tools": args.forbidden_tool,
            "max_tool_calls": args.max_tool_calls,
            "timeout_seconds": args.timeout,
            "network_allowed": args.network_allowed,
            "repository_write_allowed": args.repository_write_allowed,
            "process_execution_allowed": args.process_execution_allowed,
        },
        "game_write": args.game_write,
        "parameters": _read_json(Path(args.parameters), 64_000, project_root=root)
        if args.parameters
        else {},
        "timeout_seconds": args.timeout,
    }
    _validate_declared_outputs(args.game_write, args.output_scope, args.artifact)
    spec = WorkflowTaskSpec.model_validate_json(json.dumps(task, allow_nan=False))
    manifest = FactoryWorkflowManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": "factory-workflow-1.0.0",
                "workflow_id": generate_id("WF-FACTORY"),
                "project_id": cfg.project.id,
                "name": args.workflow_name,
                "tasks": [spec.model_dump(mode="json")],
                "require_human_game_acceptance": True,
            },
            allow_nan=False,
        )
    )
    return manifest.model_dump(mode="json")


def _create_engine(
    root: Path,
    db: Database,
    executors: ExecutorRegistry,
    agents: AgentRegistry,
    gates: GateExecutorRegistry,
) -> WorkflowEngine:
    from gamefactory.cli.main import _engine

    return _engine(root, db, factory_registries=(executors, agents, gates))


def dispatch_factory_command(
    args: Any, root: Path, db: Database | None
) -> tuple[Any, int, str | None] | None:
    """Handle the additional CLI commands; return ``None`` for legacy dispatch."""
    if args.command == "new":
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = root / target
        created = create_godot_project(target, args.name, args.dimension)
        project_root, project_config, already = ConfigLoader.init_project(
            target, args.name, custom_godot_path=args.godot_path
        )
        return (
            {
                "project": created,
                "factory_initialized": True,
                "already_initialized": already,
                "project_id": project_config.project.id,
            },
            0,
            f"Created and initialized {args.name} at {project_root}",
        )
    if args.command == "discover":
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = root / target
        return discover_project(target, include_git=args.include_git), 0, None
    if args.command == "factory":
        if args.factory_command == "manifest":
            if args.manifest_action == "create":
                payload = _manifest_template(root, args)
                output = Path(args.output).expanduser()
                if not output.is_absolute():
                    output = root / output
                output = PathGuard(root).resolve_safe_path(output)
                _write_new_json(output, payload)
                return (
                    {"manifest": str(output), "schema_version": payload["schema_version"]},
                    EXIT_SUCCESS,
                    f"Created workflow manifest: {output}",
                )
            if args.manifest_action == "preflight":
                data = _read_json(Path(args.path), _MANIFEST_LIMIT, project_root=root)
                manifest = FactoryWorkflowManifest.model_validate_json(
                    json.dumps(data, allow_nan=False)
                )
                executors, agents, gates, providers = load_factory_registries(root, (manifest,))
                preflight = preflight_manifest(manifest, executors, agents, gates)
                return (
                    {"providers": providers, "preflight": preflight},
                    _preflight_code(preflight["status"]),
                    None,
                )
        if args.factory_command == "providers":
            provider: _ConfiguredCapabilityExecutor
            if args.provider_action == "list":
                _, _, _, providers = load_factory_registries(root)
                return {"providers": providers, "remote_calls_made": False}, EXIT_SUCCESS, None
            if args.provider_action == "add-codex":
                from gamefactory.core.domain.agent_contracts import AgentKind, CostConstraints

                provider_id = "agent.codex.readonly"
                if args.max_cost < 0:
                    raise ValidationError("--max-cost must be non-negative")
                codex_config = CodexAgentConfig(model=args.model, codex_path=args.codex_path)
                charged = args.cost_class in {"METERED", "PAID", "EXPENSIVE"}
                cost = CostConstraints(
                    cost_class=args.cost_class,
                    max_amount=args.max_cost,
                    currency=args.currency,
                    unit=args.cost_unit,
                    approval_required=charged,
                )
                if args.max_cost == 0 and charged:
                    raise ValidationError(
                        "Charged Codex providers require a positive explicit --max-cost"
                    )
                definition = AgentDefinition(
                    schema_version="1.0.0",
                    agent_id=provider_id,
                    display_name="Codex read-only",
                    role="Propose bounded design or code changes from declared context",
                    kinds=(AgentKind.CODE, AgentKind.DESIGN),
                    capabilities=("code.propose", "design.propose"),
                    allowed_tools=("codex.execute_readonly",),
                    forbidden_tools=("shell.execute", "repository.write"),
                    cost=cost,
                    timeout_seconds=codex_config.timeout_seconds,
                )
                provider = CodexAgentProvider(codex_config)
                entry = {
                    "provider_id": provider_id,
                    "kind": "codex.readonly",
                    "config": {
                        "model": args.model,
                        "codex_path": args.codex_path,
                        "timeout_seconds": codex_config.timeout_seconds,
                        "provider_id": provider_id,
                    },
                    "agent_definition": definition.model_dump(mode="json"),
                }
                saved = _save_provider(root, entry, replace_existing=args.replace)
                ready = provider.readiness()
                return (
                    {
                        **saved,
                        "status": ready.status.value,
                        "live_execution_verified": False,
                        "credential_values_stored": False,
                    },
                    EXIT_SUCCESS,
                    "Provider configuration saved; no model request was made.",
                )
            if args.provider_action == "add-process":
                config_doc = _read_json(Path(args.config), 64_000)
                agent_doc = _read_json(Path(args.agent_definition), 64_000)
                process_config = ProcessProviderConfig.model_validate_json(
                    json.dumps(config_doc, allow_nan=False)
                )
                definition = AgentDefinition.model_validate_json(
                    json.dumps(agent_doc, allow_nan=False)
                )
                provider = OperatorProcessAgentProvider(process_config)
                entry = {
                    "provider_id": process_config.provider_id,
                    "kind": "process",
                    "config": process_config.model_dump(mode="json"),
                    "agent_definition": definition.model_dump(mode="json"),
                }
                saved = _save_provider(root, entry, replace_existing=args.replace)
                ready = provider.readiness()
                return (
                    {
                        **saved,
                        "status": ready.status.value,
                        "live_execution_verified": False,
                        "credential_values_stored": False,
                    },
                    EXIT_SUCCESS,
                    "Provider configuration saved; provider executable was not launched.",
                )
            if args.provider_action in {
                "add-openai-image",
                "add-openai-speech",
                "add-openai-vision",
            }:
                config_doc = _read_json(Path(args.config), 64_000)
                from gamefactory.core.domain.agent_contracts import AgentKind

                if args.provider_action == "add-openai-image":
                    image_config = OpenAIMediaConfig.model_validate_json(
                        json.dumps(config_doc, allow_nan=False)
                    )
                    provider_id, kind = "provider.openai.image", "openai.image"
                    provider = OpenAIImageProvider(image_config, provider_id=provider_id)
                    agent_kind = AgentKind.IMAGE
                    provider_capability = "image.generate"
                    provider_tool = "openai.images.generate"
                    provider_cost = image_config.cost
                    provider_timeout = image_config.timeout_seconds
                    provider_config = image_config.model_dump(mode="json")
                    voice = None
                elif args.provider_action == "add-openai-speech":
                    speech_config = OpenAIMediaConfig.model_validate_json(
                        json.dumps(config_doc, allow_nan=False)
                    )
                    provider_id, kind = "provider.openai.audio-speech", "openai.speech"
                    provider = OpenAIAudioSpeechProvider(
                        speech_config, voice=args.voice, provider_id=provider_id
                    )
                    agent_kind = AgentKind.AUDIO
                    provider_capability = "audio.speech"
                    provider_tool = "openai.audio.speech"
                    provider_cost = speech_config.cost
                    provider_timeout = speech_config.timeout_seconds
                    provider_config = speech_config.model_dump(mode="json")
                    voice = args.voice
                else:
                    from gamefactory.adapters.agents.openai_vision import (
                        OpenAIVisionConfig,
                        OpenAIVisionReviewProvider,
                    )

                    vision_config = OpenAIVisionConfig.model_validate_json(
                        json.dumps(config_doc, allow_nan=False)
                    )
                    provider_id, kind = "provider.openai.vision-review", "openai.vision"
                    provider = OpenAIVisionReviewProvider(vision_config, provider_id=provider_id)
                    agent_kind = AgentKind.VISION
                    provider_capability = "vision.review"
                    provider_tool = "openai.responses.vision"
                    provider_cost = vision_config.cost
                    provider_timeout = vision_config.timeout_seconds
                    provider_config = vision_config.model_dump(mode="json")
                    voice = None
                definition = AgentDefinition(
                    schema_version="1.0.0",
                    agent_id=provider_id,
                    display_name=kind,
                    role="Bounded advisory media provider",
                    kinds=(agent_kind,),
                    capabilities=(provider_capability,),
                    allowed_tools=(provider_tool,),
                    forbidden_tools=("shell.execute", "repository.write"),
                    cost=provider_cost,
                    timeout_seconds=provider_timeout,
                )
                entry = {
                    "provider_id": provider_id,
                    "kind": kind,
                    "config": provider_config,
                    "agent_definition": definition.model_dump(mode="json"),
                }
                if voice is not None:
                    entry["voice"] = voice
                saved = _save_provider(root, entry, replace_existing=args.replace)
                ready = provider.readiness()
                return (
                    {
                        **saved,
                        "status": ready.status.value,
                        "live_execution_verified": False,
                        "credential_values_stored": False,
                    },
                    EXIT_SUCCESS,
                    "Provider configuration saved; no OpenAI request was made.",
                )
        if args.factory_command == "run":
            if db is None:
                raise ConfigurationError("Project is not initialized; run gamefactory init first")
            manifest_data = _read_json(Path(args.manifest), _MANIFEST_LIMIT, project_root=root)
            manifest = FactoryWorkflowManifest.model_validate_json(
                json.dumps(manifest_data, allow_nan=False)
            )
            executors, agents, gates, providers = load_factory_registries(root, (manifest,))
            preflight = preflight_manifest(manifest, executors, agents, gates)
            if preflight["status"] != "READY":
                return (
                    {"preflight": preflight, "providers": providers},
                    _preflight_code(preflight["status"]),
                    "Manifest is not ready; no workflow was launched.",
                )
            workflow, tasks = build_workflow(manifest)
            engine = _create_engine(root, db, executors, agents, gates)
            from gamefactory.adapters.persistence.repositories import (
                TaskRepository,
                WorkflowRepository,
            )

            existing_workflow = WorkflowRepository(db).get(workflow.id)
            if existing_workflow is not None:
                saved_workflow_manifest = next(
                    (
                        item
                        for item in saved_factory_manifests(root, db)
                        if item.workflow_id == workflow.id
                    ),
                    None,
                )
                if (
                    saved_workflow_manifest is None
                    or saved_workflow_manifest.sha256 != manifest.sha256
                ):
                    raise ValidationError(
                        "Workflow id already exists with a different or unreconstructable manifest"
                    )
                result = engine.run_workflow(workflow.id)
                return (
                    {
                        "workflow": {"id": workflow.id, "manifest_sha256": manifest.sha256},
                        "result": {
                            "status": result.status.value,
                            "pending_approval_id": result.pending_approval_id,
                            "error": result.error_message,
                        },
                    },
                    _workflow_code(result.status.value),
                    f"Workflow {workflow.id}: {result.status.value}",
                )
            task_repo = TaskRepository(db)
            collisions = [task.id for task in tasks if task_repo.get(task.id) is not None]
            if collisions:
                raise ValidationError(
                    "Task identity already exists in another workflow: " + ", ".join(collisions)
                )
            engine.register_workflow(workflow, tasks)
            result = engine.run_workflow(workflow.id)
            return (
                {
                    "workflow": {"id": workflow.id, "manifest_sha256": manifest.sha256},
                    "result": {
                        "status": result.status.value,
                        "pending_approval_id": result.pending_approval_id,
                        "error": result.error_message,
                    },
                },
                _workflow_code(result.status.value),
                f"Workflow {workflow.id}: {result.status.value}",
            )
    if args.command == "test-game" or args.command == "performance-review":
        if db is None:
            raise ConfigurationError("Project is not initialized; run gamefactory init first")
        cfg = ConfigLoader.load_config(root)
        executable = (
            args.godot_path
            or cfg.engine.executable_path
            or os.environ.get("GAMEFACTORY_GODOT_PATH")
        )
        if not executable:
            raise ConfigurationError("Select Godot with --godot-path or project config")
        scenario = Path(args.scenario).expanduser()
        if not scenario.is_absolute():
            scenario = root / scenario
        workflow, tasks = create_gameplay_quality_workflow(
            cfg.project.id, root, executable, scenario, timeout_seconds=args.timeout
        )
        engine = _create_runtime_engine(root, db)
        engine.register_workflow(workflow, tasks)
        result = engine.run_workflow(workflow.id)
        return (
            {
                "workflow_id": workflow.id,
                "status": result.status.value,
                "pending_approval_id": result.pending_approval_id,
                "error": result.error_message,
            },
            _workflow_code(result.status.value),
            f"Gameplay quality workflow {workflow.id}: {result.status.value}",
        )
    if args.command in {"build", "release", "editor", "run-scene"}:
        if db is None:
            raise ConfigurationError("Project is not initialized; run gamefactory init first")
        cfg = ConfigLoader.load_config(root)
        if args.command == "build":
            operation, params = (
                "export",
                {
                    "executable": args.executable,
                    "preset": args.preset,
                    "output_name": args.output_name,
                    "timeout_seconds": args.timeout,
                },
            )
        elif args.command == "release":
            operation, params = "release", {"build_attempt_id": args.build_attempt}
        elif args.command == "editor":
            operation, params = (
                "editor",
                {"executable": args.executable, "timeout_seconds": args.timeout},
            )
        else:
            operation, params = (
                "run",
                {
                    "executable": args.executable,
                    "scene": args.scene,
                    "timeout_seconds": args.timeout,
                },
            )
        workflow, tasks = build_project_operation_workflow(cfg.project.id, root, operation, params)
        engine = _create_runtime_engine(root, db)
        engine.register_workflow(workflow, tasks)
        result = engine.run_workflow(workflow.id)
        payload = {
            "workflow_id": workflow.id,
            "status": result.status.value,
            "pending_approval_id": result.pending_approval_id,
            "error": result.error_message,
        }
        if args.command == "build" and result.status.value == "COMPLETED":
            export_task = next(
                (
                    item
                    for item in engine.task_repo.list_by_workflow(workflow.id)
                    if item.task_type == "godot_export"
                ),
                None,
            )
            attempt = engine.exec_repo.get_latest_attempt(export_task.id) if export_task else None
            if attempt is not None:
                payload["build_attempt_id"] = attempt.id
        return (
            payload,
            _workflow_code(result.status.value),
            f"Godot {args.command} workflow {workflow.id}: {result.status.value}",
        )
    if args.command == "operation":
        if db is None:
            raise ConfigurationError("Project is not initialized; run gamefactory init first")
        repository = GenericOperationRepository(db)
        if args.operation_action == "inspect":
            cfg = ConfigLoader.load_config(root)
            rows = repository.list_operations(
                workflow_id=args.workflow,
                project_id=None if args.workflow else args.project_id or cfg.project.id,
            )
            return {"operations": rows}, EXIT_SUCCESS, None
        if args.operation_action == "reconcile":
            if not args.apply:
                return (
                    {
                        "applied": False,
                        "task_id": args.task,
                        "status": args.status,
                        "request_fingerprint": args.request_fingerprint,
                    },
                    EXIT_SUCCESS,
                    "Dry run only. Add --apply after reviewing the intent and evidence references.",
                )
            row = repository.reconcile(
                args.task,
                expected_request_fingerprint=args.request_fingerprint,
                expected_status=args.expected_status,
                status=args.status,
                actual_cost=args.actual_cost,
                external_id=args.external_id,
                actor=args.actor,
                comment=args.comment,
                evidence_refs=args.evidence_ref,
            )
            return (
                {"applied": True, "operation": row},
                EXIT_SUCCESS,
                f"Reconciled operation for task {args.task}",
            )
    if args.command == "factory" and args.factory_command == "recover-apply":
        if db is None:
            raise ConfigurationError("Project is not initialized; run gamefactory init first")
        if not args.apply:
            journal_preview = inspect_factory_apply_journal(root, args.journal, db=db)
            code = (
                EXIT_APPROVAL_BLOCKED
                if journal_preview.get("recovery_action") == "BLOCKED"
                else EXIT_SUCCESS
            )
            return (
                journal_preview,
                code,
                "Dry run only. Add --apply with the displayed journal hash and audit details to recover.",
            )
        if not all((args.expected_journal_sha256, args.actor, args.comment)):
            raise ValidationError(
                "--apply requires --expected-journal-sha256, --actor, and --comment"
            )
        recovery_result = recover_factory_apply_journal(
            root,
            args.journal,
            db=db,
            expected_journal_sha256=args.expected_journal_sha256,
            actor=args.actor,
            comment=args.comment,
        )
        code = (
            EXIT_APPROVAL_BLOCKED
            if recovery_result.get("recovery_action") == "BLOCKED"
            or recovery_result.get("conflicts")
            else EXIT_SUCCESS
        )
        message = f"Apply journal recovery: {recovery_result.get('recovery_action')}"
        if recovery_result.get("retry_ready"):
            message += (
                f"; continue with gamefactory retry {recovery_result['workflow_id']} "
                f"{recovery_result['task_id']}"
            )
        return recovery_result, code, message
    return None


def _create_runtime_engine(root: Path, db: Database) -> WorkflowEngine:
    from gamefactory.cli.main import _engine

    return _engine(root, db)
