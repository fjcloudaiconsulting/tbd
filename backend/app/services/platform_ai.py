"""Platform-provided AI constants and the adapter factory (TBD-586).

The factory derives the adapter class, API root and key from
``platform_provider`` ALONE. It never reads a stored ``provider``,
``base_url``, ``base_url_is_api_root`` or key column and never decrypts, so a
tampered platform row cannot redirect the house key; a BYOK row never reaches
it.
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.ai_providers import NativeNotAvailable
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import (
    OPENAI_COMPATIBLE_PRESETS,
    OpenAICompatibleAdapter,
)

PLATFORM_PROVIDERS = ("openrouter", "openai", "anthropic", "gemini")

_ALL = ["chat", "embed", "structured_output", "function_call", "stream"]
# Fixed per provider; written by PR2's create action, never by the org.
PLATFORM_CAPABILITIES: dict[str, list[str]] = {
    "openrouter": list(_ALL),
    "openai": list(_ALL),
    "gemini": list(_ALL),
    "anthropic": [c for c in _ALL if c != "embed"],
}

# OpenAI chat models vetted to accept ``max_tokens`` (reasoning models need
# ``max_completion_tokens`` and would 400). C12: a forcing test pins every
# priced OpenAI chat model into this set.
OPENAI_ACCEPTS_MAX_TOKENS = frozenset({"gpt-4o", "gpt-4o-mini"})

_PRESET_URL = {p["key"]: p["base_url"] for p in OPENAI_COMPATIBLE_PRESETS}


def platform_key(platform_provider: str) -> str:
    return getattr(settings, f"platform_ai_{platform_provider}_api_key", "") or ""


def build_adapter(platform_provider: str) -> Any:
    key = platform_key(platform_provider)
    if platform_provider not in PLATFORM_PROVIDERS or not key:
        raise NativeNotAvailable("platform_provider_unavailable")
    if platform_provider == "openai":
        return OpenAIAdapter(api_key=key)
    if platform_provider == "anthropic":
        return AnthropicAdapter(api_key=key)
    return OpenAICompatibleAdapter(
        api_key=key,
        base_url=_PRESET_URL[platform_provider],
        base_url_is_api_root=True,
        extra_body=(
            {"provider": {"data_collection": "deny"}}
            if platform_provider == "openrouter" else {}
        ),
    )
