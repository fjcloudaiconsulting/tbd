# Overdue recurring obligations belong in the forecast (TBD-260)

**Status:** implemented.
**Scope:** `backend/app/services/forecast_service.py`, `backend/app/services/date_utils.py`,
`backend/app/services/recurring_service.py` (one constant). Backend only. No schema change, no
migration, no API-contract change, no money-writing path touched.
**Predecessor:** `specs/2026-07-30-forecast-period-window-design.md` (TBD-243), which measured this
defect, declined to fix it, and handed it off by name.

---

## 1. The defect

`compute_forecast` selected recurring templates with

```python
RecurringTransaction.next_due_date <= window_end,
RecurringTransaction.next_due_date > today,
```

An **overdue** template (`next_due_date <= today`) therefore contributed zero to
`recurring_income` / `recurring_expense` and zero to the `cat_recurring` breakdown.

`generate_due_transactions` materialises it anyway. Its window is
`current_cycle_window(cycle_day, today)` — derived from `organizations.billing_cycle_day` and the
clock, **roster-independent**, nothing to do with `window_end`. Its catch-up loop is

```python
while r.next_due_date <= period_end:
```

with **no lower bound at all**.

So the obligation arrived in `pending_*` having never been in `recurring_*`, and `forecast_expense`
moved with **no user action**. The scheduler runs generation every 900 s by default
(`config.py:258-259`, per-org opt-out defaults on), so for the automated majority this is a ≤15-minute
**sawtooth**, not a stable understatement — the number changes while the user is looking at it.

Measured on both rosters, before this change: `forecast_net` 0 → −100.00.

## 2. Why the ticket's prescribed fix is a NULL FIX

TBD-260's description says the query should be *"bounded below by `p_start`"*, which reads as
`RecurringTransaction.next_due_date >= p_start`.

**It leaves the ticket's own headline scenario broken in the same direction and by the same amount.**

`next_due_date` is **not an occurrence date**. It is a **frontier**: the next un-materialised
occurrence. A template whose frontier sits before `p_start` still has occurrences inside
`[p_start, window_end]`, and generation materialises every one of them. Gating the template out by
its stale frontier drops real in-window obligations.

Trace. Monthly 100.00; `next_due = p_start − 1 month`; occurrence grid
`{p_start−1mo, p_start, p_start+1mo}` with `p_start+1mo > window_end`:

| implementation | `recurring_*` before | `pending_*` after | `forecast_net` |
|---|---|---|---|
| `main` (`next_due_date > today`) | 0 | 100 | 0 → −100 **BREAK** |
| ticket's `next_due_date >= p_start` | 0 (template excluded) | 100 | 0 → −100 **SAME BREAK** |
| no lower bound at all | 200 | 100 | −200 → −100 **BREAK, opposite sign** |
| **(a′), §3 below** | **100** | **100** | **conserved** |

Also rejected: the plausible one-liner `d = max(r.next_due_date, p_start)`. It shifts the grid off
the template's day-of-month and projects dates generation never creates — see §5 on path dependence.

Fenced by **F13**, which is RED against all three wrong rows.

## 3. The invariant that ships — (a′)

> `recurring_*` projects exactly the occurrences of each active template that fall in
> `[p_start, window_end]` and have **not** already been materialised. `pending_*` / `executed_*`
> count exactly the materialised ones. The two sets **partition the same occurrence grid**.

Consequences, all of them load-bearing:

1. **The bound is on the OCCURRENCE, not on `next_due_date`.** The query keeps
   `next_due_date <= window_end` (a cheap pre-filter: a frontier past the window has no occurrence
   inside it) and loses its lower bound entirely. There is no clock predicate left anywhere in the
   recurring path.
