"""Platform-provided AI: OpenRouter only, driven by the ``platform_ai_models``
catalog (TBD-586).

The factory derives the adapter, API root and key from ``platform_provider``
ALONE. It never reads a stored ``provider``, ``base_url``,
``base_url_is_api_root`` or key column and never decrypts, so a tampered
platform row cannot redirect the house key; a BYOK row never reaches it. The
key string never leaves this module: callers get ``platform_key_configured``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.config import settings
from app.models.platform_ai_model import PlatformAIModel
from app.services.ai_pricing import cost_tier
from app.services.ai_providers import NativeNotAvailable
from app.services.ai_providers.openai_compatible import (
    OPENAI_COMPATIBLE_PRESETS,
    OpenAICompatibleAdapter,
)
from app.services.scheduler.jobs import platform_ai_catalog

PlatformProvider = Literal["openrouter"]
PLATFORM_PROVIDERS: tuple[str, ...] = get_args(PlatformProvider)
PLATFORM_LABEL = "Included AI"
# No embed: the embeddings body never carries ``extra_body`` (D7).
PLATFORM_CAPABILITIES: tuple[str, ...] = ("chat", "structured_output", "function_call", "stream")

MIN_CONTEXT = 128_000
PLATFORM_MAX_OUTPUT_TOKENS = 4096
PLATFORM_MIN_OUTPUT_TOKENS = 1024
TIER_ORDER = {"budget": 0, "standard": 1, "frontier": 2}

_ROOT = next(p["base_url"] for p in OPENAI_COMPATIBLE_PRESETS if p["key"] == "openrouter")
# Request-wide, so it binds every endpoint and fallback model OpenRouter uses.
_PROVIDER = {"zdr": True, "data_collection": "deny", "require_parameters": True, "allow_fallbacks": True}


def platform_key_configured(platform_provider: str) -> bool:
    return platform_provider == "openrouter" and bool(settings.platform_ai_openrouter_api_key)


class _PlatformAdapter(OpenAICompatibleAdapter):
    async def embed(self, **kw):
        # The embeddings body never merges ``extra_body``, so it would reach
        # OpenRouter without zdr / data_collection=deny.
        raise NativeNotAvailable("platform_embed_refused")


def build_adapter(
    platform_provider: str, *, models: list[str], max_price: tuple[int, int]
) -> OpenAICompatibleAdapter:
    """``max_price`` is the reserved (padded) micro-USD per Mtok pair. The
    privacy prefs are constants here, never caller input."""
    key = settings.platform_ai_openrouter_api_key
    if platform_provider != "openrouter" or not key:
        raise NativeNotAvailable("platform_provider_unavailable")
    p, c = max_price
    extra_body: dict = {"provider": {
        **_PROVIDER, "max_price": {"prompt": p / 1e6, "completion": c / 1e6, "request": 0},
    }}
    if len(models) > 1:
        extra_body["models"] = list(models)
    return _PlatformAdapter(
        api_key=key, base_url=_ROOT, base_url_is_api_root=True, extra_body=extra_body,
    )


def can(row: PlatformAIModel, capability: str) -> bool:
    return bool(
        capability in PLATFORM_CAPABILITIES
        and (capability != "structured_output" or row.supports_structured_outputs)
        and row.context_length >= MIN_CONTEXT
        and row.max_completion_tokens is not None
        and row.max_completion_tokens >= PLATFORM_MAX_OUTPUT_TOKENS
    )


def blend(row: PlatformAIModel) -> int:
    return 3 * row.zdr_prompt_micros_per_mtok + row.zdr_completion_micros_per_mtok


def tier(row: PlatformAIModel) -> str:
    return cost_tier(row.zdr_prompt_micros_per_mtok, row.zdr_completion_micros_per_mtok)


def cost_micros(pt: int, ct: int, p: int, c: int) -> int:
    """Tokens at micro-USD per Mtok, rounded up."""
    return -(-(pt * p + ct * c) // 1_000_000)


async def candidates(db: AsyncSession, capability: str) -> list[PlatformAIModel]:
    """Offerable rows that can serve ``capability``, cheapest blend first."""
    now = utcnow_naive()
    rows = (await db.scalars(select(PlatformAIModel).where(PlatformAIModel.enabled.is_(True)))).all()
    return sorted(
        (r for r in rows if platform_ai_catalog.offerable(r, now) and can(r, capability)),
        key=lambda r: (blend(r), r.openrouter_id),
    )


async def offerable_ids(db: AsyncSession) -> list[str]:
    return [r.openrouter_id for r in await candidates(db, "function_call")]


@dataclass(frozen=True)
class Pick:
    model: str
    models: list[str]  # [chosen] or [chosen, fallback]
    price: tuple[int, int]  # componentwise max over ``models``, +1 micro/Mtok each


async def pick(db: AsyncSession, capability: str, routed: str | None) -> Pick | None:
    """The routed model when it is a candidate, else the cheapest; plus the
    first other candidate no dearer by tier as OpenRouter's fallback."""
    cands = await candidates(db, capability)
    if not cands:
        return None
    chosen = next((r for r in cands if r.openrouter_id == routed), cands[0])
    fallback = next(
        (r for r in cands if r is not chosen and TIER_ORDER[tier(r)] <= TIER_ORDER[tier(chosen)]),
        None,
    )
    pair = [chosen] if fallback is None else [chosen, fallback]
    # +1: padding against the float comparison at OpenRouter.
    price = (
        max(r.zdr_prompt_micros_per_mtok for r in pair) + 1,
        max(r.zdr_completion_micros_per_mtok for r in pair) + 1,
    )
    return Pick(chosen.openrouter_id, [r.openrouter_id for r in pair], price)
