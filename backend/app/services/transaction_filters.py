"""Shared SQL predicates and expressions over ``Transaction``.

⚠ The module used to be described as "transfer-leg EXCLUSION". Since TBD-471 it
also holds a transfer-leg **inclusion** clause (``reciprocal_transfer_filter``),
so read that family as "predicates ABOUT transfer legs" -- some exclude, one
selects, and two of them do so with deliberately OPPOSITE failure polarities.

⚠ Since TBD-553 the module is not only about transfer legs and balance
contribution. ``signed_amount_expr`` is an EXPRESSION, not a filter, and is
about sign direction rather than row membership. It lives here because it is
shared across a router, two services and a report source, and this module is
already the common low-level home each of them imports.

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
from sqlalchemy import and_, case, exists, func, or_, select, true
from sqlalchemy.orm import aliased

from app.models.account import Account
from app.models.transaction import Transaction, TransactionType


# L3.2 Wave 2B (PR #247 P1): states whose rows are excluded from
# reportable aggregates AND whose balance has been reverted at the
# state transition. Keep in sync with ``reconciliation_service``.
_RECON_EXCLUDED_STATES: tuple[str, ...] = ("skipped", "rejected")

# Public alias of the SAME tuple, for callers outside aggregate filtering
# that need to ask "has this row's balance contribution already been
# reverted, and is it out of every reportable aggregate?" -- e.g.
# ``transaction_service.promote_to_recurring``'s guard and
# ``delete_transaction``'s orphan demotion (TBD-292 / TBD-294). One roster,
# two names; do NOT fork it into a second literal.
REVERTED_RECONCILIATION_STATES = _RECON_EXCLUDED_STATES

# Self-join alias for ``balance_contribution_filter()``'s reciprocity
# check. Defined once at module level so the correlated EXISTS subquery
# below can reference it.
_bcf_partner = aliased(Transaction)
# TBD-471. A SEPARATE alias from ``_bcf_partner``: a ``transfer=true`` query
# carries both clauses, so sharing one alias would collide in the rendered SQL.
_rtf_partner = aliased(Transaction)


def signed_amount_expr():
    """+``amount`` for INCOME, -``amount`` for EXPENSE (TBD-553).

    ``Transaction.amount`` is an unsigned magnitude; direction lives in
    ``type``. This CASE existed verbatim three times before this function --
    ``routers/settings.py`` (inside ``func.sum``), ``cc_statement_service.py``
    (bare, projected as a column), ``reports/sources/networth.py`` (inside
    ``func.sum``) -- with identical polarity at every site, so one helper
    returning the bare expression fits all three unchanged. Unlike the
    ``reciprocal_transfer_filter`` / ``balance_contribution_filter`` pair,
    there is no split here: every existing site hands a non-INCOME row
    ``-amount`` via ``else_``, including transfer legs (which are typed
    INCOME/EXPENSE by direction; ``TransactionType.TRANSFER`` is unused on
    legs), so this function pins that ``else_`` behaviour and nothing more.
    """
    return case(
        (Transaction.type == TransactionType.INCOME, Transaction.amount),
        else_=-Transaction.amount,
    )


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

    The always-on half of ``reportable_transaction_filter``: it drops every
    row whose amount was reverted from ``accounts.balance`` -- skipped/rejected
    rows AND reconcile-matched duplicates -- while keeping reciprocal transfer
    legs and manual balance adjustments.

    Used by the Reports "Include transfers & adjustments" opt-in and by the
    pending aggregates in ``account_balance_forecast_service`` and
    ``recurring_service._settle_due_auto``: a reverted row must stay out,
    otherwise its amount double-counts against a balance that no longer
    contains it.

    ⚠ TBD-470: this is ``balance_contribution_filter()``, NOT a state clause
    over ``_RECON_EXCLUDED_STATES``. A matched duplicate is reverted by its
    ONE-WAY LINK, and the link (and the revert) survives MATCHED -> ACCEPTED
    -> PENDING_REVIEW, so neither the old state-only clause nor adding
    ``'matched'`` to that roster drops it.
    """
    return balance_contribution_filter()


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


