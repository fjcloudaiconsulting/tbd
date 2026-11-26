"""Which recurring occurrence an imported bank row settles (TBD-755 PR2).

Banks move direct debits off weekends and holidays, so a charge due Monday the
1st lands on Friday the 29th. This finds, for each bank row, the occurrences
of the account's series due within ``WINDOW_DAYS`` of it, and classifies the
row:

* a candidate QUALIFIES when the amount equals the expected amount OR the
  merchant (``normalize_description``) equals the series description. Same
  merchant with a different amount (energy 107 -> 365) binds; an amount match
  with neither merchant nor category is only a suggestion; "amount differs AND
  merchant differs" is no match;
* it is STRONG when the merchant matches, or the row's smart-rule category is
  the series category;
* one qualifying strong candidate -> ``bind`` (the only verdict anything may
  apply without a human: it is the import preselection and sync's auto-bind);
  one qualifying weak candidate -> ``suggest_bind``; two or more ->
  ``choose_occurrence``; two rows claiming one candidate as ``bind`` -> both
  ``choose_occurrence``.

A candidate is either an existing bindable occurrence, or the series' frontier
occurrence that generation has not written yet (``occurrence_id`` None); a
bind creates it. Transfer series are out of v1.
"""
from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionType
from app.services.category_rules_service import normalize_description
from app.services.recurring_filters import active_series_filter
from app.services.transaction_filters import REVERTED_RECONCILIATION_STATES

WINDOW_DAYS = 4


@dataclass(frozen=True)
class Candidate:
    recurring_id: int
    occurrence_id: int | None
    due_date: datetime.date
    expected_amount: Decimal
    description: str
    category_id: int
    type: TransactionType

    @property
    def key(self) -> tuple[int, datetime.date]:
        return (self.recurring_id, self.due_date)


@dataclass(frozen=True)
class RowInput:
    date: datetime.date
    amount: Decimal
    type: TransactionType
    description: str
    suggested_category_id: int | None


@dataclass(frozen=True)
class Match:
    action: str  # "none" | "bind" | "suggest_bind" | "choose_occurrence"
    candidates: tuple[tuple[Candidate, str], ...] = ()  # (candidate, signal)


NO_MATCH = Match("none")


def merchant_key(description: str) -> str:
    """The merchant a description names, for the merchant-equal test."""
    return normalize_description(description) or description.strip().upper()


def _signal(row: RowInput, c: Candidate) -> str | None:
    if merchant_key(row.description) == merchant_key(c.description):
        return "merchant"
    if row.amount != c.expected_amount:
        return None
    if row.suggested_category_id is not None and row.suggested_category_id == c.category_id:
        return "category"
    return "amount"


def classify(rows: list[RowInput | None], candidates: list[Candidate]) -> list[Match]:
    """Pure: one ``Match`` per row; ``None`` rows (already answered by another
    detector) get ``NO_MATCH`` and claim nothing."""
    out: list[Match] = []
    for row in rows:
        if row is None:
            out.append(NO_MATCH)
            continue
        found = [
            (c, sig) for c in candidates
            if c.type == row.type
            and abs((c.due_date - row.date).days) <= WINDOW_DAYS
            and (sig := _signal(row, c)) is not None
        ]
        found.sort(key=lambda cs: (abs((cs[0].due_date - row.date).days), cs[0].due_date))
        if not found:
            out.append(NO_MATCH)
        elif len(found) > 1:
            out.append(Match("choose_occurrence", tuple(found)))
        elif found[0][1] == "amount":
            out.append(Match("suggest_bind", tuple(found)))
        else:
            out.append(Match("bind", tuple(found)))
    claims = Counter(m.candidates[0][0].key for m in out if m.action == "bind")
    return [
        Match("choose_occurrence", m.candidates)
        if m.action == "bind" and claims[m.candidates[0][0].key] > 1 else m
        for m in out
    ]


async def find_candidates(
    db: AsyncSession,
    org_id: int,
    *,
    account_id: int,
    start: datetime.date,
    end: datetime.date,
    not_before: datetime.date,
) -> list[Candidate]:
    """Every candidate due in ``[start - WINDOW_DAYS, end + WINDOW_DAYS]`` on
    the account, in a fixed number of plain (non-locking) reads.

    ``not_before`` is the earliest date a bind may still create an occurrence
    on (``recurring_service.frontier_lower_bound``); an older frontier is
    generation's to write, not the import's.
    """
    lo = start - datetime.timedelta(days=WINDOW_DAYS)
    hi = end + datetime.timedelta(days=WINDOW_DAYS)
    types = (TransactionType.EXPENSE, TransactionType.INCOME)
    referrer = aliased(Transaction)

    # (a) Existing bindable occurrences (``_is_bindable_occurrence``), not
    # reverted, with no live row already matched onto them.
    existing = await db.execute(
        select(Transaction, RecurringTransaction.description, RecurringTransaction.category_id)
        .join(RecurringTransaction, RecurringTransaction.id == Transaction.recurring_id)
        .where(
            Transaction.org_id == org_id,
            Transaction.account_id == account_id,
            Transaction.type.in_(types),
            Transaction.date.between(lo, hi),
            Transaction.is_imported.is_(False),
            Transaction.import_batch_id.is_(None),
            Transaction.linked_transaction_id.is_(None),
            Transaction.recurring_origin_id.is_(None),
            Transaction.bound_bank_tx_id.is_(None),
            Transaction.reconciliation_state.not_in(REVERTED_RECONCILIATION_STATES),
            ~exists().where(and_(
                referrer.org_id == org_id,
                referrer.linked_transaction_id == Transaction.id,
                referrer.reconciliation_state.not_in(REVERTED_RECONCILIATION_STATES),
            )),
        )
    )
    out = [
        Candidate(tx.recurring_id, tx.id, tx.date, tx.amount, desc, cat, tx.type)
        for tx, desc, cat in existing.all()
    ]

    # (b) Frontier occurrences not written yet. A bind spends exactly one
    # occurrence, so only the frontier can be created.
    templates = (await db.execute(
        select(RecurringTransaction).where(
            RecurringTransaction.org_id == org_id,
            RecurringTransaction.account_id == account_id,
            RecurringTransaction.type.in_(("expense", "income")),
            RecurringTransaction.next_due_date.between(max(lo, not_before), hi),
            active_series_filter(),
        )
    )).scalars().all()
    if templates:
        written = set((await db.execute(
            select(Transaction.recurring_id, Transaction.date).where(
                Transaction.org_id == org_id,
                Transaction.recurring_id.in_([r.id for r in templates]),
                Transaction.date.between(lo, hi),
            )
        )).all())
        out += [
            Candidate(r.id, None, r.next_due_date, r.amount, r.description, r.category_id,
                      TransactionType(r.type))
            for r in templates if (r.id, r.next_due_date) not in written
        ]
    return out
