# Link reciprocity: one discriminator for `linked_transaction_id`

**Tickets:** TBD-280, TBD-281, TBD-282, TBD-293 (epic TBD-279, Group R)
**Date:** 2026-07-31 · **Revision 2**
**Branch:** `TBD-280-link-reciprocity-guards`

**Method:** two independent architects → concede-or-defend on two forks → two adversarial sign-offs, one of which **built the kernel and ran it**. Revision 2 folds 6 blocking findings + 5 measured corrections. Every claim below marked *(measured)* was verified by execution against MySQL 8, not by reading.

---

## 1. The rule

> **A link is a transfer link if, and only if, the partner links back.**

| writer | file:line | shape |
|---|---|---|
| `transaction_service._link_pair` | writes at `:1219-1220` | **bidirectional** |
| `reconciliation_service._apply_match` | writes at `:571` | **ONE-WAY** |
| `transaction_service.unpair_transactions` | clears at `:1616-1617` | — |

*(measured)* A grep of every `linked_transaction_id` assignment across `backend/app/` yields **exactly these three writers**. No raw SQL, no `update().values(...)`, no migration data step, no import-pairing bypass (`import_service.py:538,658` route through `_link_pair`). This is the evidence base for §3.

`reconciliation_service.py:544-552` states the one-way direction is a **load-bearing discriminator** for `balance_contribution_filter`. This spec does not touch it.

---

## 2. Scope

| Ticket | Site | Defect |
|---|---|---|
| **TBD-280** | `delete_transaction:868-890` | cascades the delete to the partner on any non-null link |
| **TBD-293** | `delete_transaction:876-886` | gates **both** legs' reverts on `tx`'s status alone |
| **TBD-280** | `bulk_delete_transactions:925-987` | same cascade; also counts collateral rows in `deleted_count` |
| **TBD-281** | `unpair_transactions:1570-1571` | non-nullness only, then rewrites **both** `category_id` at `:1616-1619` |
| **TBD-282** | `_apply_match:554-571` | no guard against the target already linking back |

### Removed from scope in revision 2

**TBD-292 (`update_transaction:517`) is OUT.** The sign-off proved the prescribed one-line fix *introduces* balance corruption.

*(measured)* `update_transaction` reverts and re-applies the cached balance gated on **status alone**, with no link awareness — `:583-584` (`if old_status == SETTLED: revert_balance(...)`) and `:643-644` (`if tx.status == SETTLED: apply_balance(...)`). A matched row's contribution was already reverted at match time, so `PATCH {"amount": "10.00"}` on a matched SETTLED row of 100.00 yields `revert(+100)` then `apply(-10)` = **+90.00 drift**. The 409 at `:518` is the only thing preventing this: **it fails safe**. Removing it without gating the balance arms is strictly worse than leaving the usability bug.

Also out, each with its own ticket: **TBD-289** (frontend, product ruling), **TBD-294** (`ondelete=SET NULL` orphan, one-way door), and the **`_delete_many` merge** (§7).

---

## 3. Ruling: `balance_contribution_filter` is FROZEN

Both architects agreed after concede-or-defend. `transaction_filters.py:110-121` is **not modified**. Not one byte.

1. The org and not-self clauses a reformulation would add are **dead code** — §1's writer census proves neither cross-org nor self links are producible by any writer.
2. *(measured)* Both the current positive form and the proposed negative form **agree on self-links** (kept). The correlated `EXISTS` against the `_bcf_partner` alias **does** match a row against itself — confirmed by executing the compiled SQL on MySQL 8, `KEPT = [9001, ...]` where 9001 is the self-link. The "correct only by accident" charge is true as description, empty as consequence.
3. The negative form's failure direction is **KEEP on uncertainty**, which for this filter *is* the CC bug it exists to prevent.
4. Written the obvious way, `partner.linked_transaction_id != Transaction.id` is NULL-unsafe: when the partner's link is NULL — the common case — the comparison yields NULL, the EXISTS collapses, and **every reconcile match silently re-enters the balance**.

**Instead:** add a comment at `transaction_filters.py:110` cross-referencing `_transfer_collapse_clause`, stating self-links are kept **deliberately**, and why the two predicates have opposite polarity.

---

## 4. The helpers

Both in `backend/app/services/transaction_filters.py`. *(measured)* No new import edges needed; `Transaction` is already imported.

