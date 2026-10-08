"""Operator settings for platform-provided AI (TBD-586). Reads ``SystemSetting``
ONLY (never ``OrgSetting``: an org must not be able to flip any of these).

Every read fails closed: absent or malformed means off, 0, or an empty
allowlist."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting
from app.services.platform_ai import PLATFORM_PROVIDERS

ENABLED = "platform_ai.enabled"
GLOBAL_MONTHLY_CENTS = "platform_ai.global_monthly_cents"
MODELS = "platform_ai.models"


@dataclass(frozen=True)
class PlatformAISettings:
    enabled: bool = False
    global_monthly_cents: int = 0
    models: dict[str, list[str]] = field(default_factory=dict)


def _models(raw: str | None) -> dict[str, list[str]]:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for provider in PLATFORM_PROVIDERS:
        v = data.get(provider)
        if isinstance(v, list) and v and all(isinstance(m, str) and m for m in v):
            out[provider] = list(v)
    return out


async def load(db: AsyncSession) -> PlatformAISettings:
    rows = dict(
        (await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_((ENABLED, GLOBAL_MONTHLY_CENTS, MODELS))
            )
        )).all()
    )
    cents = rows.get(GLOBAL_MONTHLY_CENTS) or ""
    return PlatformAISettings(
        enabled=rows.get(ENABLED) == "on",
        global_monthly_cents=int(cents) if cents.isascii() and cents.isdigit() else 0,
        models=_models(rows.get(MODELS)),
    )
