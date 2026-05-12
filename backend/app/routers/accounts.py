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
from app.services.exceptions import ConflictError, ValidationError
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
        is_default=account.is_default,
        opening_balance=account.opening_balance,
        opening_balance_date=account.opening_balance_date,
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
    if at_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=400, detail="Invalid account type")

    # opening_balance_date: caller may omit (and ride the DB default of
    # CURRENT_DATE) or supply an explicit date. We pass it through only
    # when supplied so the column-level server_default applies on omission.
    kwargs = dict(
        org_id=current_user.org_id,
        account_type_id=body.account_type_id,
        name=body.name,
        balance=body.balance,
        currency=body.currency,
        close_day=body.close_day,
        opening_balance=body.opening_balance,
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
    result = await db.execute(
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id, Account.org_id == current_user.org_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    # Snapshot opening fields BEFORE mutation so the audit detail can
    # compare old vs new even after commit-time refresh / expire.
    old_opening_balance = account.opening_balance
    old_opening_balance_date = account.opening_balance_date
    opening_changed = False

    if body.name is not None:
        account.name = body.name
    if body.account_type_id is not None:
        at_result = await db.execute(
            select(AccountType).where(
                AccountType.id == body.account_type_id,
                AccountType.org_id == current_user.org_id,
            )
        )
        if at_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=400, detail="Invalid account type")
        account.account_type_id = body.account_type_id
    if body.is_active is not None:
        if body.is_active is False and account.balance != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot deactivate account with balance {account.balance}. Transfer the balance first.",
            )
        account.is_active = body.is_active
    if "close_day" in body.model_fields_set:
        account.close_day = body.close_day
    if body.is_default is True:
        async with db.begin_nested():
            await db.execute(
                update(Account)
                .where(Account.org_id == current_user.org_id, Account.id != account.id)
                .values(is_default=False)
            )
            account.is_default = True
    elif body.is_default is False:
        account.is_default = False

    # Opening balance fields. Both fields are optional in the body; we
    # only mutate when explicitly supplied. The audit-event is emitted
    # iff at least one of the two fields actually changed.
    if body.opening_balance is not None and body.opening_balance != account.opening_balance:
        account.opening_balance = body.opening_balance
        opening_changed = True
    if (
        body.opening_balance_date is not None
        and body.opening_balance_date != account.opening_balance_date
    ):
        account.opening_balance_date = body.opening_balance_date
        opening_changed = True

    # Capture identity NOW so any post-commit expire doesn't break the
    # audit-event payload.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)
    new_opening_balance = account.opening_balance
    new_opening_balance_date = account.opening_balance_date

    await db.commit()

    if opening_changed:
        # Fire-and-forget audit row in its own session. Matches the
        # convention in tags / admin_users / auth.
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
