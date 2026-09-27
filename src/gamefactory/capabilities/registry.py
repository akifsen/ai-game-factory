"""Capability registry cataloging environment, engine, tool, and provider capabilities.

Classifies capabilities as:
- AVAILABLE
- UNAVAILABLE
- MISCONFIGURED
- APPROVAL_REQUIRED
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.external.meshy import MeshyProvider


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


@dataclass
class CapabilityEntry:
    name: str
    description: str
    status: CapabilityStatus
    provider: str
    requires_approval: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "provider": self.provider,
            "requires_approval": self.requires_approval,
            "details": self.details,
        }


class CapabilityRegistry:
    """Discovers and catalogs capabilities in the current environment."""

    def __init__(
        self,
        godot_adapter: GodotAdapter | None = None,
        blender_adapter: BlenderAdapter | None = None,
        meshy_provider: MeshyProvider | None = None,
    ) -> None:
        self.godot_adapter = godot_adapter or GodotAdapter()
        self.blender_adapter = blender_adapter or BlenderAdapter()
        self.meshy_provider = meshy_provider or MeshyProvider()
        self._entries: dict[str, CapabilityEntry] = {}

    def discover(
        self,
        project_dir: Path | str | None = None,
        custom_godot_path: str | None = None,
        custom_blender_path: str | None = None,
    ) -> dict[str, CapabilityEntry]:
        """Perform discovery across engines, DCC, and external providers."""
        entries: dict[str, CapabilityEntry] = {}

        # 1. Godot Engine detection
        godot_res = self.godot_adapter.detect_engine(custom_path=custom_godot_path)
        godot_status = (
            CapabilityStatus.AVAILABLE
            if godot_res.available
            else (
                CapabilityStatus.MISCONFIGURED
                if godot_res.status == "MISCONFIGURED"
                else CapabilityStatus.UNAVAILABLE
            )
        )
        entries["engine.godot.detect"] = CapabilityEntry(
            name="engine.godot.detect",
            description="Detect and inspect Godot executable",
            status=godot_status,
            provider="godot",
            details={
                "version": godot_res.version,
                "executable_path": godot_res.executable_path,
                "details": godot_res.details,
            },
        )
        # 2. Blender DCC detection
        blender_res = self.blender_adapter.detect_tool(custom_path=custom_blender_path)
        blender_status = (
            CapabilityStatus.AVAILABLE
            if blender_res.available
            else (
                CapabilityStatus.MISCONFIGURED
                if blender_res.status == "MISCONFIGURED"
                else CapabilityStatus.UNAVAILABLE
            )
        )
        entries["dcc.blender.detect"] = CapabilityEntry(
            name="dcc.blender.detect",
            description="Detect and inspect Blender executable",
            status=blender_status,
            provider="blender",
            details={
                "version": blender_res.version,
                "executable_path": blender_res.executable_path,
                "details": blender_res.details,
            },
        )

        # 3. Meshy provider boundary
        is_meshy_configured = self.meshy_provider.is_configured()
        # Credentials can be present, but no production Meshy transport exists in V0.1.
        meshy_status = CapabilityStatus.UNAVAILABLE
        entries["asset.3d.meshy"] = CapabilityEntry(
            name="asset.3d.meshy",
            description="Meshy provider boundary (generation not implemented in V0.1)",
            status=meshy_status,
            provider="meshy",
            requires_approval=True,
            details={
                "is_configured": is_meshy_configured,
                "cost_class": "PAID",
                "milestone": "V0.1 boundary only",
                "implemented": False,
            },
        )

        # 4. Built-in Core capabilities
        entries["workflow.dag.execute"] = CapabilityEntry(
            name="workflow.dag.execute",
            description="Sequential DAG workflow orchestration with state machine",
            status=CapabilityStatus.AVAILABLE,
            provider="core",
        )
        entries["security.policy.enforce"] = CapabilityEntry(
            name="security.policy.enforce",
            description="Centralized cost and operation policy enforcement",
            status=CapabilityStatus.AVAILABLE,
            provider="core",
        )
        entries["artifacts.hash.verify"] = CapabilityEntry(
            name="artifacts.hash.verify",
            description="Cryptographic SHA-256 artifact verification and evidence collection",
            status=CapabilityStatus.AVAILABLE,
            provider="core",
        )

        self._entries = entries
        return entries

    def get(self, name: str) -> CapabilityEntry | None:
        return self._entries.get(name)

    def is_available(self, name: str) -> bool:
        entry = self.get(name)
        return entry is not None and entry.status in (
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.APPROVAL_REQUIRED,
        )
