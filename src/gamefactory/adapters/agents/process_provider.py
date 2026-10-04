"""Operator-configured, bounded JSON stdin/stdout provider process adapter."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import wave
from pathlib import Path
from typing import Any, Literal

from PIL import Image, UnidentifiedImageError
from pydantic import ConfigDict, Field, field_validator, model_validator

from gamefactory.adapters.agents.base import (
    ProviderReadiness,
    ProviderStatus,
    stable_config_fingerprint,
    validate_context,
    validate_provider_cost,
    verify_execution_authorization,
)
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentResultProposal,
    AgentTaskContract,
    CostConstraints,
    StrictModel,
    VisualReview,
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
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.core.execution.redaction import redactor

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_STDOUT_BYTES = 1_000_000


class ProcessProviderConfig(StrictModel):
    """Operator-owned configuration; model output never selects a command or arguments."""

    provider_id: str = Field(min_length=1, max_length=128)
    executable: str = Field(min_length=1, max_length=1024)
    fixed_args: tuple[str, ...] = ()
    kind: AgentKind
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=64)
    tool_name: str = "process.execute"
    model: str | None = Field(default=None, max_length=128)
    credential_env_names: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=300, gt=0, le=3600, allow_inf_nan=False)
    cost: CostConstraints = Field(default_factory=CostConstraints)
    allows_network: bool = False
    max_input_bytes: int = Field(default=2_000_000, ge=1, le=16_777_216)
    max_output_files: int = Field(default=16, ge=1, le=256)
    max_output_bytes: int = Field(default=32_000_000, ge=1, le=64_000_000)
    allowed_media_types: tuple[str, ...] = ("text/plain", "application/octet-stream")

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    @field_validator("provider_id")
    @classmethod
    def valid_provider_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value):
            raise ValueError("provider_id is invalid")
        return value

    @field_validator("credential_env_names")
    @classmethod
    def valid_env_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        sensitive_words = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
        if len(names) != len(set(names)) or any(
            not _ENV_NAME.fullmatch(name)
            or not any(word in name.upper() for word in sensitive_words)
            for name in names
        ):
            raise ValueError(
                "credential_env_names must be unique secret environment-variable names"
            )
        return names

    @field_validator("fixed_args")
    @classmethod
    def safe_fixed_args(cls, args: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in arg or len(arg) > 4096 for arg in args):
            raise ValueError("fixed_args contain an invalid or oversized argument")
        forbidden_flags = {"--api-key", "--token", "--password", "--authorization"}
        if any(arg.lower().split("=", 1)[0] in forbidden_flags for arg in args):
            raise ValueError(
                "credentials must be passed by explicit environment-variable reference"
            )
        return args

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value) for value in values
        ):
            raise ValueError("capabilities must be unique capability identifiers")
        return values

    @model_validator(mode="after")
    def no_automatic_fallback(self) -> ProcessProviderConfig:
        if self.cost.fallback_max_amount != 0:
            raise ValueError("V1 process providers cannot use an automatic fallback cost")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", self.tool_name):
            raise ValueError("tool_name must be a capability identifier")
        if (
            len(self.fixed_args) > 64
            or sum(len(arg.encode("utf-8")) for arg in self.fixed_args) > 16_384
        ):
            raise ValueError("fixed_args exceed the argument count or total byte limit")
        return self


class ProcessFileReference(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    staged_path: str = Field(min_length=1, max_length=1024)
    sha256: str
    media_type: str = Field(min_length=1, max_length=128)
    expected_before_sha256: str | None = None

    @field_validator("path", "staged_path")
    @classmethod
    def relative_paths(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("process file references must be normalized relative paths")
        return value

    @field_validator("sha256", "expected_before_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("file hashes must be lowercase SHA-256")
        return value


class ProcessProviderResponse(StrictModel):
    schema_version: Literal["agent-process-response-1.0.0"]
    status: Literal["COMPLETED", "FAILED", "UNCERTAIN"]
    proposal: AgentResultProposal | None = None
    visual_review: VisualReview | None = None
    files: tuple[ProcessFileReference, ...] = ()
    actual_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    cost_unit: str | None = Field(default=None, max_length=32)
    external_id: str | None = Field(default=None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)


def resolve_configured_executable(value: str) -> Path | None:
    """Resolve an operator-configured executable without launching it."""
    raw = Path(value).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        lexical = Path(os.path.abspath(raw))
    else:
        found = shutil.which(value)
        if not found:
            return None
        lexical = Path(os.path.abspath(found))
    if _contains_reparse_component(lexical):
        return None
    candidate = lexical.resolve(strict=False)
    if candidate.suffix.lower() in {".cmd", ".bat"}:
        return None
    return candidate if candidate.is_file() else None


class OperatorProcessAgentProvider:
    """Launch only the fixed executable and argv supplied in trusted operator config."""

    def __init__(
        self, config: ProcessProviderConfig, *, runner: ProcessRunner | None = None
    ) -> None:
        self.config = config
        self.provider_id = config.provider_id
        self.capabilities = frozenset(config.capabilities)
        self.runner = runner or ProcessRunner(sanitize_output=True)

    @property
    def config_fingerprint(self) -> str:
        executable = resolve_configured_executable(self.config.executable)
        return stable_config_fingerprint(self.config.model_dump(mode="json"), executable=executable)

    def readiness(self) -> ProviderReadiness:
        if not self.config.allows_network:
            return ProviderReadiness(
                self.provider_id,
                ProviderStatus.MISCONFIGURED,
                self.capabilities,
                reason="No OS egress sandbox is available; operator must explicitly permit process network access",
            )
        executable = resolve_configured_executable(self.config.executable)
        if executable is None:
            return ProviderReadiness(
                self.provider_id,
                ProviderStatus.UNAVAILABLE,
                self.capabilities,
                reason="Configured provider executable is not installed or is an unsafe launcher",
            )
        missing = tuple(
            name for name in self.config.credential_env_names if not os.environ.get(name)
        )
        if missing:
            return ProviderReadiness(
                self.provider_id,
                ProviderStatus.CREDENTIAL_MISSING,
                self.capabilities,
                reason="One or more operator-configured credentials are absent",
                executable_path=str(executable),
                missing_credentials=missing,
            )
        return ProviderReadiness(
            self.provider_id,
            ProviderStatus.NOT_VERIFIED,
            self.capabilities,
            reason="Executable and credential names are present; provider protocol was not launched",
            executable_path=str(executable),
            live_execution_verified=False,
        )

    @property
    def is_configured(self) -> bool:
        """Static path and named-credential readiness only; never launches the process."""
        return self.readiness().status in {ProviderStatus.AVAILABLE, ProviderStatus.NOT_VERIFIED}

    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun:
        if contract.kind != self.config.kind:
            raise ValidationError("Task kind does not match configured process provider")
        if (
            contract.selected_agent_id is not None
            and contract.selected_agent_id != self.provider_id
        ):
            raise ValidationError(
                "Selected agent identity does not match the registered process provider"
            )
        if (
            contract.prompt_template.template_id != "operator.process"
            or contract.prompt_template.version != "1.0.0"
        ):
            raise ValidationError(
                "Process provider requires the operator.process 1.0.0 protocol template"
            )
        required = {item.name for item in contract.required_capabilities if item.required}
        if not required.issubset(self.capabilities):
            raise ValidationError("Configured process provider lacks required capabilities")
        if (
            self.config.tool_name not in contract.allowed_tools
            or self.config.tool_name not in contract.tool_constraints.allowed_tools
            or self.config.tool_name in contract.forbidden_tools
            or contract.tool_constraints.max_tool_calls < 1
            or not contract.tool_constraints.process_execution_allowed
        ):
            raise ValidationError(
                "Process execution is not explicitly allowed by the task contract"
            )
        # ProcessRunner has no OS-level network sandbox. Treat every arbitrary local
        # process adapter as network-capable, even when its operator profile says it
        # does not expect network access.
        if not contract.tool_constraints.network_allowed:
            raise ValidationError("Process adapters require explicit contract network permission")
        if not self.config.allows_network:
            raise ValidationError(
                "Operator configuration has not permitted this process adapter to run"
            )
        validate_provider_cost(self.config.cost, contract)
        validate_context(contract, context)
        run_root = _prepare_fresh_workspace(workspace)
        request = {
            "schema_version": "agent-process-request-1.0.0",
            "provider_id": self.provider_id,
            "model": self.config.model,
            "request_fingerprint": request_fingerprint,
            "contract": contract.model_dump(mode="json"),
            "context": [
                {
                    "source": item.source.model_dump(mode="json"),
                    "content_base64": base64.b64encode(item.content).decode("ascii"),
                }
                for item in context.items
            ],
            "workspace": str(run_root),
            "output_scopes": list(contract.allowed_output_paths),
            "response_schema": ProcessProviderResponse.model_json_schema(),
        }
        request_text = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(request_text.encode("utf-8")) > min(
            self.config.max_input_bytes, contract.context_max_bytes + 1_000_000
        ):
            raise ValidationError("Process provider request exceeds configured input bound")
        executable = resolve_configured_executable(self.config.executable)
        if executable is None:
            raise ProviderUnavailable(
                "Configured process provider executable disappeared", provider=self.provider_id
            )
        env_overrides = {
            name: os.environ[name]
            for name in self.config.credential_env_names
            if os.environ.get(name)
        }
        verify_execution_authorization(
            provider_id=self.provider_id,
            contract=contract,
            request_fingerprint=request_fingerprint,
            authorization=authorization,
            verifier=authorization_verifier,
        )
        try:
            result = self.runner.run(
                CommandRequest(
                    args=[str(executable), *self.config.fixed_args],
                    cwd=run_root,
                    env_overrides=env_overrides,
                    timeout_seconds=min(
                        self.config.timeout_seconds, contract.tool_constraints.timeout_seconds
                    ),
                    minimal_env=True,
                    structured_json_output=True,
                    stdin_text=request_text,
                )
            )
        except (FactoryTimeoutError, OSError, ToolExecutionError):
            return _uncertain_process_run(self.provider_id, "process outcome is ambiguous")
        if result.timed_out:
            return ProviderRun(
                schema_version="provider-run-1.0.0",
                status=ProviderExecutionStatus.UNCERTAIN,
                proposal=None,
                metadata={
                    "provider_id": self.provider_id,
                    "request_protocol": "operator.process-1.0.0",
                    "reason": "timeout",
                },
            )
        if result.exit_code != 0:
            return _uncertain_process_run(
                self.provider_id, "process exited after invocation", exit_code=result.exit_code
            )
        raw = result.protocol_stdout
        if raw is None or result.stdout_truncated or result.stderr_truncated:
            return _uncertain_process_run(
                self.provider_id, "provider response is missing or truncated"
            )
        if len(raw.encode("utf-8")) > _MAX_STDOUT_BYTES:
            return _uncertain_process_run(
                self.provider_id, "provider response exceeds its size limit"
            )
        try:
            response = ProcessProviderResponse.model_validate_json(raw)
        except ValueError:
            return _uncertain_process_run(self.provider_id, "provider response is malformed")
        if any(secret and secret in raw for secret in env_overrides.values()):
            return _uncertain_process_run(
                self.provider_id, "provider response contained a credential"
            )
        proposal = response.proposal
        visual_review = response.visual_review
        if proposal is not None and proposal.task_id != contract.task_id:
            return _uncertain_process_run(
                self.provider_id,
                "process proposal identity does not match its task",
                actual_cost=response.actual_cost,
                cost_currency=response.cost_currency,
                cost_unit=response.cost_unit,
            )
        if proposal is not None and proposal.agent_id != self.provider_id:
            return _uncertain_process_run(
                self.provider_id,
                "process proposal identity does not match its provider",
                actual_cost=response.actual_cost,
                cost_currency=response.cost_currency,
                cost_unit=response.cost_unit,
            )
        if proposal is not None and visual_review is not None:
            return _uncertain_process_run(
                self.provider_id,
                "process response combined incompatible proposal types",
                actual_cost=response.actual_cost,
                cost_currency=response.cost_currency,
                cost_unit=response.cost_unit,
            )
        if visual_review is not None:
            if (
                visual_review.task_id != contract.task_id
                or visual_review.provider_id != self.provider_id
            ):
                return _uncertain_process_run(
                    self.provider_id,
                    "visual review identity does not match the task/provider",
                    actual_cost=response.actual_cost,
                    cost_currency=response.cost_currency,
                    cost_unit=response.cost_unit,
                )
            declared_sources = {(item.path, item.sha256) for item in contract.sources}
            if any(
                (item.path, item.sha256) not in declared_sources
                for item in visual_review.reviewed_sources
            ):
                return _uncertain_process_run(
                    self.provider_id,
                    "visual review is not bound to the approved task context",
                    actual_cost=response.actual_cost,
                    cost_currency=response.cost_currency,
                    cost_unit=response.cost_unit,
                )
        if response.actual_cost is not None and (
            response.cost_currency is None or response.cost_unit is None
        ):
            return _uncertain_process_run(
                self.provider_id, "reported cost lacks a currency or unit"
            )
        cost_exceeds_authorization = response.actual_cost is not None and (
            response.cost_currency != contract.cost_constraints.currency
            or response.cost_unit != contract.cost_constraints.unit
            or response.actual_cost > authorization.max_cost
            or response.actual_cost > contract.cost_constraints.max_amount
        )
        if response.status != "COMPLETED":
            if response.files:
                return _uncertain_process_run(
                    self.provider_id,
                    "non-completed process response included staged files",
                    actual_cost=response.actual_cost,
                    cost_currency=response.cost_currency,
                    cost_unit=response.cost_unit,
                )
            return ProviderRun(
                schema_version="provider-run-1.0.0",
                status=(
                    ProviderExecutionStatus.UNCERTAIN
                    if cost_exceeds_authorization
                    else ProviderExecutionStatus(response.status)
                ),
                proposal=proposal if not cost_exceeds_authorization else None,
                visual_review=visual_review,
                actual_cost=response.actual_cost,
                cost_currency=response.cost_currency,
                cost_unit=response.cost_unit,
                external_id=response.external_id,
                metadata={
                    **redactor.redact_data(response.metadata, list(env_overrides.values())),
                    "provider_id": self.provider_id,
                    "model": self.config.model,
                    "request_protocol": "operator.process-1.0.0",
                    **(
                        {"reason": "reported cost does not match or exceeds authorization"}
                        if cost_exceeds_authorization
                        else {}
                    ),
                },
            )
        if self.config.kind == AgentKind.VISION:
            if proposal is not None or visual_review is None:
                raise ValidationError("Vision provider must return only a strict VisualReview")
        elif proposal is None or visual_review is not None:
            raise ValidationError(
                "Completed process provider response requires only an AgentResultProposal"
            )
        if cost_exceeds_authorization:
            return ProviderRun(
                schema_version="provider-run-1.0.0",
                status=ProviderExecutionStatus.UNCERTAIN,
                proposal=None,
                actual_cost=response.actual_cost,
                cost_currency=response.cost_currency,
                cost_unit=response.cost_unit,
                external_id=response.external_id,
                metadata={
                    "provider_id": self.provider_id,
                    "request_protocol": "operator.process-1.0.0",
                    "reason": "reported cost does not match or exceeds authorization",
                },
            )
        outputs: list[ProviderOutputFile] = []
        total_bytes = 0
        max_files = min(self.config.max_output_files, contract.max_output_files)
        max_bytes = min(self.config.max_output_bytes, contract.max_output_bytes)
        if len(response.files) > max_files:
            raise ValidationError("Process provider exceeded output file count")
        for reference in response.files:
            if reference.path not in contract.allowed_output_paths:
                raise ValidationError(
                    "Process provider output is outside the exact contract scope",
                    details={"path": reference.path},
                )
            if reference.media_type not in self.config.allowed_media_types:
                raise ValidationError(
                    "Process provider returned an unadvertised media type",
                    details={"media_type": reference.media_type},
                )
            source_path = _safe_staged_file(run_root, reference.staged_path)
            try:
                descriptor = os.open(
                    source_path,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                stream_context = os.fdopen(descriptor, "rb")
            except OSError as err:
                raise ValidationError("Process staged output could not be opened safely") from err
            with stream_context as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    raise ValidationError("Process staged output is not a bounded regular file")
                path_info = source_path.lstat()
                if (
                    stat.S_ISLNK(path_info.st_mode)
                    or getattr(path_info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                ):
                    raise ValidationError("Process staged output changed during safe open")
                content = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValidationError("Process staged output changed during bounded read")
            if len(content) > max_bytes or hashlib.sha256(content).hexdigest() != reference.sha256:
                raise ValidationError(
                    "Process staged output size or SHA-256 does not match the response"
                )
            if any(
                secret and secret.encode("utf-8") in content for secret in env_overrides.values()
            ):
                raise ValidationError(
                    "Staged output appears to contain configured credential material"
                )
            total_bytes += len(content)
            if total_bytes > max_bytes:
                raise ValidationError("Process provider exceeded aggregate output byte limit")
            _validate_advertised_media(reference.media_type, content)
            if reference.expected_before_sha256 is not None:
                source = next(
                    (item for item in contract.sources if item.path == reference.path), None
                )
                if source is None or source.sha256 != reference.expected_before_sha256:
                    raise ValidationError(
                        "Process output update before-hash does not match explicit task source"
                    )
            elif any(item.path == reference.path for item in contract.sources):
                raise ValidationError(
                    "Process output replacing an explicit source requires its before-hash"
                )
            outputs.append(
                ProviderOutputFile(
                    path=reference.path,
                    content_base64=base64.b64encode(content).decode("ascii"),
                    sha256=reference.sha256,
                    media_type=reference.media_type,
                    expected_before_sha256=reference.expected_before_sha256,
                    staged_path=reference.staged_path,
                )
            )
        if proposal is not None:
            proposed_files = {item.path: item for item in proposal.proposed_files}
            staged_files = {item.path: item for item in outputs}
            if set(proposed_files) != set(staged_files):
                raise ValidationError(
                    "Process proposal file list must exactly match its staged output envelope"
                )
            for path, staged_file in staged_files.items():
                proposed_file = proposed_files[path]
                if (
                    proposed_file.output_sha256 != staged_file.sha256
                    or proposed_file.before_sha256 != staged_file.expected_before_sha256
                    or proposed_file.content_base64 != staged_file.content_base64
                ):
                    raise ValidationError(
                        "Process proposal content/hash does not match staged output",
                        details={"path": path},
                    )
        auth = authorization
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=proposal,
            files=tuple(outputs),
            actual_cost=response.actual_cost,
            cost_currency=response.cost_currency,
            cost_unit=response.cost_unit,
            external_id=response.external_id,
            visual_review=visual_review,
            metadata={
                **redactor.redact_data(response.metadata, list(env_overrides.values())),
                "provider_id": self.provider_id,
                "model": self.config.model,
                "authorized_max_cost": auth.max_cost,
                "request_protocol": "operator.process-1.0.0",
            },
        )


def _prepare_fresh_workspace(workspace: Path | str) -> Path:
    raw = Path(os.path.abspath(workspace))
    if _contains_reparse_component(raw):
        raise ValidationError("Provider workspace cannot cross symlinks or reparse points")
    if raw.exists() and not raw.is_dir():
        raise ValidationError("Provider workspace must be a regular directory")
    raw.mkdir(parents=True, exist_ok=True)
    if any(raw.iterdir()):
        raise ValidationError("Provider workspace must be fresh and empty")
    return raw.resolve(strict=True)


def _uncertain_process_run(
    provider_id: str,
    reason: str,
    *,
    exit_code: int | None = None,
    actual_cost: float | None = None,
    cost_currency: str | None = None,
    cost_unit: str | None = None,
) -> ProviderRun:
    cost_is_fully_specified = (
        actual_cost is not None and cost_currency is not None and cost_unit is not None
    )
    metadata: dict[str, Any] = {
        "provider_id": provider_id,
        "request_protocol": "operator.process-1.0.0",
        "reason": reason,
    }
    if exit_code is not None:
        metadata["exit_code"] = exit_code
    return ProviderRun(
        schema_version="provider-run-1.0.0",
        status=ProviderExecutionStatus.UNCERTAIN,
        proposal=None,
        actual_cost=actual_cost if cost_is_fully_specified else None,
        cost_currency=cost_currency if cost_is_fully_specified else None,
        cost_unit=cost_unit if cost_is_fully_specified else None,
        metadata=metadata,
    )


def _contains_reparse_component(path: Path) -> bool:
    current = path
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            return True
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _safe_staged_file(workspace: Path, relative: str) -> Path:
    root = Path(workspace).resolve()
    lexical_path = Path(os.path.abspath(root / relative))
    try:
        lexical_path.relative_to(root)
    except ValueError as err:
        raise ValidationError("Process staged path escapes its workspace") from err
    current = lexical_path
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            raise ValidationError("Process provider staged path cannot cross links or junctions")
        if current == root:
            break
        current = current.parent
    return PathGuard(workspace).resolve_safe_path(relative)


def _validate_advertised_media(media_type: str, content: bytes) -> None:
    if not content:
        raise ValidationError("Provider output cannot be empty")
    if media_type in {"image/png", "image/jpeg"}:
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.format != ("PNG" if media_type == "image/png" else "JPEG"):
                    raise ValidationError("Image bytes do not match the advertised media type")
                width, height = image.size
                if width < 1 or height < 1 or width * height > 32_000_000:
                    raise ValidationError("Image dimensions exceed the decoded pixel limit")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
        except (UnidentifiedImageError, OSError, ValueError) as err:
            if isinstance(err, ValidationError):
                raise
            raise ValidationError("Staged image is corrupt or unsupported") from err
    elif media_type == "audio/wav":
        try:
            with wave.open(io.BytesIO(content), "rb") as stream:
                channels, rate, width, frames = (
                    stream.getnchannels(),
                    stream.getframerate(),
                    stream.getsampwidth(),
                    stream.getnframes(),
                )
                if (
                    channels not in {1, 2}
                    or not 8000 <= rate <= 192000
                    or width not in {1, 2, 3, 4}
                    or frames <= 0
                ):
                    raise ValidationError("WAV audio header values are outside safe bounds")
                if (
                    frames / rate > 1800
                    or len(stream.readframes(frames)) != frames * channels * width
                ):
                    raise ValidationError("WAV audio is truncated or exceeds 30 minutes")
        except (wave.Error, EOFError) as err:
            raise ValidationError("Staged WAV is corrupt") from err
    elif media_type in {"audio/flac", "audio/ogg", "audio/mpeg"}:
        raise ValidationError(
            "This adapter does not have a safe decoder for the advertised audio format"
        )
    if media_type == "text/plain":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise ValidationError("text/plain output is not valid UTF-8") from err
