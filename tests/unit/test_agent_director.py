"""Bounded, deterministic checks for untrusted agent capability routing."""

from __future__ import annotations

import pytest

from gamefactory.agents.director import Director
from gamefactory.agents.registry import AgentRegistry
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
)
from gamefactory.core.domain.errors import ValidationError


def contract(**overrides):
    values = {
        "schema_version": "1.0.0",
        "task_id": "task-1",
        "task_type": "design.level",
        "kind": AgentKind.DESIGN,
        "objective": "Propose a level layout",
        "project_id": "game",
        "prompt_template": PromptTemplateRef(template_id="director", version="1.0.0"),
        "required_capabilities": (CapabilityRequirement(name="design.level"),),
        "allowed_tools": ("planner",),
        "cost_constraints": CostConstraints(max_amount=0, cost_class="LOCAL"),
        "dependencies": ("base",),
        "required_evidence_types": ("design_notes",),
    }
    values.update(overrides)
    return AgentTaskContract(**values)


def setup():
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            schema_version="1.0.0",
            agent_id="agent.design",
            display_name="Designer",
            role="Design",
            kinds=(AgentKind.DESIGN,),
            capabilities=("design.level",),
            allowed_tools=("planner",),
            cost=CostConstraints(max_amount=0, cost_class="LOCAL"),
        )
    )
    proposal = AgentResultProposal(
        schema_version="1.0.0",
        task_id="task-1",
        agent_id="agent.design",
        outcome=AgentOutcome.PROPOSED,
        summary="Layout proposal",
        requested_capabilities=("design.level",),
        requested_tools=("planner",),
        evidence=(EvidenceClaim(evidence_type="design_notes", description="Layout rationale"),),
    )
    return registry, proposal


def test_director_validates_capabilities_dependencies_and_keeps_completion_non_authoritative():
    registry, proposal = setup()
    validated = Director().validate_proposal(
        contract(),
        proposal,
        registry,
        available_capabilities={"design.level"},
        completed_dependencies={"base"},
    )
    assert validated.authoritative_completion is False
    assert validated.eligible_capabilities == ("design.level",)


def test_director_rejects_missing_dependencies_unavailable_capabilities_and_untrusted_test_claims():
    registry, proposal = setup()
    with pytest.raises(ValidationError):
        Director().validate_proposal(
            contract(), proposal, registry, available_capabilities={"design.level"}
        )
    with pytest.raises(ValidationError):
        Director().validate_proposal(
            contract(),
            proposal,
            registry,
            available_capabilities=set(),
            completed_dependencies={"base"},
        )
    forged = proposal.model_copy(
        update={"evidence": (EvidenceClaim(evidence_type="test_passed", description="trust me"),)}
    )
    with pytest.raises(ValidationError):
        Director().validate_proposal(
            contract(),
            forged,
            registry,
            available_capabilities={"design.level"},
            completed_dependencies={"base"},
        )
