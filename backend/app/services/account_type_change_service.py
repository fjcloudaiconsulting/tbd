"""Account-type change service (Edit Account Type).

This module owns the write path for ``PUT /api/v1/accounts/{id}`` calls
that mutate ``account_type_id`` or ``close_day``. It runs in its OWN
session/transaction (Pattern (b) in
``specs/2026-05-09-edit-account-type.md`` § 4.3) so the row lock
acquires on a fresh ``async with svc_db.begin():`` block without
colliding with the request-session autobegin from the auth dependency
chain. The router invokes this service when the body touches either of
those two columns; pure name / is_active / is_default / opening_balance
edits remain on the request session and do NOT pass through here.

Cascade rules (§ 3.1):

- Source S = current slug, Target T = target slug after the change.
- (not credit_card -> credit_card)  payload MUST carry close_day (400 if missing).
- (credit_card -> not credit_card)  server clears close_day to NULL; payload
   carrying a non-null close_day is rejected (400).
- (credit_card -> credit_card)      no-op on type; payload may set close_day.
- (not credit_card -> not credit_card)  payload MUST NOT carry close_day (400);
   this also covers the "no type change, only close_day on non-CC" hole the
   PUT path silently accepted before this spec.

CC payment day columns (spec 2026-05-28-cc-billing-cycle.md, Slice 1):

- ``payment_day`` and ``payment_day_relative_month`` mirror the close_day
  invariant: NULL on non-CC accounts, optional on CC accounts (NULL means
  "use resolver default"). Cascade is independent of close_day:
  - (not CC -> CC): payload MAY carry either or both; both are optional.
  - (CC -> not CC): server clears both to NULL regardless of payload.
  - (CC -> CC): payload MAY update either or both; explicit NULL on a CC
    account is permitted (unlike close_day) because NULL means "use default".
  - (not CC -> not CC): payload MUST NOT carry non-null values (400).

Cross-org target type ID resolves to 422 (entity-not-for-you semantics)
to leave 400 reserved for cascade violations. Out-of-range values are
caught by Pydantic before this service runs (422).
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models.account import Account, AccountType
from app.models.cc_cycle_payment import CcCyclePayment


_CC = "credit_card"


class TypeChangeResult:
    """Lightweight DTO so the router can audit-log and respond.

    Carries the post-commit snapshot. The route refetches via the
    request session for the response projection; this struct only
    feeds the audit payload.
    """

    __slots__ = (
        "account_id",
        "old_type_id",
        "new_type_id",
        "old_type_slug",
        "new_type_slug",
        "old_close_day",
        "new_close_day",
        "old_payment_day",
        "new_payment_day",
        "old_payment_day_relative_month",
        "new_payment_day_relative_month",
        "type_changed",
        "deleted_cycle_payments",
    )

    def __init__(
        self,
        *,
        account_id: int,
        old_type_id: int,
        new_type_id: int,
        old_type_slug: Optional[str],
        new_type_slug: Optional[str],
        old_close_day: Optional[int],
        new_close_day: Optional[int],
        old_payment_day: Optional[int],
        new_payment_day: Optional[int],
        old_payment_day_relative_month: Optional[int],
        new_payment_day_relative_month: Optional[int],
        type_changed: bool,
        deleted_cycle_payments: Optional[list] = None,
    ) -> None:
        self.account_id = account_id
        self.old_type_id = old_type_id
        self.new_type_id = new_type_id
        self.old_type_slug = old_type_slug
        self.new_type_slug = new_type_slug
        self.old_close_day = old_close_day
        self.new_close_day = new_close_day
        self.old_payment_day = old_payment_day
        self.new_payment_day = new_payment_day
        self.old_payment_day_relative_month = old_payment_day_relative_month
        self.new_payment_day_relative_month = new_payment_day_relative_month
        self.type_changed = type_changed
        self.deleted_cycle_payments = deleted_cycle_payments or []


def validate_close_day_cascade(
    *,
    source_slug: Optional[str],
    target_slug: Optional[str],
    close_day_in_payload: bool,
    close_day_value: Optional[int],
) -> None:
    """Validate the cascade matrix in spec § 3.1. Raises HTTPException
    with the spec-locked status/detail strings; returns None on success.

    ``close_day_in_payload`` indicates whether the request body
    explicitly set the field (``"close_day" in body.model_fields_set``).
    A caller that omits the field has ``close_day_in_payload=False``;
    a caller that sends ``"close_day": null`` has
    ``close_day_in_payload=True`` and ``close_day_value=None``.
    """
    target_is_cc = target_slug == _CC
    source_is_cc = source_slug == _CC

    # Target = credit_card branch (spec § 3.1 rows 1 and 3).
    if target_is_cc:
        if source_is_cc:
            # CC -> CC: no-op on type per spec § 3.1 row 3. Payload MAY
            # include close_day to update the day, or MAY omit it (leave
            # the existing day untouched). Explicit ``close_day: null``
            # is invalid because a credit_card row must keep a non-null
            # close_day. PR #246 second-review P1 fix: previously this
            # branch required close_day in the payload even for no-op
            # type updates.
            if close_day_in_payload and close_day_value is None:
                raise HTTPException(
                    status_code=400,
                    detail="close_day is required for credit_card accounts",
                )
            return
        # not-CC -> CC: payload MUST carry a non-null close_day
        # (spec § 3.1 row 1).
        if not close_day_in_payload or close_day_value is None:
            raise HTTPException(
                status_code=400,
                detail="close_day is required when changing to credit_card",
            )
        # Range check is enforced by Pydantic (Field(ge=1, le=31)) so
        # by the time we reach this branch close_day is in [1, 31]. The
        # cc_cycle_service resolver clamps 29-31 in short months.
        return

    # Target != credit_card. Payload must NOT carry a non-null close_day.
    # This also closes the "close_day-only edit on non-CC account"
    # silent-tolerance hole called out in spec § 4.2 row 5.
    if close_day_in_payload and close_day_value is not None:
        raise HTTPException(
            status_code=400,
            detail="close_day is only allowed on credit_card accounts",
        )


def validate_create_close_day(
    *,
    target_slug: Optional[str],
    close_day_value: Optional[int],
) -> None:
    """Spec § 3.1.1 — create-path validation mirrors the PUT cascade.

    Distinct from ``validate_close_day_cascade`` because create has no
    "source slug" or "payload has the field set" concept. The
    ``AccountCreate`` schema declares ``close_day: Optional[int]``
    defaulting to ``None``, so we only check whether the resolved
    value is null/non-null against the target slug.
    """
    if target_slug == _CC and close_day_value is None:
        raise HTTPException(
            status_code=400,
            detail="close_day is required when creating a credit_card account",
        )
    if target_slug != _CC and close_day_value is not None:
        raise HTTPException(
            status_code=400,
            detail="close_day is only allowed on credit_card accounts",
        )


# Resolver NULL-defaults (mirror cc_cycle_service._DEFAULT_*); a NULL
# payment_day means "day 1", a NULL relative_month means "next month".
_DEFAULT_PAYMENT_DAY = 1
_DEFAULT_PAYMENT_DAY_RELATIVE_MONTH = 1


def _reject_same_month_payment_before_close(
    *,
    close_day: Optional[int],
    payment_day: Optional[int],
    payment_day_relative_month: Optional[int],
) -> None:
    """Reject a CC config that would pay a statement on/before it closes.

    When the payment lands in the SAME calendar month as the close
    (``payment_day_relative_month == 0``), the resolved payment date must
    fall strictly AFTER the close date. You cannot owe or pay a statement
    before it is issued, so a same-month ``payment_day <= close_day`` is
    semantically undefined (and collapses the forecast's credit-attribution
    window ``close < eff <= payment_date`` to empty, overstating the
    projected payment — see ``cc_forecast_service`` line ~155).

    Operates on EFFECTIVE values, matching the resolver: a NULL
    ``payment_day`` means day 1, a NULL ``relative_month`` means next month
    (always after close, never rejected). Only an EXPLICIT same-month
    config is checked. Callers must invoke this only for CC targets, where
    ``close_day`` is guaranteed non-null.

    400 (cross-column business-rule violation) matches the sibling cascade
    validators; 422 stays reserved for cross-org entity resolution and
    Pydantic field-range checks.
    """
    eff_relative_month = (
        payment_day_relative_month
        if payment_day_relative_month is not None
        else _DEFAULT_PAYMENT_DAY_RELATIVE_MONTH
    )
    if eff_relative_month != 0:
        # Next month (or later) is always after this cycle's close.
        return
    if close_day is None:
        # CC accounts always carry a close_day; defensively no-op otherwise
        # (the missing-close_day case is caught by the close_day validator).
        return
    eff_payment_day = (
        payment_day if payment_day is not None else _DEFAULT_PAYMENT_DAY
    )
    if eff_payment_day <= close_day:
        raise HTTPException(
            status_code=400,
            detail=(
                "payment_day must be after close_day when the payment is in "
                "the same month as the close (payment_day_relative_month = 0)"
            ),
        )


def validate_payment_day_cascade(
    *,
    target_slug: Optional[str],
    payment_day_in_payload: bool,
    payment_day_value: Optional[int],
    payment_day_relative_month_in_payload: bool,
    payment_day_relative_month_value: Optional[int],
) -> None:
    """Validate payment_day / payment_day_relative_month cascade rules.

    Mirrors the shape of ``validate_close_day_cascade`` but with relaxed
    CC-target rules: on CC accounts both new columns are OPTIONAL (NULL
    is valid — it means "use resolver default"). Only non-CC payloads
    carrying non-null values are rejected.

    Cascade matrix (spec D3 / Slice 1):
    - (not CC -> CC): payload MAY carry either column; NULL/omitted is fine.
    - (CC -> not CC): server clears both to NULL; payload carrying non-null
      value(s) is rejected (400).
    - (CC -> CC): payload MAY update either column; NULL is allowed.
    - (not CC -> not CC): payload MUST NOT carry non-null values (400).
    """
    target_is_cc = target_slug == _CC

    if target_is_cc:
        # CC target: any value (including NULL) is fine. The range check
        # is Pydantic's job (ge/le constraints on the schema field). Nothing
        # to reject here.
        return

    # Target != CC. Non-null values are forbidden on non-CC accounts.
    if payment_day_in_payload and payment_day_value is not None:
        raise HTTPException(
            status_code=400,
            detail="payment_day is only allowed on credit_card accounts",
        )
    if (
        payment_day_relative_month_in_payload
        and payment_day_relative_month_value is not None
    ):
        raise HTTPException(
            status_code=400,
            detail="payment_day_relative_month is only allowed on credit_card accounts",
        )


def validate_create_payment_day(
    *,
    target_slug: Optional[str],
    payment_day_value: Optional[int],
    payment_day_relative_month_value: Optional[int],
    close_day_value: Optional[int] = None,
) -> None:
    """Create-path validation for payment_day / payment_day_relative_month.

    Mirrors ``validate_create_close_day`` but with relaxed CC rules:
    both new columns are optional on CC accounts (NULL = resolver default).
    Non-CC accounts must not carry non-null values. On CC accounts the
    same-month payment-before-close rule is enforced against the create
    payload's ``close_day``.
    """
    if target_slug != _CC:
        if payment_day_value is not None:
            raise HTTPException(
                status_code=400,
                detail="payment_day is only allowed on credit_card accounts",
            )
        if payment_day_relative_month_value is not None:
            raise HTTPException(
                status_code=400,
                detail="payment_day_relative_month is only allowed on credit_card accounts",
            )
        return

    _reject_same_month_payment_before_close(
        close_day=close_day_value,
        payment_day=payment_day_value,
        payment_day_relative_month=payment_day_relative_month_value,
    )


async def apply_type_change_in_session(
    svc_db: AsyncSession,
    *,
    account_id: int,
    org_id: int,
    target_type_id: int,
    close_day_in_payload: bool,
    close_day_value: Optional[int],
    payment_day_in_payload: bool = False,
    payment_day_value: Optional[int] = None,
    payment_day_relative_month_in_payload: bool = False,
    payment_day_relative_month_value: Optional[int] = None,
) -> tuple[Account, TypeChangeResult]:
    """Lock the row, validate the cascade, stage the type change.

    The four payment_day_* params default to "omitted" so existing call
    sites that only flip the account type (no payment-day touch) keep
    working unchanged. PR #374 review noted this is a silent gap if a
    future caller forgets to thread payment-day through — guarded today
    by the router's ``touches_type_or_cc_columns`` gate, which routes
    any payment-day-touching PUT through this function with the params
    set.

    Caller owns the transaction and the commit. This is the form the
    PUT route uses (PR #246 review feedback) so it can chain other
    field mutations (name, is_active, opening_balance, etc.) into the
    same transaction and roll back atomically on any failure.

    The ``SELECT ... FOR UPDATE`` row lock still acquires inside the
    caller-owned ``async with svc_db.begin():`` block; on MySQL the
    lock holds for the duration of the outer transaction, exactly as
    spec § 4.3 requires. SQLite ignores ``with_for_update()``; that is
    enforced at the prod DB layer.

    Returns the locked ``Account`` row (mutated in-place) and a
    ``TypeChangeResult`` snapshot the caller hands to
    ``audit_service.record_audit_event()`` AFTER the outer commit
    succeeds. Raises ``HTTPException`` for 400/404/422 outcomes; the
    caller's ``async with`` block rolls back on the exception.
    """
    stmt = (
        select(Account)
        .options(selectinload(Account.account_type))
        .where(Account.id == account_id)
        .where(Account.org_id == org_id)
        .with_for_update()
    )
    account = (await svc_db.execute(stmt)).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    old_type_id = account.account_type_id
    old_type_slug = (
        account.account_type.slug if account.account_type else None
    )
    old_close_day = account.close_day
    old_payment_day = account.payment_day
    old_payment_day_relative_month = account.payment_day_relative_month

    # Target-type existence + cross-org check. 422 (entity-not-for-you)
    # rather than 400, to leave 400 reserved for cascade violations
    # per spec § 4.2.
    target_type = (
        await svc_db.execute(
            select(AccountType).where(
                AccountType.id == target_type_id,
                AccountType.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if target_type is None:
        raise HTTPException(status_code=422, detail="Invalid account type")
    target_slug = target_type.slug
    type_changed = target_type_id != old_type_id

    # Validate the cascade against the post-lock snapshot.
    validate_close_day_cascade(
        source_slug=old_type_slug,
        target_slug=target_slug,
        close_day_in_payload=close_day_in_payload,
        close_day_value=close_day_value,
    )
    validate_payment_day_cascade(
        target_slug=target_slug,
        payment_day_in_payload=payment_day_in_payload,
        payment_day_value=payment_day_value,
        payment_day_relative_month_in_payload=payment_day_relative_month_in_payload,
        payment_day_relative_month_value=payment_day_relative_month_value,
    )

    # Same-month payment-before-close guard, evaluated against the MERGED
    # post-write triple (payload value if carried, else the locked row's
    # current value). A partial PUT touching only relative_month=0 must
    # still be validated against the existing payment_day / close_day, so
    # the guard cannot live in a stateless schema validator.
    if target_slug == _CC:
        merged_close_day = close_day_value if close_day_in_payload else old_close_day
        merged_payment_day = (
            payment_day_value if payment_day_in_payload else old_payment_day
        )
        merged_relative_month = (
            payment_day_relative_month_value
            if payment_day_relative_month_in_payload
            else old_payment_day_relative_month
        )
        _reject_same_month_payment_before_close(
            close_day=merged_close_day,
            payment_day=merged_payment_day,
            payment_day_relative_month=merged_relative_month,
        )

    # Apply: type first, then cascade the close_day column.
    account.account_type_id = target_type_id

    deleted_cycle_payments: list = []

    if target_slug == _CC:
        # CC target: write close_day only when the payload carried it.
        # The validator guarantees:
        #   - on not-CC -> CC, ``close_day_in_payload`` is True and
        #     ``close_day_value`` is non-null;
        #   - on CC -> CC with omitted close_day, the field stays
        #     untouched (spec § 3.1 row 3, PR #246 second-review P1 fix).
        if close_day_in_payload:
            account.close_day = close_day_value
        # Payment columns: write only when the payload carried them.
        # NULL is a valid value (means "use resolver default") so we use
        # the ``_in_payload`` sentinel rather than null-checking.
        if payment_day_in_payload:
            account.payment_day = payment_day_value
        if payment_day_relative_month_in_payload:
            account.payment_day_relative_month = payment_day_relative_month_value
    else:
        # Server-side clear when leaving CC, regardless of whether the
        # payload carried these columns. Idempotent for non-CC -> non-CC.
        account.close_day = None
        account.payment_day = None
        account.payment_day_relative_month = None
        # Payment Source Foundation: a "paid from" pointer only makes sense
        # on a liability. Clear it on leaving CC so an asset account can't
        # retain an orphaned pointer (which the CC-only picker could never
        # surface to clear). Mirrors the close_day leave-CC cascade above.
        account.payment_source_account_id = None
        # Credit Card Model V1 (Slice 1): the four CC-only metadata columns
        # only make sense on a credit_card row. Clear them on leaving CC so
        # an asset account can't retain an orphaned credit_limit / apr /
        # payment_strategy / fixed_payment_amount (mirrors the close_day and
        # payment_source leave-CC cascades above).
        account.credit_limit = None
        account.apr = None
        account.payment_strategy = None
        account.fixed_payment_amount = None

        # Credit Card Model V1 (Slice 2): per-cycle payment rows are money-
        # bearing and anchored to the close_day being cleared here. Keeping
        # them orphans money data no UI can surface and risks resurrecting
        # stale amounts on a later revert. ON DELETE CASCADE only covers
        # account DELETION, not a type change, so delete explicitly; snapshot
        # first so the router can emit account.cycle_payment.deleted events.
        _cp_rows = (
            await svc_db.execute(
                select(CcCyclePayment).where(
                    CcCyclePayment.account_id == account_id
                )
            )
        ).scalars().all()
        deleted_cycle_payments = [
            {"year": r.period_anchor_year, "month": r.period_anchor_month, "amount": str(r.amount)}
            for r in _cp_rows
        ]
        if _cp_rows:
            await svc_db.execute(
                delete(CcCyclePayment).where(CcCyclePayment.account_id == account_id)
            )

    result = TypeChangeResult(
        account_id=account_id,
        old_type_id=old_type_id,
        new_type_id=target_type_id,
        old_type_slug=old_type_slug,
        new_type_slug=target_slug,
        old_close_day=old_close_day,
        new_close_day=account.close_day,
        old_payment_day=old_payment_day,
        new_payment_day=account.payment_day,
        old_payment_day_relative_month=old_payment_day_relative_month,
        new_payment_day_relative_month=account.payment_day_relative_month,
        type_changed=type_changed,
        deleted_cycle_payments=deleted_cycle_payments,
    )
    return account, result


async def change_account_type(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    account_id: int,
    org_id: int,
    target_type_id: int,
    close_day_in_payload: bool,
    close_day_value: Optional[int],
    payment_day_in_payload: bool = False,
    payment_day_value: Optional[int] = None,
    payment_day_relative_month_in_payload: bool = False,
    payment_day_relative_month_value: Optional[int] = None,
) -> TypeChangeResult:
    """Thin owning-transaction wrapper for callers that only want to
    flip the type and have no other fields to mutate.

    The PUT route does NOT use this wrapper because it needs to chain
    additional mutations (name, is_active w/ balance guard,
    opening_balance) into the same transaction so the whole request
    is atomic (PR #246 review feedback, P1 atomicity bug). See
    ``apply_type_change_in_session`` for the route's path.

    Kept on the surface for tests / future callers that genuinely
    want a stand-alone type change. Pattern (b) per spec § 4.3.
    """
    async with session_factory() as svc_db:
        async with svc_db.begin():
            _account, result = await apply_type_change_in_session(
                svc_db,
                account_id=account_id,
                org_id=org_id,
                target_type_id=target_type_id,
                close_day_in_payload=close_day_in_payload,
                close_day_value=close_day_value,
                payment_day_in_payload=payment_day_in_payload,
                payment_day_value=payment_day_value,
                payment_day_relative_month_in_payload=payment_day_relative_month_in_payload,
                payment_day_relative_month_value=payment_day_relative_month_value,
            )
            # Context manager commits on clean exit. Do NOT call
            # await svc_db.commit() here -- spec § 4.3 warning.
    return result
