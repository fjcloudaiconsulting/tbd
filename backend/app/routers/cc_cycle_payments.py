"""Per-cycle CC payment endpoints (Credit Card Model V1, Slice 2).

``specs/2026-07-22-cc-model-v1-design.md`` § "Router wiring / New
router". Collection feeds the "Upcoming payments" mini-list; the
{year}/{month} mutations are the close-month anchor.

Org isolation: the parent account is always loaded under
``current_user.org_id`` (the table has no org_id column). Reads are a
NORMAL org-scoped account read (any member); mutations are owner/admin
only (``_is_admin_user`` — money-bearing, mirrors opening-balance).
Audit events fire post-commit via ``record_audit_event`` (own session).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.account import Account
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.user import User
from app.rate_limit import get_client_ip
from app.routers.accounts import _is_admin_user, _request_id
from app.services import audit_service
from app.services import cc_cycle_payment_service as cycle_svc

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/accounts", tags=["cc-cycle-payments"])


class UpcomingCyclePaymentResponse(BaseModel):
    year: int
    month: int
    close_date: date
    due_date: date
    amount: Optional[Decimal] = None


class CyclePaymentWrite(BaseModel):
    amount: Decimal = Field(max_digits=12, decimal_places=2)


async def _load_account_or_404(
    db: AsyncSession, *, account_id: int, org_id: int
) -> Account:
    account = (
        await db.execute(
            select(Account)
            .options(selectinload(Account.account_type))
            .where(Account.id == account_id, Account.org_id == org_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _slug(account: Account) -> Optional[str]:
    return account.account_type.slug if account.account_type else None


@router.get(
    "/{account_id}/cycle-payments",
    response_model=list[UpcomingCyclePaymentResponse],
)
async def list_cycle_payments(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Next N=3 upcoming cycles for the CC, each with the stored amount
    (or null). Normal org-scoped read. Non-CC / no close_day -> []."""
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if _slug(account) != "credit_card" or account.close_day is None:
        return []

    rows = (
        await db.execute(
            select(CcCyclePayment).where(CcCyclePayment.account_id == account_id)
        )
    ).scalars().all()
    by_anchor = {
        (r.period_anchor_year, r.period_anchor_month): r.amount for r in rows
    }
    cycles = cycle_svc.upcoming_cycles(account, today=date.today())
    return [
        UpcomingCyclePaymentResponse(
            year=c.period_end_inclusive.year,
            month=c.period_end_inclusive.month,
            close_date=c.period_end_inclusive,
            due_date=c.payment_date,
            amount=by_anchor.get(
                (c.period_end_inclusive.year, c.period_end_inclusive.month)
            ),
        )
        for c in cycles
    ]


async def _audit_cycle_payment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: str,
    current_user: User,
    request: Request,
    account_id: int,
    year: int,
    month: int,
    detail_extra: dict,
) -> None:
    await audit_service.record_audit_event(
        session_factory,
        event_type=event_type,
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"account_id": account_id, "year": year, "month": month, **detail_extra},
    )


@router.post("/{account_id}/cycle-payments/{year}/{month}")
async def create_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    body: CyclePaymentWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    cycle_svc.validate_cycle_payment(
        account=account, account_slug=_slug(account),
        year=year, month=month, today=date.today(), amount=body.amount,
    )
    existing = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="cycle payment already exists")

    db.add(
        CcCyclePayment(
            account_id=account_id,
            period_anchor_year=year,
            period_anchor_month=month,
            amount=body.amount,
        )
    )
    await db.commit()
    await _audit_cycle_payment(
        session_factory, event_type="account.cycle_payment.created",
        current_user=current_user, request=request,
        account_id=account_id, year=year, month=month,
        detail_extra={"amount": str(body.amount)},
    )
    return {"year": year, "month": month, "amount": str(body.amount)}


@router.put("/{account_id}/cycle-payments/{year}/{month}")
async def upsert_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    body: CyclePaymentWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    cycle_svc.validate_cycle_payment(
        account=account, account_slug=_slug(account),
        year=year, month=month, today=date.today(), amount=body.amount,
    )
    row = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        db.add(
            CcCyclePayment(
                account_id=account_id,
                period_anchor_year=year,
                period_anchor_month=month,
                amount=body.amount,
            )
        )
        await db.commit()
        await _audit_cycle_payment(
            session_factory, event_type="account.cycle_payment.created",
            current_user=current_user, request=request,
            account_id=account_id, year=year, month=month,
            detail_extra={"amount": str(body.amount)},
        )
    else:
        old_amount = row.amount
        row.amount = body.amount
        await db.commit()
        await _audit_cycle_payment(
            session_factory, event_type="account.cycle_payment.updated",
            current_user=current_user, request=request,
            account_id=account_id, year=year, month=month,
            detail_extra={"old_amount": str(old_amount), "amount": str(body.amount)},
        )
    return {"year": year, "month": month, "amount": str(body.amount)}


@router.delete("/{account_id}/cycle-payments/{year}/{month}")
async def delete_cycle_payment(
    account_id: int,
    year: int,
    month: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    account = await _load_account_or_404(
        db, account_id=account_id, org_id=current_user.org_id
    )
    if not _is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    row = (
        await db.execute(
            select(CcCyclePayment).where(
                CcCyclePayment.account_id == account_id,
                CcCyclePayment.period_anchor_year == year,
                CcCyclePayment.period_anchor_month == month,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="cycle payment not found")

    old_amount = row.amount
    await db.delete(row)
    await db.commit()
    await _audit_cycle_payment(
        session_factory, event_type="account.cycle_payment.deleted",
        current_user=current_user, request=request,
        account_id=account_id, year=year, month=month,
        detail_extra={"amount": str(old_amount)},
    )
    return {"year": year, "month": month, "deleted": True}
