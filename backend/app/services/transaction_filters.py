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
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import aliased

from app.models.transaction import Transaction


# L3.2 Wave 2B (PR #247 P1): states whose rows are excluded from
# reportable aggregates AND whose balance has been reverted at the
# state transition. Keep in sync with ``reconciliation_service``.
_RECON_EXCLUDED_STATES: tuple[str, ...] = ("skipped", "rejected")

# Self-join alias for ``balance_contribution_filter()``'s reciprocity
# check. Defined once at module level so the correlated EXISTS subquery
# below can reference it.
_bcf_partner = aliased(Transaction)


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
    ledger reconstruction must replay to get B_k right. By construction,
    ``sum(signed(rows passing this filter)) == account.balance -
    account.opening_balance`` for settled rows.

    ARCHITECT CORRECTION (Slice 3 fix): a flat-column predicate (e.g.
    ``import_batch_id IS NULL OR linked_transaction_id IS NULL``) is NOT
    sufficient. A genuine transfer leg that happens to be import-paired
    and a reconcile-MATCHED duplicate are byte-identical across every
    flat column -- both can carry ``import_batch_id`` set,
    ``linked_transaction_id`` set, and ``reconciliation_state='accepted'``.
    Filtering on those columns alone over-excludes real transfer legs.

    The actual discriminator is the *direction* of the partner link:

    - ``_link_pair`` (real transfers, including import-time pairing of
      two legs of one transfer) sets ``linked_transaction_id``
      BIDIRECTIONALLY -- each leg points at the other, so the partner's
      own ``linked_transaction_id`` points back. These rows contribute
      to balance and must be KEPT.
    - ``_apply_match`` (reconciliation match) sets ``linked_transaction_id``
      ONE-WAY onto the imported/duplicate row only (see
      ``reconciliation_service.py``) -- the canonical row it matched
      against is NOT linked back. Matching flips the row non-reportable
      and reverts its balance contribution
      (``_apply_balance_for_transition``), so these rows must be DROPPED
      to avoid double-counting the canonical charge they duplicate.

    So: keep a linked row only if its partner links back to it
    (reciprocal); an unlinked row always contributes. SKIPPED / REJECTED
    rows are still reverted-and-excluded via the state clause.

    FROZEN (TBD-280 ruling). This clause is deliberately NOT reformulated:

    * It has no org clause and no not-self clause. Both would be dead
      code: the only three writers of ``linked_transaction_id``
      (``_link_pair``, ``_apply_match``, ``unpair_transactions``) can
      produce neither a cross-org nor a self link.
    * A self-link is KEPT here, on purpose. The correlated EXISTS against
      ``_bcf_partner`` matches a row against itself, so a self-linked row
      passes. That is the intended polarity: this filter's failure
      direction must be KEEP-on-uncertainty, because dropping a row that
      really is in ``accounts.balance`` is the CC carried-balance bug it
      exists to prevent.
    * Note the OPPOSITE polarity of the Python predicate
      ``is_reciprocal_pair`` and of ``transaction_service.
      _transfer_collapse_clause``: those answer "are these two rows ONE
      transfer pair?" and must fail CLOSED, so they exclude self-links.
      This filter answers "is this row's amount inside the cached
      balance?" and must fail OPEN. Same column, two questions, two
      polarities. Do not "harmonise" them.
    * Writing the reciprocity test in the obvious negative form
      (``partner.linked_transaction_id != Transaction.id``) is NULL-unsafe:
      when the partner's link is NULL -- the common reconcile-match case --
      the comparison yields NULL, the EXISTS collapses, and every matched
      row silently re-enters the balance.

    ``contributes_to_cached_balance()`` below is the Python sibling; keep
    the two in step (see the parity fence in
    ``tests/services/test_link_reciprocity_predicates.py``).
    """
    return and_(
        Transaction.reconciliation_state.notin_(_RECON_EXCLUDED_STATES),
        or_(
            Transaction.linked_transaction_id.is_(None),
            exists().where(
                and_(
                    _bcf_partner.id == Transaction.linked_transaction_id,
                    _bcf_partner.linked_transaction_id == Transaction.id,
                )
            ),
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


# ── Link reciprocity (TBD-280 / 281 / 282 / 293) ─────────────────────────────
#
# THE RULE: a link is a transfer link if, and only if, the partner links back.
#
# ``linked_transaction_id`` has exactly three writers:
#   * ``transaction_service._link_pair``          -- BIDIRECTIONAL (real transfer)
#   * ``reconciliation_service._apply_match``     -- ONE-WAY (reconcile match)
#   * ``transaction_service.unpair_transactions`` -- clears both sides
# Every predicate that means "transfer pair" must therefore test mutuality,
# never non-nullness.


def is_reciprocal_pair(tx: Transaction, partner: Transaction | None) -> bool:
    """True iff (tx, partner) are the two legs of ONE transfer pair.

    THE RULE: a link is a transfer link iff the partner links back.

    Pure. No I/O, no lazy attribute access -- the caller passes both
    instances; every caller already holds them under FOR UPDATE or from
    an eager load.

    Self-links are NOT a pair: no writer creates them, so a self-linked
    row is corrupt data containing exactly one row, and treating it as a
    pair makes every two-row path double-count it.

    Fails CLOSED: an unproven link is never treated as a pair.

    ``tx.linked_transaction_id is not None`` is LOAD-BEARING, not
    belt-and-braces: without it a transient (unflushed) partner makes
    ``None == None`` true and the predicate becomes argument-order
    sensitive. No call site passes an unflushed row -- keep it anyway.
    """
    return (
        partner is not None
        and tx.linked_transaction_id is not None
        and partner.id == tx.linked_transaction_id
        and partner.id != tx.id
        and partner.org_id == tx.org_id
        and partner.linked_transaction_id == tx.id
    )


def contributes_to_cached_balance(
    tx: Transaction, partner: Transaction | None
) -> bool:
    """Python sibling of ``balance_contribution_filter()`` -- the LINK and
    RECONCILIATION-STATE half of the question only.

    ⚠ NOT a complete answer to "is this row's amount inside
    accounts.balance". It has NO status term, because the SQL has none
    either. Pending amounts are never in the cached balance, so every
    caller MUST conjoin ``tx.status == TransactionStatus.SETTLED``. Every
    SQL caller already does (see networth.py, cc_statement_service).

    Transcribed branch-for-branch from the SQL. Do NOT rewrite as
    ``not is_reciprocal_pair(...)``: that inverts the RECIPROCAL case
    (a real transfer leg would report False, and delete_transaction would
    skip the revert on BOTH legs of every transfer, drifting each account
    UP by its leg amount). It happens to give the right answer for a
    self-link, which is why the obvious fence for it is vacuous.

    Fails OPEN whenever the partner cannot be resolved: an unprovable
    link keeps its contribution, because nothing ever reverted it.

    DIVERGENCE from the SQL: the predicate disagrees with
    ``balance_contribution_filter()`` whenever ``partner`` is
    unresolvable, for ANY reason -- a cross-org one-way link and a
    dangling link are two known members of an open-ended class. Both are
    unreachable in production (no writer produces a cross-org link; the
    MySQL FK ``transactions_ibfk_4`` is ``ON DELETE SET NULL``), and the
    parity fence pins the known cell with ``xfail(strict=True)``.
    """
    if tx.reconciliation_state in _RECON_EXCLUDED_STATES:
        return False
    if tx.linked_transaction_id is None:
        return True
    if partner is None or partner.id != tx.linked_transaction_id:
        return True                       # see DIVERGENCE
    return partner.linked_transaction_id == tx.id
