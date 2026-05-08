"""Filters and predicates expressing transfer-leg exclusion in aggregates.

Lives in its own module to avoid a circular import with category_rules_service,
which already imports from transaction_service.

Excluded from reportable aggregates:

- Transfer legs (``linked_transaction_id IS NOT NULL``): not income/expense.
- Manual balance adjustments (``is_manual_adjustment = True``): controlled
  escape hatch from the "balance from transactions" invariant. Counted by
  ``reconcile_account`` (so stored balance == sum of settled rows holds)
  but excluded from budget/forecast totals because they reflect the act
  of correcting a balance, not actual income or expense activity.

Future-proofed to grow additional reasons (voided, refunded) without
renaming call sites.
"""
from sqlalchemy import and_, func

from app.models.transaction import Transaction


def reportable_transaction_filter():
    """SQL clause: rows that count toward income/expense aggregates."""
    return and_(
        Transaction.linked_transaction_id.is_(None),
        Transaction.is_manual_adjustment.is_(False),
    )


def effective_period_date_expr():
    """Period-bucketing date for billing-window queries.

    Settled rows count against the period in which they settled.
    Pending rows with a settled_date estimate count against that estimate.
    Pending rows without a settled_date fall back to purchase date, the
    only signal we have for hand-keyed pending entries.
    """
    return func.coalesce(Transaction.settled_date, Transaction.date)


def is_reportable_transaction(tx: Transaction) -> bool:
    """Python predicate version of reportable_transaction_filter()."""
    return tx.linked_transaction_id is None and not tx.is_manual_adjustment


def is_transfer_leg(tx: Transaction) -> bool:
    """Direct link-detection predicate for UI/feature code that needs to
    distinguish transfer legs from plain transactions without the
    'reportable' framing.
    """
    return tx.linked_transaction_id is not None
