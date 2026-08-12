# TBD-308 — Skipping a transfer leg must revert its contribution

**Status:** ready to build
**Effort:** S (two code changes, three comment corrections, five fences)
**Owns these files:** `backend/app/services/reconciliation_service.py`,
`backend/app/services/transaction_service.py` (`update_transaction` balance arms,
`pair_existing_transactions` docstring), plus one new test module.

## The defect

Two halves of one root cause: **balance bookkeeping is keyed on a *reports*
predicate.**

`_apply_balance_for_transition` (`reconciliation_service.py:634`) derives its
revert/apply action from `is_reportable_transaction`, which ANDs
`linked_transaction_id is None` (`transaction_filters.py:174-178`) and is
therefore **False for every linked row**. Transitioning a genuine,
bidirectionally-linked transfer leg to SKIPPED gives a `False -> False` diff, so
nothing is reverted — while `balance_contribution_filter`'s state clause
(`:148`) drops that row from the reconstruction the instant it reads `skipped`.

The amount stays in `accounts.balance` and leaves the reconstruction. Permanent
drift: `Sum signed(rows passing balance_contribution_filter) != balance -
opening_balance`.

The compounding half: arms **4b** (`transaction_service.py:656-657`) and **4f**
(`:717-718`) are ungated. When the partner's amount is not inside
`accounts.balance`, 4b reverts an amount that was never there and 4f applies the
new one — the partner's account moves by `new - old` on every edit.

### Reachability

Both halves are reachable through the shipped UI.

* **Half 1** — `import_service` puts the `pair_with_existing` /
  `create_transfer_pair` leg into `imported_transaction_ids`, so a
  bidirectionally-linked leg gets an `import_batch_id`. Imported rows land
  `accepted`; the inbox offers **Reopen** then **Skip** (`ACCEPTED ->
  PENDING_REVIEW -> SKIPPED`, both in `ALLOWED_TRANSITIONS`). Two clicks.
* **Half 2** — via **skip-then-pair**, *not* pair-then-skip.
  `find_match_candidates` (`transaction_service.py:1734-1750`) filters
  `linked_transaction_id IS NULL`, `recurring_id IS NULL` and
  `is_manual_adjustment IS FALSE` but carries **no `reconciliation_state`
  term**; `_link_pair` (`:1629-1668`) and `pair_existing_transactions`
  (`:1810`) carry none either. So: skip an unlinked imported row (the revert
  fires correctly), then pair it through ordinary "Mark as transfer". The row is
  now reciprocal + skipped + already-reverted, and SKIPPED is terminal
  (`ALLOWED_TRANSITIONS[SKIPPED] = frozenset()`), so it can never be reconciled
  again.

## What ships

### 1. Predicate swap (root fix)

`_apply_balance_for_transition` uses `contributes_to_cached_balance` at **both**
snapshots.

The partner is resolved **once, org-scoped, in `_reconcile_one`, before any
mutation**, and both snapshots are passed in. Constraints:

* The helper never resolves a partner itself and never receives `None` as
  "unknown" — `contributes_to_cached_balance` **fails open** on an unresolvable
  partner, so passing `None` on the target side of a MATCHED transition returns
  `True`, collapses the diff to `True -> True`, and **silently disables the
  match revert**. That is a worse and far more common regression than the bug
  being fixed.
* On the MATCHED path the post-mutation partner is the row `_apply_match`
  **already loaded** (`reconciliation_service.py:572-577`) and returns — no
  second query that could observe a different snapshot.
* Fail-open polarity is safe here because the state clause is tested *first*
  (`transaction_filters.py:261-262`), so for any target in
  `('skipped','rejected')` the predicate returns False without reaching the
  fail-open branch.
* No status re-check is needed: `:673` already returns early on non-SETTLED,
  satisfying `contributes_to_cached_balance`'s documented caller contract.

**Semantic delta.** For `is_manual_adjustment = False`, `is_reportable_transaction`
and `contributes_to_cached_balance` differ **if and only if the row is
reciprocal**. Unlinked rows and one-way (reconcile-match) rows get identical
answers from both. The swap therefore changes exactly the reciprocal-leg cells
and provably nothing else.

### 2. Gate arms 4b/4f

Gate on `contributes_to_cached_balance(pair_partner, tx)` — that argument order.
The first argument is the row whose balance membership is in question; flipping
it re-asks about `tx`, which `:552` already answered.

**4b and 4f are one gate in two halves.** Gating only one is strictly worse than
gating neither: 4b-only injects a full new amount, 4f-only removes a full old
amount, where neither-gated is wrong only by the edit delta.

**4d stays ungated** — it mirrors the amount, moves no money, and keeps a legacy
pair's amounts coherent.

`pair_partner` is never non-None-but-non-reciprocal at this site (`:551` already
filters through `is_reciprocal_pair`), so the predicate's two link branches are
dead code here and it reduces to the state clause. It is used anyway, as the
statement of *why*, on the precedent already set at `:1016-1027` — and is
labelled **not test-killable on those branches**, so nobody writes a fence
pretending to kill them.

### 3. Three comment corrections

* `transaction_service.py:638-652` currently records the 4b/4f hole as "REAL but
  PRE-EXISTING". Replace with the true statement: 4b/4f are gated because
  reciprocal+reverted is **reachable via skip-then-pair**.
* `pair_existing_transactions` docstring `:1823` claims "No balance changes (both
  rows already exist with correct per-leg balance contributions)". **False** for
  a reverted row. It must also record that pairing a reverted row is *permitted
  on purpose* — SKIPPED/REJECTED are terminal, so refusing it would strand the
  row — and is safe precisely because 4b/4f are gated.