### 4.1 `is_reciprocal_pair(tx, partner) -> bool`

```python
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
    """
    return (
        partner is not None
        and tx.linked_transaction_id is not None
        and partner.id == tx.linked_transaction_id
        and partner.id != tx.id
        and partner.org_id == tx.org_id
        and partner.linked_transaction_id == tx.id
    )
```

`tx.linked_transaction_id is not None` is **load-bearing, not belt-and-braces**: without it, a transient (unflushed) partner makes `None == None` true and the predicate becomes argument-order sensitive. No call site passes an unflushed row; document it so nobody removes it.

### 4.2 `contributes_to_cached_balance(tx, partner) -> bool`

Needed by TBD-293. **Revision 2 rewrote this**: the original derivation, docstring rationale and self-link branch were all wrong.

```python
def contributes_to_cached_balance(tx: Transaction, partner: Transaction | None) -> bool:
    """Python sibling of ``balance_contribution_filter()`` -- the LINK and
    RECONCILIATION-STATE half of the question only.

    ⚠ NOT a complete answer to "is this row's amount inside
    accounts.balance". It has NO status term, because the SQL has none
    either. Pending amounts are never in the cached balance, so every
    caller MUST conjoin ``tx.status == TransactionStatus.SETTLED``. Every
    SQL caller already does (see networth.py:209, cc_statement_service).

    Transcribed branch-for-branch from the SQL. Do NOT rewrite as
    ``not is_reciprocal_pair(...)``: that inverts the RECIPROCAL case
    (a real transfer leg would report False, and delete_transaction would
    skip the revert on BOTH legs of every transfer, drifting each account
    UP by its leg amount). It happens to give the right answer for a
    self-link, which is why the obvious fence for it is vacuous.

    Fails OPEN whenever the partner cannot be resolved: an unprovable
    link keeps its contribution, because nothing ever reverted it.
    """
    if tx.reconciliation_state in _RECON_EXCLUDED_STATES:
        return False
    if tx.linked_transaction_id is None:
        return True
    if partner is None or partner.id != tx.linked_transaction_id:
        return True                       # see DIVERGENCE
    return partner.linked_transaction_id == tx.id
```

Three revision-2 corrections, each from a measured finding:

- **The self-link branch was DELETED as dead code.** *(measured)* Removing `if tx.linked_transaction_id == tx.id: return True` left the entire parity suite green — it has no observable effect, because a self-link either resolves the partner to the row itself (fall-through gives `tx.id == tx.id` → True) or to `None` (caught above). A branch no test can kill is decoration; this repo has shipped 17+ of those.
- **`partner.id != tx.linked_transaction_id` added** for argument-safety, matching §4.1. *(measured)* Without it, passing a wrong partner flipped the answer from `False` to `True` on a one-way link. Zero behavioural cost on correct input.
- **DIVERGENCE restated.** The original said "the single cell where the two disagree (cross-org one-way)". *(measured)* **There are two cells, and the accurate rule is: the predicate disagrees with the SQL whenever `partner` is unresolvable, for any reason.** Cross-org one-way is one reason; a **dangling link** (partner row gone) is another. Both are unreachable in production — the MySQL FK is `ON DELETE SET NULL` *(measured: `transactions_ibfk_4`)* — but the class is open-ended and must be documented as such, not enumerated.

---

## 5. Call-site changes

| # | Site | Change |
|---|---|---|
| 1 | `to_response:89-99` | `is_reciprocal_pair(tx, tx.__dict__.get("linked_transaction"))`. **Keep the `__dict__` probe** — `linked_transaction` is absent from `_load_opts():48-53`; lazy-loading raises `MissingGreenlet`. *(measured)* behaviour-neutral on the collapse path, which eager-loads it at `:2199`. |
| 2 | `delete_transaction:840-843` | **UNCHANGED.** The lock set must keep pulling any non-null link: reciprocity cannot be evaluated before the partner is read, and deciding on the unlocked preview reintroduces the TOCTOU the lock closes. A superset locked in ascending id order can never deadlock. **Add a comment saying this is deliberate.** |
| 3 | `delete_transaction:868-890` | resolve `raw_partner` and `pair_partner` as **two separate names**. Cascade on `pair_partner`. Revert **per row**, keyed by id, gated on `r.status == SETTLED and contributes_to_cached_balance(r, ...)`. Collapsing the two names is how this bug returns. |
| 4 | `bulk_delete_transactions` | split **lock set** from **delete set**. The delete set = requested rows **plus reciprocal partners of requested rows** (the documented cascade at `:901-902` must survive). Key it as a dict by id so a self-link is a no-op. Revert per row with the same conjunction. `deleted_count = len(delete_set)`. |
| 5 | `unpair_transactions` | cheap preview check, plus the **authoritative** check after the `FOR UPDATE` and **before** the type-composition check at `:1609-1613`. |
| 6 | `_apply_match:571` | two guards, both **before** the write. |
| 7 | `_transfer_collapse_clause` | **UNCHANGED.** Merging its branches is a behaviour-neutral refactor with no ticket, and PR #600's suite is the only thing pinning it. |

