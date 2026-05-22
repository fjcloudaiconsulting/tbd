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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.schemas.org_ai_credential import OrgAICredentialCreate
from app.services import audit_service
from app.services.ai_credential_crypto import (
    decrypt,
    encrypt,
    fingerprint,
    last_four,
)
from app.services.ai_providers import ValidateResult, get_adapter


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
    api_key: str,
    bearer_token: Optional[str],
    base_url: Optional[str],
) -> ValidateResult:
    adapter = get_adapter(
        provider,
        api_key=api_key,
        bearer_token=bearer_token,
        base_url=base_url,
    )
    return await adapter.validate()


async def list_credentials_for_org(
    db: AsyncSession, *, org_id: int
) -> list[OrgAICredential]:
    result = await db.execute(
        select(OrgAICredential)
        .where(OrgAICredential.org_id == org_id)
        .order_by(OrgAICredential.created_at.desc())
    )
    return list(result.scalars().all())


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
    )
    if not result.ok:
        raise _credential_validation_failure(result.error or "validation failed")

    row = OrgAICredential(
        org_id=org_id,
        provider=payload.provider,
        encrypted_api_key=encrypt(payload.api_key),
        encrypted_bearer_token=(
            encrypt(payload.bearer_token) if payload.bearer_token else None
        ),
        base_url=payload.base_url,
        key_fingerprint=fingerprint(payload.api_key),
        last_four=last_four(payload.api_key),
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
    result = await _run_validate(
        provider=credential.provider,
        api_key=new_api_key,
        bearer_token=new_bearer_token,
        base_url=credential.base_url,
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
    api_key = decrypt(credential.encrypted_api_key)
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
