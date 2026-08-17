# TBD-310 — A reopened matched row must be editable from the reconcile inbox

**Status:** in build
**Ticket:** TBD-310 (Bug, effort-s, group-r, epic TBD-279)
**Sibling:** TBD-292 (the same defect class one module over, already shipped)

---

## 1. The defect, verified

`reconciliation_service._apply_edits` refuses **any** row with a non-null
`linked_transaction_id`:

```python
# reconciliation_service.py:517-521
if tx.linked_transaction_id is not None:
    raise ValidationError(
        "Cannot edit a transfer leg from the reconciliation inbox; "
        "edit via the transactions page."
    )
```

`linked_transaction_id` has three writers and non-nullness does **not** mean
"transfer leg". `_apply_match:662` writes it **one-way** — a reconcile match.
`ACCEPTED -> PENDING_REVIEW` is a legal reopen (`ALLOWED_TRANSITIONS:94-96`) and
nothing clears the link on reopen — `_apply_match` guard 2 says so outright at
`:637-640` and depends on it. `PENDING_REVIEW -> EDITED` is allowed (`:68-76`).

So a `pending_review` row carrying a live one-way link is a **documented
supported state**, and this guard refuses to edit it.

### It is fully reachable through the shipped UI

`ReconcileClient.tsx:76-92` mirrors the server's table: `accepted: ["pending_review"]`
labelled **"Reopen"**, and `pending_review` offers **"Edit"**. There is no
client-side gate on `linked_transaction_id`. Four clicks:

> Match -> Accept -> Reopen -> Edit -> *"Cannot edit a transfer leg…"* on a row
> that is not a transfer leg.

---

## 2. The balance trap — why narrowing the guard alone is WORSE than the bug

For a SETTLED row that is one-way matched, sitting in `PENDING_REVIEW`:

`contributes_to_cached_balance(tx, partner)` walks
`transaction_filters.py:261` (state not excluded) -> `:263` (link set) ->
`:265` (partner resolves) -> `:267` `partner.linked_transaction_id == tx.id`
=> **False**. The row's amount is **not** inside `accounts.balance` — it was
reverted when the row entered MATCHED (fenced today at
`test_reconciliation_service.py:882`).

But `_apply_edits`' amount arm runs unconditionally for any settled row:

```
start:                      B0            (holds NOTHING of this row)
revert_balance(acct, 100)   B0 + 100
apply_balance (acct, 175)   B0 + 100 - 175  =  B0 - 75
```

`_apply_balance_for_transition` then sees `False -> False` and correctly
no-ops. **`_apply_edits` is the sole source of drift.** Correct movement is
**zero**; a naive fix drifts by the full edit delta, on every amount edit, in
whichever direction the user edits, silently. That is a fresh instance of the
TBD-292 / TBD-308 class.

Contrast — an **unlinked** settled row, identical code path:

```
start:                      B0 - 100
revert_balance(acct, 100)   B0
apply_balance (acct, 175)   B0 - 175       CORRECT, drift 0
```

The arm is gated on `status`, which answers *"was this ever applied"*. The
question it must ask is *"is it in there **now**"*. Same wrong question
`is_reportable_transaction` asked in TBD-308.

### The membership diff cannot flip on any edge reaching `_apply_edits`

Both architects proved this independently, and it is load-bearing for the fix:

- `EDITED`'s only sources are `PENDING_REVIEW` and `UNMATCHED` (`:68-85`).
  None of the three states is in `_RECON_EXCLUDED_STATES = ("skipped","rejected")`
  (`transaction_filters.py:35`).
- `ReconciliationEdits` (`schemas/import_reconciliation.py:130-149`) is
  `extra="forbid"` with exactly four fields — `description`, `amount`, `date`,
  `category_id`. It cannot write `linked_transaction_id`, `reconciliation_state`,
  `status`, `type` or `account_id`. `_apply_edits` writes none of them either.
- The partner instance is not mutated.

Therefore `source_in_cached_balance == target_in_cached_balance` on **every**
edge reaching `_apply_edits`, and `_apply_balance_for_transition` is a
structural no-op on the entire edit path.