2. **`compute_forecast` is now clock-independent except through `window_end`.** `today` is resolved
   once and consumed at exactly one site, the `period_spend_window_end` call. **F8** asserts the
   whole payload is identical for `today = p_start` and `today = window_end` on a roster where the
   `max(derived, today)` floor is demonstrably inert. That equality kills any residual `today`
   predicate *structurally*, not merely by value.
3. **Generation can now only move an amount BETWEEN buckets**, never into or out of the total.
   `forecast_net` is stable across `generate_due_transactions` on every roster: **F4** (future-dated),
   **G2** (overdue, both rosters), F13, F14, F10c, F15.

## 4. The anti-double-count probe, and the ruling that settles it

Dropping the clock gate without a probe **double-counts**: a template whose frontier has not advanced
past the window is selected even though its occurrence is already a row.

```python
materialised: set[tuple[int, datetime.date]] = set()
if recurring_items:
    rows = await db.execute(
        select(Transaction.recurring_id, Transaction.date).where(
            Transaction.org_id == org_id,
            Transaction.recurring_id.in_([r.id for r in recurring_items]),
            Transaction.date >= p_start,
            Transaction.date <= window_end,
        )
    )
    materialised = {(rid, d) for rid, d in rows.all()}
```

**The probe asks exactly one question: does a row for this occurrence key already exist?**

It carries **no** `reportable_transaction_filter()` and is **not** bounded on
`effective_period_date_expr()`. This was contested; it is settled, on the following argument.

### 4.1 The fixed-point proof

The probe's job is to suppress a projection that would otherwise be double-counted or would
self-clear. A candidate narrower predicate amounts to:

> suppress **iff** the key exists **AND** (the row is counted in one of this period's sums **OR**
> generation will not re-create it).

Generation's create-condition matches on `(org_id, recurring_id, date)` with **no status, no
reportability, no effective-date term**, and on a hit it takes the `exists` branch — skip, advance.
So *generation never re-creates a row whose key exists*. The second disjunct is therefore always
true, the whole parenthesis collapses to `True`, and the predicate collapses to **key existence**.

Any narrowing projects an occurrence generation will never materialise, and the projected value then
**moves on its own at the next scheduler tick** — precisely the defect this ticket removes, relocated.

### 4.2 The two narrowings, and their fences

- **A reportable filter** would drop a paired transfer leg out of the probe. **F18** builds exactly
  that state through real service calls (`promote_to_recurring`, then `pair_existing_transactions`)
  and asserts `recurring_expense == 0`: the narrowed probe projects a phantom 100.00 expense **for a
  transfer**, which then vanishes on its own.
- **An `effective_period_date_expr()` bound** would drop a reportable pending row whose
  `settled_date` estimate pushes it into the successor period. **F19** asserts the two-period sum:
  the narrowed probe projects 100.00 *here* while the successor also carries 100.00 in `pending_*` —
  200.00 across two periods for one obligation.

### 4.3 The date bounds are a narrowing only

`Transaction.date >= p_start` / `<= window_end` carry **no semantics**: every projected occurrence is
inside `[p_start, window_end]` by construction, so the key equality already decides the question. They
are an index-friendly narrowing and nothing else. **F16** nevertheless pins both of them from the
"in" side with materialised occurrences placed exactly on `p_start` (10.00) and exactly on
`window_end` (1000.00) — a boundary pinned from one side is not pinned.

## 5. `occurrences_in_window` is ITERATED, never closed-form

New pure helper in `date_utils.py`. It fast-forwards `while d < start` and then collects
`while d <= end`, with a **single** iteration budget spanning both loops from the same origin.

`advance_date` is **path-dependent** at month ends: `Jan 31 → Feb 28 → Mar 28 → Apr 28`, *not*
`Mar 31 / Apr 30`. `generate_due_transactions` walks the same way from the same origin. A closed-form
jump to the first in-window date would disagree with the dates generation actually creates — and
conservation is a claim about exactly those dates, not about a count.

