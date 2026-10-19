"""Superadmin-only endpoints for global + per-org feature gate management.

Mounted at ``/api/v1/admin``.  Every endpoint requires ``is_superadmin``
(intentionally stricter than org OWNER/ADMIN — a globally-disabled feature
must not be re-enableable by an org's own admin).

Endpoints
---------
GET  /api/v1/admin/features
    List every Feature with its global_value and env_floor.
PUT  /api/v1/admin/features/{feature}
    Upsert (value="on"|"off") or delete (value="inherit") the SystemSetting
    row; audit via ``feature.global.set``.
GET  /api/v1/admin/orgs/{org_id}/features
    List per-org overrides + the org's own opt-out (``org_preference``) +
    the PLATFORM effective resolution for every Feature.
PUT  /api/v1/admin/orgs/{org_id}/features/{feature}
    Upsert / delete OrgSetting row; audit via ``feature.org.set``.
GET  /api/v1/admin/platform-ai
    The platform AI settings dispatch sees, the catalog, and whether the
    env key exists.
PUT  /api/v1/admin/platform-ai
    The two ``platform_ai.*`` rows and, when ``models`` is sent, the catalog
    allowlist (``platform_ai_models.enabled``), in one commit; audit via
    ``admin.platform_ai.updated``.
"""
from __future__ import annotations

from typing import Annotated, Literal

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app._time import utcnow_naive
from app.config import settings
from app.database import get_db
from app.auth.pat import require_interactive_session
from app.deps import get_current_user, get_session_factory
from app.models.platform_ai_model import PlatformAIModel
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import Organization, User
from app.rate_limit import get_client_ip
from app.services import audit_service, platform_ai, platform_ai_settings
from app.services.scheduler.jobs import platform_ai_catalog
from app.services.feature_gate import (
    Feature,
    _resolve_platform_feature,
    env_floor,
    feature_setting_key,
    normalize_onoff,
    org_preference_key,
    upsert_org_setting,
)
from app import logs

logger = logging.getLogger(__name__)


async def require_superadmin(
    current_user: User = Depends(get_current_user),
) -> User:
    """FastAPI dependency: resolve the current user and enforce superadmin access.

    Runs during dependency resolution — before request-body validation — so
    unauthenticated or non-superadmin callers receive 403, never 422.
    """
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required",
        )
    return current_user


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin-features"],
    dependencies=[Depends(require_superadmin)],
)


# ─── request body ──────────────────────────────────────────────────────────


class FeatureValueBody(BaseModel):
    value: Literal["on", "off", "inherit"]


# Printable ASCII, no space; 120 = the ledger/routing ``model`` width.
_ModelId = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"^[\x21-\x7e]+$")]


class PlatformAIBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    # x10,000 at the dispatch gate must still fit a BIGINT bind value
    global_monthly_cents: StrictInt = Field(ge=0, le=(2**53 - 1) // 10_000)
    # Omitted = allowlist untouched; a list = full replace.
    models: Annotated[list[_ModelId], Field(max_length=50)] | None = None


# ─── misc helpers ─────────────────────────────────────────────────────────


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return logs.CONTEXT.get({}).get("request_id")


def _feature_from_str(name: str) -> Feature:
    """Parse a feature name string; raise 404 if unknown."""
    try:
        return Feature(name)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown feature: {name!r}",
        )


async def _upsert_system_setting(db: AsyncSession, key: str, value: str) -> None:
    """Upsert a SystemSetting row (works on both SQLite and MySQL)."""
    existing = await db.scalar(
        select(SystemSetting).where(SystemSetting.key == key)
    )
    if existing is not None:
        existing.value = value
    else:
        db.add(SystemSetting(key=key, value=value))


async def _org_preference(db: AsyncSession, org_id: int, feat: Feature) -> str:
    """Return the org's OWN preference for *feat*: ``"off"`` or ``"inherit"``.

    Surfaced beside ``effective`` so an operator can explain the discrepancy.
    Without it, an org that opted out reads as ``override: inherit,
    effective: true`` while the tenant sees the feature closed, and the
    operator has nothing to look at.
    """
    raw = await db.scalar(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id,
            OrgSetting.key == org_preference_key(feat),
        )
    )
    return "off" if normalize_onoff(raw) == "off" else "inherit"


# ─── endpoints ────────────────────────────────────────────────────────────


