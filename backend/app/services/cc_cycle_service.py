"""Per-CC cycle resolver (spec 2026-05-28-cc-billing-cycle.md, Slice 1).

Single responsibility (D8): given the raw cycle fields from an Account
row — ``close_day``, ``payment_day``, ``payment_day_relative_month`` —
and a target date, return the ``CreditCardCycle`` the target date falls
in, along with the payment date for that cycle.

Slice 1 scope:
- ``source`` is always ``"default"``; override lookup arrives in Slice 3.
- The resolver is a pure function — no DB access, no async. Callers
  pass the three column values directly (or an Account object via the
  convenience wrapper ``resolve_cycle_for_account``).

Architecture decisions honoured here:
  D2 — Inclusive close day (tx on close day belongs to closing cycle).
  D3 — Default payment: day 1, next calendar month after close month.
  D8 — All cycle math lives in this service; callers must not re-derive.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

# Sentinel defaults applied when the stored columns are NULL.
_DEFAULT_PAYMENT_DAY = 1
_DEFAULT_PAYMENT_DAY_RELATIVE_MONTH = 1  # 1 = next calendar month


@dataclass(frozen=True)
class CreditCardCycle:
    """Immutable triple returned by the resolver.

    ``source`` is always ``"default"`` in Slice 1; Slice 3 adds
    ``"override"`` when a ``cc_cycle_overrides`` row wins.
    """

    period_start: date
    period_end_inclusive: date
    payment_date: date
    source: Literal["default", "override"]


def _clamp_day(year: int, month: int, day: int) -> date:
    """Return ``date(year, month, min(day, last_day_of_month))``.

    Mirrors the clamp PFV already uses for ``Organization.billing_cycle_day``
    in ``lib/date_utils.py``. Prevents ValueError on Feb 29/30/31, Apr 31, etc.
    """
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _resolve_payment_date(close: date, payment_day: int, payment_day_relative_month: int) -> date:
    """Compute the payment date from the cycle's effective close date.

    Exactly mirrors the D3 pseudocode in the spec (the fixed version using
    ``calendar.monthrange`` for the clamp):

        months_offset = close.month - 1 + payment_day_relative_month
        target_year   = close.year + months_offset // 12
        target_month  = months_offset % 12 + 1
        last_day      = calendar.monthrange(target_year, target_month)[1]
        return date(target_year, target_month, min(payment_day, last_day))

    ``payment_day_relative_month = 0`` means same calendar month as the
    close date; ``1`` means the following calendar month, etc.
    """
    months_offset = close.month - 1 + payment_day_relative_month
    target_year = close.year + months_offset // 12
    target_month = months_offset % 12 + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(payment_day, last_day))


def resolve_cycle(
    *,
    close_day: Optional[int],
    payment_day: Optional[int],
    payment_day_relative_month: Optional[int],
    target_date: date,
) -> CreditCardCycle:
    """Derive the ``CreditCardCycle`` that contains ``target_date``.

    Parameters
    ----------
    close_day:
        The ``Account.close_day`` value. NULL signals a non-CC account and
        raises ``ValueError`` immediately (callers must guard).
    payment_day:
        ``Account.payment_day``. NULL means "use default" (day 1).
    payment_day_relative_month:
        ``Account.payment_day_relative_month``. NULL means "use default"
        (1 = next calendar month after close month).
    target_date:
        Any date inside the desired cycle (typically today or a transaction
        date).

    Returns
    -------
    CreditCardCycle
        Immutable result with ``period_start``, ``period_end_inclusive``,
        ``payment_date``, and ``source="default"``.

    Raises
    ------
    ValueError
        If ``close_day`` is None (indicates a non-CC account).
    """
    if close_day is None:
        raise ValueError(
            "resolve_cycle called on a non-credit-card account (close_day is None). "
            "Callers should check account type before calling the CC cycle resolver."
        )

    # Apply defaults for NULL stored values (D3).
    eff_payment_day = payment_day if payment_day is not None else _DEFAULT_PAYMENT_DAY
    eff_relative_month = (
        payment_day_relative_month
        if payment_day_relative_month is not None
        else _DEFAULT_PAYMENT_DAY_RELATIVE_MONTH
    )

    # Determine which cycle the target_date falls in.
    #
    # The close day is INCLUSIVE (D2): a transaction on the close day
    # belongs to the cycle that closes on that day, not the next one.
    #
    # Strategy:
    #   - Compute the effective close date for the current calendar month
    #     (clamped for short months).
    #   - If target_date <= that close date → the cycle closes this month.
    #   - Otherwise → the cycle closes next month (target is in the gap
    #     between this month's close and next month's close).

    cy = target_date.year
    cm = target_date.month

    this_month_close = _clamp_day(cy, cm, close_day)

    if target_date <= this_month_close:
        # Target is before or on the close date of this calendar month →
        # cycle closes this month.
        close_date = this_month_close

        # Compute previous month's close date to derive period_start.
        if cm == 1:
            prev_year, prev_month = cy - 1, 12
        else:
            prev_year, prev_month = cy, cm - 1
        prev_close = _clamp_day(prev_year, prev_month, close_day)

        # period_start = the day after the previous month's close.
        # Use date arithmetic to avoid month-boundary edge cases.
        from datetime import timedelta
        period_start = prev_close + timedelta(days=1)
    else:
        # Target is after the close date of this calendar month →
        # cycle closes next month.
        if cm == 12:
            next_year, next_month = cy + 1, 1
        else:
            next_year, next_month = cy, cm + 1
        close_date = _clamp_day(next_year, next_month, close_day)

        # period_start = day after this month's close.
        from datetime import timedelta
        period_start = this_month_close + timedelta(days=1)

    payment_date = _resolve_payment_date(close_date, eff_payment_day, eff_relative_month)

    return CreditCardCycle(
        period_start=period_start,
        period_end_inclusive=close_date,
        payment_date=payment_date,
        source="default",
    )


def resolve_cycle_for_account(account: object, target_date: date) -> CreditCardCycle:
    """Convenience wrapper that reads the three columns from an Account ORM row.

    Raises ``ValueError`` if the account is not a credit-card type
    (``account.close_day is None``).
    """
    return resolve_cycle(
        close_day=getattr(account, "close_day", None),
        payment_day=getattr(account, "payment_day", None),
        payment_day_relative_month=getattr(account, "payment_day_relative_month", None),
        target_date=target_date,
    )
