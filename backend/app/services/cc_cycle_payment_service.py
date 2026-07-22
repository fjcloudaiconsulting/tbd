"""Per-cycle CC payment validation + cycle enumeration (Slice 2).

Thin service over the shipped ``cc_cycle_service`` resolver
(``specs/2026-07-22-cc-model-v1-design.md`` § Validation, § Router
wiring). All cycle math is delegated to
``resolve_cycle_for_account`` (D8: callers never re-derive).

Rules (per spec):
  - Gate on ``slug == 'credit_card'`` ONLY (NOT on payment_strategy):
    amounts are stored regardless of the strategy in effect; the
    forecast reader (Slice 3) decides at read time whether to consult
    the table. Non-CC -> 422.
  - ``amount > 0`` -> else 422 (skipped when ``amount is None``, i.e.
    the DELETE path).
  - The (account, year, month) anchor must be CURRENT-or-FUTURE; a
    past-cycle write -> 409 (D6 read-only-past). "Current" = the close
    month of the cycle ``resolve_cycle_for_account(account, today)``
    falls in.

Anchor = the cycle's CLOSE month
(``period_end_inclusive.year`` / ``.month``). ``resolve_anchor_cycle``
resolves for day 1 of the anchor month: day 1 is always <= that month's
(clamped) close day, so the resolver returns exactly the cycle closing
in that month.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException

from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle_for_account


N_UPCOMING_CYCLES = 3
_CC = "credit_card"


def resolve_anchor_cycle(account: object, *, year: int, month: int) -> CreditCardCycle:
    """Map a close-month anchor ``(year, month)`` to its cycle.

    Resolves for day 1 of the anchor month. Because the close day is
    always >= 1, day 1 falls on-or-before that month's close, so the
    resolver returns the cycle whose ``period_end_inclusive`` is in
    ``(year, month)``. Raises ``ValueError`` on a non-CC account
    (``close_day is None``) — the resolver's own guard.
    """
    return resolve_cycle_for_account(account, date(year, month, 1))


def upcoming_cycles(
    account: object, *, today: date, n: int = N_UPCOMING_CYCLES
) -> list[CreditCardCycle]:
    """Return the next ``n`` cycles at/after ``today``.

    Walks forward: resolve the cycle for ``today`` (its
    ``period_end_inclusive`` is the next close on-or-after today), then
    step to the day after that close and resolve again.
    """
    cycles: list[CreditCardCycle] = []
    cursor = today
    for _ in range(n):
        cycle = resolve_cycle_for_account(account, cursor)
        cycles.append(cycle)
        cursor = cycle.period_end_inclusive + timedelta(days=1)
    return cycles


def _anchor_key(cycle: CreditCardCycle) -> tuple[int, int]:
    return (cycle.period_end_inclusive.year, cycle.period_end_inclusive.month)


def validate_cycle_payment(
    *,
    account: object,
    account_slug: Optional[str],
    year: int,
    month: int,
    today: date,
    amount: Optional[Decimal] = None,
) -> None:
    """Validate a per-cycle payment write against the spec rules.

    Raises ``HTTPException(422)`` for a non-CC account or a non-positive
    amount, ``HTTPException(409)`` for a past anchor. Returns ``None`` on
    success. ``amount=None`` (DELETE) skips the amount check but still
    enforces the CC gate + current-or-future rule.
    """
    if account_slug != _CC or getattr(account, "close_day", None) is None:
        raise HTTPException(
            status_code=422,
            detail="cycle payments are only allowed on credit_card accounts",
        )
    if amount is not None and amount <= 0:
        raise HTTPException(
            status_code=422,
            detail="amount must be greater than 0",
        )
    try:
        resolve_anchor_cycle(account, year=year, month=month)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="the requested cycle does not resolve to a credit_card cycle",
        )
    current = resolve_cycle_for_account(account, today)
    if (year, month) < _anchor_key(current):
        raise HTTPException(
            status_code=409,
            detail="cannot set a payment for a past cycle",
        )