`MAX_OCCURRENCE_ITERATIONS = 500` lives in `date_utils.py`; `recurring_service.MAX_CATCHUP_ITERATIONS`
is an **alias** of it, not a second literal, so a pathologically stale template truncates identically
in both walks. **F17** pins the grid with a month-end fixture (`next_due = 2026-01-31`) and pins the
alias with an **AST guard** over `recurring_service`'s source — a value comparison cannot tell an
alias from a duplicated literal, and there is no type checker in CI.

## 6. One walk feeds both the totals and the categories

The `:173-180` totals loop and the `:218-224` category loop are folded into one walk, with the
category dict updated **inside the same suppression branch**. Two walks is how the breakdown and the
totals came to disagree on suppressed occurrences.

This matters beyond tidiness: `ai_forecast_refine_service` reads `baseline["categories"][*]["forecast"]`
(which is `str(ex + pe + rc)`) at `:272`, `:314` and `:344-352`. Totals that move without the
breakdown moving make the AI baseline internally inconsistent, and F3's breakdown-sums-to-totals
assertions go red.

`compute_forecast`'s signature is **unchanged** — `(db, org_id, period_start=None, *, today=...)`.
The fakes at `test_ai_forecast_refine_service.py:162, :202, :271` implement
`(db, org_id, period_start=None)` and are never called with `today=`.

## 7. This is a CORRECTNESS fix, not a product change

TBD-243 deferred this as *"a product question about what 'upcoming' means"*. That framing does not
survive contact with the rest of the codebase. **Three surfaces already ship the clock-free shape;
`forecast_service` was the outlier:**

- **Loans and credit cards.** The TBD-243 design itself ruled the opposite way at `:305-313`, and the
  ruling is pinned in a test docstring at `test_forecast_window_end.py:611-612`: *"a past-due but
  genuinely unpaid instalment must still be projected; do **NOT** add a `> today` gate"*.
  `due_cycles_in_horizon` and `due_loan_payment_dates` scan `[p_start, W]` with no clock gate, and
  dedupe clock-free via `already_paid` / `p_k_owned` — structurally the same probe this PR adds.
- **`forecast_plan_service.populate_from_sources:509-536`** already ships this exact clock-free
  fast-forward: `while d < p_start and d <= p_end: d = advance_date(...)`, then
  `while d <= p_end: total += r.amount`. Same frontier, same fast-forward, same iterated walk.

An obligation does not stop existing because the calendar moved past it. Consistency across the four
surfaces is the correctness argument; it does not need a product decision.

## 8. A LIVE double-count on `main`, fixed as a side effect

`promote_to_recurring` (`transaction_service.py:705-789`) sets `tx.recurring_id` on the **source row**
(`:781`) and creates a template with `next_due_date = body.next_due_date`. The UI supplies it at
`frontend/components/floating/TransactionForm.tsx:249-250`:

```js
const nextDue = date < today ? today : date;   // future-dated tx -> nextDue === date
```

So ticking "Repeats" on a transaction dated `D >= today` yields `tx.date == template.next_due_date == D`.
On `main`, for `D > today`, the `> today` gate **passes**: the projection counts `D` **and** the row is
already in `pending_*`. One transaction, twice, in `forecast_expense`.

It was **unfenced** — `test_forecast_service.py` seeds no recurring templates at all. The probe fixes
it as a side effect. **F18** and **F16** are its fences.

## 9. User-visible effect

- The **verdict badge is NOT affected.** `frontend/components/dashboard/OnTrackTile.tsx:291` anchors
  on `executed_expense`, with a comment at `:287-289` saying so. Only the muted "Expected spending"
  stat moves.
- For orgs with automation **on** (the default), today's behaviour is a ≤15-minute sawtooth rather
  than a stable understatement, so they see **no persistent change** — the number simply stops
  jumping.
- Orgs with automation **off** and an overdue template see "Expected spending" rise to include the
  obligation they already owe. That is the fix.

## 10. Fences, and the injection evidence

