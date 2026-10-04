"""Explicitly configured agent execution adapters."""

from gamefactory.adapters.agents.base import (
    AgentProvider,
    AudioGenerationProvider,
    ImageGenerationProvider,
    ProviderReadiness,
    ProviderRegistry,
    ProviderStatus,
    VisionReviewProvider,
)
from gamefactory.adapters.agents.codex import CodexAgentProvider
from gamefactory.adapters.agents.openai_media import OpenAIAudioSpeechProvider, OpenAIImageProvider
from gamefactory.adapters.agents.openai_vision import OpenAIVisionConfig, OpenAIVisionReviewProvider
from gamefactory.adapters.agents.process_provider import (
    OperatorProcessAgentProvider,
    ProcessProviderConfig,
)
from gamefactory.adapters.agents.registry import ExplicitProviderRegistry

__all__ = [
    "AgentProvider",
    "AudioGenerationProvider",
    "CodexAgentProvider",
    "ImageGenerationProvider",
    "OpenAIAudioSpeechProvider",
    "OpenAIImageProvider",
    "OpenAIVisionConfig",
    "OpenAIVisionReviewProvider",
    "OperatorProcessAgentProvider",
    "ProcessProviderConfig",
    "ExplicitProviderRegistry",
    "ProviderReadiness",
    "ProviderRegistry",
    "ProviderStatus",
    "VisionReviewProvider",
]
