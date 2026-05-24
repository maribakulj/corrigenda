"""Provider registry."""

from __future__ import annotations

from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import BaseProvider
from app.providers.google_provider import GoogleProvider
from app.providers.mistral_provider import MistralProvider
from app.providers.openai_provider import OpenAIProvider
from app.schemas import Provider

_REGISTRY: dict[Provider, BaseProvider] = {
    Provider.OPENAI: OpenAIProvider(),
    Provider.ANTHROPIC: AnthropicProvider(),
    Provider.MISTRAL: MistralProvider(),
    Provider.GOOGLE: GoogleProvider(),
}


def get_provider(provider: Provider) -> BaseProvider:
    return _REGISTRY[provider]


__all__ = [
    "AnthropicProvider",
    "BaseProvider",
    "GoogleProvider",
    "MistralProvider",
    "OpenAIProvider",
    "get_provider",
]
