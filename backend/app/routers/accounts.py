from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account, AccountType
from app.models.transaction import Transaction
from app.models.user import Organization, Role, User
from app.rate_limit import get_client_ip, limiter
from app.schemas.account import (
    AccountCreate,
    AccountResponse,
    AccountUpdate,
    BalanceAdjustmentRequest,
    BalanceAdjustmentResponse,
    ReconcileResponse,
)
from app.services import audit_service
from app.services.account_type_change_service import (
    apply_type_change_in_session,
    validate_create_close_day,
    validate_create_payment_day,
)
from app.services.credit_card_service import validate_credit_card_fields
from app.services.exceptions import ConflictError, ValidationError
from app.services.payment_source_service import validate_payment_source_account
from app.services.transaction_service import (
    adjust_account_balance,
    assert_no_dependents,
    reconcile_account,
)

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return structlog.contextvars.get_contextvars().get("request_id")


def _is_admin_user(user: User) -> bool:
    return user.role in (Role.OWNER, Role.ADMIN) or user.is_superadmin


def _to_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        account_type_id=account.account_type_id,
        account_type_name=account.account_type.name if account.account_type else "",
        account_type_slug=account.account_type.slug if account.account_type else None,
        balance=account.balance,
        currency=account.currency,
        is_active=account.is_active,
        close_day=account.close_day,
        payment_day=account.payment_day,
        payment_day_relative_month=account.payment_day_relative_month,
        is_default=account.is_default,
        opening_balance=account.opening_balance,
        opening_balance_date=account.opening_balance_date,
        payment_source_account_id=account.payment_source_account_id,
        credit_limit=account.credit_limit,
        apr=account.apr,
        payment_strategy=account.payment_strategy,
        fixed_payment_amount=account.fixed_payment_amount,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.org_id == current_user.org_id)
        .order_by(Account.name)
    )
    return [_to_response(a) for a in result.scalars().all()]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    at_result = await db.execute(
        select(AccountType).where(
            AccountType.id == body.account_type_id,
            AccountType.org_id == current_user.org_id,
        )
    )
    target_type = at_result.scalar_one_or_none()
    if target_type is None:
        raise HTTPException(status_code=400, detail="Invalid account type")

    # Spec § 3.1.1 — create-path close_day cascade. Mirrors the PUT
    # path's invariant (close_day IS NULL iff slug != 'credit_card').
    # Before this rule the create endpoint silently accepted any
    # combination, e.g. a Checking account with close_day=15.
    validate_create_close_day(
        target_slug=target_type.slug, close_day_value=body.close_day
    )
    # CC billing cycle Slice 1: payment_day / payment_day_relative_month
    # are CC-only optional columns. Both must be NULL on non-CC accounts.
    validate_create_payment_day(
        target_slug=target_type.slug,
        payment_day_value=body.payment_day,
        payment_day_relative_month_value=body.payment_day_relative_month,
    )
    # Credit Card Model V1 (Slice 1): the four CC-only fields. Non-CC
    # accounts must leave all four NULL; CC accounts get optional
    # credit_limit/apr and a strategy-gated fixed_payment_amount.
    validate_credit_card_fields(
        target_slug=target_type.slug,
        credit_limit=body.credit_limit,
        apr=body.apr,
        payment_strategy=body.payment_strategy,
        fixed_payment_amount=body.fixed_payment_amount,
    )
    # Payment Source Foundation: validate the paid-from pointer before the
    # insert. target_account_id is None (the row has no id yet), so the
    # self-pay check is a no-op here; the rest (same-org, allowlisted type,
    # active) still applies.
    await validate_payment_source_account(
        db,
        org_id=current_user.org_id,
        source_account_id=body.payment_source_account_id,
        target_account_id=None,
    )

    # opening_balance_date: caller may omit (and ride the DB default of
    # CURRENT_DATE) or supply an explicit date. We pass it through only
    # when supplied so the column-level server_default applies on omission.
    #
    # L1.1 L4: the live ``balance`` field is seeded from the user-stated
    # ``opening_balance`` rather than from a free-form ``balance`` input
    # (which would have bypassed the audit trail). Any subsequent change
    # to ``balance`` flows through transaction_service or the audited
    # /adjust-balance endpoint, so no path mutates ``Account.balance``
    # without a corresponding ledger row.
    kwargs = dict(
        org_id=current_user.org_id,
        account_type_id=body.account_type_id,
        name=body.name,
        balance=body.opening_balance,
        currency=body.currency,
        close_day=body.close_day,
        payment_day=body.payment_day,
        payment_day_relative_month=body.payment_day_relative_month,
        opening_balance=body.opening_balance,
        payment_source_account_id=body.payment_source_account_id,
        credit_limit=body.credit_limit,
        apr=body.apr,
        payment_strategy=body.payment_strategy,
        fixed_payment_amount=body.fixed_payment_amount,
    )
    if body.opening_balance_date is not None:
        kwargs["opening_balance_date"] = body.opening_balance_date

    account = Account(**kwargs)
    db.add(account)
    await db.commit()

    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account.id)
    )
    return _to_response(result.scalar_one())


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id, Account.org_id == current_user.org_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return _to_response(account)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: int,
    body: AccountUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Update an account.

    PR #246 review feedback (P1 atomicity bug): when the request touches
    ``account_type_id`` or ``close_day`` the WHOLE handler runs inside a
    single service-owned transaction so a 4xx from a later guard
    (e.g. the nonzero-balance deactivation guard at status 409) rolls
    back the type change too. Audit events fire only AFTER the outer
    commit succeeds, so a half-applied state never produces an
    ``account.type_changed`` row.

    Pattern (b) from spec § 4.3 still holds for the locking goal — the
    ``SELECT ... FOR UPDATE`` row lock acquires on a fresh session,
    independent of the auth-autobegun request session — but the
    transaction boundary widens to wrap every other field mutation.
    The spec's explicit guidance is that the transaction boundary is
    the implementer's call ("The transaction boundary in which it
    runs, however, is left to the implementer"). Widening it here
    satisfies that guidance and closes the partial-commit hole.
    Documented as a deviation in the PR body.

    Plain edits that do not touch ``account_type_id`` or ``close_day``
    keep using the request session (no row lock needed; the existing
    optimistic-concurrency behavior is fine and matches how
    ``is_default`` already behaves).
    """
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    touches_type_or_cc_columns = (
        body.account_type_id is not None
        or "close_day" in body.model_fields_set
        or "payment_day" in body.model_fields_set
        or "payment_day_relative_month" in body.model_fields_set
        or "credit_limit" in body.model_fields_set
        or "apr" in body.model_fields_set
        or "payment_strategy" in body.model_fields_set
        or "fixed_payment_amount" in body.model_fields_set
    )

    if touches_type_or_cc_columns:
        return await _update_account_atomic(
            account_id=account_id,
            body=body,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            actor_org_id=actor_org_id,
            req_id=req_id,
            ip=ip,
            session_factory=session_factory,
        )

    # ── Fast path: no type/cc-column touch, stay on the request session.
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id, Account.org_id == actor_org_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    old_opening_balance = account.opening_balance
    old_opening_balance_date = account.opening_balance_date
    opening_changed = await _apply_non_type_fields(
        db, account, body, actor_org_id, nested_default=True,
        resolved_slug=account.account_type.slug if account.account_type else None,
    )
    new_opening_balance = account.opening_balance
    new_opening_balance_date = account.opening_balance_date

    await db.commit()

    if opening_changed:
        await audit_service.record_audit_event(
            session_factory,
            event_type="account.opening_balance.update",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=None,
            request_id=req_id,
            ip_address=ip,
            outcome="success",
            detail={
                "account_id": account_id,
                "old_opening_balance": str(old_opening_balance),
                "new_opening_balance": str(new_opening_balance),
                "old_opening_balance_date": old_opening_balance_date.isoformat()
                if old_opening_balance_date is not None
                else None,
                "new_opening_balance_date": new_opening_balance_date.isoformat()
                if new_opening_balance_date is not None
                else None,
            },
        )

    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account.id)
    )
    return _to_response(result.scalar_one())


async def _apply_non_type_fields(
    db: AsyncSession,
    account: Account,
    body: AccountUpdate,
    org_id: int,
    *,
    nested_default: bool,
    resolved_slug: Optional[str],
) -> bool:
    """Apply name / is_active / is_default / opening_* mutations.

    Returns ``True`` iff at least one opening_balance field actually
    changed (caller uses this to gate the audit event).

    Raises ``HTTPException`` for the existing guards (409 on nonzero
    deactivate). When invoked inside an outer
    ``async with svc_db.begin()`` block the exception aborts the txn
    and every staged write disappears with it — that is the whole
    point of the atomic refactor (PR #246 review P1).

    ``nested_default`` selects how the "set this row as default and
    clear every sibling" two-write step is wrapped. Inside the request
    session the existing nested-savepoint behavior is preserved
    (request-session autobegin needs the savepoint). Inside the
    service-owned outer txn a plain ``execute`` is sufficient since
    the outer begin already covers it.
    """
    opening_changed = False

    if body.name is not None:
        account.name = body.name

    # Payment Source Foundation. The model_fields_set idiom distinguishes
    # "omitted" (preserve) from "explicit null" (clear the pointer). Validate
    # before applying so a bad source id aborts the whole PUT (in the atomic
    # path the raised HTTPException rolls back the outer begin() with it).
    if "payment_source_account_id" in body.model_fields_set:
        await validate_payment_source_account(
            db,
            org_id=org_id,
            source_account_id=body.payment_source_account_id,
            target_account_id=account.id,
        )
        account.payment_source_account_id = body.payment_source_account_id

    # Credit Card Model V1 (Slice 1). Validate the RESULTING row state
    # (post-lock snapshot overlaid with payload deltas) against the
    # post-change slug, then apply the deltas. model_fields_set
    # distinguishes "omitted" (preserve) from an explicit value/null.
    cc_credit_limit = (
        body.credit_limit
        if "credit_limit" in body.model_fields_set
        else account.credit_limit
    )
    cc_apr = body.apr if "apr" in body.model_fields_set else account.apr
    cc_strategy = (
        body.payment_strategy
        if "payment_strategy" in body.model_fields_set
        else account.payment_strategy
    )
    cc_fixed = (
        body.fixed_payment_amount
        if "fixed_payment_amount" in body.model_fields_set
        else account.fixed_payment_amount
    )
    validate_credit_card_fields(
        target_slug=resolved_slug,
        credit_limit=cc_credit_limit,
        apr=cc_apr,
        payment_strategy=cc_strategy,
        fixed_payment_amount=cc_fixed,
    )
    if "credit_limit" in body.model_fields_set:
        account.credit_limit = body.credit_limit
    if "apr" in body.model_fields_set:
        account.apr = body.apr
    if "payment_strategy" in body.model_fields_set:
        account.payment_strategy = body.payment_strategy
    if "fixed_payment_amount" in body.model_fields_set:
        account.fixed_payment_amount = body.fixed_payment_amount

    # Apply the opening_balance shift BEFORE the is_active deactivate guard so
    # the guard evaluates the FINAL (post-shift) balance. A single PUT that
    # both edits opening_balance and deactivates must be judged on the balance
    # the account will actually hold (finding 15): shifting up to a nonzero
    # balance must block deactivation, and shifting down to zero must allow it.
    if body.opening_balance is not None and body.opening_balance != account.opening_balance:
        # Opening balance is the foundation of the live balance
        # (balance == opening_balance + Σ settled txns). Correcting it must
        # shift the live balance by the same delta, otherwise the cached
        # balance silently drifts from reality (2026-06-14 incident).
        account.balance += body.opening_balance - account.opening_balance
        account.opening_balance = body.opening_balance
        opening_changed = True
    if (
        body.opening_balance_date is not None
        and body.opening_balance_date != account.opening_balance_date
    ):
        account.opening_balance_date = body.opening_balance_date
        opening_changed = True

    if body.is_active is not None:
        if body.is_active is False and account.balance != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate account with balance {account.balance}. Transfer the balance first.",
            )
        account.is_active = body.is_active

    if body.is_default is True:
        if nested_default:
            async with db.begin_nested():
                await db.execute(
                    update(Account)
                    .where(Account.org_id == org_id, Account.id != account.id)
                    .values(is_default=False)
                )
                account.is_default = True
        else:
            await db.execute(
                update(Account)
                .where(Account.org_id == org_id, Account.id != account.id)
                .values(is_default=False)
            )
            account.is_default = True
    elif body.is_default is False:
        account.is_default = False

    return opening_changed


async def _update_account_atomic(
    *,
    account_id: int,
    body: AccountUpdate,
    actor_user_id: int,
    actor_email: str,
    actor_org_id: int,
    req_id: str | None,
    ip: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> AccountResponse:
    """Single-transaction PUT path (PR #246 review P1 fix).

    Wraps the locked type-change AND every other field mutation in ONE
    service-owned transaction. A 4xx raised by any guard inside the
    block aborts the outer ``begin()`` context manager and rolls back
    every staged write atomically. Audit events fire only after the
    outer commit succeeds.
    """
    type_result = None
    opening_changed = False
    old_opening_balance = None
    old_opening_balance_date = None
    new_opening_balance = None
    new_opening_balance_date = None
    response_payload: AccountResponse | None = None

    async with session_factory() as svc_db:
        async with svc_db.begin():
            target_type_id = body.account_type_id
            if target_type_id is None:
                current_type_id = await svc_db.scalar(
                    select(Account.account_type_id).where(
                        Account.id == account_id,
                        Account.org_id == actor_org_id,
                    )
                )
                if current_type_id is None:
                    raise HTTPException(
                        status_code=404, detail="Account not found"
                    )
                target_type_id = current_type_id

            account, type_result = await apply_type_change_in_session(
                svc_db,
                account_id=account_id,
                org_id=actor_org_id,
                target_type_id=target_type_id,
                close_day_in_payload="close_day" in body.model_fields_set,
                close_day_value=body.close_day,
                payment_day_in_payload="payment_day" in body.model_fields_set,
                payment_day_value=body.payment_day,
                payment_day_relative_month_in_payload="payment_day_relative_month" in body.model_fields_set,
                payment_day_relative_month_value=body.payment_day_relative_month,
            )

            old_opening_balance = account.opening_balance
            old_opening_balance_date = account.opening_balance_date
            opening_changed = await _apply_non_type_fields(
                svc_db, account, body, actor_org_id, nested_default=False,
                resolved_slug=type_result.new_type_slug,
            )
            new_opening_balance = account.opening_balance
            new_opening_balance_date = account.opening_balance_date
            # ``async with svc_db.begin()`` commits on clean exit.

    # Outer txn committed cleanly. The cached ``account.account_type``
    # relationship inside the service session points at the OLD
    # AccountType (selectinload was eager-loaded before the mutation,
    # and SA's identity map keeps the stale object). Re-read on a
    # fresh session so ``_to_response`` projects the new slug + name.
    async with session_factory() as fresh_db:
        refreshed = (
            await fresh_db.execute(
                select(Account)
                .options(selectinload(Account.account_type))
                .where(Account.id == account_id)
            )
        ).scalar_one()
        response_payload = _to_response(refreshed)

    # Post-commit audits. Outside the session ``async with`` because
    # audit_service.record_audit_event opens its own session.
    if type_result is not None and type_result.type_changed:
        await audit_service.record_audit_event(
            session_factory,
            event_type="account.type_changed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=None,
            request_id=req_id,
            ip_address=ip,
            outcome="success",
            detail={
                "account_id": account_id,
                "old_type_id": type_result.old_type_id,
                "new_type_id": type_result.new_type_id,
                "old_type_slug": type_result.old_type_slug,
                "new_type_slug": type_result.new_type_slug,
                "closes_day_set": type_result.new_close_day
                if type_result.new_type_slug == "credit_card"
                and type_result.old_type_slug != "credit_card"
                else None,
                "closes_day_cleared": type_result.old_close_day
                if type_result.old_type_slug == "credit_card"
                and type_result.new_type_slug != "credit_card"
                else None,
            },
        )
        await logger.ainfo(
            "account.type_changed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            account_id=account_id,
            old_type_slug=type_result.old_type_slug,
            new_type_slug=type_result.new_type_slug,
        )

    if opening_changed:
        await audit_service.record_audit_event(
            session_factory,
            event_type="account.opening_balance.update",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=None,
            request_id=req_id,
            ip_address=ip,
            outcome="success",
            detail={
                "account_id": account_id,
                "old_opening_balance": str(old_opening_balance),
                "new_opening_balance": str(new_opening_balance),
                "old_opening_balance_date": old_opening_balance_date.isoformat()
                if old_opening_balance_date is not None
                else None,
                "new_opening_balance_date": new_opening_balance_date.isoformat()
                if new_opening_balance_date is not None
                else None,
            },
        )

    assert response_payload is not None
    return response_payload


@router.get("/{account_id}/reconcile", response_model=ReconcileResponse)
async def reconcile(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account).where(
            Account.id == account_id, Account.org_id == current_user.org_id
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    stored, computed, consistent = await reconcile_account(db, current_user.org_id, account)
    return ReconcileResponse(
        account_id=account_id,
        stored_balance=stored,
        computed_balance=computed,
        is_consistent=consistent,
    )


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Account).where(
            Account.id == account_id, Account.org_id == current_user.org_id
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    if account.balance != 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete account with balance {account.balance}. Transfer the balance first.",
        )

    await assert_no_dependents(
        db, Transaction,
        [Transaction.account_id == account.id, Transaction.org_id == current_user.org_id],
        "transaction", "account",
    )

    await db.delete(account)
    await db.commit()


@router.post("/{account_id}/adjust-balance", response_model=BalanceAdjustmentResponse)
@limiter.limit("20/hour")
async def adjust_balance(
    account_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Track E: org-admin endpoint to set an account's balance directly.

    Order of guards (architect-locked precedence):
      1. 401 (auth) — handled by ``get_current_user``.
      2. 403 admin — non-admin caller, regardless of org flag.
      3. 403 flag — admin caller but ``allow_manual_balance_adjustment``
         is OFF for the org. Distinct message so the frontend can
         differentiate "you don't have the role" from "feature is off".
      4. 422 (Pydantic) — out-of-range target, oversized reason,
         malformed JSON. Body is parsed manually AFTER the auth and flag
         gates so a non-admin caller with an invalid body sees 403, not
         422 — Pydantic's default dependency-time parsing would invert
         that order.
      5. 404 — account does not belong to the caller's org.
      6. 409 — delta is exactly zero ("no change to apply").

    On success returns the response body and writes a
    ``org.account.balance.adjust`` audit row in the SAME transaction
    as the balance write (see ``adjust_account_balance``).
    """
    # 2. admin gate first (architect: admin-403 wins over flag-403).
    if not _is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    # 3. flag gate. Re-fetch the org so a stale `current_user.organization`
    # snapshot (set at login) doesn't authorize a privilege the admin just
    # toggled off in another tab.
    org = await db.scalar(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    if org is None or not org.allow_manual_balance_adjustment:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manual balance adjustment is disabled for this organization",
        )

    # 4. body validation. Manual parse to keep the gates above ahead of
    # 422 in the precedence order. Pydantic's default `body: Schema`
    # dependency would resolve before the handler body runs.
    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid JSON body",
        )
    try:
        body = BalanceAdjustmentRequest.model_validate(raw_body)
    except PydanticValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=e.errors(),
        )

    # Snapshot the actor identity NOW. Once we await on db, a rollback
    # path could expire `current_user` and lazy-loads on .email / .id /
    # .organization would raise.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    actor_org_name = org.name
    req_id = _request_id()
    ip = get_client_ip(request)

    # 5. cross-org or missing account → 404.
    target_acct = await db.scalar(
        select(Account).where(
            Account.id == account_id, Account.org_id == actor_org_id
        )
    )
    if target_acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        tx, old_balance, new_balance, delta = await adjust_account_balance(
            db,
            actor_org_id,
            account_id,
            target_balance=body.target_balance,
            reason=body.reason,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            actor_org_name=actor_org_name,
            request_id=req_id,
            ip_address=ip,
        )
    except ConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await logger.ainfo(
        "org.account.balance.adjust",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        account_id=account_id,
        old_balance=str(old_balance),
        new_balance=str(new_balance),
        delta=str(delta),
        generated_transaction_id=tx.id,
    )

    return BalanceAdjustmentResponse(
        account_id=account_id,
        old_balance=old_balance,
        new_balance=new_balance,
        delta=delta,
        transaction_id=tx.id,
    )