* `reconciliation_service.py:112-138` names `is_reportable_transaction` as the
  source of truth for "does this row contribute to the cached balance". That
  sentence caused this bug. Rewrite, do not append.

## Explicitly NOT in scope (rulings, with reasons)

* **No refusal guard** in `_reconcile_one`. Refusing Skip on a reciprocal leg was
  proposed and withdrawn: unpair-then-skip already produces the identical orphan
  on `main`, correctly, so reverting on Skip makes Skip agree with an existing
  correct path rather than inventing an outcome. Refusal would also replace the
  ticket's stated DoD with an unauthorised 422, remove a capability, and pull in
  a new inbox DTO field and a frontend change under the TBD-289 rule.
* **No state guard on `_link_pair` / `find_match_candidates`.** SKIPPED is
  terminal, so refusing to pair a reverted row means a mis-skip can never be
  paired, never un-skipped, and can only be deleted — the TBD-295 closed loop.
  `_demote_match_orphans` already rejected refusal on that exact ground
  (`:983-988`). Once 4b/4f are gated, reciprocal+reverted is **arithmetically
  safe**, so the guard buys hygiene at the price of a permanent dead end.
* **No repair migration.** A pair-then-skip row (drifted) and a skip-then-pair
  row (already consistent) are **byte-identical across every column**; nothing
  records which happened first. Any blind state-flip repairs the first
  population and corrupts the second by the same amount. `reconcile_account`
  (`:2877-2926`, exposed at `routers/accounts.py:737`) already measures drift
  per account directly; repair, if any, goes through the audited
  `adjust_account_balance` hatch.

## Fences

Every fence asserts the **invariant** via `reconcile_account(...)` —
`(stored, computed, is_consistent)` gated on `balance_contribution_filter()` on
both subqueries — never a hand-computed number.

| id | type | asserts | wrong implementation it kills |
|---|---|---|---|
| **F1** | fence | Move a reciprocal transfer leg into a reverted state through the inbox; `is_consistent` on **both** accounts. **Parametrized over `skipped` AND `rejected`.** | `main`'s `is_reportable_transaction` derivation (`False -> False`, reverts nothing). Also kills `not is_reciprocal_pair(...)` substituted for the predicate, and — via the `rejected` case — an implementation that special-cases the literal `"skipped"` inside this code path instead of deferring to `_RECON_EXCLUDED_STATES`. |
| **F2** | fence | Skip-then-pair built **through the real service functions**, then edit the surviving partner's amount; invariant on both accounts | ungated 4b/4f. Hand-writing `reconciliation_state` would still go RED but would not pin the *route*, and the route is the finding. |
| **F3** | guard | A real, unskipped transfer pair still mirrors the amount and still moves **both** balances | any over-reach that freezes transfer edits. Without it, hard-coding the gate to `False` passes F1 and F2. |
| **F4** | guard | Pairing a SKIPPED row still **succeeds**, and `find_match_candidates` still **offers** it | the deliberate absence of the `_link_pair` state guard, and — via the candidate assertion — a `reconciliation_state` filter added to `find_match_candidates`, which would close the skip-then-pair route from the UI while every other fence stayed green. A future hygiene PR adding either goes RED and must argue with the dead end. |
| **F5** | guard | A row carrying a **stale one-way** link after `ACCEPTED -> PENDING_REVIEW` still behaves as today; its skip moves no balance | passing `None` as the partner, and any blanket `linked_transaction_id is not None` treatment. |

Additional required kill, covered by F1 + F5 together:

* **Asymmetric swap** — switching only the target snapshot (or only the source)
  to the new predicate. Source-only produces a spurious revert when a transfer
  leg is merely ACCEPTED; target-only makes the fix vacuous.
* **Partner at target only, `None` at source** — double-revert on a reopened
  matched row.

`test_reconciliation_service.py::test_match_reverts_account_balance_for_this_row`
already fences the "match revert stops firing" case and must stay green.

### Added during review (the roster as first written missed these)

* **F6** — a reciprocal leg transitioned to ACCEPTED moves no money. Kills the
  **source-only** asymmetric swap. F1 cannot: F1's transition ends in a
  reverted state where both predicates agree on `False`, so the source-only
  mutant gets the right answer there by luck. A boundary pinned from one side
  is not pinned.
* **F1 parametrized over `rejected`** — an implementation special-casing
  `"skipped"` *inside* `_apply_balance_for_transition` passed all six original
  fences. The parity fences on the shared predicate cannot see it either,
  because it never touches the shared predicate.
* **F4's `find_match_candidates` assertion** — nothing previously pinned the
  reachability claim the whole ticket rests on.

## Verification gate

Each fence runs three legs: **RED before the fix**, **green after**, then
**RED again against the named wrong implementation re-injected**, then restore
and **confirm green**. Record the evidence. Mutation testing is not vacuity
detection and does not substitute.

Full backend suite must pass. Redirect pytest to a file and `echo $?` **before**
inspecting — a piped `pytest | tail` reports the pipe's exit code.

## Follow-ups to file (backlog, not the running sprint)

1. **Product question:** should the reconcile inbox permit half-skipping a
   transfer pair, or require an explicit unpair first? The outcome is already
   reachable today via unpair-then-skip, so this is purely about which route the
   product wants.
2. **Display-only:** `_transfer_collapse_clause` branch 4
   (`transaction_service.py:2584`) renders the lower-id leg, so a
   reciprocal+skipped row with `P.id < Q.id` becomes the pair's visible
   representative while the live leg is collapsed. No sum, filter or balance is
   affected.
3. **Asymmetry of record:** `main` refuses MATCHED on a reciprocal leg
   (`_apply_match` Guard 2) while permitting SKIPPED and REJECTED — it blocks the
   harmless transition and allows the two that corrupt the balance.
