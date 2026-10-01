"""Internal-only skinned GLB validation entrypoint (ADR 0017/0019).

Not reachable from production ``validate_glb`` / ``select_composition``.
"""

from __future__ import annotations

import struct
from pathlib import Path

from gamefactory.adapters.assets.internal_skin_decode import (
    InvalidInternalSkinGLB,
    decode_internal_skinned_glb,
)
from gamefactory.adapters.assets.internal_skin_validate import run_internal_skin_checks
from gamefactory.core.domain.asset_contracts import AssetValidationResult, ValidationFinding
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.internal_skin_contract import InternalSkinContract

_PARSE_ERRORS = (
    OSError,
    InvalidInternalSkinGLB,
    KeyError,
    TypeError,
    ValueError,
    IndexError,
    struct.error,
    AttributeError,
    RecursionError,
)


def validate_internal_skinned_glb(
    path: Path,
    contract: InternalSkinContract,
    *,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> AssetValidationResult:
    """Validate one GLB against the internal skin contract."""
    artifact = str(path)
    findings: list[ValidationFinding] = []
    if not path.is_file():
        findings.append(
            ValidationFinding(
                "internal_skin.exists",
                Severity.FAIL,
                "regular GLB file",
                "missing",
                artifact,
                "Artifact does not exist",
            )
        )
        return AssetValidationResult(Severity.FAIL, findings, "internal skin validation failed")
    try:
        decoded = decode_internal_skinned_glb(path, max_file_size_bytes=max_file_size_bytes)
        findings.extend(run_internal_skin_checks(decoded, contract))
    except _PARSE_ERRORS as exc:
        findings.append(
            ValidationFinding(
                "internal_skin.parse",
                Severity.FAIL,
                "supported internal skinned GLB subset",
                str(exc),
                artifact,
                "GLB could not be safely decoded for internal skin validation",
            )
        )
    status = Severity.FAIL if any(f.severity == Severity.FAIL for f in findings) else Severity.PASS
    failures = sum(f.severity == Severity.FAIL for f in findings)
    return AssetValidationResult(
        status,
        findings,
        f"{'Passed' if not failures else 'Failed'} internal skin validation ({failures} failing)",
    )


def internal_skin_rule_group_ids() -> tuple[str, ...]:
    """Closed ``skin_internal`` group ids (ADR 0018); not used in production composition."""
    from gamefactory.adapters.assets.internal_skin_rules import SKIN_INTERNAL_RULES

    return tuple(rule.__name__ for rule in SKIN_INTERNAL_RULES)