---

## 3. The guard has a real job — do NOT delete it

The ticket frames the guard as protecting a state that does not exist. **It
does exist.** `import_service` calls `_link_pair` (BIDIRECTIONAL) on both the
`pair_with_existing` and `create_transfer_pair` branches, and both actions
append to `imported_transaction_ids`, which `create_import_batch` enrols into
the batch. So **genuine reciprocal transfer legs land in an import batch** and
reach `_apply_edits` on reopen.

Editing one from the inbox would move only `tx`'s account and never the
partner's, breaking the pair-amount invariant `update_transaction` enforces at
`transaction_service.py:762-765`. The guard must be **narrowed, never removed**.

---

## 4. The fix

### Design ruling — settled by measurement, not argument

Two architects deadlocked on where the balance gate belongs:

- **Option A** — thread the values `_reconcile_one` already computes (the raw
  `source_partner` and `source_in_cached_balance`) into `_apply_edits`; gate
  the arm there. `_apply_balance_for_transition` untouched.
- **Option B′** — delete the balance block from `_apply_edits`; capture
  `source_amount`; replace the helper's diff with two independent arms.

They swapped positions once, then each re-defended on verified evidence. A
build-it reviewer implemented **both** and measured:

| | 7 named suites | full backend suite | probe (reopened match, 100 -> 175) |
|---|---|---|---|
| baseline (`main`) | 122 passed, exit 0 | — | **refused** (the bug) |
| **Option A** | 122 passed, exit 0 | **4144 passed, exit 0** | **delta 0.00** ✅ |
| **Option B′** | 122 passed, exit 0 | **4 FAILED, exit 1** | delta 0.00 ✅ |

**Ruling: Option A.** Grounds:

