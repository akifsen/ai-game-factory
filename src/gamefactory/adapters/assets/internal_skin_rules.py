"""ADR 0018 ``skin_internal`` rule group (tests and internal tooling only)."""

from __future__ import annotations

from pathlib import Path

from gamefactory.adapters.assets.validation_rules import RuleInput
from gamefactory.core.domain.asset_contracts import ValidationFinding
from gamefactory.core.domain.internal_skin_contract import InternalSkinContract


def _contract(inp: RuleInput) -> InternalSkinContract:
    contract = inp.extras.get("internal_skin_contract")
    if not isinstance(contract, InternalSkinContract):
        raise ValueError("internal_skin_contract missing from RuleInput.extras")
    return contract


def rule_internal_skin_bundle(inp: RuleInput) -> list[ValidationFinding]:
    from gamefactory.adapters.assets.internal_skin import validate_internal_skinned_glb

    contract = _contract(inp)
    path = Path(inp.artifact)
    result = validate_internal_skinned_glb(path, contract)
    return result.findings


SKIN_INTERNAL_RULES = (rule_internal_skin_bundle,)
