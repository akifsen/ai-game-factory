from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import ValidationError

from gamefactory.core.domain.agent_contracts import (
    AgentDefinition,
    AgentKind,
    AgentOutcome,
    AgentResultProposal,
    AgentTaskContract,
    CapabilityRequirement,
    CostConstraints,
    EvidenceClaim,
    PromptTemplateRef,
    ProposedFile,
    SourceReference,
    ToolConstraints,
    export_agent_json_schemas,
)


def task(**overrides):
    values = {
        "schema_version": "1.0.0",
        "task_id": "task-1",
        "task_type": "code.change",
        "kind": AgentKind.CODE,
        "objective": "Propose a bounded code change",
        "project_id": "project-1",
        "prompt_template": PromptTemplateRef(template_id="code", version="1.2.0"),
        "sources": (
            SourceReference(
                path="src/main.py", sha256="a" * 64, size_bytes=10, purpose="Relevant module"
            ),
        ),
        "required_capabilities": (CapabilityRequirement(name="code.edit"),),
        "allowed_tools": ("patch",),
        "forbidden_tools": ("shell",),
        "tool_constraints": ToolConstraints(allowed_tools=("patch",), forbidden_tools=("shell",)),
        "cost_constraints": CostConstraints(max_amount=0, cost_class="LOCAL"),
        "dependencies": (),
        "acceptance_criteria": ("change proposed",),
        "required_evidence_types": ("review_notes",),
    }
    values.update(overrides)
    return AgentTaskContract(**values)


def definition(**overrides):
    values = {
        "schema_version": "1.0.0",
        "agent_id": "agent.code",
        "display_name": "Code",
        "role": "Propose code changes",
        "kinds": (AgentKind.CODE,),
        "capabilities": ("code.edit",),
        "allowed_tools": ("patch",),
        "forbidden_tools": ("shell",),
        "cost": CostConstraints(max_amount=0, cost_class="LOCAL"),
    }
    values.update(overrides)
    return AgentDefinition(**values)


def test_contracts_reject_unknown_and_incompatible_tools():
    with pytest.raises(ValidationError):
        task(unknown_field=True)
    with pytest.raises(ValidationError):
        task(allowed_tools=("shell",))


def test_contracts_export_versioned_json_schemas():
    schemas = export_agent_json_schemas()
    assert set(schemas) == {"AgentDefinition", "AgentTaskContract", "AgentResultProposal"}
    assert schemas["AgentTaskContract"]["properties"]["schema_version"]["const"] == "1.0.0"


def test_result_proposal_keeps_agent_claims_unverified():
    result = AgentResultProposal(
        schema_version="1.0.0",
        task_id="task-1",
        agent_id="agent.code",
        outcome=AgentOutcome.PROPOSED,
        summary="Proposed change",
        evidence=(EvidenceClaim(evidence_type="review_notes", description="Reviewed"),),
    )
    assert result.outcome == AgentOutcome.PROPOSED
    assert result.evidence[0].independently_verified is False
    assert hashlib.sha256(b"fixture").hexdigest() != result.evidence[0].sha256


def test_strict_models_reject_python_string_enum_coercion_but_accept_json():
    raw = task().model_dump_json()
    assert AgentTaskContract.model_validate_json(raw).kind == AgentKind.CODE
    values = task().model_dump()
    values["kind"] = "code"
    with pytest.raises(ValidationError):
        AgentTaskContract.model_validate(values)


def test_proposed_file_binds_content_hash_and_explicit_scope():
    content = b"print('hello')\n"
    item = ProposedFile(
        path="src/new_game.py",
        operation="CREATE",
        content_base64=base64.b64encode(content).decode("ascii"),
        output_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert item.path == "src/new_game.py"
    with pytest.raises(ValidationError):
        ProposedFile(
            path="../escape.py",
            operation="CREATE",
            content_base64="",
            output_sha256=hashlib.sha256(b"").hexdigest(),
        )


@pytest.mark.parametrize(
    "path",
    [
        "CON",
        "assets/name. ",
        "C:/game.cfg",
        "assets/.env.local",
        "assets/private-key.pem",
        "src/.agents/hidden.py",
        "assets/file:stream",
        "assets/bad?.png",
        "assets/bad|name.png",
    ],
)
def test_source_and_output_paths_match_factory_portability_and_secret_rules(path):
    with pytest.raises(ValidationError):
        SourceReference(path=path, sha256="a" * 64, size_bytes=0, purpose="test")
    with pytest.raises(ValidationError):
        ProposedFile(
            path=path,
            operation="CREATE",
            content_base64="",
            output_sha256=hashlib.sha256(b"").hexdigest(),
        )


@pytest.mark.parametrize(
    "paths",
    [
        ("Main.gd", "main.gd"),
        ("levels", "levels/main.gd"),
        ("assets/ui", "assets/UI/icon.png", "assets/ui/theme.tres"),
    ],
)
def test_task_output_paths_reject_portable_aliases_and_ancestor_collisions(paths):
    with pytest.raises(ValidationError):
        task(allowed_output_paths=paths)


@pytest.mark.parametrize(
    "paths",
    [
        ("Main.gd", "main.gd"),
        ("levels", "levels/main.gd"),
        ("assets/ui", "assets/UI/icon.png"),
    ],
)
def test_agent_proposal_rejects_portable_aliases_and_ancestor_collisions(paths):
    files = tuple(
        ProposedFile(
            path=path,
            operation="CREATE",
            content_base64="",
            output_sha256=hashlib.sha256(b"").hexdigest(),
        )
        for path in paths
    )
    with pytest.raises(ValidationError):
        AgentResultProposal(
            schema_version="1.0.0",
            task_id="task-1",
            agent_id="agent.code",
            outcome=AgentOutcome.PROPOSED,
            summary="Bounded proposal",
            proposed_files=files,
        )