**The `status == SETTLED` conjunction in rows 3 and 4 is not optional.** `contributes_to_cached_balance` has no status term (§4.2). Implemented without it, deleting an ordinary **unlinked PENDING** row reverts an amount that was never applied — the most-travelled delete path in the app, which *(measured)* has **zero test coverage today**.

### 5.1 `unpair_transactions` (TBD-281)

A one-way link is **refused with 400**:

- No representable no-op — the endpoint returns exactly two legs; returning the unrelated canonical row invents a pair.
- The current failure is already an error, just a lying one: `_apply_match` does not require opposite types, so a same-type one-way link already hits `:1612` and returns **409 "Pair has invalid type composition"**, blaming the data instead of the request.
- `ConflictError` means "refresh and retry" here and clients may auto-retry; retrying can never help. 409 stays for the genuine race at `:1606`.

Message: `"Transaction is not part of a transfer pair"`, identical to `:1571`. At this layer we know the link is non-mutual, not *why*. **The check must precede `rows_by_type`.**

⚠ Place the check **before** the `len(rows) != 2` test at `:1606`, or a self-linked row (`sorted([id, id])` → one row) reports 409 "Pair partner not found" instead of the intended 400.

### 5.2 `_apply_match` guards (TBD-282)

Both **before** `tx.linked_transaction_id = match_id`:

```python
if target.linked_transaction_id == tx.id:
    raise ValidationError(
        f"Transaction {target.id} is already matched to transaction {tx.id}; "
        "matching in the opposite direction would make them look like a "
        "transfer pair. Accept or reject one of the two rows instead.")
```

**Narrow on purpose** — `== tx.id`, never `is not None`: matching an imported row against a leg of a real transfer is a supported flow that `find_duplicate_of_linked_leg:1282-1329` exists to surface.

```python
if tx.linked_transaction_id is not None:
    current = await db.scalar(select(Transaction).where(
        Transaction.id == tx.linked_transaction_id, Transaction.org_id == org_id))
    if is_reciprocal_pair(tx, current):
        raise ValidationError(
            f"Transaction {tx.id} is a transfer leg; unlink the transfer "
            "before matching it to another transaction.")
```

Deliberately narrower than `_apply_edits`' blanket refusal: refuses only **mutual** links, so re-matching a previously one-way-matched row after a reopen keeps working. Both messages embed both ids because `reconcile_request` is all-or-nothing.

---

## 6. Fences

**A fence that does not name a mutant is decoration.** Each must be proven RED by re-introducing its named mutant, then restored.

### 6.0 Fixture hazards

1. **A symmetric pair nets to zero across accounts.** Never assert a cross-account or org-wide total. **Assert per account**, with **different opening balances** per account so a swapped attribution is visible.
2. Distinct amounts, distinct powers of two (1/2/4/8/16/32), so every subset sum is unique.
3. Assert **id sets**, never counts. A count of 2 is satisfied by the wrong 2.
4. **The adversarial fixture is mandatory:** a one-way link between two rows satisfying *every* transfer invariant except mutuality — opposite types, equal amounts, different accounts, same currency, same org.
5. Unpair fences: fallback categories must **differ** from both rows' current categories, and assertions compare category **ids** — `_link_pair:1215-1216` gives both legs the same category.
6. **No fixture may rely on id `1`.** Where a helper seeds the first row in a fresh SQLite file, offset the ids explicitly.
7. *(measured)* Self-links and cross-org links need **no raw SQL and no FK disabling** in either engine — a plain `UPDATE` after INSERT suffices. Only a **dangling** link needs `PRAGMA foreign_keys=OFF`.

### 6.1 Fence table

