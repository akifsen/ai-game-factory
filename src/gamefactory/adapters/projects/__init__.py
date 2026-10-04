"""Native project discovery and onboarding adapters."""

from .discovery import discover_project
from .onboarding import create_godot_project

__all__ = ["discover_project", "create_godot_project"]
