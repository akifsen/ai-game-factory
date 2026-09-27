"""DCC (Digital Content Creation) tool adapter interface and DTOs."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DccDetectionResult:
    available: bool
    tool_name: str
    version: str | None = None
    executable_path: str | None = None
    status: str = "UNAVAILABLE"
    details: dict[str, Any] = field(default_factory=dict)


class DccAdapter(ABC):
    """Abstract interface for DCC tool adapters."""

    @abstractmethod
    def detect_tool(self, custom_path: str | None = None) -> DccDetectionResult:
        """Detect DCC tool and query version."""
        ...
