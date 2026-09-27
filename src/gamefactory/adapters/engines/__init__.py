"""Engine adapters package."""

from gamefactory.adapters.engines.base import (
    EngineAdapter,
    EngineDetectionResult,
    ProjectInspectionResult,
)
from gamefactory.adapters.engines.godot import GodotAdapter

__all__ = [
    "EngineAdapter",
    "EngineDetectionResult",
    "GodotAdapter",
    "ProjectInspectionResult",
]
