"""Integration tests for CapabilityRegistry."""

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
            CapabilityStatus.UNAVAILABLE,
        )
