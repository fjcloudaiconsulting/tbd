"""Filters and predicates expressing transfer-leg exclusion in aggregates.

Lives in its own module to avoid a circular import with category_rules_service,
which already imports from transaction_service.

Today these all delegate to ``linked_transaction_id IS NULL``. Future-proofed
to grow additional reasons (voided, refunded) without renaming call sites.
"""
from app.models.transaction import Transaction


def reportable_transaction_filter():
    """SQL clause: rows that count toward income/expense aggregates."""
    return Transaction.linked_transaction_id.is_(None)


def is_reportable_transaction(tx: Transaction) -> bool:
    """Python predicate version of reportable_transaction_filter()."""
    return tx.linked_transaction_id is None


def is_transfer_leg(tx: Transaction) -> bool:
    """Direct link-detection predicate for UI/feature code that needs to
    distinguish transfer legs from plain transactions without the
    'reportable' framing.
    """
    return tx.linked_transaction_id is not None
