"""Fake adapters package for testing and deterministic workflows."""

from gamefactory.adapters.fakes.fake_provider import (
    FakeAgentProvider,
    FakeAssetGenerationProvider,
)

__all__ = ["FakeAgentProvider", "FakeAssetGenerationProvider"]
