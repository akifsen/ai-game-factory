"""Integration tests for CapabilityRegistry."""

from unittest.mock import MagicMock

import pytest

from gamefactory.adapters.external.meshy_cli import MeshyCliRunner, MeshyDoctorResult
from gamefactory.capabilities.registry import (
    CapabilityRegistry,
    CapabilityStatus,
)


class TestCapabilityRegistry:
    def test_discovery_contains_core_capabilities(self) -> None:
        reg = CapabilityRegistry()
        capabilities = reg.discover()

        assert "workflow.dag.execute" in capabilities
        assert "security.policy.enforce" in capabilities
        assert "artifacts.hash.verify" in capabilities
        assert "engine.godot.detect" in capabilities
        assert "dcc.blender.detect" in capabilities
        assert "asset.3d.meshy" in capabilities

        # Core capabilities are AVAILABLE
        assert capabilities["workflow.dag.execute"].status == CapabilityStatus.AVAILABLE
        assert capabilities["security.policy.enforce"].status == CapabilityStatus.AVAILABLE

    def test_meshy_capability_classified_with_approval_or_unavailable(self) -> None:
        reg = CapabilityRegistry()
        capabilities = reg.discover()
        meshy_entry = capabilities["asset.3d.meshy"]

        assert meshy_entry.provider == "meshy"
        assert meshy_entry.requires_approval is True
        assert meshy_entry.status in (
            CapabilityStatus.APPROVAL_REQUIRED,
            CapabilityStatus.MISCONFIGURED,
            CapabilityStatus.UNAVAILABLE,
        )


@pytest.mark.parametrize(
    ("local_status", "credential", "expected"),
    [
        ("AVAILABLE", True, CapabilityStatus.APPROVAL_REQUIRED),
        ("CREDENTIAL_MISSING", False, CapabilityStatus.MISCONFIGURED),
        ("MISCONFIGURED", True, CapabilityStatus.MISCONFIGURED),
        ("UNAVAILABLE", False, CapabilityStatus.UNAVAILABLE),
    ],
)
def test_meshy_local_readiness_never_implies_paid_authorization(
    local_status: str, credential: bool, expected: CapabilityStatus
) -> None:
    runner = MagicMock(spec=MeshyCliRunner)
    runner.doctor.return_value = MeshyDoctorResult(
        available=local_status in {"AVAILABLE", "CREDENTIAL_MISSING"},
        status=local_status,
        has_credential=credential,
    )
    entries = CapabilityRegistry(meshy_cli_runner=runner).discover()
    entry = entries["asset.3d.meshy"]
    assert entry.status == expected
    assert entry.requires_approval
    assert entry.details["api_verified"] is False
    assert entry.details["generation_verified"] is False
    assert entries["asset.specification.validate"].status == CapabilityStatus.AVAILABLE
    assert entries["asset.3d.generate"].status == expected
    assert entries["asset.3d.generate"].requires_approval
    assert entries["dcc.blender.process"].status == entries["asset.blender.process"].status
    assert entries["engine.godot.import"].details["asset_import_probed"] is False
    assert entries["image.generate"].status == CapabilityStatus.NOT_VERIFIED
    assert entries["image.generate"].details["external_generation_verified"] is False
    runner.doctor.assert_called_once_with()
