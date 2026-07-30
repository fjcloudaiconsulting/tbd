"""Per-account expected month-end balance projection.

Dashboard-only view. Distinct from forecast_service.compute_forecast,
which deals with reportable income/expense aggregates (and excludes
transfer legs / manual adjustments). This module answers a different
question:

  "What will each account's balance be at the end of this billing period?"

Account balance is the sum of all settled transactions on the account,
including transfer legs. Pending transactions on the account haven't
moved the stored balance yet but will. So the projection is simply:

  expected_account_balance = stored_balance + sum(pending deltas in period)

with sign by type (income +, expense -). Transfer legs MUST be included
because they DO move balances per-account, even though they're not
reportable income/expense. Manual adjustments are settled-only by design,
but we filter them out defensively from the pending delta in case that
ever changes.

Currency totals are grouped: never sum unlike currencies.
"""

import datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.models.cc_cycle_payment import CcCyclePayment
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import cc_forecast_service, loan_forecast_service
from app.services.billing_service import period_spend_window_end, resolve_period
from app.services.cc_statement_service import load_cc_ledgers
from app.services.loan_service import compute_pmt
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
)


async def compute_account_balance_forecast(
    db: AsyncSession,
    org_id: int,
    *,
    period_start: datetime.date | None = None,
    today: datetime.date | None = None,
) -> dict:
    """Compute expected month-end balance per account for a billing period.

    ``today`` is keyword-only and injectable because the period window floors
    at the wall clock (see ``window_end`` below); it is resolved once, here.

    Returns the spec shape:

        {
          "period_start": "YYYY-MM-DD",
          "period_end": "YYYY-MM-DD",
          "totals": [{currency, balance, pending_delta, expected_month_end_balance}],
          "accounts": [{account_id, account_name, currency, is_default,
                        account_type_slug, balance, pending_delta,
                        expected_month_end_balance}],
        }
    """
    today = today if today is not None else datetime.date.today()

    period = await resolve_period(db, org_id, period_start)

    p_start = period.start_date

    # ── The period's ONE window (TBD-243) ─────────────────────────────────
    # Same single value as `forecast_service.compute_forecast`, for the same
    # reason: it bounds the pending aggregate, the CC synthesis horizon, the
    # loan already-paid probe, the loan synthesis horizon AND the emitted
    # `period_end`. Splitting the settled-sum bound from the projection
    # horizon is rejected by
    # `specs/2026-07-30-forecast-period-window-design.md` §2 (it breaks
    # conservation across `generate_due_transactions`), and every
    # anti-double-count guard here — the loan `already_paid` probe below, CC's
    # `p_k_owned` — has to sit on the window anyway.
    #
    # Closed rows verbatim, never floored (§4 F6). The `None` arm is the
    # roster tail; the calendar expression there keeps `period_end` non-null
    # and keeps `col <= window_end` compilable (§4 F5).
    if period.end_date is not None:
        window_end = period.end_date
    else:
        derived = await period_spend_window_end(db, org_id, period, today=today)
        window_end = derived if derived is not None else (
            p_start + relativedelta(months=1) - datetime.timedelta(days=1)
        )

    accounts_result = await db.execute(
        select(Account, AccountType.slug)
        .join(AccountType, Account.account_type_id == AccountType.id)
        .where(
            Account.org_id == org_id,
            Account.is_active.is_(True),
        )
    )
    rows = accounts_result.all()

    # Aggregate pending transactions in the selected period by account.
    # Sign by type: income +, expense -. Include transfer legs (this is
    # per-account balance math, not reportable). Defensively exclude
    # manual adjustments (settled-only today).
    eff_date = effective_period_date_expr()
    pending_result = await db.execute(
        select(
            Transaction.account_id,
            Transaction.type,
            func.coalesce(func.sum(Transaction.amount), Decimal("0")),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.status == TransactionStatus.PENDING,
            Transaction.is_manual_adjustment.is_(False),
            and_(eff_date >= p_start, eff_date <= window_end),
        )
        .group_by(Transaction.account_id, Transaction.type)
    )

    pending_by_account: dict[int, Decimal] = {}
    for account_id, tx_type, total in pending_result.all():
        delta = Decimal(str(total or 0))
        if tx_type == TransactionType.EXPENSE:
            delta = -delta
        pending_by_account[account_id] = (
            pending_by_account.get(account_id, Decimal("0")) + delta
        )

    # ── Credit-card projected-payment synthesis (Slice 3) ─────────────────────
    # Ephemeral in-memory deltas with provenance source="credit_card_payment":
    # on each resolved due date the source asset drops and the CC liability
    # moves toward zero. Synthesized HERE (per-account balances include transfer
    # legs), never in forecast_service (reportable aggregate excludes them).
    # Totals are NOT adjusted (they derive from balance+pending); same-currency
    # conservation keeps them correct, and cross-currency is skipped so the
    # per-currency rollup never desyncs.
    accounts_by_id = {acct.id: (acct, slug) for acct, slug in rows}
    cc_accounts = [
        acct for acct, slug in rows
        if slug == "credit_card"
        and acct.close_day is not None
        and acct.payment_source_account_id is not None
    ]
    synth_delta_by_account: dict[int, Decimal] = {}
    cc_payments_by_account: dict[int, list[dict]] = {}

    if cc_accounts:
        cc_ids = [a.id for a in cc_accounts]

        pcp_rows = (await db.execute(
            select(CcCyclePayment.account_id, CcCyclePayment.period_anchor_year,
                   CcCyclePayment.period_anchor_month, CcCyclePayment.amount)
            .where(CcCyclePayment.account_id.in_(cc_ids))
        )).all()
        per_cycle_amounts = {(aid, y, m): Decimal(str(amt)) for aid, y, m, amt in pcp_rows}

        # Single source of the CC ledger query (cc_statement_service):
        # UNBOUNDED (no up_to). A due cycle's payment_date is not
        # guaranteed to be >= its own close_date -- with payment_day <
        # close_day and payment_day_relative_month == 0 (same-month
        # payment), payment_date can fall BEFORE close_date. Bounding the
        # fetch at window_end would then drop ledger rows in
        # (window_end, close_date] that balance_at_close(close_date) needs,
        # silently under-counting outstanding. This matches the
        # pre-refactor inline query, which
        # was also unbounded and let balance_at_close's own close_date
        # re-filter do the work.
        ledger_by_account = await load_cc_ledgers(db, org_id, cc_ids)

        credit_rows = (await db.execute(
            select(Transaction.id, Transaction.account_id, eff_date.label("eff"), Transaction.amount)
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(cc_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   balance_contribution_filter())
        )).all()
        credits_by_account: dict[int, list[tuple]] = {}
        for cid, aid, eff, amt in credit_rows:
            credits_by_account.setdefault(aid, []).append((cid, eff, Decimal(str(amt))))

        for cc in cc_accounts:
            source_entry = accounts_by_id.get(cc.payment_source_account_id)
            if source_entry is None:
                continue  # source inactive/not loaded -> no-op (do not resurrect)
            source, _ = source_entry
            if source.currency != cc.currency:
                continue  # no FX in V1 -> would desync per-currency totals
            payments = cc_forecast_service.synthesize_account_cc_payments(
                cc, p_start=p_start, p_end=window_end,
                opening_balance=Decimal(str(cc.opening_balance)),
                ledger=ledger_by_account.get(cc.id, []),
                credits=credits_by_account.get(cc.id, []),
                per_cycle_amounts=per_cycle_amounts,
            )
            for pay_date, outflow in payments:
                synth_delta_by_account[source.id] = synth_delta_by_account.get(source.id, Decimal("0")) - outflow
                synth_delta_by_account[cc.id] = synth_delta_by_account.get(cc.id, Decimal("0")) + outflow
                cc_payments_by_account.setdefault(cc.id, []).append(
                    {"amount": _q(outflow), "date": pay_date.isoformat()})

    # ── Loan projected-payment synthesis (Slice 2) ────────────────────────────
    # Same conserving shape as the CC synthesis above (source drops, loan moves
    # toward zero on the scheduled payment date), accumulated into the SAME
    # synth_delta_by_account map so a source that funds both a CC and a loan
    # keeps both deltas. Design A (period-skip): outstanding uses the CURRENT
    # balance and we SKIP the period when a loan payment-in leg is already
    # accounted for. See loan_forecast_service for the O2 rationale.
    loan_accounts = [
        acct for acct, slug in rows
        if slug == "loan"
        and acct.payment_source_account_id is not None
        and acct.principal_amount is not None
        and acct.interest_rate_apr is not None
        and acct.term_months is not None
        and acct.origination_date is not None
        and acct.first_payment_date is not None
    ]
    loan_payments_by_account: dict[int, list[dict]] = {}

    if loan_accounts:
        loan_ids = [a.id for a in loan_accounts]

        # Already-paid signal: any linked INCOME (payment-in) leg on the loan
        # whose effective date falls in the period. balance_contribution_filter()
        # carries NO status filter, so this catches BOTH settled legs (already
        # in loan.balance) and pending legs (already in pending_delta) -- DO NOT
        # add a status filter here or a pending loan payment double-counts
        # against pending_delta (regression-tested). Disbursement legs are an
        # EXPENSE on the loan and never match; manual adjustments are unlinked.
        loan_paid_rows = (await db.execute(
            select(Transaction.account_id).distinct()
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(loan_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   balance_contribution_filter(),
                   and_(eff_date >= p_start, eff_date <= window_end))
        )).all()
        loan_paid_ids = {aid for (aid,) in loan_paid_rows}

        for loan in loan_accounts:
            source_entry = accounts_by_id.get(loan.payment_source_account_id)
            if source_entry is None:
                continue  # source inactive/not loaded -> no-op (do not resurrect)
            source, _ = source_entry
            if source.currency != loan.currency:
                continue  # no FX in V1 -> would desync per-currency totals
            pmt = compute_pmt(
                Decimal(str(loan.principal_amount)),
                Decimal(str(loan.interest_rate_apr)),
                int(loan.term_months),
            )
            payments = loan_forecast_service.synthesize_account_loan_payment(
                first_payment_date=loan.first_payment_date,
                term_months=int(loan.term_months),
                balance=Decimal(str(loan.balance)),
                pmt=pmt,
                p_start=p_start,
                p_end=window_end,
                already_paid=loan.id in loan_paid_ids,
                account_id=loan.id,
            )
            for pay_date, applied in payments:
                synth_delta_by_account[source.id] = synth_delta_by_account.get(source.id, Decimal("0")) - applied
                synth_delta_by_account[loan.id] = synth_delta_by_account.get(loan.id, Decimal("0")) + applied
                loan_payments_by_account.setdefault(loan.id, []).append(
                    {"amount": _q(applied), "date": pay_date.isoformat()})

    accounts_payload: list[dict] = []
    totals_by_currency: dict[str, dict[str, Decimal]] = {}

    sorted_rows = sorted(
        rows,
        key=lambda r: (
            not r[0].is_default,
            r[0].name.casefold(),
            r[0].id,
        ),
    )

    for account, type_slug in sorted_rows:
        balance = Decimal(str(account.balance))
        delta = pending_by_account.get(account.id, Decimal("0"))
        synth = synth_delta_by_account.get(account.id, Decimal("0"))
        expected = balance + delta + synth

        accounts_payload.append(
            {
                "account_id": account.id,
                "account_name": account.name,
                "currency": account.currency,
                "is_default": account.is_default,
                "account_type_slug": type_slug,
                "balance": _q(balance),
                "pending_delta": _q(delta),
                "expected_month_end_balance": _q(expected),
                "cc_payments": cc_payments_by_account.get(account.id, []),
                "loan_payments": loan_payments_by_account.get(account.id, []),
            }
        )

        bucket = totals_by_currency.setdefault(
            account.currency,
            {"balance": Decimal("0"), "pending_delta": Decimal("0")},
        )
        bucket["balance"] += balance
        bucket["pending_delta"] += delta

    totals_payload = [
        {
            "currency": currency,
            "balance": _q(b["balance"]),
            "pending_delta": _q(b["pending_delta"]),
            "expected_month_end_balance": _q(b["balance"] + b["pending_delta"]),
        }
        for currency, b in sorted(totals_by_currency.items())
    ]

    return {
        "period_start": p_start.isoformat(),
        "period_end": window_end.isoformat(),
        "totals": totals_payload,
        "accounts": accounts_payload,
    }


_TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> str:
    """Format a Decimal as a fixed 2-decimal string for JSON transport."""
    return str(value.quantize(_TWOPLACES))