@router.get("/features")
async def list_global_features(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all features with their global_value and env_floor.

    Superadmin only.
    """
    result = []
    for feature in Feature:
        key = feature_setting_key(feature)
        global_val = await db.scalar(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
        result.append(
            {
                "feature": feature.value,
                "global_value": normalize_onoff(global_val),
                "env_floor": env_floor(feature),
            }
        )
    return result


@router.put(
    "/features/{feature}",
    dependencies=[Depends(require_interactive_session)],
)
async def set_global_feature(
    feature: str,
    body: FeatureValueBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict:
    """Upsert or delete a global SystemSetting feature row.

    Superadmin only.  ``value="inherit"`` deletes the row (falls back to
    env-floor).  Audit event: ``feature.global.set``.
    """
    feat = _feature_from_str(feature)
    key = feature_setting_key(feat)

    # Snapshot actor before any await that could expire the ORM object
    actor_user_id = current_user.id
    actor_email = current_user.email
    req_id = _request_id()
    ip = get_client_ip(request)

    # Read the current value for the audit detail
    old_raw = await db.scalar(
        select(SystemSetting.value).where(SystemSetting.key == key)
    )
    old_value = old_raw if old_raw in ("on", "off") else "inherit"

    if body.value == "inherit":
        await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
        new_global_value = None
    else:
        await _upsert_system_setting(db, key, body.value)
        new_global_value = body.value

    await db.commit()

    await audit_service.record_audit_event(
        session_factory,
        event_type="feature.global.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=req_id,
        ip_address=ip,
        outcome="success",
        detail={"feature": feat.value, "old": old_value, "new": body.value},
    )

    return {
        "feature": feat.value,
        "global_value": new_global_value,
        "env_floor": env_floor(feat),
    }


@router.get("/orgs/{org_id}/features")
async def list_org_features(
    org_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List per-org feature overrides + effective resolution.

    Superadmin only.  Returns 404 when org doesn't exist.

    ``effective`` is the PLATFORM answer (``_resolve_platform_feature``), not
    the tenant-facing one: this surface exists to show the operator what the
    operator controls. The org's own opt-out is reported separately as
    ``org_preference`` (``"off"`` / ``"inherit"``) — conflating the two into a
    single boolean is what left an operator staring at ``override: inherit,
    effective: true, global on`` with no way to explain the tenant's closed
    surface.
    """
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    result = []
    for feat in Feature:
        key = feature_setting_key(feat)
        override_raw = await db.scalar(
            select(OrgSetting.value).where(
                OrgSetting.org_id == org_id, OrgSetting.key == key
            )
        )
        override = normalize_onoff(override_raw) or "inherit"
        effective = await _resolve_platform_feature(feat, org_id, db)
        result.append(
            {
                "feature": feat.value,
                "override": override,
                "org_preference": await _org_preference(db, org_id, feat),
                "effective": effective,
            }
        )
    return result


@router.put(
    "/orgs/{org_id}/features/{feature}",
    dependencies=[Depends(require_interactive_session)],
)
async def set_org_feature(
    org_id: int,
    feature: str,
    body: FeatureValueBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict:
    """Upsert or delete a per-org OrgSetting feature row.

    Superadmin only.  Returns 404 when org or feature is unknown.
    Audit event: ``feature.org.set``.
    """
    feat = _feature_from_str(feature)

    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Snapshot actor before any await that could expire the ORM object
    actor_user_id = current_user.id
    actor_email = current_user.email
    org_name = org.name
    req_id = _request_id()
    ip = get_client_ip(request)

    key = feature_setting_key(feat)

    # Read current per-org value for audit detail
    old_raw = await db.scalar(
        select(OrgSetting.value).where(
            OrgSetting.org_id == org_id, OrgSetting.key == key
        )
    )
    old_value = old_raw if old_raw in ("on", "off") else "inherit"

    if body.value == "inherit":
        await db.execute(
            delete(OrgSetting).where(
                OrgSetting.org_id == org_id, OrgSetting.key == key
            )
        )
        new_override = "inherit"
        await db.commit()
    else:
        # Commits internally, with the unique-key retry.
        await upsert_org_setting(db, org_id, key, body.value)
        new_override = body.value

    # Resolve the PLATFORM value after the commit — same reason as the GET.
    effective = await _resolve_platform_feature(feat, org_id, db)

    await audit_service.record_audit_event(
        session_factory,
        event_type="feature.org.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=org_name,
        request_id=req_id,
        ip_address=ip,
        outcome="success",
        detail={"feature": feat.value, "old": old_value, "new": body.value},
    )

    # Read and write must not disagree: same field set, same resolver.
    return {
        "feature": feat.value,
        "override": new_override,
        "org_preference": await _org_preference(db, org_id, feat),
        "effective": effective,
    }


# ─── platform AI (TBD-586) ────────────────────────────────────────────────


async def _enabled_ids(db: AsyncSession) -> list[str]:
    return list((await db.scalars(
        select(PlatformAIModel.openrouter_id)
        .where(PlatformAIModel.enabled.is_(True))
        .order_by(PlatformAIModel.openrouter_id)
    )).all())


async def _platform_ai_view(db: AsyncSession) -> dict:
    conf = await platform_ai_settings.load(db)
    now = utcnow_naive()
    rows = (await db.scalars(select(PlatformAIModel).where(
        PlatformAIModel.zdr_prompt_micros_per_mtok.is_not(None),
        PlatformAIModel.zdr_completion_micros_per_mtok.is_not(None),
    ))).all()
    return {
        "enabled": conf.enabled,
        "global_monthly_cents": conf.global_monthly_cents,
        "models": await _enabled_ids(db),
        "env_floor": settings.ai_native_enabled,
        "providers": [
            {"key": "openrouter", "key_configured": platform_ai.platform_key_configured("openrouter")}
        ],
        "catalog": [
            {
                "openrouter_id": r.openrouter_id,
                "name": r.name,
                "context_length": r.context_length,
                "max_completion_tokens": r.max_completion_tokens,
                "supports_structured_outputs": r.supports_structured_outputs,
                "zdr_prompt_micros_per_mtok": r.zdr_prompt_micros_per_mtok,
                "zdr_completion_micros_per_mtok": r.zdr_completion_micros_per_mtok,
                "tier": platform_ai.tier(r),
                "expires_at": r.expires_at,
                "last_seen_at": r.last_seen_at,
                "enabled": r.enabled,
                # An enabled row the job or time made un-offerable shows False.
                "offered": platform_ai_catalog.offerable(r, now) and platform_ai.can(r, "function_call"),
            }
            for r in sorted(rows, key=lambda r: (platform_ai.blend(r), r.openrouter_id))
        ],
    }


@router.get("/platform-ai")
async def get_platform_ai(db: AsyncSession = Depends(get_db)) -> dict:
    """What dispatch sees. Never key material, only whether a key is set."""
    return await _platform_ai_view(db)


@router.put(
    "/platform-ai",
    dependencies=[Depends(require_interactive_session)],
)
async def set_platform_ai(
    body: PlatformAIBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> dict:
    """``models`` omitted leaves the allowlist untouched (the kill switch
    ``{enabled: false, global_monthly_cents}`` always saves); a list is a full
    replace, deduped, and every id must be an eligible, tool-capable catalog
    row whatever ``enabled`` is, so an enabled row is never silently
    un-offered at the moment it is enabled."""
    models = None if body.models is None else list(dict.fromkeys(body.models))
    if models:
        now = utcnow_naive()
        rows = {r.openrouter_id: r for r in (await db.scalars(
            select(PlatformAIModel).where(PlatformAIModel.openrouter_id.in_(models))
        )).all()}
        for m in models:
            r = rows.get(m)
            if r is None or not (platform_ai_catalog.eligible(r, now) and platform_ai.can(r, "function_call")):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "platform_model_not_offerable",
                        "message": f"{m!r} is not offerable",
                        "model": m,
                    },
                )

    actor_user_id = current_user.id
    actor_email = current_user.email
    conf = await platform_ai_settings.load(db)
    old = {
        "enabled": conf.enabled,
        "global_monthly_cents": conf.global_monthly_cents,
        "models": await _enabled_ids(db),  # read BEFORE the UPDATEs below
    }
    if (
        body.enabled
        and (old["models"] if models is None else models)
        and not platform_ai.platform_key_configured("openrouter")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "platform_provider_key_missing",
                "message": "no platform key is configured",
            },
        )
    for key, value in (
        (platform_ai_settings.ENABLED, "on" if body.enabled else "off"),
        (platform_ai_settings.GLOBAL_MONTHLY_CENTS, str(body.global_monthly_cents)),
    ):
        await _upsert_system_setting(db, key, value)
    if models is not None:
        await db.execute(update(PlatformAIModel).where(
            PlatformAIModel.openrouter_id.not_in(models)).values(enabled=False))
        await db.execute(update(PlatformAIModel).where(
            PlatformAIModel.openrouter_id.in_(models)).values(enabled=True))
    await db.commit()  # ONE commit: settings and allowlist change together or not at all

    view = await _platform_ai_view(db)
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.platform_ai.updated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "old": old,
            "new": {k: view[k] for k in ("enabled", "global_monthly_cents", "models")},
        },
    )
    return view
