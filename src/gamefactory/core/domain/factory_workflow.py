"""Strict operator-authored manifests for the generic game production workflow."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.agent_contracts import AgentKind, CostConstraints, ToolConstraints

FACTORY_WORKFLOW_VERSION = "factory-workflow-1.0.0"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_GATES = frozenset({"code", "gameplay", "visual", "performance"})
_FAMILIES = frozenset({"design", "feature", "level", "ui", "image", "audio", "vision"})
_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)
_WINDOWS_INVALID = frozenset('<>"|?*')


class StrictWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkflowInput(StrictWorkflowModel):
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    source_task_id: str | None = None
    artifact_path: str | None = None
    source_gate: str | None = None
    source_gate_scope: str | None = None
    evidence_name: str | None = None
    purpose: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def one_source(self) -> WorkflowInput:
        direct = (
            self.path is not None
            and self.source_task_id is None
            and self.artifact_path is None
            and self.source_gate is None
            and self.source_gate_scope is None
            and self.evidence_name is None
        )
        generated = (
            self.path is None
            and self.source_task_id is not None
            and self.artifact_path is not None
            and self.source_gate is None
            and self.source_gate_scope is None
            and self.evidence_name is None
        )
        gate_evidence = (
            self.source_task_id is not None
            and self.source_gate is not None
            and self.source_gate_scope in {"predecessor", "combined_candidate"}
            and self.evidence_name is not None
            and self.artifact_path is None
            and self.path is not None
        )
        if sum((direct, generated, gate_evidence)) != 1:
            raise ValueError(
                "input must select one direct path, predecessor output, or named predecessor gate evidence"
            )
        if self.source_task_id is not None and not _ID.fullmatch(self.source_task_id):
            raise ValueError("source_task_id is invalid")
        if self.artifact_path is not None:
            self.relative_path(self.artifact_path)
        if self.source_gate is not None and self.source_gate not in _GATES:
            raise ValueError("source_gate is unsupported")
        if self.source_gate_scope is not None and self.source_gate_scope not in {
            "predecessor",
            "combined_candidate",
        }:
            raise ValueError("source_gate_scope must be predecessor or combined_candidate")
        if self.evidence_name is not None:
            if (
                not self.evidence_name
                or len(self.evidence_name) > 256
                or "/" in self.evidence_name
                or "\\" in self.evidence_name
                or any(ord(ch) < 32 for ch in self.evidence_name)
            ):
                raise ValueError("evidence_name must be a bounded file label, not a path")
        if gate_evidence:
            self.relative_path(self.path)
        return self

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        path = value
        if (
            "\\" in path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path)
            or any(
                p in {"", ".", ".."}
                or ":" in p
                or any(c in _WINDOWS_INVALID for c in p)
                or p.endswith((".", " "))
                or _DEVICE.fullmatch(p)
                or any(ord(c) < 32 for c in p)
                for p in path.split("/")
            )
        ):
            raise ValueError("input path must be normalized and project-relative")
        if any(
            part.lower()
            in {
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
                ".godot",
                ".verify-pytest",
                ".verify-venv",
                ".verification",
            }
            for part in path.split("/")
        ):
            raise ValueError("input path cannot enter reserved or secret directories")
        return path


class WorkflowTaskSpec(StrictWorkflowModel):
    task_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    family: str
    kind: AgentKind
    executor_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=10000)
    prompt_template_id: str = Field(min_length=1, max_length=128)
    prompt_template_version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    dependencies: tuple[str, ...] = ()
    inputs: tuple[WorkflowInput, ...] = ()
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    tool_constraints: ToolConstraints = Field(default_factory=ToolConstraints)
    output_scopes: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    required_gates: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_output_files: int = Field(default=16, ge=1, le=256)
    max_output_bytes: int = Field(default=2_000_000, ge=1, le=50_000_000)
    max_context_bytes: int = Field(default=256_000, ge=0, le=16_777_216)
    timeout_seconds: float = Field(default=300, gt=0, le=86400, allow_inf_nan=False)
    max_retries: int = Field(default=0, ge=0, le=3)
    cost: CostConstraints = Field(default_factory=CostConstraints)
    game_write: bool = False

    @field_validator("task_id", "executor_id", "agent_id", "prompt_template_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("identifier is invalid")
        return value

    @field_validator("family")
    @classmethod
    def known_family(cls, value: str) -> str:
        if value not in _FAMILIES:
            raise ValueError(f"family must be one of {sorted(_FAMILIES)}")
        return value

    @field_validator(
        "dependencies",
        "capabilities",
        "allowed_tools",
        "forbidden_tools",
        "output_scopes",
        "expected_artifacts",
        "required_gates",
        "acceptance_criteria",
    )
    @classmethod
    def unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(
            not value or len(value) > 1024 for value in values
        ):
            raise ValueError("entries must be unique, non-empty, and bounded")
        return values

    @model_validator(mode="after")
    def valid_scope_and_gates(self) -> WorkflowTaskSpec:
        if set(self.required_gates) - _GATES:
            raise ValueError("required_gates contains an unsupported deterministic gate")
        if (self.family == "vision") != (self.kind == AgentKind.VISION):
            raise ValueError(
                "vision family and read-only vision agent kind must be selected together"
            )
        if self.kind == AgentKind.VISION and (self.output_scopes or self.expected_artifacts):
            raise ValueError("vision tasks are read-only and cannot declare output files")
        if self.game_write and not self.output_scopes:
            raise ValueError("game_write tasks must declare output_scopes")
        if self.game_write and (
            not self.expected_artifacts or set(self.expected_artifacts) != set(self.output_scopes)
        ):
            raise ValueError("game_write expected_artifacts must list every exact output path")
        for path in self.output_scopes:
            normalized = path
            secret_file = re.search(
                r"(^|[._-])(secrets?|credentials?|tokens?|passwords?|private(?:[_-]?keys?)?)([._-]|$)",
                normalized.split("/")[-1],
                re.I,
            )
            reserved = {
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
                ".godot",
                ".verify-pytest",
                ".verify-venv",
                ".verification",
            }
            secret_suffix = normalized.lower().endswith(
                (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
            )
            if (
                "\\" in normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
                or any(
                    part in {"", ".", ".."}
                    or ":" in part
                    or any(c in _WINDOWS_INVALID for c in part)
                    or part.endswith((".", " "))
                    or _DEVICE.fullmatch(part)
                    or any(ord(c) < 32 for c in part)
                    for part in normalized.split("/")
                )
                or any(part.lower() in reserved for part in normalized.split("/"))
                or secret_file
                or secret_suffix
                or normalized.split("/")[-1].lower().startswith(".env")
            ):
                raise ValueError("output_scopes must contain exact normalized relative paths")
        folded_scopes = [path.casefold() for path in self.output_scopes]
        for index, path in enumerate(folded_scopes):
            for other in folded_scopes[index + 1 :]:
                if path == other or path.startswith(other + "/") or other.startswith(path + "/"):
                    raise ValueError(
                        "output scopes cannot alias by case or overlap as file/parent paths"
                    )
        if self.game_write and not (set(self.required_gates) & {"code", "gameplay", "performance"}):
            raise ValueError(
                "game_write tasks require a registered deterministic code/gameplay/performance gate"
            )
        if self.cost.fallback_max_amount != 0:
            raise ValueError("V1 provider workflows prohibit fallback spending")
        if (
            self.cost.cost_class in {"METERED", "PAID", "EXPENSIVE"}
            and not self.cost.approval_required
        ):
            raise ValueError("charged provider calls always require approval")
        if set(self.allowed_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both allowed and forbidden")
        if set(self.tool_constraints.allowed_tools) - set(self.allowed_tools) or set(
            self.tool_constraints.forbidden_tools
        ) - set(self.forbidden_tools):
            raise ValueError(
                "tool_constraints cannot expand the explicit allowed/forbidden tool lists"
            )
        try:
            json.dumps(self.parameters, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("parameters must be finite JSON") from exc
        return self


class FactoryWorkflowManifest(StrictWorkflowModel):
    schema_version: Literal["factory-workflow-1.0.0"]
    workflow_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    tasks: tuple[WorkflowTaskSpec, ...] = Field(min_length=1, max_length=256)
    require_human_game_acceptance: bool = True

    @field_validator("workflow_id", "project_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("workflow and project ids must be identifiers")
        return value

    @model_validator(mode="after")
    def valid_dependencies_and_acceptance(self) -> FactoryWorkflowManifest:
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            if set(task.dependencies) - known or task.task_id in task.dependencies:
                raise ValueError(f"task {task.task_id} has an unknown or self dependency")
            for source in task.inputs:
                if source.source_task_id is not None:
                    if source.source_task_id not in task.dependencies:
                        raise ValueError(
                            "generated artifact inputs require a direct task dependency"
                        )
                    producer = next(
                        item for item in self.tasks if item.task_id == source.source_task_id
                    )
                    if source.source_gate is not None:
                        if (
                            source.source_gate_scope == "combined_candidate"
                            and task.kind != AgentKind.VISION
                        ):
                            raise ValueError(
                                "combined_candidate gate evidence can only feed a vision task"
                            )
                        if (
                            source.source_gate_scope == "predecessor"
                            and source.source_gate not in producer.required_gates
                        ):
                            raise ValueError(
                                "gate evidence inputs require that gate on the predecessor task"
                            )
                        if (
                            source.source_gate_scope == "combined_candidate"
                            and not producer.game_write
                        ):
                            raise ValueError(
                                "combined_candidate gate evidence requires an explicit game-write dependency"
                            )
                    elif source.artifact_path not in producer.output_scopes:
                        raise ValueError(
                            "generated artifact input path must be declared by its producer"
                        )
        # Validate cycles here as well as in the runtime DAG builder, so invalid
        # manifests cannot be persisted as apparently valid workflow plans.
        graph = {task.task_id: task.dependencies for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("workflow dependencies must be acyclic")
            if node in visited:
                return
            visiting.add(node)
            for parent in graph[node]:
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
        for index, left in enumerate(self.tasks):
            for right in self.tasks[index + 1 :]:
                left_paths = [path.casefold() for path in left.output_scopes]
                right_paths = [path.casefold() for path in right.output_scopes]
                if any(
                    a == b or a.startswith(b + "/") or b.startswith(a + "/")
                    for a in left_paths
                    for b in right_paths
                ):
                    raise ValueError(
                        "output scopes may not alias by case or overlap as file/parent paths"
                    )
        writes = [task for task in self.tasks if task.game_write]
        if writes and not self.require_human_game_acceptance:
            raise ValueError("game writes require mandatory human acceptance")
        if writes and any(
            task.kind == AgentKind.VISION
            and (
                not any(
                    item.source_gate is not None and item.source_gate_scope == "combined_candidate"
                    for item in task.inputs
                )
                or any(
                    item.source_gate is not None and item.source_gate_scope != "combined_candidate"
                    for item in task.inputs
                )
            )
            for task in self.tasks
        ):
            raise ValueError(
                "visual providers in a game-write workflow must review combined_candidate captures"
            )
        charged = [
            task.cost
            for task in self.tasks
            if task.cost.cost_class in {"METERED", "PAID", "EXPENSIVE"}
        ]
        if len({(item.currency, item.unit) for item in charged}) > 1:
            raise ValueError("one workflow cannot mix cost currencies or units")
        return self

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ancestors(task_id: str, graph: dict[str, tuple[str, ...]]) -> set[str]:
    found: set[str] = set()
    stack = list(graph[task_id])
    while stack:
        item = stack.pop()
        if item not in found:
            found.add(item)
            stack.extend(graph[item])
    return found
