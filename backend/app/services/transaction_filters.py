"""Filters and predicates expressing transfer-leg exclusion in aggregates.

Lives in its own module to avoid a circular import with category_rules_service,
which already imports from transaction_service.

Excluded from reportable aggregates:

- Transfer legs (``linked_transaction_id IS NOT NULL``): not income/expense.
  This also covers MATCHED reconciliation rows -- ``_apply_match`` writes
  ``linked_transaction_id`` on the inbox row so the matched-against row
  stays canonical and the imported duplicate disappears from reports.
- Manual balance adjustments (``is_manual_adjustment = True``): controlled
  escape hatch from the "balance from transactions" invariant. Counted by
  ``reconcile_account`` (so stored balance == sum of settled rows holds)
  but excluded from budget/forecast totals because they reflect the act
  of correcting a balance, not actual income or expense activity.
- Reconciliation SKIPPED / REJECTED rows (L3.2 Wave 2B PR #247 P1 fix):
  the row stays in the DB for audit + recoverability, but its amount
  was reverted from ``accounts.balance`` and it must not appear in
  reportable aggregates. ``_RECON_EXCLUDED_STATES`` pins the list so
  future state-machine additions stay coherent.

Future-proofed to grow additional reasons (voided, refunded) without
renaming call sites.
"""
from sqlalchemy import and_, func, or_

from app.models.transaction import Transaction


# L3.2 Wave 2B (PR #247 P1): states whose rows are excluded from
# reportable aggregates AND whose balance has been reverted at the
# state transition. Keep in sync with ``reconciliation_service``.
_RECON_EXCLUDED_STATES: tuple[str, ...] = ("skipped", "rejected")


def reportable_transaction_filter():
    """SQL clause: rows that count toward income/expense aggregates.

    L3.2 Wave 2B (PR #247 P1): SKIPPED and REJECTED reconciliation
    rows are excluded here in addition to transfer legs and manual
    balance adjustments. Their balance was reverted at the state
    transition (see ``reconciliation_service._apply_balance_for_transition``),
    so the "stored balance == sum of reportable rows" invariant holds
    across the new states.
    """
    return and_(
        Transaction.linked_transaction_id.is_(None),
        Transaction.is_manual_adjustment.is_(False),
        Transaction.reconciliation_state.notin_(_RECON_EXCLUDED_STATES),
    )


def non_reverted_transaction_filter():
    """SQL clause: rows whose amount still counts against the account balance.

    The always-on half of ``reportable_transaction_filter``: it excludes ONLY
    the reverted reconciliation rows (skipped/rejected), whose amount was
    reverted from ``accounts.balance`` at the state transition. Transfer legs
    and manual balance adjustments are NOT excluded here.

    Used by the Reports "Include transfers & adjustments" opt-in: when a report
    widget asks to include transfers + adjustments, it must still drop the
    reverted rows, otherwise their amount double-counts against a balance that
    no longer contains them.
    """
    return Transaction.reconciliation_state.notin_(_RECON_EXCLUDED_STATES)


def balance_contribution_filter():
    """SQL clause: rows that make up the incrementally-maintained
    ``accounts.balance`` value -- i.e. the set the Slice-3 CC forecast
    ledger reconstruction must replay to get B_k right.

    ``non_reverted_transaction_filter()`` alone is NOT this set: it only
    drops SKIPPED / REJECTED rows. A reconcile-MATCHED imported duplicate
    (``status=settled``, ``reconciliation_state`` accepted/matched,
    ``import_batch_id`` set, ``linked_transaction_id`` pointing at the
    canonical row it matched) is *not* skipped/rejected, so
    ``non_reverted_transaction_filter()`` still includes it -- but its
    balance contribution WAS reverted at match time, because matching
    flips it non-reportable and ``_apply_balance_for_transition`` fires
    ``revert_balance`` on that True -> False reportability diff (see
    ``reconciliation_service._apply_balance_for_transition``). Including
    such a row here would double-count the canonical charge it duplicates.

    Among imported rows (``import_batch_id IS NOT NULL``), non-reportable
    is equivalent to "linked" (``linked_transaction_id IS NOT NULL``): the
    only way an imported row goes non-reportable post-import is a
    reconciliation match, which sets the link. So requiring
    ``import_batch_id IS NULL OR linked_transaction_id IS NULL`` drops
    exactly the reverted matched duplicates while keeping:
      - transfer legs / manual adjustments / manual entries
        (``import_batch_id IS NULL`` -- always kept, they DO contribute
        to balance),
      - imported-but-unlinked reportable rows (``linked_transaction_id
        IS NULL`` -- kept, never reverted).
    """
    return and_(
        non_reverted_transaction_filter(),
        or_(
            Transaction.import_batch_id.is_(None),
            Transaction.linked_transaction_id.is_(None),
        ),
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
    return (
        tx.linked_transaction_id is None
        and not tx.is_manual_adjustment
        and tx.reconciliation_state not in _RECON_EXCLUDED_STATES
    )


def is_transfer_leg(tx: Transaction) -> bool:
    """Direct link-detection predicate for UI/feature code that needs to
    distinguish transfer legs from plain transactions without the
    'reportable' framing.
    """
    return tx.linked_transaction_id is not None