def reciprocal_transfer_filter():
    """SQL clause: rows that are ONE LEG of a REAL transfer pair (TBD-471).

    THE RULE, from ``is_reciprocal_pair`` above: a link is a transfer link iff
    the partner links back. This is its SQL twin, term for term, and it FAILS
    CLOSED -- an unproven link is never treated as a transfer.

    ⚠⚠ OPPOSITE POLARITY to ``balance_contribution_filter()``, which fails OPEN
    and is FROZEN under TBD-280. That one asks "is this row's amount inside
    ``accounts.balance``?" and must KEEP on uncertainty, because dropping a row
    there loses money from a reconstruction. This one asks "are these two rows
    ONE transfer pair?" and must DROP on uncertainty, because keeping a row here
    calls a reconcile match a transfer. Same column, two questions, two
    polarities. **Do not harmonise them, and do not factor out the shared
    EXISTS** -- the six lines they have in common are the least valuable part;
    the polarity is the whole point.

    ⚠ ``_rtf_partner.id != Transaction.id`` IS LOAD-BEARING and is the term that
    diverges from the frozen sibling. A self-linked row satisfies the other
    conjuncts against ITSELF, so without it this clause calls corrupt data a
    transfer. ``balance_contribution_filter`` deliberately KEEPS self-links, so
    copying its EXISTS verbatim ships exactly that mutant, green.

    ⚠ ``org_id`` is likewise a deliberate addition, matching
    ``is_reciprocal_pair``'s ``partner.org_id == tx.org_id``. Under a
    fail-closed polarity a cross-org link is not a pair.

    ⚠ WHICH CELLS ARE VISIBLE COMPOSED, stated precisely because the first
    draft of this docstring got it wrong. Conjoined with
    ``balance_contribution_filter()`` -- which is what happens on every path
    this clause runs on today -- a bare ``linked_transaction_id IS NOT NULL``
    mutant reduces to ``link NOT NULL AND EXISTS_bcf``. That is NOT identical to
    this clause, because ``balance_contribution_filter``'s EXISTS carries only
    TWO conjuncts (partner-by-id, links-back) and this one carries four:
      * SELF-LINK -- diverges, and is VISIBLE composed, because
        ``balance_contribution_filter`` deliberately KEEPS self-links;
      * CROSS-ORG -- diverges in principle, unfenced in practice (no fixture
        builds one, and no writer produces one);
      * ONE-WAY reconcile match -- does NOT diverge composed, because
        ``balance_contribution_filter`` has already dropped it.
    So the one-way cell is the one that needs an UNCOMPOSED fence, and that is
    why F1 in ``tests/services/test_reports_transfer_axis.py`` compiles this
    clause alone. Do not read that as "the end-to-end fences prove nothing" --
    they do kill the mutant, via the self-link cell.

    ⚠ HONEST SCOPE. On data the product can actually produce, this clause
    selects exactly what ``linked_transaction_id IS NOT NULL`` selects: no
    writer creates a self-link or a cross-org link, and the one-way case is
    already excluded upstream. It is written this way for the NEXT caller, who
    will not have ``balance_contribution_filter()`` conjoined.

    No NULL guard is needed on ``linked_transaction_id``: when it is NULL the
    first conjunct is NULL, the EXISTS matches nothing, and the row drops.
    Fail-closed by construction rather than by a defensive clause. Both engines
    short-circuit the correlated subquery for unlinked rows.
    """
    return exists().where(
        and_(
            _rtf_partner.id == Transaction.linked_transaction_id,
            _rtf_partner.org_id == Transaction.org_id,
            _rtf_partner.linked_transaction_id == Transaction.id,
            _rtf_partner.id != Transaction.id,
        )
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


def org_currency_filter(
    org_id: int,
    currency_scope: dict | None,
    account_id_col=Transaction.account_id,
):
    """SQL clause: rows on accounts denominated in the org's primary currency.

    TBD-325 PR 2. ``transactions`` has no currency column -- currency lives
    only on ``accounts.currency`` -- so every period aggregate currently sums
    EUR and USD into one unlabelled number.

    Takes the WHOLE ``currency_scope`` dict from
    ``currency_service.resolve_currency_scope``, not a currency string, because
    the short-circuit decision must be made in ONE place. Fourteen call sites
    each picking which key to pass is how half of them end up scoping the 99%
    path that has nothing to exclude.

    ⚠ The short-circuit is on ``excluded_account_count == 0``, NOT on
    ``currency is None``. Keying it on NULL-ness only short-circuits the
    zero-account and legacy-multi-currency orgs; an ORDINARY single-currency
    org has a non-NULL ``primary_currency``, so the correlated subquery would
    be emitted on every aggregate in the product to select rows that are all
    selected anyway. Measured on MySQL 8.4: keying on NULL-ness gave
    ``compute_forecast`` 13 statements and 2 ``accounts`` references against a
    baseline of 11 and 0; keying on the count gives 12 and 1, and the one
    remaining reference is the scope resolution itself, not an aggregate.

    Shape, not just semantics: a CORRELATED SUBQUERY, so the clause is
    join-free and splats into an existing ``.where()`` exactly like every other
    helper in this module. The precedent is ``balance_contribution_filter()``
    above. A ``.join(Account, ...)`` would force all 14 call sites to be
    restructured and would change the row multiplicity of any statement that
    already joins.

    ``Transaction.account_id`` is NOT NULL (``models/transaction.py:97-99``),
    so there is no NULL-safety trap in the ``IN``.

    ⚠ ``currency_scope=None`` returns ``true()`` rather than raising, and that
    is the whole point: call sites write ``org_currency_filter(org_id, scope)``
    and nothing else. An earlier cut made the parameter non-optional, which
    forced ``... if scope else true()`` at every call site -- handing the
    short-circuit decision back to the fourteen places this function exists to
    take it away from.

    ``account_id_col`` covers ``RecurringTransaction.account_id`` (also NOT
    NULL, ``models/recurring.py:36``), so the recurring projection scopes
    through this same function instead of a second copy of the subquery.
    """
    if not currency_scope or not currency_scope["excluded_account_count"]:
        return true()
    return account_id_col.in_(
        select(Account.id).where(
            Account.org_id == org_id,
            Account.currency == currency_scope["currency"],
        )
    )