1. **B′ breaks a file neither architect named.**
   `tests/services/test_reconcile_opening_balance.py` calls
   `_apply_balance_for_transition` **directly, as a production-truth oracle**
   ("so the fixture cannot disagree with the production revert it is supposed
   to model"). Both architects reasoned about that helper as private to its
   module. It is not — B′'s blast radius leaves the ticket's file. The break is
   arity-only and repairable, but the premise it falsifies is not.
2. **B′ buys nothing measurable.** Behaviourally identical to A across 4144
   tests plus the probe. No measured case exists where A is wrong and B′ right.
3. **B′ emits a spurious forensic event.** Two independent arms create a third
   cell with no name (neither arm fires); the logger sits after them
   unconditionally, so the F->F-with-amount-change cell logs
   `direction=noop … amount=175.00` and takes a real `SELECT … FOR UPDATE` for
   zero balance movement. A early-returns and logs nothing.

**Not dismissed:** B′'s shape (one owner for all cached-balance movement) is
arguably the better long-term architecture, and Architect B produced a verified
finding in its favour — the helper's `else: apply_balance` branch at `:763-765`
is **dead and unreachable** (F->T cannot occur: a `False` source needs either a
terminal state or a one-way link, and no transition ever clears or symmetrises
a link), and `test_reconciliation_service.py:1074` admits in its own docstring
that it does not cover it. That consolidation deserves its own ticket with its
own fences. It is not folded here.

### ⚠⚠ The existing suite cannot see this bug

The build-it reviewer also measured the **half-fix** — narrow the guard to
`is_reciprocal_pair`, leave the balance arms ungated:

```
PROBE: balance_before=999.00  balance_after=924.00  delta=-75.00
7 named suites: 122 passed, 1 xfailed, EXIT=0
```

**A silent ledger break, and the suite is green.** Any TBD-310 PR that does not
ship the probe-shaped fence is vacuous by construction. And because
`SKIPPED`/`REJECTED` are terminal and `MATCHED` goes only to `ACCEPTED`, **the
reopened-match path is the only way to reach `_apply_edits` with
`source_in_cached_balance == False`** — that fence has exactly one live cell and
nothing else in the repo touches it.

### Mandatory comment (Option A's one obligation)

Option A's correctness rests on a fact that is true but stated nowhere, and
which **no test can pin** (since `source_in ≡ target_in` on every reachable
edge, no fixture can distinguish source-gating from target-gating). Record it
imperatively at the helper's revert line, in the house style used four times
across these two files:

> `revert_balance` takes the **post-mutation** `tx.amount` deliberately.
> `_validate_payload_shape` forbids `edits` on every target that can flip
> membership, so this line and `_apply_edits`' amount arm are mutually
> exclusive and provably equal today. If a future edge ever carries edits **and**
> flips membership, this must be hardened to a pre-mutation snapshot **and**
> `_apply_edits`' balance arm gated off, in the same change. Do not do one
> without the other.

---

**Editability rule.** Refuse iff `is_reciprocal_pair(tx, source_partner)` —
fails CLOSED, the correct polarity for a refusal, so an unproven link never
blocks an edit (which is literally the bug).

**Money rule.** Gate the balance movement on the caller's **pre-mutation**
`source_in_cached_balance`, computed at `_reconcile_one:855` from the **RAW**
link target. The revert and the apply are **one gate in two halves** — gating
only the revert drifts by the full new amount, gating only the apply drifts by
the full old amount, and either half alone is worse than gating neither
(`transaction_service.py:649` says this in those words, because it already
happened once on the transactions page).

### ⚠ THE TRAP — the single highest-probability wrong implementation

```python
partner = link_target if is_reciprocal_pair(tx, link_target) else None
if contributes_to_cached_balance(tx, partner):   # WRONG
```

`contributes_to_cached_balance(tx, None)` returns **True** — it fails OPEN by
design (`transaction_filters.py:250-251, 265-266`). For every matched row
`partner` is `None`, so the gate is **vacuously true**, the 422 disappears, the
balance drifts instead, and every status-code test stays green. Documented
twice already: `transaction_service.py:541` ("THE TRAP", capitals) and
`test_matched_row_actions.py:37-46`.

Both questions take the **RAW** link target. The inbox needs exactly **two**
names — `tx` and the raw `source_partner` — not `update_transaction`'s three
(it needs `pair_partner` only because it *mirrors* to the partner; the inbox
*refuses* instead).

### ⚠ Do NOT clear `linked_transaction_id` on edit

Tempting ("an edit supersedes the match"). No sanctioned path can re-apply the
amount afterwards — `unpair_transactions` refuses non-mutual links at both the
preview and the locked check, and deleting the canonical row demotes the
referrer to `REJECTED`, which `contributes_to_cached_balance:261` answers False
for. Clearing the link re-enters the row into `balance_contribution_filter`
carrying an amount that is **not** in `accounts.balance` — the CC
carried-balance bug reintroduced.

### The refusal message

**The ticket's DoD clause is wrong as literally written.** It says *"the refusal
message no longer claims 'transfer leg'"*. Once the guard tests reciprocity the
refusal fires **only** on genuine, mutually-linked transfer legs, so "transfer
leg" becomes **true** for the first time, and "edit via the transactions page"
becomes true and actionable (`update_transaction` mirrors the amount at
`:736-738` and re-checks the pair invariants at `:758-766` — exactly the
capability the inbox lacks).

The required change is to the **predicate**, not the **string**. A PR that only
reworded the message would satisfy the DoD as written and fix nothing.

Correct DoD: *"the refusal is no longer raised on rows that are not transfer
legs."* Keep the message; tighten the second clause for actionability:

> `"Cannot edit a transfer leg from the reconciliation inbox; edit it on the
> transactions page so both legs stay in sync."`

(No em-dashes — customer-facing copy.)

### Observability

`import.reconcile.balance_changed` (`:772-784`) fires only on membership flips,
so today an amount edit on this path moves money and logs **nothing**. Grepped:
the event has exactly one occurrence repo-wide (the emit site) and **no test or
doc consumer**, so it is free to extend. Emit it from the gated block with
`direction="amount_edit"` plus `source_amount` / `target_amount`.

Level: `ainfo`, **not** `awarning`. `_apply_match`'s two warnings carry an
explicit, non-transferable justification at `:621-623` — they fire only on
states the design argues are *unreachable*. This refusal fires on a reachable,
expected, user-driven action; a warning there is noise and inverts the signal
those events were built to carry.

---

## 5. Partner-resolution matrix

`is_reciprocal_pair` fails **CLOSED** (correct for editability);
`contributes_to_cached_balance` fails **OPEN** (correct for money). Both
receive the same raw, org-scoped partner from `_reconcile_one:849-854`.

| Shape | reciprocal? | Editable? | Balance moves? | Correct |
|---|---|---|---|---|
| Resolved **reciprocal** (real transfer leg, import-paired) | True | **No — refused** | n/a | ✅ amount IS in the balance; a correct edit needs the partner mirror the inbox lacks. Message now true. |
| Resolved **one-way** (reconcile match, reopened) | False | **Yes** | **No** | ✅ the ticket's target case |
| **Unlinked** | False | Yes | Yes, by delta | ✅ unchanged path, fenced at `test_reconciliation_service.py:527` |
| **Dangling** (link set, row gone) | False | Yes | Yes | ⚠ unreachable — FK is `ON DELETE SET NULL`; documented Python/SQL divergence pinned `xfail(strict=True)`. **Do not fence, do not fix.** |
| **Cross-org** | False | Yes | Yes | ⚠ same divergence; resolver is org-scoped so it degrades to dangling. No writer produces one. |
| **Self-link** | False (`partner.id != tx.id`) | Yes | Yes, by delta | ✅ and it AGREES with the SQL, which keeps self-links deliberately (`transaction_filters.py:122-129`) |
| Source `SKIPPED`/`REJECTED` | — | unreachable (both terminal) | No | ✅ latent-correct |

**No wrong cells.** The opposite polarities are load-bearing.

---

## 6. Fences

Placement: **`backend/tests/services/test_matched_row_actions.py`**, new
section. That file already owns this theme and its fixture rules are exactly
the ones these fences need.

**Fixture rules (inherited, mandatory):**
- Every matched row is built through the **real `reconcile_request`** path. A
  hand-written `linked_transaction_id` never ran
  `_apply_balance_for_transition`, so the premise under test — that the
  contribution was already reverted — would be absent and every assertion
  untethered.
- New helper `_make_reopened_match(...)` = `_make_matched_pair(...)` then two
  more real `_reconcile` calls, `MATCHED -> ACCEPTED -> PENDING_REVIEW`. **Never
  hand-set `reconciliation_state`** — the reopen is precisely what leaves the
  stale link beside a reverted balance.
- Balance asserted **per account** against the reconstruction through the **SQL**
  `balance_contribution_filter`, never the Python sibling (they intentionally
  disagree on an unresolvable partner).
- Accounts open at different balances; amounts are distinct powers of two;
  `PRAGMA foreign_keys=ON`.

| # | Fence | Wrong implementation it kills (re-introduce -> confirm RED) |
|---|---|---|
| **F1** | Reopened matched row + `edits(description=…)` -> 200, row updated, link unchanged | The blanket `if tx.linked_transaction_id is not None: raise` at `:517-521`. RED on `main` with `ValidationError`. **Insufficient alone** — it asserts a status code, not the ledger. |
| **F2** ⭐ | Reopened matched row, SETTLED, amount **100.00 -> 175.00**: `accounts.balance` byte-identical before/after; invariant holds; row really is 175.00 | Kills independently: (a) guard narrowed, arm ungated -> drifts −75; (b) gating **only the revert** -> drifts the full 175; (c) gating **only the apply** -> drifts the full 100; (d) **THE TRAP** — `partner = raw if is_reciprocal_pair(...) else None` then `contributes_to_cached_balance(tx, partner)` -> fails open, gate vacuous, drifts −75 **while F1 and F3 stay green**. |
| **F3** | **Unlinked** settled row, amount edit -> balance moves by exactly the delta | Already exists and is green: `test_reconciliation_service.py:527` (`test_edit_amount_recomputes_account_balance`). Kills hard-coding the gate to `False` or deleting the arm — without it, `source_in = False` passes F1 and F2. **Confirm it stays green; cite it in the PR body.** |
| **F4** | **Genuine reciprocal** transfer leg in a batch (built via the real import pairing path), reopened, edit attempted -> refused; **both** accounts unchanged; assert on the **detail string** | Kills deleting the guard outright, and — **with F1** — kills the polarity inversion "editable iff NOT in the cached balance", which would allow the transfer leg and refuse the matched row, exactly backwards. Neither F1 nor F4 alone kills it. |

### Added after the review round (unfenced sub-terms on lines this diff wrote)

Both reviewers returned **no blocking findings**, but the vacuity axis found
single-token mutants on newly-written lines that survived every fence above.
All are folded in, none needed a ruling:

| # | Fence | Wrong implementation it kills |
|---|---|---|
| **F42b** | Description-only edit on a transfer leg is still refused | `is_reciprocal_pair(...) and edits.amount is not None` — "only refuse edits that would move money", which passes F40-F43b because F42 was the only transfer-leg fence and it edits the amount |
| **F44** | SETTLED row date edit applies and mirrors | `<` -> `<=` on the TBD-407 guard. The mirror sets `settled_date = date` immediately BEFORE the check, so `<=` 422s **every** settled date edit — the commonest shape in the inbox. F43 and F43b both survive it (their dates are strictly ordered), and before this diff **no test in the repo passed `date=` on this path at all**, so the operator was pinned from neither side. |
| **F45** | PENDING row with NULL `settled_date` can move its date | Dropping the `is not None` term -> `TypeError: '<' not supported between NoneType and date`, an unhandled **500** on a routine edit. Every other TBD-407 fence sets `settled_date` explicitly. |
| **F46** | PENDING row amount edit moves no money | Dropping `tx.status.value == "settled"` from the gate. An unlinked PENDING row answers **True** to `source_in_cached_balance`, so the arm fires and moves a balance the amount was never inside — the same defect class as this ticket, on the same line, in the other direction. F41 cannot see it (its row is skipped for the *other* reason). |
| **F47** | The emit fires when money moves, and NOT on the matched-row edit | Deleting the emit; dropping `tx_type` (without it the delta is unsigned — 100 -> 175 is +75 for INCOME, -75 for EXPENSE); and, **independently of F41**, THE TRAP: under a vacuously-true gate the arm runs on the matched row and the event fires. |

⚠ F47 binds a recorder onto the module's own `logger` attribute rather than
using `structlog.testing.capture_logs()`. That helper swaps global processor
state, so a fence built on it is green alone AND green on either half of the
suite, then RED in a full run once another module has configured structlog
first.

**Every balance fence MUST change the amount.** `:534` short-circuits on
`edits.amount is None` and on `edits.amount == tx.amount`, so a description-only
edit never enters the arm and is green against **every** mutant listed. The
ticket's note on this is correct and is its single highest-risk instruction.

### Anti-fences — do NOT write these

- Any balance assertion on a description-only, category-only, or
  same-amount edit. Arms cancel exactly; green against unfixed code.
- *"A SKIPPED/REJECTED row's edit moves no money."* Unconstructible — both
  states are terminal, so the fixture is refused by `_validate_transition`
  (a 409), not by the guard you aimed at. **A downstream guard masking the
  mutant.**
- *"`_apply_balance_for_transition` no-ops on the EDITED path."* Green on
  unfixed code and provably can never go red. Documentation dressed as a fence.
- Any source-vs-target gating fence. Since `source_in ≡ target_in` on every
  reachable edge, **no test can distinguish the two implementations.** That arm
  is structurally unfenceable; enforce it by parameter name and comment.
- Any test that hand-writes `linked_transaction_id` (premise absent), or asserts
  against the **Python** `contributes_to_cached_balance` instead of the SQL
  reconstruction (circular with the code under test).

---

## 7. Folded in: the `settled_date < date` hole (separate key)

`_apply_edits:545-550` writes `tx.date` and mirrors `settled_date` **only when
SETTLED**. On a PENDING row carrying an expected `settled_date`, moving the date
past it yields `settled_date < date`. The ordering rule has exactly two
enforcement sites and **neither covers this path**:

- `schemas/transaction.py:39` — a validator on the *transaction* schemas.
  `ReconciliationEdits` has no model validator.
- `transaction_service.py:733` — inside `update_transaction`, which the inbox
  does not call.

The model flush listener (`models/transaction.py:181-199`) enforces only
*SETTLED ⇒ settled_date not null*, **not** the ordering. There is no
`CheckConstraint`. Reachable today on unlinked pending rows — TBD-310 does not
arm it, it widens the population.

**Folded in** because it shares a root cause (`_apply_edits` is a thin second
writer that skipped `update_transaction`'s integrity guards — the amount arm
skipped the membership guard, the date arm skipped the ordering guard) and
because this ticket already restructures the same function, so a separate
ticket would edit the same lines immediately afterward. Precedent for two keys
under one squash subject: `89e26635` (TBD-311, TBD-312).

Scope: one guard after the date write, reusing the **exact** message string
from `transaction_service.py:733` so the two surfaces cannot drift, plus one
fence. Do **not** add a `CheckConstraint` or model validator — that is a
migration and a third ticket.

**Fence F5:** PENDING row with `settled_date = D`, edit `date = D + 30` ->
`ValidationError("settled_date must be on or after date")`, row unmutated. Must
use a PENDING row — on a SETTLED row the mirror keeps them equal and the fence
is vacuous. The docstring must state whether the shape is constructed directly
or arrives from a real import, so nobody later reads it as a live-regression
fence.

---

## 8. Out of scope — with written ownership boundaries

**TBD-310 owns `_apply_edits` and its call site at `_reconcile_one:866`.**

1. **Self-transition discards edits** (file separately, together with #2 — both
   live in `_reconcile_one`/`close_batch_if_complete`). `_reconcile_one:823-826`
   returns early on `source_state == target_state` **before** `_apply_edits`
   runs, while `_validate_payload_shape` accepts `edits` on an `EDITED` target.
   So `EDITED -> EDITED` with edits returns **200 with the id listed as
   transitioned** and silently discards them. It also bypasses
   `_validate_transition`, converting a would-be 409 into a 200 — a contract
   violation, not only data loss. Same two lines discard
   `match_with_transaction_id` on `MATCHED -> MATCHED`, leaving the row linked
   to the OLD target while the client believes it was re-pointed. API-only
   (the UI's `ALLOWED_NEXT` offers neither).
2. **Auto-closed batch + reopened row.** `close_batch_if_complete:950` returns
   early when already CLOSED and **nothing anywhere reopens a batch** —
   `ImportBatchStatus.OPEN` is written in exactly one place, at batch creation.
   So reopening a row on an auto-closed batch leaves `pending_count = 1`
   permanently under a `"closed"` header. Needs a product ruling.
3. **Lock granularity** (flag, do not scope). `_reconcile_one:807-812` reads the
   transaction with a plain `select` — no `FOR UPDATE`. Reconcile serialises on
   the **batch** row; `update_transaction` serialises on the **transaction**
   row. The two surfaces can interleave on the same row. Pre-existing for every
   transition. Changing lock granularity inside a bug fix is unreviewable — a
   lock-order inversion is invisible to every test.
4. **Frozen:** `contributes_to_cached_balance`, `is_reciprocal_pair`,
   `balance_contribution_filter`. No polarity harmonisation, no org clause. The
   `xfail(strict=True)` divergence cell stays.
5. **No frontend change.** Hiding "Edit" on linked rows would re-implement the
   bug in the client and mask the server fix.
6. **Do not widen `ALLOWED_TRANSITIONS`**, do not add fields to
   `ReconciliationEdits` (today's revert/apply pair sharing one `tx.type` is
   only safe because type is immutable here), do not add an unlink action, do
   not touch `update_transaction`.