Every fence below was verified by injecting the named wrong implementation and confirming RED.
Existing coverage that must stay green and unmodified: F1, F2, F3, F6, F7a, F7b, F9, F11, G1, F10a;
`test_forecast_parity_after_generate.py`; `test_forecast_service.py`;
`test_recurring_generate_fill_period.py`; `test_account_balance_forecast_service.py`;
`test_ai_forecast_refine_service.py`; `test_period_spend_window.py`.

| Fence | Where | Kills | Injected → RED |
|---|---|---|---|
| **G2** (rewritten from guard) | `test_forecast_window_end.py` | the shipped `> today` gate; a projection horizon at `cycle_end` | A: `recurring_expense` 0 vs 100 · L: 200.00 vs 100 |
| **F8** (promoted from guard) | `test_forecast_window_end.py` | any residual clock predicate in the recurring path, structurally | A: `at_end["recurring_expense"]` 0 vs 20 |
| **F12** (extended) | `test_forecast_window_end.py` | nine upper bounds + the recurring lower bound from **both** sides | A · C: 1049 vs 52 · D: 23 vs 52 · E: 29 vs 52 · F: 29 vs 52 · O |
| **F13** | `test_forecast_overdue_recurring.py` | **the ticket's `next_due_date >= p_start`**; `> today`; no lower bound; `d <= start` for `d < start` | A: 0 vs 100 · B: 0 vs 100 · C: 200 vs 100 · D: 0 vs 100 |
| **F14** (weekly) | ″ | counting from `next_due` with no fast-forward (60); only the first occurrence (10); closed-form | A · B: 0 vs 40 · C · O |
| **F10c** | ″ | projecting to `current_cycle_window(...)[1]` instead of `window_end` | A · L: 50 vs 10 |
| **F15** (`auto_settle`) | ″ | projecting only non-`auto_settle` templates; conservation asserted via `pending_*` alone | A · M: 0 vs 100 |
| **F16** (probe, frontier rewound) | ″ | no probe (2220); probe `date > p_start` (1120); probe `date < window_end` (2110); split category walk | G: 100.00 · J: 10.00 · K: 1000.00 · O |
| **F17** (month-end + AST) | ″ | closed-form occurrence grid; `MAX_CATCHUP_ITERATIONS` re-declared as a literal | C · N: `Constant(value=500)` |
| **F18** (promote + pair) | ″ | **probe carrying `reportable_transaction_filter()`**; no probe; split category walk | G · H: 100.00 vs 0 · O |
| **F19** (successor period) | ″ | **probe bounded on `effective_period_date_expr()`**; no probe; no lower bound | C · G · I: 100.00 vs 0 |

**Boundary sweep** (flip one bound at a time, confirm RED):

| bound | flip | RED at |
|---|---|---|
| query `next_due_date <= window_end` | `<` | F12 (29 vs 52) |
| helper `while d <= end` | `<` | F12 (29 vs 52) |
| helper `while d < start` | `<=` | F13 (0 vs 100), F12 (23 vs 52) |
| helper `while d < start` | removed entirely | F13 (200), F14 (60), F17, F19, F12 (1049) |
| probe `Transaction.date >= p_start` | `>` | F16 (10.00 leaks) |
| probe `Transaction.date <= window_end` | `<` | F16 (1000.00 leaks) |

## 11. Residuals

- **The lapsed-roster phantom projections for loans and credit cards (F9, G1) are untouched.** They
  are a different mechanism — synthesis horizons, not recurring templates — and TBD-243 §5 announced
  them.
- **`ai_forecast_refine_service` still labels an N-month window "monthly".** Untouched; TBD-243 §5
  announced it; it needs its own design round.
- **A template whose frontier is more than `MAX_OCCURRENCE_ITERATIONS` periods stale** truncates in
  both walks, identically. F17 pins the identity, not the value; nothing depends on 500 in particular.
