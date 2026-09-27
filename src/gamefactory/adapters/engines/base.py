"""Engine adapter interface and data transfer objects."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EngineDetectionResult:
    available: bool
    engine_type: str
    version: str | None = None
    executable_path: str | None = None
    status: str = "UNAVAILABLE"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectInspectionResult:
    is_project: bool
    engine_type: str
    project_name: str | None = None
    project_file_path: str | None = None
    features: list[str] = field(default_factory=list)
    main_scene: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class EngineAdapter(ABC):
    """Abstract interface for game engine integrations."""

    @abstractmethod
    def detect_engine(self, custom_path: str | None = None) -> EngineDetectionResult:
        """Detect engine executable and query its version."""
        ...

    @abstractmethod
    def inspect_project(self, project_dir: Path | str) -> ProjectInspectionResult:
        """Inspect a local directory for engine-specific project files."""
        ...
