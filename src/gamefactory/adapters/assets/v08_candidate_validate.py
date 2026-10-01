"""Explicit V0.8-3A candidate GLB validation entrypoint."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_rules import (
    _PARSE_ERRORS,
    CandidateComposition,
    CandidateRuleContext,
    decode_candidate_glb,
    select_v08_candidate_composition,
)
from gamefactory.core.domain.asset_contracts import AssetValidationResult, ValidationFinding
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract
from gamefactory.core.domain.v08_candidate_contracts import (
    VALIDATION_REPORT_SCHEMA_V08_CANDIDATE,
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
    CandidateContractError,
    candidate_capabilities,
    load_packaged_candidate_profile,
    revalidate_candidate_binding,
)


def composition_for_v08_candidate(
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> CandidateComposition:
    return select_v08_candidate_composition(candidate_capabilities(spec, profile))


def validation_report_metadata(
    spec: AssetSpecificationV08Candidate,
    composition: CandidateComposition,
) -> dict[str, Any]:
    """Canonical production-candidate report fields (production_eligible stays false)."""
    return {
        "schema_version": VALIDATION_REPORT_SCHEMA_V08_CANDIDATE,
        "rule_groups": list(composition.groups),
        "composition": composition.name,
        "production_eligible": False,
        "candidate_state": "CLOSED",
        "public_status": "UNSUPPORTED",
        "profile": spec.profile,
        "profile_version": spec.profile_version,
        "source_kind": spec.source_kind,
        "origin_contract": spec.origin_contract,
        "visual_mesh_name": spec.visual_mesh_name,
        "rig_contract_id": spec.rig_contract_id,
    }


def validate_v08_candidate_glb(
    path: Path,
    spec: AssetSpecificationV08Candidate,
    *,
    profile: AssetProfileV08Candidate | None = None,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> AssetValidationResult:
    """Validate a processed GLB against a typed CLOSED candidate specification."""
    bound_profile = profile or load_packaged_candidate_profile()
    artifact = str(path)
    try:
        spec, bound_profile = revalidate_candidate_binding(spec, bound_profile)
    except CandidateContractError as exc:
        return AssetValidationResult(
            Severity.FAIL,
            [
                ValidationFinding(
                    "core_v08_candidate.binding",
                    Severity.FAIL,
                    "typed spec bound to canonical CLOSED candidate profile",
                    str(exc),
                    artifact,
                    "Candidate specification failed strict profile binding at validation boundary",
                )
            ],
            "candidate validation failed: profile binding",
        )
    try:
        composition = composition_for_v08_candidate(spec, bound_profile)
    except ValueError as exc:
        return AssetValidationResult(
            Severity.FAIL,
            [
                ValidationFinding(
                    "core_v08_candidate.composition",
                    Severity.FAIL,
                    "closed single_mesh humanoid_skin capsule composition",
                    str(exc),
                    artifact,
                    "Candidate validation composition rejected supplied capabilities",
                )
            ],
            "candidate validation failed: composition",
        )
    contract = bound_profile.processing_contract(spec)
    skin_contract = load_internal_skin_contract()
    if skin_contract.contract_id != spec.rig_contract_id:
        return AssetValidationResult(
            Severity.FAIL,
            [
                ValidationFinding(
                    "core_v08_candidate.rig.contract",
                    Severity.FAIL,
                    spec.rig_contract_id,
                    skin_contract.contract_id,
                    str(path),
                    "Packaged rig contract id mismatch",
                )
            ],
            "candidate validation failed: rig contract mismatch",
        )

    findings: list[ValidationFinding] = []
    if not path.is_file():
        findings.append(
            ValidationFinding(
                "core_v08_candidate.glb.exists",
                Severity.FAIL,
                "regular GLB file",
                "missing",
                artifact,
                "Artifact does not exist",
            )
        )
        return AssetValidationResult(Severity.FAIL, findings, "candidate validation failed")

    try:
        decoded = decode_candidate_glb(path, max_file_size_bytes=max_file_size_bytes)
        ctx = CandidateRuleContext(
            artifact=artifact,
            spec=spec,
            profile=bound_profile,
            contract=contract,
            decoded=decoded,
            skin_contract=skin_contract,
        )
        for rule in composition.rules:
            findings.extend(rule(ctx))
    except _PARSE_ERRORS as exc:
        findings.append(
            ValidationFinding(
                "core_v08_candidate.glb.parse",
                Severity.FAIL,
                "supported candidate skinned GLB subset",
                str(exc),
                artifact,
                "GLB could not be safely decoded for candidate validation",
            )
        )
    except struct.error as exc:
        findings.append(
            ValidationFinding(
                "core_v08_candidate.glb.parse",
                Severity.FAIL,
                "supported candidate skinned GLB subset",
                str(exc),
                artifact,
                "GLB could not be safely decoded for candidate validation",
            )
        )

    status = Severity.FAIL if any(f.severity == Severity.FAIL for f in findings) else Severity.PASS
    failures = sum(f.severity == Severity.FAIL for f in findings)
    return AssetValidationResult(
        status,
        findings,
        f"{'Passed' if not failures else 'Failed'} candidate validation ({failures} failing)",
    )
