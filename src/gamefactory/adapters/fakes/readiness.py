"""Fake production readiness probes for testing and offline fixtures."""

from __future__ import annotations

from gamefactory.workflows.production_readiness import (
    ReadinessCheck,
    ReadinessContext,
    ReadinessProbes,
)


class PassingReadinessProbes(ReadinessProbes):
    """Readiness probe test double where every check reports PASS."""

    def evaluate(self, context: ReadinessContext) -> list[ReadinessCheck]:
        return [
            ReadinessCheck(
                name="provider_available",
                category="provider",
                status="PASS",
                critical=True,
                detail="Provider is available and configured",
                observed={"provider": getattr(context.provider, "name", "fake")},
            ),
            ReadinessCheck(
                name="blender_available",
                category="tools",
                status="PASS",
                critical=True,
                detail="Blender executable is available",
                observed={"path": context.blender_path or "mock_blender"},
            ),
            ReadinessCheck(
                name="godot_available",
                category="tools",
                status="PASS",
                critical=True,
                detail="Godot executable is available",
                observed={"path": context.godot_path or "mock_godot"},
            ),
            ReadinessCheck(
                name="filesystem_writable",
                category="filesystem",
                status="PASS",
                critical=True,
                detail="Asset workspace is writable",
                observed={"asset_dir": str(context.asset_dir)},
            ),
            ReadinessCheck(
                name="profile_supported",
                category="profile",
                status="PASS",
                critical=False,
                detail="Profile is supported and verified",
                observed={"profile": str(context.profile)},
            ),
        ]

    def recheck_critical(self, context: ReadinessContext) -> list[ReadinessCheck]:
        return [c for c in self.evaluate(context) if c.critical]
