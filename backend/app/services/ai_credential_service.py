"""Per-org AI credential service (PR1).

Drives the validate -> encrypt -> persist -> audit flow used by the
``/api/v1/settings/ai-providers`` router. Plaintext keys MUST NOT
leak into structured logs, audit detail blobs, or response bodies.
The crypto helper computes a fingerprint + last_four pair that is
safe to display.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.services import (
    ai_consent_service,
    audit_service,
    feature_service,
    platform_ai,
    platform_ai_settings,
)
from app.services.ai_credential_crypto import (
    decrypt,
    encrypt,
    fingerprint,
    last_four,
)
from app.services.ai_providers import ValidateResult, get_adapter
from app.services.list_query import resolve_order_by
from app.services.platform_reserve import CENTS, TOKENS
from app.services.usage_service import PlanLimitReached


# Closed sort whitelist for the settings/ai-providers credentials table.
# Keys are the public sort tokens the frontend sends; values are the
# columns to order by. Limited to UI-exposed columns (Provider, Label),
# plus created_at as the default ordering key.
_CREDENTIAL_SORT_COLUMNS = {
    "provider": OrgAICredential.provider,
    "label": OrgAICredential.label,
    "created_at": OrgAICredential.created_at,
}


logger = structlog.stdlib.get_logger()


def _credential_validation_failure(error: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "credential_validation_failed",
            "message": error,
        },
    )


async def _run_validate(
    *,
    provider: AiProvider,
    api_key: Optional[str],
    bearer_token: Optional[str],
    base_url: Optional[str],
    base_url_is_api_root: bool,
) -> ValidateResult:
    adapter = get_adapter(
        provider,
        api_key=api_key,
        bearer_token=bearer_token,
        base_url=base_url,
        base_url_is_api_root=base_url_is_api_root,
    )
    return await adapter.validate()


async def list_credentials_for_org(
    db: AsyncSession,
    *,
    org_id: int,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[OrgAICredential]:
    """Org-scoped credentials.

    ``sort_by`` is resolved against the closed ``_CREDENTIAL_SORT_COLUMNS``
    whitelist (unknown key raises ``ValidationError`` → router 400);
    default is ``created_at`` desc with ``id`` desc as a stable
    tiebreaker. ``limit``/``offset`` page the result when supplied.
    """
    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_CREDENTIAL_SORT_COLUMNS,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=OrgAICredential.id.desc(),
    )
    stmt = (
        select(OrgAICredential)
        .where(OrgAICredential.org_id == org_id)
        .order_by(*order_by)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_credentials_for_org(db: AsyncSession, *, org_id: int) -> int:
    """COUNT over the same filter as ``list_credentials_for_org``."""
    return (
        await db.scalar(
            select(func.count())
            .select_from(OrgAICredential)
            .where(OrgAICredential.org_id == org_id)
        )
    ) or 0


async def get_credential_for_org(
    db: AsyncSession, *, org_id: int, credential_id: int
) -> Optional[OrgAICredential]:
    result = await db.execute(
        select(OrgAICredential).where(
            OrgAICredential.org_id == org_id,
            OrgAICredential.id == credential_id,
        )
    )
    return result.scalar_one_or_none()


def _native_not_available() -> HTTPException:
    """PR1: native is structurally rejected at credential creation.

    The full consent + adapter scaffolding ships now (so PR4 only flips
    a gate, not a code path), but there is no native backend yet. We
    use HTTP 400 with a typed code so a hand-rolled API client sees a
    machine-readable refusal. Spec §5.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "native_not_available",
            "message": "Native provider is not yet available",
        },
    )


async def create_credential(
    db: AsyncSession,
    *,
    org_id: int,
    payload: OrgAICredentialCreate,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAICredential:
    if payload.provider == AiProvider.NATIVE:
        raise _native_not_available()
    result = await _run_validate(
        provider=payload.provider,
        api_key=payload.api_key,
        bearer_token=payload.bearer_token,
        base_url=payload.base_url,
        base_url_is_api_root=True,
    )
    if not result.ok:
        raise _credential_validation_failure(result.error or "validation failed")

    row = OrgAICredential(
        org_id=org_id,
        provider=payload.provider,
        encrypted_api_key=encrypt(payload.api_key) if payload.api_key else None,
        encrypted_bearer_token=(
            encrypt(payload.bearer_token) if payload.bearer_token else None
        ),
        base_url=payload.base_url,
        base_url_is_api_root=True,
        key_fingerprint=fingerprint(payload.api_key) if payload.api_key else None,
        last_four=last_four(payload.api_key) if payload.api_key else None,
        discovered_capabilities=result.discovered_capabilities,
        discovered_models=result.discovered_models,
        label=payload.label,
        last_validated_at=datetime.now(timezone.utc),
        validation_error=None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.created",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "credential_id": row.id,
            "provider": row.provider.value,
            "last_four": row.last_four,
        },
    )
    return row


