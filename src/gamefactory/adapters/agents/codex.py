"""Read-only Codex CLI agent that returns strict proposals and staged file content."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.agents.base import (
    ProviderReadiness,
    ProviderStatus,
    stable_config_fingerprint,
    validate_context,
    verify_execution_authorization,
)
from gamefactory.adapters.planning.codex_cli_agent import (
    _run_codex_via_process_runner,
    resolve_codex_executable,
)
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentResultProposal,
    AgentTaskContract,
)
from gamefactory.core.domain.errors import (
    ProviderUnavailable,
    ToolExecutionError,
    ValidationError,
)
from gamefactory.core.domain.errors import (
    TimeoutError as FactoryTimeoutError,
)
from gamefactory.core.domain.provider_execution import (
    AuthorizationVerifier,
    ProviderAuthorization,
    ProviderExecutionStatus,
    ProviderOutputFile,
    ProviderRun,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.core.execution.redaction import redactor

_RESOURCE_DIR = Path(__file__).parents[2] / "resources" / "agents"
_MAX_PROPOSAL_BYTES = 2_000_000
_ISOLATION_OVERRIDES = (
    'web_search="disabled"',
    "features.apps=false",
    "features.goals=false",
    "features.hooks=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.remote_plugin=false",
    "features.shell_snapshot=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "mcp_servers={}",
)


@dataclass(frozen=True)
class CodexAgentConfig:
    model: str
    codex_path: str | None = None
    timeout_seconds: float = 300
    provider_id: str = "agent.codex.readonly"


class CodexAgentProvider:
    """Runs Codex with read-only sandbox and only the explicitly bundled context."""

    capabilities = frozenset({"code.propose", "design.propose"})

    def __init__(
        self,
        config: CodexAgentConfig,
        *,
        runner: ProcessRunner | None = None,
        exec_runner: Callable[..., Any] | None = None,
    ) -> None:
        if not config.model.strip() or not config.provider_id.strip():
            raise ValidationError("Codex provider model and provider_id are required")
        if not 1 <= config.timeout_seconds <= 3600:
            raise ValidationError("Codex timeout must be in the range 1..3600 seconds")
        self.config = config
        self.provider_id = config.provider_id
        self.runner = runner or ProcessRunner(sanitize_output=True)
        self.exec_runner = exec_runner

    def readiness(self) -> ProviderReadiness:
        try:
            executable = resolve_codex_executable(self.config.codex_path)
        except (ProviderUnavailable, ToolExecutionError) as err:
            status = (
                ProviderStatus.UNAVAILABLE
                if isinstance(err, ProviderUnavailable)
                else ProviderStatus.MISCONFIGURED
            )
            return ProviderReadiness(
                self.provider_id, status, self.capabilities, reason=err.message
            )
        return ProviderReadiness(
            self.provider_id,
            ProviderStatus.NOT_VERIFIED,
            self.capabilities,
            reason="Codex executable is present; authentication and live model execution were not probed",
            executable_path=executable[0],
            live_execution_verified=False,
        )

    @property
    def is_configured(self) -> bool:
        """True when static executable configuration is usable; no live model probe occurs."""
        return self.readiness().status in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}

    @property
    def config_fingerprint(self) -> str:
        executable = None
        try:
            executable = Path(resolve_codex_executable(self.config.codex_path)[0])
        except (ProviderUnavailable, ToolExecutionError):
            pass
        bundle = b"\0".join(
            name.encode() + b"\0" + (_RESOURCE_DIR / name).read_bytes()
            for name in (
                "codex-role-1.0.0.md",
                "codex-task-1.0.0.md",
                "codex-context-1.0.0.md",
                "codex-output-schema-1.0.0.md",
            )
        )
        config = {
            "provider_id": self.provider_id,
            "model": self.config.model,
            "configured_codex_path": self.config.codex_path,
            "timeout_seconds": self.config.timeout_seconds,
            "prompt_bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "isolation_overrides": _ISOLATION_OVERRIDES,
        }
        return stable_config_fingerprint(config, executable=executable)

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun:
        if contract.kind not in {AgentKind.CODE, AgentKind.DESIGN}:
            raise ValidationError("Codex proposal provider supports code and design tasks only")
        if (
            contract.selected_agent_id is not None
            and contract.selected_agent_id != self.provider_id
        ):
            raise ValidationError(
                "Selected agent identity does not match the registered Codex provider"
            )
        if (
            contract.prompt_template.template_id != "codex.readonly"
            or contract.prompt_template.version != "1.0.0"
        ):
            raise ValidationError(
                "Codex provider requires the versioned codex.readonly 1.0.0 prompt"
            )
        if contract.cost_constraints.cost_class == "LOCAL":
            raise ValidationError("Remote Codex execution cannot be classified as LOCAL cost")
        if (
            "codex.execute_readonly" not in contract.allowed_tools
            or "codex.execute_readonly" not in contract.tool_constraints.allowed_tools
            or "codex.execute_readonly" in contract.forbidden_tools
            or contract.tool_constraints.max_tool_calls < 1
            or not contract.tool_constraints.network_allowed
            or not contract.tool_constraints.process_execution_allowed
            or contract.tool_constraints.repository_write_allowed
        ):
            raise ValidationError(
                "Codex CLI requires one explicitly authorized read-only network/process invocation"
            )
        if not {item.name for item in contract.required_capabilities if item.required}.issubset(
            self.capabilities
        ):
            raise ValidationError("Codex provider lacks a required task capability")
        validate_context(contract, context)
        run_root = _prepare_fresh_workspace(workspace)
        prompt, prompt_hash, prompt_bundle_hash = self._build_prompt(contract, context)
        schema_path = run_root / "agent-result.schema.json"
        message_path = run_root / "agent-result.json"
        schema_path.write_text(
            json.dumps(_strict_output_schema(), sort_keys=True), encoding="utf-8"
        )
        args = [
            *resolve_codex_executable(self.config.codex_path),
            *[part for override in _ISOLATION_OVERRIDES for part in ("-c", override)],
            "exec",
            "--model",
            self.config.model,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(message_path),
            "-",
        ]
        verify_execution_authorization(
            provider_id=self.provider_id,
            contract=contract,
            request_fingerprint=request_fingerprint,
            authorization=authorization,
            verifier=authorization_verifier,
        )
        try:
            if self.exec_runner is None:
                result = _run_codex_via_process_runner(
                    args=args,
                    cwd=run_root,
                    stdin_text=prompt,
                    timeout_seconds=min(
                        self.config.timeout_seconds, contract.tool_constraints.timeout_seconds
                    ),
                    runner=self.runner,
                )
            else:
                result = self.exec_runner(
                    args=args,
                    cwd=run_root,
                    stdin_text=prompt,
                    timeout_seconds=min(
                        self.config.timeout_seconds, contract.tool_constraints.timeout_seconds
                    ),
                    message_path=message_path,
                )
        except (FactoryTimeoutError, OSError, ToolExecutionError):
            return _uncertain_codex_run(self.provider_id, "Codex process outcome is ambiguous")
        if result.timed_out:
            return ProviderRun(
                schema_version="provider-run-1.0.0",
                status=ProviderExecutionStatus.UNCERTAIN,
                proposal=None,
                external_id=None,
                metadata={"provider_id": self.provider_id, "reason": "timeout"},
            )
        if result.exit_code != 0:
            return _uncertain_codex_run(
                self.provider_id, "Codex exited after invocation", exit_code=result.exit_code
            )
        try:
            payload = _read_bounded_regular_file(message_path, run_root, _MAX_PROPOSAL_BYTES)
            raw_text = payload.decode("utf-8")
            if redactor.redact_text(raw_text) != raw_text:
                raise ValidationError("Codex proposal contains secret-like material")
            proposal = AgentResultProposal.model_validate_json(payload)
        except (OSError, ValueError):
            return _uncertain_codex_run(
                self.provider_id, "Codex returned a malformed or missing structured proposal"
            )
        if proposal.task_id != contract.task_id:
            raise ValidationError("Codex proposal task id does not match the contract")
        if proposal.agent_id != self.provider_id:
            raise ValidationError(
                "Codex proposal agent_id does not match the registered provider identity"
            )
        outputs: list[ProviderOutputFile] = []
        if len(proposal.proposed_files) > contract.max_output_files:
            raise ValidationError("Codex proposal exceeds the task output file count")
        aggregate_output_bytes = 0
        for index, item in enumerate(proposal.proposed_files):
            if not _path_in_scopes(item.path, contract.allowed_output_paths):
                raise ValidationError(
                    "Codex proposed a file outside its exact output scope",
                    details={"path": item.path},
                )
            if item.operation == "DELETE":
                raise ValidationError("Codex provider cannot stage delete operations")
            source = {entry.path: entry for entry in contract.sources}.get(item.path)
            if item.operation == "UPDATE" and (
                source is None or source.sha256 != item.before_sha256
            ):
                raise ValidationError(
                    "Codex UPDATE before hash does not match explicit source context"
                )
            if item.operation == "CREATE" and source is not None:
                raise ValidationError("Codex CREATE targets a supplied existing source")
            content = base64.b64decode(item.content_base64, validate=True)
            aggregate_output_bytes += len(content)
            if aggregate_output_bytes > contract.max_output_bytes:
                raise ValidationError("Codex proposal exceeds the task output byte limit")
            try:
                text_content = content.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ValidationError(
                    "Codex text provider proposed non-UTF-8 file content"
                ) from err
            if redactor.redact_text(text_content) != text_content:
                raise ValidationError("Codex proposed file contains secret-like material")
            if item.output_sha256 is None:
                raise ValidationError("Codex proposed file is missing its validated output hash")
            staged = _stage_bytes(
                run_root, f"outputs/{index:03d}-{secrets.token_hex(4)}.txt", content
            )
            outputs.append(
                ProviderOutputFile(
                    path=item.path,
                    content_base64=item.content_base64,
                    sha256=item.output_sha256,
                    media_type="text/plain; charset=utf-8",
                    expected_before_sha256=item.before_sha256,
                    staged_path=staged.relative_to(run_root).as_posix(),
                )
            )
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=proposal,
            files=tuple(outputs),
            actual_cost=None,
            metadata={
                "provider_id": self.provider_id,
                "model": self.config.model,
                "prompt_template_id": contract.prompt_template.template_id,
                "prompt_template_version": contract.prompt_template.version,
                "prompt_sha256": prompt_hash,
                "prompt_bundle_sha256": prompt_bundle_hash,
            },
        )

    def _build_prompt(self, contract: AgentTaskContract, context: Any) -> tuple[str, str, str]:
        resource_names = (
            "codex-role-1.0.0.md",
            "codex-task-1.0.0.md",
            "codex-context-1.0.0.md",
            "codex-output-schema-1.0.0.md",
        )
        resource_bytes = [(name, (_RESOURCE_DIR / name).read_bytes()) for name in resource_names]
        bundle_hash = hashlib.sha256(
            b"\0".join(name.encode() + b"\0" + content for name, content in resource_bytes)
        ).hexdigest()
        if (
            contract.prompt_template.sha256 is not None
            and contract.prompt_template.sha256 != bundle_hash
        ):
            raise ValidationError(
                "Codex prompt template hash does not match the versioned prompt assets"
            )
        role, task_template, context_template, schema_note = (
            content.decode("utf-8") for _, content in resource_bytes
        )
        source_payload = []
        for item in context.items:
            try:
                text = item.content.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ValidationError(
                    "Codex text provider accepts only UTF-8 explicit context"
                ) from err
            source_payload.append({**item.source.model_dump(mode="json"), "text": text})
        task_prompt = task_template.format(
            objective=contract.objective,
            acceptance_criteria=json.dumps(contract.acceptance_criteria, ensure_ascii=False),
            allowed_output_paths=json.dumps(contract.allowed_output_paths),
            task_id=contract.task_id,
            agent_id=self.provider_id,
        )
        context_prompt = context_template.format(
            context_json=json.dumps(source_payload, ensure_ascii=False)
        )
        prompt = "\n\n".join(
            (
                role,
                task_prompt,
                context_prompt,
                schema_note,
                "Return exactly one AgentResultProposal JSON object conforming to the attached output schema.",
            )
        )
        return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest(), bundle_hash


def _prepare_fresh_workspace(workspace: Path | str) -> Path:
    raw = Path(os.path.abspath(workspace))
    current = raw
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValidationError("Provider workspace cannot cross symlinks or reparse points")
        if current.parent == current:
            break
        current = current.parent
    if raw.exists() and not raw.is_dir():
        raise ValidationError("Provider workspace must be a regular directory")
    raw.mkdir(parents=True, exist_ok=True)
    if any(raw.iterdir()):
        raise ValidationError("Provider workspace must be fresh and empty")
    return raw.resolve(strict=True)


def _path_in_scopes(path: str, scopes: tuple[str, ...]) -> bool:
    return path in scopes


def _strict_output_schema() -> dict[str, Any]:
    """Return the recursively strict schema required by Codex structured output."""
    schema = AgentResultProposal.model_json_schema()

    def normalize(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(properties)
                for child in properties.values():
                    normalize(child)
            for key, value in node.items():
                if key not in {"properties", "required"}:
                    normalize(value)
        elif isinstance(node, list):
            for child in node:
                normalize(child)

    normalize(schema)
    return schema


def _uncertain_codex_run(
    provider_id: str, reason: str, *, exit_code: int | None = None
) -> ProviderRun:
    metadata: dict[str, Any] = {"provider_id": provider_id, "reason": reason}
    if exit_code is not None:
        metadata["exit_code"] = exit_code
    return ProviderRun(
        schema_version="provider-run-1.0.0",
        status=ProviderExecutionStatus.UNCERTAIN,
        proposal=None,
        external_id=None,
        metadata=metadata,
    )


def _stage_bytes(workspace: Path, relative: str, payload: bytes) -> Path:
    guard = PathGuard(workspace)
    target = guard.ensure_safe_parent(relative)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return target


def _read_bounded_regular_file(path: Path, root: Path, max_bytes: int) -> bytes:
    lexical = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    try:
        lexical.relative_to(root)
    except ValueError as err:
        raise ValidationError("Codex output path escaped the fresh provider workspace") from err
    current = lexical
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            raise ValidationError("Codex output cannot cross links or junctions")
        if current == root:
            break
        current = current.parent
    try:
        descriptor = os.open(
            lexical, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ValidationError("Codex proposal must be a bounded regular file")
            payload = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
            path_info = lexical.lstat()
            if (
                len(payload) > max_bytes
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino) != (path_info.st_dev, path_info.st_ino)
            ):
                raise ValidationError("Codex proposal changed during bounded read")
            return payload
    except OSError as err:
        raise ValidationError("Codex proposal could not be read safely") from err
