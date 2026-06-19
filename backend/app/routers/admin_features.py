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
    List per-org overrides + effective resolution for every Feature.
PUT  /api/v1/admin/orgs/{org_id}/features/{feature}
    Upsert / delete OrgSetting row; audit via ``feature.org.set``.
"""
from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import Organization, User
from app.rate_limit import get_client_ip
from app.services import audit_service
from app.services.feature_gate import (
    Feature,
    env_floor,
    feature_setting_key,
    resolve_feature,
)

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin", tags=["admin-features"])


# ─── request body ──────────────────────────────────────────────────────────


class FeatureValueBody(BaseModel):
    value: Literal["on", "off", "inherit"]


# ─── auth helper ──────────────────────────────────────────────────────────


def _require_superadmin(user: User) -> None:
    """Raise 403 unless the user holds the platform superadmin flag."""
    if not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required",
        )


# ─── misc helpers ─────────────────────────────────────────────────────────


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return structlog.contextvars.get_contextvars().get("request_id")


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


async def _upsert_org_setting(
    db: AsyncSession, org_id: int, key: str, value: str
) -> None:
    """Upsert an OrgSetting row."""
    existing = await db.scalar(
        select(OrgSetting).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
    )
    if existing is not None:
        existing.value = value
    else:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))


# ─── endpoints ────────────────────────────────────────────────────────────


@router.get("/features")
async def list_global_features(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all features with their global_value and env_floor.

    Superadmin only.
    """
    _require_superadmin(current_user)

    result = []
    for feature in Feature:
        key = feature_setting_key(feature)
        global_val = await db.scalar(
            select(SystemSetting.value).where(SystemSetting.key == key)
        )
        result.append(
            {
                "feature": feature.value,
                "global_value": global_val if global_val in ("on", "off") else None,
                "env_floor": env_floor(feature),
            }
        )
    return result


@router.put("/features/{feature}")
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
    _require_superadmin(current_user)
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
    """
    _require_superadmin(current_user)

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
        override = override_raw if override_raw in ("on", "off") else "inherit"
        effective = await resolve_feature(feat, org_id, db)
        result.append(
            {"feature": feat.value, "override": override, "effective": effective}
        )
    return result


@router.put("/orgs/{org_id}/features/{feature}")
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
    _require_superadmin(current_user)
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
    else:
        await _upsert_org_setting(db, org_id, key, body.value)
        new_override = body.value

    await db.commit()

    # Resolve effective value after the commit
    effective = await resolve_feature(feat, org_id, db)

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

    return {
        "feature": feat.value,
        "override": new_override,
        "effective": effective,
    }
