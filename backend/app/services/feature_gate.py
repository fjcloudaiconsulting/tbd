"""Feature gate resolution service.

A feature is on for a tenant when **two independent questions** both say yes,
ANDed:

1. **Does the platform offer it to this org?** — ``_resolve_platform_feature``,
   the operator-owned three-level chain (lowest → highest priority):

     a. Env-floor  — ``settings.feature_<name>`` (operator-tunable)
     b. Global     — ``SystemSetting`` row ``key="feature.<name>"``,
                     ``value="on"`` / ``"off"``
     c. Per-org    — ``OrgSetting`` row, same key scheme, scoped to an
                     ``org_id``. Written ONLY by the superadmin endpoints in
                     ``admin_features.py``.

   Unrecognised or absent values at any level fall through to the next.

2. **Does the org want it?** — the tenant mask. An ``OrgSetting`` row
   ``key="orgpref.<name>"``, ``value="off"`` means the org's own admin switched
   the tool off. This slot is **off-only**: it has no ``"on"`` value to write,
   so a tenant can never escalate past the operator's answer, and an org
   "enable" is a row *deletion* that cannot destroy a superadmin grant.

``resolve_feature()`` is the masked, **tenant-facing** answer and is what every
tenant-facing call site must use — ``require_feature`` and ``/auth/status``
both do. ``_resolve_platform_feature()`` is module-private and operator-facing:
``admin_features.py`` reads it so a superadmin sees the platform answer and the
org's preference as two separate fields rather than one conflated boolean.

⚠ Making the *safe* resolver the default name is deliberate. When masking was a
call-site obligation, a build that masked only ``/auth/status`` shipped a hidden
nav item, a page notice, and every backend route still open — with a fully green
test table. There is now no call site that can forget.

Fail-closed: if the env floor is False and no DB rows exist, the feature is off.
"""
from __future__ import annotations

from enum import Enum

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import User


class Feature(str, Enum):
    REPORTS = "reports"
    PLANS = "plans"
    CUSTOM_DASHBOARD = "custom_dashboard"
    FORECAST = "forecast"
    BUDGETS = "budgets"


_ENV_FLOOR = {
    Feature.REPORTS: lambda: app_settings.feature_reports_v2,
    Feature.PLANS: lambda: app_settings.feature_plans,
    Feature.CUSTOM_DASHBOARD: lambda: app_settings.feature_custom_dashboard,
    Feature.FORECAST: lambda: app_settings.feature_forecast,
    Feature.BUDGETS: lambda: app_settings.feature_budgets,
}

# The tenant slot. Deliberately a DIFFERENT namespace from ``feature.`` so the
# two intents never share one row: ``admin_features.py`` owns ``feature.``,
# ``settings.py``'s planning-tool endpoint owns ``orgpref.``. Both namespaces
# are blocked from the generic org-settings writer.
ORG_PREFERENCE_PREFIX = "orgpref."


def feature_setting_key(feature: Feature) -> str:
    """Return the canonical DB key for a feature, e.g. ``"feature.reports"``."""
    return f"feature.{feature.value}"


def org_preference_key(feature: Feature) -> str:
    """Return the tenant opt-out key for a feature, e.g. ``"orgpref.budgets"``."""
    return f"{ORG_PREFERENCE_PREFIX}{feature.value}"


