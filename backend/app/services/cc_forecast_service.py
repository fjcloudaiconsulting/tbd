"""Pure credit-card projected-payment math (Credit Card Model V1, Slice 3).

DB-free. All arithmetic for the forecast synthesis lives here so it is
unit-testable without a database; ``projection_kernel``
supplies the batch-fetched inputs and applies the resulting deltas.

Locked contract (``specs/2026-07-22-cc-model-v1-design.md`` § "Forecast
integration", § "Amount resolution", § "Edge cases", § "Resolved by
architect review"):

  outstanding_at_close = max(0, -B_k)          # owed stored NEGATIVE
  B_k = opening_balance + Σ signed(eff_date <= close_date)   # cash-basis, as-of-CLOSE
  target  = per-strategy target payment
  capped  = min(target, outstanding_at_close)  # never pay a card into credit
  outflow = max(0, capped - P_k_owned - S_prev)

``S_prev`` threads earlier synthesized outflows this horizon (stops a
two-due-date horizon double-billing carried debt); ``P_k_owned``
attributes each real payment-in credit to exactly ONE owning cycle (the
earliest window), so overlapping windows (payment_day_relative_month>=2)
don't double-net.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import logging
from dateutil.relativedelta import relativedelta

from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle_for_account

logger = logging.getLogger(__name__)

# Safety margin on the enumeration walk, on top of two steps per month of the
# horizon (a duplicate close costs a step). It only guards against a
# pathological resolver loop; a horizon of any length is walked in full.
_WALK_MARGIN = 12


def due_cycles_in_horizon(
    account: object, *, p_start: date, p_end: date
) -> list[CreditCardCycle]:
    """Every cycle whose ``payment_date`` falls in ``[p_start, p_end]``.

    Walks close dates forward from a lookback point far enough before the
    horizon to catch grace-period cycles (a cycle that closed before the
    horizon but is due inside it). ``payment_date`` is monotonic in the
    close date, so once it exceeds ``p_end`` the walk is done.
    """
    lookback_months = (getattr(account, "payment_day_relative_month", None) or 1) + 1
    cursor = p_start - relativedelta(months=lookback_months)
    seen: set[date] = set()
    cycles: list[CreditCardCycle] = []
    months = (p_end.year - cursor.year) * 12 + (p_end.month - cursor.month) + 1
    for _ in range(2 * months + _WALK_MARGIN):
        cycle = resolve_cycle_for_account(account, cursor)
        close = cycle.period_end_inclusive
        if close in seen:
            cursor = close + timedelta(days=1)
            continue
        seen.add(close)
        if cycle.payment_date > p_end:
            break
        if cycle.payment_date >= p_start:
            cycles.append(cycle)
        cursor = close + timedelta(days=1)
    else:
        # Never silent: a truncated walk would drop every later payment.
        logger.error(
            "cc_forecast.cycle_walk_cap_hit",
            extra={
                "account_id": getattr(account, "id", None),
                "p_start": p_start.isoformat(),
                "p_end": p_end.isoformat(),
            },
        )
    cycles.sort(key=lambda c: c.payment_date)
    return cycles


def balance_at_close(
    opening_balance: Decimal,
    ledger: list[tuple[date, Decimal]],
    close_date: date,
) -> Decimal:
    """B_k = opening_balance + Σ signed(eff_date <= close_date).

    ``ledger`` is (eff_date, signed_amount) pairs (income +, expense -,
    transfer legs included) for this CC only; the caller filters to
    ``balance_contribution_filter()`` (transaction_filters.py) so the
    reconstruction matches the stored balance -- including dropping
    reconcile-matched imported duplicates whose contribution was reverted
    at match time.
    """
    total = Decimal(str(opening_balance))
    for eff, signed in ledger:
        if eff <= close_date:
            total += signed
    return total


def outstanding_at_close(b_k: Decimal) -> Decimal:
    """Positive magnitude of owed debt at close (owed is stored negative)."""
    return max(Decimal("0"), -b_k)


def _strategy_value(account: object) -> str:
    s = getattr(account, "payment_strategy", None)
    if s is None:
        return "full_balance"          # NULL-at-rest -> resolver default
    return s.value if hasattr(s, "value") else str(s)


def cc_target_payment(
    account: object,
    cycle: CreditCardCycle,
    outstanding: Decimal,
    per_cycle_amounts: dict[tuple[int, int, int], Decimal],
) -> Decimal:
    """Per-cycle target BEFORE the uniform clamp + net.

    Override-first (F2): a stored per-cycle amount, anchored on the cycle's
    CLOSE month, wins for ANY strategy — a single-cycle "pay X this cycle"
    decision that never auto-carries. With no override, fixed_amount pays its
    literal and full_balance / NULL pays the whole outstanding balance.
    """
    anchor = (
        account.id,
        cycle.period_end_inclusive.year,
        cycle.period_end_inclusive.month,
    )
    if anchor in per_cycle_amounts:
        return per_cycle_amounts[anchor]
    if _strategy_value(account) == "fixed_amount":
        amt = getattr(account, "fixed_payment_amount", None)
        return Decimal(str(amt)) if amt is not None else Decimal("0")
    return outstanding


def synthesize_account_cc_payments(
    account: object,
    *,
    p_start: date,
    p_end: date,
    opening_balance: Decimal,
    ledger: list[tuple[date, Decimal]],
    credits: list[tuple[int, date, Decimal]],
    per_cycle_amounts: dict[tuple[int, int, int], Decimal],
    projected: list[tuple[date, Decimal]] = (),
    projected_after: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Chronological (payment_date, outflow) pairs for one CC (outflow > 0).

    ``credits`` is (id, eff_date, amount) for real CC payment-in legs
    (transfer income legs). Threads ``S_prev`` and a consumed-credit set
    across the horizon's cycles.

    ``projected`` is (date, signed) charges that do not exist as rows yet
    (un-generated recurring occurrences). They join the ledger only for a
    cycle whose payment is due after ``projected_after``.
    """
    with_projected = list(ledger) + list(projected)
    cycles = due_cycles_in_horizon(account, p_start=p_start, p_end=p_end)
    s_prev = Decimal("0")
    consumed: set[int] = set()
    payments: list[tuple[date, Decimal]] = []
    for cycle in cycles:
        # Belt-and-suspenders: a payment date on or before the close date
        # is degenerate. For payment < close the projection is temporally
        # nonsensical (a payment dated before the charges it pays), and for
        # payment == close the credit-attribution window (close < eff <=
        # payment) is empty so p_k_owned stays 0. Either way the cycle
        # cannot be projected meaningfully. The create/PUT validation guard
        # forbids the config that produces payment <= close (same-month
        # payment_day <= close_day), but a same-month day-of-month clamp
        # collision (e.g. Feb close 28 + payment 30, both clamp to Feb 28)
        # can still land here. Skip the cycle rather than emit a bogus
        # payment, and log it so the collision is observable, not silent.
        if cycle.payment_date <= cycle.period_end_inclusive:
            logger.warning(
                "cc_forecast.degenerate_cycle_skipped",
                extra={
                    "account_id": getattr(account, "id", None),
                    "close_date": cycle.period_end_inclusive.isoformat(),
                    "payment_date": cycle.payment_date.isoformat(),
                },
            )
            continue
        b_k = balance_at_close(
            opening_balance,
            with_projected if projected_after is not None
            and cycle.payment_date > projected_after else ledger,
            cycle.period_end_inclusive,
        )
        outstanding = outstanding_at_close(b_k)
        target = cc_target_payment(account, cycle, outstanding, per_cycle_amounts)
        # ``outstanding`` is the LEDGER's owed-at-close, blind to the payments
        # projected for earlier cycles, so those come off it first and the
        # target is capped at what is still owed. Subtracting ``s_prev`` from
        # the TARGET instead let a fixed-amount card pay once per horizon.
        # For full balance the two are the same number.
        capped = min(target, outstanding - s_prev)
        p_k_owned = Decimal("0")
        for cid, eff, amt in credits:
            if cid in consumed:
                continue
            if cycle.period_end_inclusive < eff <= cycle.payment_date:
                p_k_owned += amt
                consumed.add(cid)
        outflow = max(Decimal("0"), capped - p_k_owned)
        s_prev += outflow
        if outflow > 0:
            payments.append((cycle.payment_date, outflow))
    return payments
