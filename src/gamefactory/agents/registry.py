"""Explicit registry and capability-aware deterministic agent routing."""

from __future__ import annotations

from dataclasses import dataclass

from gamefactory.core.domain.agent_contracts import AgentDefinition, AgentKind, AgentTaskContract
from gamefactory.core.domain.errors import ValidationError

_COST_RANK = {"LOCAL": 0, "FREE_EXTERNAL": 1, "METERED": 2, "PAID": 3, "EXPENSIVE": 4}


@dataclass(frozen=True)
class RegisteredAgent:
    definition: AgentDefinition
    enabled: bool = True


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    def register(self, definition: AgentDefinition, *, enabled: bool = True) -> None:
        if definition.agent_id in self._agents:
            raise ValidationError("Duplicate agent id", details={"agent_id": definition.agent_id})
        self._agents[definition.agent_id] = RegisteredAgent(definition, enabled)

    def get(self, agent_id: str) -> RegisteredAgent | None:
        return self._agents.get(agent_id)

    def list(self) -> tuple[RegisteredAgent, ...]:
        return tuple(self._agents[key] for key in sorted(self._agents))

    def route(
        self,
        contract: AgentTaskContract,
        *,
        available_capabilities: set[str] | frozenset[str],
    ) -> tuple[RegisteredAgent, ...]:
        available = set(available_capabilities)
        required = {item.name for item in contract.required_capabilities if item.required}
        if not required.issubset(available):
            raise ValidationError(
                "Required agent capabilities are unavailable",
                details={"missing": sorted(required - available)},
            )
        routed: list[RegisteredAgent] = []
        for registered in self.list():
            definition = registered.definition
            if not registered.enabled:
                continue
            if contract.kind not in definition.kinds and AgentKind.GENERAL not in definition.kinds:
                continue
            if not required.issubset(set(definition.capabilities)):
                continue
            if not set(contract.allowed_tools).issubset(definition.allowed_tools):
                continue
            if set(contract.forbidden_tools) & set(definition.allowed_tools):
                continue
            agent_cost = definition.cost
            task_cost = contract.cost_constraints
            if _COST_RANK[agent_cost.cost_class] > _COST_RANK[task_cost.cost_class]:
                continue
            if agent_cost.currency != task_cost.currency or agent_cost.unit != task_cost.unit:
                continue
            if agent_cost.max_amount > task_cost.max_amount:
                continue
            if agent_cost.approval_required and not task_cost.approval_required:
                continue
            if (
                agent_cost.fallback_currency != task_cost.fallback_currency
                or agent_cost.fallback_max_amount > task_cost.fallback_max_amount
            ):
                continue
            if agent_cost.fallback_approval_required and not task_cost.fallback_approval_required:
                continue
            routed.append(registered)
        return tuple(routed)