def _parse_onoff(value: str | None) -> bool | None:
    """Parse ``"on"`` → True, ``"off"`` → False, anything else → None (fall through)."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "on":
        return True
    if v == "off":
        return False
    return None


def normalize_onoff(value: str | None) -> str | None:
    """Return the canonical ``'on'`` / ``'off'`` / ``None`` string for *value*.

    Uses the same normalization as :func:`_parse_onoff` so that the admin
    display layer and the gate resolution layer agree on what is stored.
    """
    if value is None:
        return None
    v = value.strip().lower()
    if v == "on":
        return "on"
    if v == "off":
        return "off"
    return None


async def _resolve_platform_feature(
    feature: Feature, org_id: int | None, db: AsyncSession
) -> bool:
    """Return the PLATFORM (operator) answer for *feature* scoped to *org_id*.

    Resolution order: per-org OrgSetting → global SystemSetting → env-floor.

    When *org_id* is ``None`` (unauthenticated caller), the per-org lookup is
    skipped entirely and resolution falls through to global SystemSetting →
    env-floor only.

    ⚠ Module-private on purpose: this answer ignores the org's own preference
    and is therefore NOT safe to gate a tenant-facing route on. Only the
    superadmin surface in ``admin_features.py`` reads it, and only so it can
    show the operator the platform answer and the org preference separately.
    Everything else calls :func:`resolve_feature`.
    """
    key = feature_setting_key(feature)

    # Level 3 — per-org override (skipped when caller is unauthenticated)
    if org_id is not None:
        org_val = await db.scalar(
            select(OrgSetting.value).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
        )
        parsed = _parse_onoff(org_val)
        if parsed is not None:
            return parsed

    # Level 2 — global system setting
    global_val = await db.scalar(select(SystemSetting.value).where(SystemSetting.key == key))
    parsed = _parse_onoff(global_val)
    if parsed is not None:
        return parsed

    # Level 1 — env-floor fallback.
    # ``[]`` indexing, not ``.get(..., lambda: False)``: a Feature member added
    # without an _ENV_FLOOR entry must be a loud KeyError, not a surface that
    # silently closes itself with no diagnostic. Fenced by F1b.
    return bool(_ENV_FLOOR[feature]())


async def upsert_org_setting(
    db: AsyncSession, org_id: int, key: str, value: str
) -> None:
    """Upsert an ``OrgSetting`` row and commit, surviving the unique-key race.

    ``org_settings`` carries ``uq_org_settings_org_key``, so a read-then-insert
    without this retry turns a concurrent double-PUT into a 500. Mirrors
    ``settings.upsert_setting``'s IntegrityError handling — the one existing
    writer that already got this right.
    """
    existing = await db.scalar(
        select(OrgSetting).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
    )
    if existing is not None:
        existing.value = value
    else:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Concurrent insert won the race — retry as an update.
        row = await db.scalar(
            select(OrgSetting).where(
                OrgSetting.org_id == org_id, OrgSetting.key == key
            )
        )
        if row is not None:
            row.value = value
        await db.commit()


async def resolve_feature(feature: Feature, org_id: int | None, db: AsyncSession) -> bool:
    """Return the effective, TENANT-FACING on/off state for *feature*.

    ``platform answer AND NOT org opted out``. This is the answer every
    tenant-facing gate and payload must use — see the module docstring for why
    the masked answer, not the raw chain, owns this name.

    Delegates to :func:`resolve_features` so the mask has exactly ONE
    implementation. Two copies of it would be a live divergence hazard: a
    change that dropped the mask from only one of them would leave
    ``/auth/status`` honest while every gated route stayed open — precisely the
    failure this design exists to make unreachable.
    """
    return (await resolve_features([feature], org_id, db))[feature]


async def resolve_features(
    features: list[Feature], org_id: int | None, db: AsyncSession
) -> dict[Feature, bool]:
    """Resolve several features at once — one query per level, not per feature.

    ``/auth/status`` resolves five features, each of which now reads two keys;
    done naively that is fifteen round trips on an endpoint hit by every cold
    load.

    This is the single implementation of the tenant mask —
    :func:`resolve_feature` is a one-element call into it. Keep it that way.
    """
    if not features:
        return {}

    feature_keys = {feature_setting_key(f): f for f in features}
    pref_keys = {org_preference_key(f): f for f in features}

    org_platform: dict[Feature, str] = {}
    org_pref: dict[Feature, str] = {}
    if org_id is not None:
        rows = (
            await db.execute(
                select(OrgSetting.key, OrgSetting.value).where(
                    OrgSetting.org_id == org_id,
                    OrgSetting.key.in_(list(feature_keys) + list(pref_keys)),
                )
            )
        ).all()
        for key, value in rows:
            if key in feature_keys:
                org_platform[feature_keys[key]] = value
            elif key in pref_keys:
                org_pref[pref_keys[key]] = value

    global_rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(list(feature_keys))
            )
        )
    ).all()
    global_vals = {feature_keys[k]: v for k, v in global_rows if k in feature_keys}

    resolved: dict[Feature, bool] = {}
    for feature in features:
        # Platform chain: per-org override → global → env floor.
        platform = _parse_onoff(org_platform.get(feature))
        if platform is None:
            platform = _parse_onoff(global_vals.get(feature))
        if platform is None:
            # ``[]`` indexing, not ``.get(..., lambda: False)``: a Feature added
            # without an _ENV_FLOOR entry must be a loud KeyError, not a surface
            # that silently closes itself with no diagnostic. Fenced by F1b.
            platform = bool(_ENV_FLOOR[feature]())
        # Tenant mask. Off-only by construction: only the literal "off" masks,
        # so a stray "on" in this namespace can never escalate past the
        # platform answer.
        opted_out = _parse_onoff(org_pref.get(feature)) is False
        resolved[feature] = platform and not opted_out
    return resolved


def env_floor(feature: Feature) -> bool:
    """Return the env-floor (settings-level) boolean for *feature*."""
    return bool(_ENV_FLOOR[feature]())


def require_feature(feature: Feature):
    """Return a FastAPI dependency that 404s when *feature* is off for the caller's org."""

    async def _dep(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        if not await resolve_feature(feature, current_user.org_id, db):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return _dep
