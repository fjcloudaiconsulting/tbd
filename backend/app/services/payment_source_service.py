"""Payment-source validation service (Payment Source Foundation).

Owns the validation for ``accounts.payment_source_account_id`` — the
account a liability's bill is paid FROM
(``specs/payment-source-account-foundation.md``). Foundation only: no
payment automation, no generated transactions, no cron jobs.

Mirrors the ``account_type_change_service.validate_*`` convention:
plain async helper, raises ``HTTPException`` on violation, returns
``None`` on success. Called by the accounts router from both the create
path and the shared ``_apply_non_type_fields`` update path.

Validation rules (spec § "Validation"):

1. Source account exists in the SAME ``org_id`` as the target — else 422
   (entity-not-for-you, matching the type-change service's cross-org
   convention; leaves 400 for structural payload errors).
2. Source ``!=`` target (no self-pay) — else 422. Skipped on create,
   where the target account does not exist yet.
3. Source ``account_type.slug`` is in the asset allowlist
   (``checking`` / ``savings`` / ``cash``) — NOT ``credit_card``,
   NOT ``investment``, NOT ``loan`` (once that type lands) — else 422.
4. Source is active (``is_active is True``) at write time — else 422.

Post-write deactivation-by-deletion is handled by the FK
``ON DELETE SET NULL`` (migration 072). A source that is merely
deactivated (not deleted) keeps the FK intact; the UI surfaces an
"inactive source" hint at read time by resolving the source from the
already-loaded accounts list (no backend change needed).
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.account import Account


# Asset account types a liability may be paid from. Intentionally excludes
# credit_card, investment, and (future) loan.
PAYMENT_SOURCE_ALLOWED_SLUGS = frozenset({"checking", "savings", "cash"})


async def validate_payment_source_account(
    db: AsyncSession,
    *,
    org_id: int,
    source_account_id: Optional[int],
    target_account_id: Optional[int],
) -> None:
    """Validate a proposed ``payment_source_account_id`` assignment.

    ``source_account_id`` is the value the caller wants to store.
    ``None`` clears the pointer and is always valid (nothing to check).

    ``target_account_id`` is the account being edited, used for the
    self-pay check. Pass ``None`` on create (the target has no id yet, so
    it cannot be its own source).

    Raises ``HTTPException(422)`` on any rule violation; returns ``None``
    on success.
    """
    if source_account_id is None:
        return

    # Self-pay: cheapest structural check first. On create target is None,
    # so this is skipped (a not-yet-created account can't be its own source).
    if target_account_id is not None and source_account_id == target_account_id:
        raise HTTPException(
            status_code=422,
            detail="An account cannot be its own payment source",
        )

    source = (
        await db.execute(
            select(Account)
            .options(selectinload(Account.account_type))
            .where(
                Account.id == source_account_id,
                Account.org_id == org_id,
            )
        )
    ).scalar_one_or_none()

    # Same-org existence. A cross-org or missing id is treated as
    # not-for-you (422), never leaking whether the id exists elsewhere.
    if source is None:
        raise HTTPException(
            status_code=422,
            detail="Payment source account not found",
        )

    source_slug = source.account_type.slug if source.account_type else None
    if source_slug not in PAYMENT_SOURCE_ALLOWED_SLUGS:
        raise HTTPException(
            status_code=422,
            detail="Payment source must be a checking, savings, or cash account",
        )

    if not source.is_active:
        raise HTTPException(
            status_code=422,
            detail="Payment source account is inactive",
        )