| ID | Kills (re-introduce, confirm RED) | Test |
|---|---|---|
| **F1** | `linked_tx = rows.get(...)` with no reciprocity test (`:868-872`) | `M`(8.00, acct A) → `T`(8.00, acct B) one-way. Delete `M`. Assert `T` exists. |
| **F2** | cascade fixed but two-account revert restored (`:876-883`) | same fixture; assert **`T`'s account balance unchanged**. `T` must be on a different account. |
| **F3** | `partner.id != tx.id` dropped from `is_reciprocal_pair` | self-linked settled row (4.00). Delete. Assert balance moved **exactly 4.00, not 8.00**. ⚠ Only red if `delete_transaction` reverts `tx` and `pair_partner` as separate calls — §5 row 3 mandates an id-keyed set, so **assert the row is deleted once and the balance moves once**. |
| **F4** | per-pair status gate restored (TBD-293), forward case | reciprocal pair, `tx` SETTLED (acct A, 16.00), partner PENDING (acct B, 16.00). Delete `tx`. Assert **A moved 16.00, B unchanged**. |
| **F4b** | *(new, rev 2)* same gate, **reverse** case | `tx` PENDING (acct A), partner SETTLED (acct B, 16.00). Delete `tx`. Assert **B moved 16.00**. Today both branches miss and B keeps a deleted row's money. |
| **F5** | `contributes_to_cached_balance` dropped | delete a `skipped` SETTLED row (32.00). Assert balance **unchanged**. Repeat for `matched`. |
| **F6** | *(rewritten, rev 2)* derived as `not is_reciprocal_pair` | **reciprocal pair, BOTH SETTLED**, acct A 16.00 / acct B 16.00, different opening balances. Delete one leg. Assert **both accounts moved by their leg amount**. ⚠ The rev-1 F6 used a self-link and was **vacuous** — *(measured)* the mutant returns the correct answer there. |
| **F6b** | *(new, rev 2)* same mutant, other face | one-way matched SETTLED row. Delete. Assert balance **unchanged** (mutant double-reverts, drifting DOWN). |
| **F7** | **two mutants, both named:** (a) `for tx in found: db.delete(tx)` restored (`:979-984`); (b) `return (len(found), skipped_ids)` restored (`:987`) | `bulk_delete([M.id])`, `M`→`T` one-way. Assert `T` exists **and** `T`'s balance unchanged (kills a), **and** `deleted_count == 1` (kills b). Rev 1 named only (a), against which the count clause stayed green. |
| **F7b** | *(new, rev 2)* delete set narrowed to requested ids only | `bulk_delete([expense.id])` on a **real reciprocal pair**. Assert **both** rows deleted and `deleted_count == 2`. The only existing bulk test passes both ids, so it stays green against this. |
| **F7c** | *(new, rev 2)* bulk delete set not keyed by id | self-linked row via `bulk_delete`. Assert deleted once, balance moves once, `deleted_count == 1`. |
| **F8** | null-only check restored in unpair (`:1570`) | **router-level** `POST .../unpair`. Assert 400; re-read both rows and assert `T.category_id`, `M.category_id`, `M.linked_transaction_id` **all unchanged** — kills "raise after mutating". |
| **F9** | check placed after `rows_by_type` | one-way link between two **same-type** rows. Assert "not part of a transfer pair", not "Pair has invalid type composition". |
| **F9b** | *(new, rev 2)* check placed after `len(rows) != 2` | self-linked row → unpair. Assert **400**, not 409 "Pair partner not found". |
| **F10** | `ConflictError` instead of `ValidationError` | assert **400** specifically, not `>= 400`. |
| **F11** | `_apply_match` target guard deleted | match `A`→`B`, then attempt `B`→`A`. Assert 400, `B.linked_transaction_id IS NULL`, both balances unchanged. |
| **F12** | guard in the wrong direction (`tx.linked == target.id`) | the F11 fixture discriminates **only because `A`→`B` is matched first**. State that in the docstring. |
| **F13** | target guard over-tightened to `is not None` | real pair `B`↔`C`; match imported `A`→`B`. Assert **success**, `B`↔`C` intact. |
| **F14** | guard placed **after** the write | **direct unit test on `_apply_match`**, asserting in-memory `tx.linked_transaction_id` after the raise. An API test cannot kill this — the savepoint rolls back either way. |
| **F15** | tx-side guard over-tightened to `is not None` | `A` matched to `B`, then MATCHED→ACCEPTED→PENDING_REVIEW, then matched to `D`. Assert **success**. |
| **F17** | parity drift, Python sibling vs SQL | table-driven, **EIGHT shapes** (rev 2 added #8). `xfail(strict=True)` on cross-org one-way — non-strict would pass silently if the divergence vanished, making the fence decoration. |
| **F18** | *(new, rev 2)* baseline delete semantics | unlinked **SETTLED** row → balance reverts. Unlinked **PENDING** row → balance **unchanged**. Kills the missing `status == SETTLED` conjunction (§5). Zero coverage today. |

**F17's eight shapes:** link NULL · reciprocal · one-way · self-link · cross-org reciprocal · cross-org one-way *(xfail strict)* · rejected · **chain (`A → B → C`, partner has a link but not back)**.

*(measured)* The chain shape is load-bearing: without it, the mutant `partner.linked_transaction_id is not None` — *mechanism* (partner has a link) instead of *property* (partner links **back**) — passes all seven original shapes. With it, that mutant dies.

*(measured)* Post-fold mutation matrix on §4.2: `not is_reciprocal_pair` → RED (reciprocal, one-way, chain) · drop `partner is None` branch → RED (cross-org reciprocal) · mechanism-not-property → RED (chain) · drop recon-state gate → RED (rejected) · any-link-drops → RED (6 cases).

### 6.2 Regression guards (must stay green, unmodified)

- `test_transaction_service_delete_linked.py:91`, `:104` — real pairs still cascade.
- `test_account_balance_forecast_service.py:946` — `test_balance_contribution_filter_invariant_matches_account_balance`. **The single most important existing test here.** *(measured)* green with the helpers in place.
- `test_transaction_collapse_transfers.py` — all 29 cases, especially `test_b6_self_linked_row_still_returned` and `test_b7_cross_org_reciprocal_link_is_not_collapsed`.

### 6.3 The test that must be INVERTED, not deleted

`test_transaction_service_delete_linked.py:122-140`, `test_delete_transaction_with_asymmetric_link_does_not_orphan`, **asserts the TBD-280 bug**: it seeds a one-way link and asserts *both* rows are gone. Its docstring calls the shape a "data-migration or partial import" artifact — it is the normal output of `_apply_match`.

Invert the assertion; rewrite the docstring to name `_apply_match` as the producer. **The fix cannot go green without touching this test**, which makes it the fastest check that the change landed.

### 6.4 Not test-killable — state in the PR body, do not fake

- Pruning the partner from `ids_to_lock` (`:840-843`).
- Deciding reciprocity on the unlocked `preview`.

Both need concurrency to go red. Defend with a site comment and a PR line. **Do not ship a sleep-based test.**

---

## 7. Deferred by ruling: the `_delete_many` merge

Architect A proposed it, then **conceded**. The merge would **silently fix TBD-293 as a side effect**: a reviewer would see `:876-886` replaced by a call and would have to reconstruct unaided that the old code mishandled two mixed-status cases — a balance change disguised as a refactor hunk.

Fixing TBD-293 **in place** here makes the follow-up merge a **provable no-op**: every remaining difference is either the `strict` flag (not-found → raise vs skip; manual adjustment → raise vs skip) or mechanical (return shape, lock-set construction, counting). File it after this PR merges.

---

## 8. Commit sequence

1. `transaction_filters.py`: both helpers + frozen-filter comment + F17 (8 shapes). No call sites touched.
2. `_apply_match` guards (TBD-282) + F11–F15. **Stops the bleeding first** — the only forward-looking fix.
3. `unpair_transactions` (TBD-281) + F8, F9, F9b, F10.
4. `delete_transaction` + `bulk_delete_transactions` (TBD-280 + TBD-293) + F1–F7c, F18, and the §6.3 inversion.
5. `to_response` onto the predicate + its regression check.

*(measured)* No fence in an earlier commit depends on a later one; each commit is green standalone.

## 9. Verification floor (not skippable)

- Every fence proven RED against its named mutant, then restored. **Paste real output.**
- **Full backend suite** in an isolated compose project `-p team-tbd280`, never the operator's stack. *(measured)* baseline with helpers present: `3589 passed, 12 skipped, 2 xfailed`.
- No frontend change in this PR → `tsc` / `vitest` / `check-design-tokens` are **not run**. State that explicitly rather than implying otherwise.
