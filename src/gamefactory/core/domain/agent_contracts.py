"""Versioned, adapter-neutral contracts for untrusted agent work proposals."""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_COMPONENT = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$")
_SECRET_OUTPUT_NAME = re.compile(
    r"(^|[._-])(secrets?|credentials?|tokens?|passwords?|private(?:[_-]?keys?)?)([._-]|$)", re.I
)
_RESERVED_OUTPUT_DIRS = {
    ".git",
    ".gamefactory",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".azure",
    ".gnupg",
    ".hg",
    ".svn",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class AgentKind(StrEnum):
    CODE = "code"
    DESIGN = "design"
    IMAGE = "image"
    VISION = "vision"
    AUDIO = "audio"
    GENERAL = "general"


class AgentOutcome(StrEnum):
    PROPOSED = "PROPOSED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    NEEDS_INPUT = "NEEDS_INPUT"


class ChangeDisposition(StrEnum):
    PROPOSAL = "PROPOSAL"
    NO_CHANGE = "NO_CHANGE"


class SourceReference(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str
    size_bytes: int = Field(ge=0)
    purpose: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def safe_source_path(cls, value: str) -> str:
        return _validate_relative_project_path(value)

    @field_validator("sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class PromptTemplateRef(StrictModel):
    template_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$")
    sha256: str | None = None

    @field_validator("sha256")
    @classmethod
    def valid_optional_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class ToolConstraints(StrictModel):
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    max_tool_calls: int = Field(default=0, ge=0, le=10000)
    timeout_seconds: float = Field(default=60, gt=0, le=86400, allow_inf_nan=False)
    network_allowed: bool = False
    repository_write_allowed: bool = False
    process_execution_allowed: bool = False

    @field_validator("allowed_tools", "forbidden_tools")
    @classmethod
    def validate_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _IDENTIFIER.fullmatch(v) for v in values):
            raise ValueError("tool names must be unique capability identifiers")
        return values

    @model_validator(mode="after")
    def no_conflicts(self) -> ToolConstraints:
        if set(self.allowed_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both allowed and forbidden")
        return self


class CostConstraints(StrictModel):
    max_amount: float = Field(default=0, ge=0, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    unit: Literal["request", "task", "minute", "token", "artifact"] = "request"
    cost_class: Literal["LOCAL", "FREE_EXTERNAL", "METERED", "PAID", "EXPENSIVE"] = "LOCAL"
    approval_required: bool = True
    fallback_max_amount: float = Field(default=0, ge=0, allow_inf_nan=False)
    fallback_currency: str = Field(default="USD", pattern=r"^[A-Z][A-Z0-9_]{1,31}$")
    fallback_approval_required: bool = True


class CapabilityRequirement(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    required: bool = True

    @field_validator("name")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("capability name must be an identifier")
        return value


class QualityExpectation(StrictModel):
    criterion: str = Field(min_length=1, max_length=500)
    evidence_type: str | None = Field(default=None, max_length=128)
    required: bool = True


class AgentDefinition(StrictModel):
    schema_version: Literal["1.0.0"]
    agent_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=500)
    kinds: tuple[AgentKind, ...] = (AgentKind.GENERAL,)
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    input_types: tuple[str, ...] = ()
    output_types: tuple[str, ...] = ()
    quality_expectations: tuple[QualityExpectation, ...] = ()
    cost: CostConstraints = Field(default_factory=CostConstraints)
    approval_boundaries: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=300, gt=0, le=86400, allow_inf_nan=False)
    max_retries: int = Field(default=0, ge=0, le=10)

    @field_validator("agent_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("agent_id must be an identifier")
        return value

    @field_validator(
        "capabilities",
        "allowed_tools",
        "forbidden_tools",
        "input_types",
        "output_types",
        "approval_boundaries",
    )
    @classmethod
    def unique_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not _IDENTIFIER.fullmatch(v) for v in values):
            raise ValueError("entries must be unique identifiers")
        return values

    @model_validator(mode="after")
    def tools_consistent(self) -> AgentDefinition:
        if set(self.allowed_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both allowed and forbidden")
        return self


class AgentTaskContract(StrictModel):
    schema_version: Literal["1.0.0"]
    task_id: str = Field(min_length=1, max_length=128)
    selected_agent_id: str | None = Field(default=None, max_length=128)
    task_type: str = Field(min_length=1, max_length=128)
    kind: AgentKind
    objective: str = Field(min_length=1, max_length=10000)
    project_id: str = Field(min_length=1, max_length=128)
    prompt_template: PromptTemplateRef
    sources: tuple[SourceReference, ...] = ()
    required_capabilities: tuple[CapabilityRequirement, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    tool_constraints: ToolConstraints = Field(default_factory=ToolConstraints)
    cost_constraints: CostConstraints = Field(default_factory=CostConstraints)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    required_evidence_types: tuple[str, ...] = ()
    allowed_output_paths: tuple[str, ...] = ()
    max_output_bytes: int = Field(default=64 * 1024 * 1024, ge=0, le=64 * 1024 * 1024)
    max_output_files: int = Field(default=16, ge=0, le=256)
    context_max_bytes: int = Field(default=256_000, ge=0, le=16_777_216)

    @field_validator("task_id", "task_type", "project_id", "selected_agent_id")
    @classmethod
    def safe_identifiers(cls, value: str) -> str:
        if value is None:
            return value
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier contains unsupported characters")
        return value

    @field_validator(
        "allowed_tools",
        "forbidden_tools",
        "dependencies",
        "acceptance_criteria",
        "required_evidence_types",
        "allowed_output_paths",
    )
    @classmethod
    def bounded_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("entries must be unique")
        if any(not value or len(value) > 1000 for value in values):
            raise ValueError("entries must be non-empty and bounded")
        return values

    @model_validator(mode="after")
    def consistent_constraints(self) -> AgentTaskContract:
        if set(self.allowed_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both allowed and forbidden")
        if set(self.tool_constraints.allowed_tools) - set(self.allowed_tools):
            raise ValueError("tool constraints cannot expand contract allowed_tools")
        if set(self.tool_constraints.forbidden_tools) - set(self.forbidden_tools):
            raise ValueError("tool constraints cannot expand contract forbidden_tools")
        if len({source.path for source in self.sources}) != len(self.sources):
            raise ValueError("source paths must be unique")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must be unique")
        for output_path in self.allowed_output_paths:
            _validate_relative_output_path(output_path)
        if len(set(self.allowed_output_paths)) != len(self.allowed_output_paths):
            raise ValueError("allowed_output_paths must be unique")
        _validate_noncolliding_paths(self.allowed_output_paths, "allowed_output_paths")
        return self


def _validate_relative_output_path(value: str) -> str:
    return _validate_relative_project_path(value)


def _validate_relative_project_path(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ValueError("path must be a non-empty trimmed string of at most 1024 characters")
    parts = value.split("/")
    filename = parts[-1]
    secret_file = filename.lower() in {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "authorized_keys",
        "id_rsa",
        "id_ed25519",
    }
    secret_suffix = any(
        filename.lower().endswith(suffix)
        for suffix in (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
    )
    invalid_part = any(
        ":" in part
        or any(char in part for char in '<>"|?*')
        or part.endswith((".", " "))
        or _DEVICE_COMPONENT.fullmatch(part)
        or any(ord(char) < 32 for char in part)
        for part in parts
    )
    secret_dir = any(part.lower() in _RESERVED_OUTPUT_DIRS for part in parts)
    if (
        "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in parts)
        or invalid_part
        or secret_dir
        or _SECRET_OUTPUT_NAME.search(filename)
        or secret_file
        or secret_suffix
        or filename.lower().startswith(".env")
    ):
        raise ValueError("path is absolute, traversing, reserved, or secret-bearing")
    return value


def _validate_noncolliding_paths(paths: tuple[str, ...], label: str) -> None:
    seen: set[str] = set()
    for value in paths:
        path = value.casefold()
        if path in seen:
            raise ValueError(f"{label} must not contain case-insensitive path aliases")
        parts = path.split("/")
        if any("/".join(parts[:depth]) in seen for depth in range(1, len(parts))):
            raise ValueError(f"{label} must not contain file/ancestor path collisions")
        if any(existing.startswith(path + "/") for existing in seen):
            raise ValueError(f"{label} must not contain file/ancestor path collisions")
        seen.add(path)


class ProposedFile(StrictModel):
    """A staged content proposal, never an instruction to write directly."""

    path: str = Field(min_length=1, max_length=1024)
    operation: Literal["CREATE", "UPDATE", "DELETE"]
    content_base64: str = Field(default="", max_length=90_000_000)
    before_sha256: str | None = None
    output_sha256: str | None = None

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return _validate_relative_output_path(value)

    @field_validator("before_sha256", "output_sha256")
    @classmethod
    def valid_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("file hash must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def verify_content_and_hashes(self) -> ProposedFile:
        try:
            content = b64decode(self.content_base64, validate=True)
        except (ValueError, TypeError) as err:
            raise ValueError("content_base64 must be valid base64") from err
        if len(content) > 64 * 1024 * 1024:
            raise ValueError("proposed file content exceeds 64 MiB")
        if self.operation == "CREATE" and self.before_sha256 is not None:
            raise ValueError("CREATE cannot claim an existing before_sha256")
        if self.operation == "UPDATE" and self.before_sha256 is None:
            raise ValueError("UPDATE requires before_sha256")
        if self.operation == "DELETE":
            if self.before_sha256 is None or self.output_sha256 is not None or content:
                raise ValueError("DELETE requires only before_sha256 and no output content")
        else:
            if self.output_sha256 is None:
                raise ValueError("CREATE and UPDATE require output_sha256")
            if hashlib.sha256(content).hexdigest() != self.output_sha256:
                raise ValueError("output_sha256 does not match proposed file content")
        return self


class ProposedChange(StrictModel):
    disposition: ChangeDisposition
    summary: str = Field(min_length=1, max_length=4000)
    proposed_files: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()


class EvidenceClaim(StrictModel):
    evidence_type: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    source_paths: tuple[str, ...] = ()
    sha256: str | None = None
    independently_verified: Literal[False] = False

    @field_validator("sha256")
    @classmethod
    def valid_evidence_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class VisualReview(StrictModel):
    """Advisory visual observations tied to exact selected image source hashes."""

    schema_version: Literal["agent-visual-review-1.0.0"]
    task_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    reviewed_sources: tuple[SourceReference, ...] = Field(min_length=1, max_length=64)
    conclusion: Literal["LIKELY_MATCH", "POSSIBLE_ISSUE", "INCONCLUSIVE"]
    findings: tuple[str, ...] = Field(min_length=1, max_length=128)
    criteria: tuple[str, ...] = ()
    authoritative_approval: Literal[False] = False

    @model_validator(mode="after")
    def unique_review_sources(self) -> VisualReview:
        paths = [source.path for source in self.reviewed_sources]
        if len(paths) != len(set(paths)):
            raise ValueError("visual review source paths must be unique")
        if sum(source.size_bytes for source in self.reviewed_sources) > 16 * 1024 * 1024:
            raise ValueError("visual review references exceed the bounded evidence size")
        return self

    @field_validator("findings", "criteria")
    @classmethod
    def bounded_unique_review_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not item.strip() or len(item) > 2000 for item in values
        ):
            raise ValueError("visual review text entries must be unique, non-empty and bounded")
        return values


class AgentResultProposal(StrictModel):
    schema_version: Literal["1.0.0"]
    task_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    outcome: AgentOutcome
    summary: str = Field(min_length=1, max_length=10000)
    changes: tuple[ProposedChange, ...] = ()
    proposed_files: tuple[ProposedFile, ...] = ()
    artifacts: tuple[str, ...] = ()
    evidence: tuple[EvidenceClaim, ...] = ()
    findings: tuple[str, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    requested_capabilities: tuple[str, ...] = ()
    requested_tools: tuple[str, ...] = ()
    requested_cost: CostConstraints | None = None

    @field_validator("task_id", "agent_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier contains unsupported characters")
        return value

    @field_validator(
        "artifacts",
        "findings",
        "recommended_next_actions",
        "blocking_issues",
        "requested_capabilities",
        "requested_tools",
    )
    @classmethod
    def bounded_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(not v or len(v) > 2000 for v in values):
            raise ValueError("entries must be unique, non-empty, and bounded")
        return values

    @model_validator(mode="after")
    def outcome_is_not_authority(self) -> AgentResultProposal:
        file_paths = [item.path for item in self.proposed_files]
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("proposed file paths must be unique")
        _validate_noncolliding_paths(tuple(file_paths), "proposed_files")
        if (
            self.outcome in {AgentOutcome.BLOCKED, AgentOutcome.ERROR, AgentOutcome.NEEDS_INPUT}
            and not self.blocking_issues
        ):
            raise ValueError("blocked, error, and needs_input outcomes require blocking_issues")
        if self.outcome == AgentOutcome.PROPOSED and any(
            change.disposition == ChangeDisposition.NO_CHANGE for change in self.changes
        ):
            raise ValueError("proposed outcome cannot contain NO_CHANGE dispositions")
        return self


class ValidatedAgentProposal(StrictModel):
    """Validated proposal claims; still non-authoritative and requires normal gates."""

    task_id: str
    agent_id: str
    outcome: AgentOutcome
    proposal: AgentResultProposal
    eligible_capabilities: tuple[str, ...]
    approval_required: bool
    authoritative_completion: Literal[False] = False


def export_agent_json_schemas() -> dict[str, dict[str, Any]]:
    """Return JSON Schema documents suitable for publishing with the package."""
    return {
        model.__name__: model.model_json_schema()
        for model in (AgentDefinition, AgentTaskContract, AgentResultProposal)
    }