async def rotate_credential(
    db: AsyncSession,
    *,
    credential: OrgAICredential,
    new_api_key: str,
    new_bearer_token: Optional[str],
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAICredential:
    # Bearer token is Ollama-only — same rule the create-path schema
    # validator enforces. Rotate's schema can't enforce it because the
    # provider isn't in the request body (it's looked up by id), so the
    # service layer is the enforcement point. See OrgAICredentialRotate
    # field comment.
    if (
        new_bearer_token is not None
        and credential.provider != AiProvider.OLLAMA
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "bearer_token_only_for_ollama",
                "message": "bearer_token is only valid for Ollama credentials.",
            },
        )
    result = await _run_validate(
        provider=credential.provider,
        api_key=new_api_key,
        bearer_token=new_bearer_token,
        base_url=credential.base_url,
        base_url_is_api_root=credential.base_url_is_api_root,
    )
    if not result.ok:
        raise _credential_validation_failure(result.error or "validation failed")

    credential.encrypted_api_key = encrypt(new_api_key)
    credential.encrypted_bearer_token = (
        encrypt(new_bearer_token) if new_bearer_token else None
    )
    credential.key_fingerprint = fingerprint(new_api_key)
    credential.last_four = last_four(new_api_key)
    credential.discovered_capabilities = result.discovered_capabilities
    credential.discovered_models = result.discovered_models
    credential.last_validated_at = datetime.now(timezone.utc)
    credential.validation_error = None
    await db.commit()
    await db.refresh(credential)

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.rotated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=credential.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "credential_id": credential.id,
            "provider": credential.provider.value,
            "last_four": credential.last_four,
        },
    )
    return credential


async def validate_credential(
    db: AsyncSession,
    *,
    credential: OrgAICredential,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAICredential:
    api_key = (
        decrypt(credential.encrypted_api_key)
        if credential.encrypted_api_key
        else None
    )
    bearer_token = (
        decrypt(credential.encrypted_bearer_token)
        if credential.encrypted_bearer_token
        else None
    )
    result = await _run_validate(
        provider=credential.provider,
        api_key=api_key,
        bearer_token=bearer_token,
        base_url=credential.base_url,
        base_url_is_api_root=credential.base_url_is_api_root,
    )
    credential.last_validated_at = datetime.now(timezone.utc)
    if result.ok:
        credential.discovered_capabilities = result.discovered_capabilities
        credential.discovered_models = result.discovered_models
        credential.validation_error = None
    else:
        credential.validation_error = (result.error or "validation failed")[:500]
    await db.commit()
    await db.refresh(credential)

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.revalidated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=credential.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success" if result.ok else "failure",
        detail={
            "credential_id": credential.id,
            "provider": credential.provider.value,
            "ok": result.ok,
            "error": credential.validation_error,
        },
    )
    return credential


async def update_credential_label(
    db: AsyncSession,
    *,
    credential: OrgAICredential,
    label: Optional[str],
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAICredential:
    credential.label = label
    await db.commit()
    await db.refresh(credential)
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.updated",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=credential.org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"credential_id": credential.id, "label": label},
    )
    return credential


async def delete_credential(
    db: AsyncSession,
    *,
    credential: OrgAICredential,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> None:
    org_id = credential.org_id
    credential_id = credential.id
    provider = credential.provider.value
    await db.delete(credential)
    await db.commit()
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.credential.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"credential_id": credential_id, "provider": provider},
    )


# --------------------------------------------------------------------
# Platform AI (TBD-586): an org turns a house-key provider on and off.
# A platform row is keyless; the key routes below refuse it.
# --------------------------------------------------------------------


def assert_not_platform(credential: OrgAICredential) -> None:
    """F-S3: PATCH / rotate / validate / DELETE by id refuse a platform row.
    Called after the org-scoped 404 lookup, so another org's id stays 404."""
    if credential.platform_provider is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "platform_credential",
                "message": "Platform AI is turned on and off from its own switch, not managed as a key.",
            },
        )


def _platform_exists() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "platform_credential_exists",
            "message": "Platform AI is already turned on for this provider.",
        },
    )


async def get_platform_credential(
    db: AsyncSession, *, org_id: int, platform_provider: str
) -> Optional[OrgAICredential]:
    return await db.scalar(
        select(OrgAICredential).where(
            OrgAICredential.org_id == org_id,
            OrgAICredential.platform_provider == platform_provider,
        )
    )


async def create_platform_credential(
    db: AsyncSession,
    *,
    org_id: int,
    platform_provider: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAICredential:
    conf = await platform_ai_settings.load(db)
    if platform_provider not in platform_ai_settings.offered(conf):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"code": "ai_native_not_available"},
        )
    # Plan + active overrides (never the plan row alone); both meters must
    # be open, the reservation needs both.
    try:
        ent = await feature_service.get_entitlements(db, org_id)
        lims = [(m, ent.limits[m]) for m in (TOKENS, CENTS)]
    except (SQLAlchemyError, ValidationError, KeyError):
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "platform_ai_unavailable"},
        ) from None
    for meter, lim in lims:
        if not lim.limit:
            raise PlanLimitReached(meter, lim.limit or 0, lim.period, None)
    if not await ai_consent_service.has_current_consent(db, org_id=org_id):
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "code": "ai_consent_required",
                "current_consent_version": settings.ai_native_current_consent_version,
            },
        )
    if await get_platform_credential(
        db, org_id=org_id, platform_provider=platform_provider
    ) is not None:
        raise _platform_exists()
    row = OrgAICredential(
        org_id=org_id,
        provider=platform_ai.PLATFORM_ADAPTER[platform_provider],
        platform_provider=platform_provider,
        discovered_capabilities=list(platform_ai.PLATFORM_CAPABILITIES[platform_provider]),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _platform_exists() from None
    await db.refresh(row)
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.platform.enabled",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "credential_id": row.id,
            "platform_provider": platform_provider,
            "consent_version": settings.ai_native_current_consent_version,
        },
    )
    return row


async def delete_platform_credential(
    db: AsyncSession,
    *,
    org_id: int,
    platform_provider: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> bool:
    """No flag, plan or consent check: an org can always turn it off. Routing
    rows cascade; ledger rows keep ``billing_source='platform'``."""
    row = await get_platform_credential(
        db, org_id=org_id, platform_provider=platform_provider
    )
    if row is None:
        return False
    credential_id = row.id
    await db.delete(row)
    await db.commit()
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.platform.disabled",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"credential_id": credential_id, "platform_provider": platform_provider},
    )
    return True
