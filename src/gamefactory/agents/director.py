"""Deterministic validation of untrusted Director proposals."""

from __future__ import annotations

from gamefactory.agents.registry import _COST_RANK, AgentRegistry
from gamefactory.core.domain.agent_contracts import (
    AgentOutcome,
    AgentResultProposal,
    AgentTaskContract,
    ValidatedAgentProposal,
)
from gamefactory.core.domain.errors import ValidationError


class Director:
    """Validate proposals while leaving all authoritative state to workflows."""

    def validate_proposal(
        self,
        contract: AgentTaskContract,
        proposal: AgentResultProposal,
        registry: AgentRegistry,
        *,
        available_capabilities: set[str] | frozenset[str],
        completed_dependencies: set[str] | frozenset[str] = frozenset(),
    ) -> ValidatedAgentProposal:
        if proposal.task_id != contract.task_id:
            raise ValidationError("Agent proposal task_id does not match contract")
        if contract.task_id in contract.dependencies:
            raise ValidationError("Task cannot depend on itself")
        missing_dependencies = set(contract.dependencies) - set(completed_dependencies)
        if missing_dependencies:
            raise ValidationError(
                "Task dependencies are not satisfied",
                details={"missing": sorted(missing_dependencies)},
            )
        eligible = registry.route(contract, available_capabilities=available_capabilities)
        registered = registry.get(proposal.agent_id)
        if registered is None or registered not in eligible:
            raise ValidationError(
                "Proposal agent is not registered, enabled, and eligible",
                details={"agent_id": proposal.agent_id},
            )
        definition = registered.definition
        if not set(proposal.requested_capabilities).issubset(definition.capabilities):
            raise ValidationError("Proposal requests an unregistered agent capability")
        if not set(proposal.requested_capabilities).issubset(available_capabilities):
            raise ValidationError("Proposal requests an unavailable capability")
        allowed_tools = set(contract.allowed_tools) & set(definition.allowed_tools)
        if not set(proposal.requested_tools).issubset(allowed_tools):
            raise ValidationError(
                "Proposal requests tools outside the contract and agent definition"
            )
        forbidden = set(contract.forbidden_tools) | set(definition.forbidden_tools)
        if set(proposal.requested_tools) & forbidden:
            raise ValidationError("Proposal requests a forbidden tool")
        if proposal.requested_cost is not None:
            requested, ceiling = proposal.requested_cost, contract.cost_constraints
            if (
                requested.currency != ceiling.currency
                or requested.unit != ceiling.unit
                or _COST_RANK[requested.cost_class] > _COST_RANK[ceiling.cost_class]
                or requested.max_amount > ceiling.max_amount
                or requested.approval_required
                and not ceiling.approval_required
            ):
                raise ValidationError("Proposal cost exceeds the task contract")
            if (
                requested.fallback_currency != ceiling.fallback_currency
                or requested.fallback_max_amount > ceiling.fallback_max_amount
                or requested.fallback_approval_required
                and not ceiling.fallback_approval_required
            ):
                raise ValidationError("Proposal fallback cost exceeds the task contract")
        known_sources = {source.path: source for source in contract.sources}
        for evidence in proposal.evidence:
            if not set(evidence.source_paths).issubset(known_sources):
                raise ValidationError("Evidence references a source outside the task contract")
            if evidence.sha256 and not any(
                known_sources[path].sha256 == evidence.sha256 for path in evidence.source_paths
            ):
                raise ValidationError("Evidence hash does not match an explicit contract source")
            if evidence.evidence_type.lower() in {
                "test_passed",
                "tests_passed",
                "validated",
                "completed",
                "approved",
            }:
                raise ValidationError(
                    "Agent evidence cannot claim authoritative verification or approval"
                )
        if proposal.outcome == AgentOutcome.PROPOSED:
            supplied = {claim.evidence_type for claim in proposal.evidence}
            missing = set(contract.required_evidence_types) - supplied
            if missing:
                raise ValidationError(
                    "Proposal omits required evidence claims", details={"missing": sorted(missing)}
                )
        known_sources = {source.path: source for source in contract.sources}
        for file_change in proposal.proposed_files:
            if not _path_in_scopes(file_change.path, contract.allowed_output_paths):
                raise ValidationError(
                    "Proposed file is outside the task output scope",
                    details={"path": file_change.path},
                )
            current = known_sources.get(file_change.path)
            if file_change.operation == "UPDATE":
                if current is None or current.sha256 != file_change.before_sha256:
                    raise ValidationError(
                        "Proposed file before hash does not match explicit context",
                        details={"path": file_change.path},
                    )
            elif file_change.operation == "CREATE" and current is not None:
                raise ValidationError(
                    "CREATE proposal targets an existing context file",
                    details={"path": file_change.path},
                )
        return ValidatedAgentProposal(
            task_id=contract.task_id,
            agent_id=proposal.agent_id,
            outcome=proposal.outcome,
            proposal=proposal,
            eligible_capabilities=tuple(
                sorted(set(proposal.requested_capabilities) & set(available_capabilities))
            ),
            approval_required=contract.cost_constraints.approval_required,
        )


def _path_in_scopes(path: str, scopes: tuple[str, ...]) -> bool:
    return path in scopes
