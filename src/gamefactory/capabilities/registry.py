"""Capability registry cataloging environment, engine, tool, and provider capabilities.

Classifies capabilities as:
- AVAILABLE
- UNAVAILABLE
- MISCONFIGURED
- APPROVAL_REQUIRED
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.external.meshy import MeshyProvider
from gamefactory.adapters.external.meshy_cli import MeshyCliRunner


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    NOT_VERIFIED = "NOT_VERIFIED"


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
        meshy_cli_runner: MeshyCliRunner | None = None,
    ) -> None:
        self.godot_adapter = godot_adapter or GodotAdapter()
        self.blender_adapter = blender_adapter or BlenderAdapter()
        self.meshy_provider = meshy_provider or MeshyProvider()
        self.meshy_cli_runner = meshy_cli_runner or MeshyCliRunner()
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
        entries["engine.godot.rendered_capture"] = CapabilityEntry(
            name="engine.godot.rendered_capture",
            description=(
                "Rendered viewport capture. Doctor does not open a window; "
                "only an explicit godot-capture run probes the renderer."
            ),
            status=(CapabilityStatus.NOT_VERIFIED if godot_res.available else godot_status),
            provider="godot",
            details={
                "executable_path": godot_res.executable_path,
                "runtime_renderer_probed": False,
                "headless_is_not_sufficient": True,
            },
        )
        entries["engine.godot.headless_verification"] = CapabilityEntry(
            name="engine.godot.headless_verification",
            description="Godot headless verification pipeline; an approved workflow probes the installed CLI before execution",
            status=(CapabilityStatus.NOT_VERIFIED if godot_res.available else godot_status),
            provider="godot",
            details={
                "executable_path": godot_res.executable_path,
                "engine_version": godot_res.version,
                "runtime_cli_probed": False,
                "requires_approval_when_configured": True,
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

        # Local CLI readiness is distinct from authorization and live API acceptance.
        # Doctor does not submit a model or probe the paid API.
        meshy = self.meshy_cli_runner.doctor()
        meshy_status = (
            CapabilityStatus.APPROVAL_REQUIRED
            if meshy.status == "AVAILABLE" and meshy.has_credential
            else CapabilityStatus.MISCONFIGURED
            if meshy.status in {"MISCONFIGURED", "CREDENTIAL_MISSING"}
            else CapabilityStatus.UNAVAILABLE
        )
        entries["asset.3d.meshy"] = CapabilityEntry(
            name="asset.3d.meshy",
            description="Meshy CLI 0.4.0 image-to-3D; separate concept and paid approvals required",
            status=meshy_status,
            provider="meshy",
            requires_approval=True,
            details={
                "is_configured": meshy.has_credential,
                "cost_class": "PAID",
                "milestone": "V0.4",
                "implemented": True,
                "local_status": meshy.status,
                "cli_version": meshy.cli_version,
                "node_version": meshy.node_version,
                "credential_source": meshy.credential_source,
                "api_verified": False,
                "generation_verified": False,
                "details": meshy.details,
            },
        )

        for name, description in {
            "asset.specification.validate": "Strict asset specification validation bound to a built-in profile",
            "asset.glb.validate": "Decoded GLB geometry, texture, LOD and collider validation",
            "asset.concept.ingest": "Bounded PNG ingestion with hash-bound local provenance",
            "asset.workflow.execute": "Profile-driven asset DAG with concept, paid and final human gates",
        }.items():
            entries[name] = CapabilityEntry(name, description, CapabilityStatus.AVAILABLE, "core")
        entries["asset.blender.process"] = CapabilityEntry(
            "asset.blender.process",
            "Deterministic profile-contract processing; real execution remains workflow evidence",
            CapabilityStatus.NOT_VERIFIED if blender_res.available else blender_status,
            "blender",
            details={"executable_path": blender_res.executable_path, "processing_probed": False},
        )
        # Keep existing identifiers compatible while exposing the V0.4 contract.
        entries["asset.3d.generate"] = replace(entries["asset.3d.meshy"], name="asset.3d.generate")
        entries["dcc.blender.process"] = replace(
            entries["asset.blender.process"], name="dcc.blender.process"
        )
        entries["engine.godot.import"] = CapabilityEntry(
            "engine.godot.import",
            "Staged GLB import; executable detection is not a successful asset import",
            CapabilityStatus.NOT_VERIFIED if godot_res.available else godot_status,
            "godot",
            details={"executable_path": godot_res.executable_path, "asset_import_probed": False},
        )
        entries["image.generate"] = CapabilityEntry(
            "image.generate",
            "ImageGenerationProvider contract and external concept ingestion; live generator not probed",
            CapabilityStatus.NOT_VERIFIED,
            "external_image_provider",
            details={
                "provider_contract_implemented": True,
                "fake_provider_available": True,
                "external_generation_verified": False,
                "ingestion_available": True,
                "reason": "Generate with an authorized image provider, then ingest hash-bound provenance",
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
