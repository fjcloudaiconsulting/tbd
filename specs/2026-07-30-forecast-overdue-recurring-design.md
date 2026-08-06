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
`while d <= end`. **The fast-forward loop carries no iteration budget; the collect loop does.**

`advance_date` is **path-dependent** at month ends: `Jan 31 → Feb 28 → Mar 28 → Apr 28`, *not*
`Mar 31 / Apr 30`. `generate_due_transactions` walks the same way from the same origin. A closed-form
jump to the first in-window date would disagree with the dates generation actually creates — and
conservation is a claim about exactly those dates, not about a count.

`MAX_OCCURRENCE_ITERATIONS = 500` lives in `date_utils.py`; `recurring_service.MAX_CATCHUP_ITERATIONS`
is an **alias** of it, not a second literal, so the two walks are sized by one number rather than by
two constants that drift. **F17a** pins the grid with a month-end fixture (`next_due = 2026-01-31`)
and pins the alias with an **AST guard** over `recurring_service`'s source — a value comparison cannot
tell an alias from a duplicated literal, and there is no type checker in CI.

⚠ **The alias does NOT make the two walks truncate at the same place, and the first revision of this
section said it did.** The two caps are not the same kind of cap:

- generation's cap bounds **work** and **makes progress** — the catch-up loop mutates `next_due_date`
  forward on every step, so a capped run leaves the frontier 500 steps nearer the window and the next
  run resumes from there;
- a cap on the fast-forward bounds **visibility** and makes **no** progress — on exhaustion the helper
  returned `[]`, so in-window occurrences became *invisible* rather than merely expensive.

The two walks therefore truncate at the same ordinal occurrence *from the same origin*, and generation
**moves the origin**. Full trace, measurement and fix in §12.

The collect loop keeps its budget, and that is a materially different exposure. It bounds occurrences
per **window**, and the window comes from the billing-period roster — the open period's start is
app-derived from `current_cycle_window`, closed periods are admin-created through the
overlap-validated `settings.py:746` endpoint. `next_due_date`, by contrast, is user-supplied with no
past-date guard at all. Reaching the collect budget needs a single period window longer than 500 steps
of the template's frequency (~9.6 years of weekly); see §11.

## 6. One walk feeds both the totals and the categories

The `:173-180` totals loop and the `:218-224` category loop are folded into one walk, with the
category dict updated **inside the same suppression branch**. Two walks is how the breakdown and the
totals came to disagree on suppressed occurrences.

This matters beyond tidiness: `ai_forecast_refine_service` reads `baseline["categories"][*]["forecast"]`
(which is `str(ex + pe + rc)`) at `:272`, `:314` and `:344-352`. Totals that move without the
breakdown moving make the AI baseline internally inconsistent.

⚠ **An earlier revision said "F3's breakdown-sums-to-totals assertions go red". That is false** — F3
(`test_f3_off_grid_window_stops_at_successor_including_categories`) seeds **no recurring templates at
all** and cannot see the recurring breakdown. The real fences for the split-walk mutation are **F16**
and **F18** (both assert the breakdown while a projection is suppressed), plus **F14** and **F20**,
which assert `sum(c["recurring"]) == recurring_expense` directly. Verified by injection.

The breakdown key itself is fenced by **F22**, on a fixture whose `org_id`, `account_id` and
`category_id` are pairwise **distinct**. Every seed in these files previously handed out `1` for all
three, so `cat_recurring[r.category_id]` could be swapped for `r.account_id` or `r.org_id` with zero
tests red — the name lookup resolved id 1 to "Food" either way. `_skew_ids` puts the decoy rows on a
*different* org, so a mis-keyed lookup cannot resolve and surfaces as `"Unknown"`.

The **income** half of the walk is fenced by **F23**. Before it, `recurring_income` was asserted by no
test anywhere in `backend/tests`: summing an income template into `recurring_expense`, or feeding
income into `cat_recurring`, was green across the entire backend suite. `forecast_income` and
`forecast_net` are consumed by `DashboardDataProvider.tsx`, so the branch is user-visible.

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

### 9.1 Closed / historical periods now report non-zero `recurring_*`

Undocumented in the first revision, correct, and worth saying out loud.

`GET /api/v1/forecast?period_start=` is **user-facing** — the period picker browses closed periods.
For any window entirely in the past, `main`'s gate `next_due_date <= window_end AND next_due_date >
today` was **structurally unsatisfiable**: `window_end < today`, so no row could satisfy both. Closed
periods therefore reported `recurring_* == 0` by construction, not by fact, and "Expected spending"
was blank there.

With the gate removed, a closed period reports the occurrences that genuinely fell inside it and were
never materialised. Browsing a past period now shows an "Expected spending" figure where it used to
show nothing.

This is the **conserving** direction and therefore correct: an obligation that fell in that window and
was never generated *was* owed in that window, and `pending_*`/`executed_*` for that same window count
only the materialised half. Reporting zero was the bug. **F19** exercises the two-period sum directly
(one obligation, one hundred, across a period boundary) and pins that the amount is not double-counted
across neighbours.

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
| **F17a** (month-end + AST + staleness) | ″ | closed-form occurrence grid; `MAX_CATCHUP_ITERATIONS` re-declared as a literal; **an iteration budget on the fast-forward** | C · N: `Constant(value=500)` · S: `[] != [5 dates]` |
| **F17b** (deep staleness, 521 steps) | ″ | **the shared iteration budget**; `> today`; `>= p_start`; fast-forward removed | S: `recurring_expense` 0 vs 500 |
| **F18** (promote + pair) | ″ | **probe carrying `reportable_transaction_filter()`**; no probe; split category walk | G · H: 100.00 vs 0 · O |
| **F19** (successor period) | ″ | **probe bounded on `effective_period_date_expr()`**; no probe; no lower bound | C · G · I: 100.00 vs 0 |
| **F20** (partial materialisation) | ″ | **the probe key reduced to `recurring_id`** — `if r.id in materialised_ids: continue` | 0 vs 30.00 |
| **F21** (three templates, one shared date) | ″ | **the probe key reduced to `date`** | 0 vs 7.00 |
| **F22** (distinct ids) | ″ | `cat_recurring` keyed by `r.account_id` or `r.org_id` | breakdown id + `"Unknown"` |
| **F23** (income) | ″ | income summed into `recurring_expense`; income fed into `cat_recurring`; `> today`; `>= p_start` | 300 vs 0 |
| **F24** (`is_active`) | ″ | `is_active == True` dropped from the recurring query | 107.00 vs 7.00 |

**Boundary sweep** (flip one bound at a time, confirm RED). Re-measured against the final tree; the
"RED at" column is the *complete* set of reds, not a sample:

| bound | flip | RED at |
|---|---|---|
| query `next_due_date <= window_end` | `<` | F12 |
| helper `while d <= end` | `<` | F12, F17a |
| helper `while d < start` | `<=` | F12, F13, F17a, F17b, F23 |
| helper `while d < start` | removed entirely | F12, F13, F14, F17a, F17b, F19, F23 |
| probe `Transaction.date >= p_start` | `>` | F16 |
| probe `Transaction.date <= window_end` | `<` | F16 |

## 11. Residuals

- **The lapsed-roster phantom projections for loans and credit cards (F9, G1) are untouched.** They
  are a different mechanism — synthesis horizons, not recurring templates — and TBD-243 §5 announced
  them.
- **`ai_forecast_refine_service` still labels an N-month window "monthly".** Untouched; TBD-243 §5
  announced it; it needs its own design round.
- **~~The collect loop's `max_iterations` is knowingly retained.~~ REMOVED by TBD-286.** It was the
  same defect class as §12 at a more extreme fixture: past 500 occurrences of one template in one
  window the projection truncated **in silence** — no log, no marker — while generation, capped per
  *run* but advancing its frontier every run, materialised all of them, so `forecast_net` moved with
  no user action. It was retained on the argument that its exposure was bounded by the **window
  length**, "which comes from the billing-period roster ... closed periods created through the
  overlap-validated admin endpoint".

  **That premise was false.** `POST /api/v1/settings/billing-period` reads `start_date` and
  `end_date` straight out of an admin request body. `BillingPeriodCreate` validates only their
  ORDER, and the router's second check is for OVERLAP — position, not length. **Nothing caps a
  period's span**, and `compute_forecast` reads `period.end_date` verbatim into `window_end`. A
  decades-long period is one accepted request, so >500 occurrences needs no corrupt data at all.
  The removal is exactly what the retained wording pre-registered: *"If a period roster ever admits
  multi-year windows, delete the budget."*

  Fenced by **F24** (conservation across three generation ticks over a 523-occurrence window; RED at
  `50000.00 != 52300.00` with the cap, and still RED with the constant merely raised to 522) and
  **F25** (the unit half). Raising the constant was never the fix — it moves the boundary and keeps
  the failure mode.

  The "unbounded walk" the cap was also credited with preventing does not exist: the collect loop
  terminates at `end`, and the genuinely widest walk the type system permits — `datetime.date.min`
  (0001-01-01) → `datetime.date.max` (9999-12-31), wider than any `DATE` column can hold, since
  MySQL's floor is 1000-01-01 — was MEASURED in-container at:

  | frequency | occurrences | best-of-5 | `tracemalloc` peak |
  |---|---|---|---|
  | weekly | 521,723 | 0.18s | **21.4MB** |
  | monthly | 119,988 | 0.24s | **4.9MB** |

  ⚠ **Peak, not `getsizeof`.** An earlier revision of this residual and of the `date_utils`
  docstring quoted **3.7MB / 0.8MB**. Those are `sys.getsizeof(out)` over the narrower 1900→9998
  walk — the list object's **pointer array only, excluding the `datetime.date` objects it points
  at**, which are ~4.7x larger than the pointers. Re-measured on the same narrower window the peaks
  are **17.2MB / 3.9MB**. The smaller figure understated the cost ~4.7x, inside a paragraph whose
  whole force is "measured rather than assumed". Do not recorrect it back down.

  What *did* need closing is `advance_date` past `datetime.date.max`, which raises `OverflowError`
  for `timedelta` frequencies and `ValueError` for `relativedelta` ones — already live on the
  uncapped fast-forward since #599. `_next_occurrence` folds that into the same "the grid ended"
  answer as the no-progress guard; **F26** is its fence, and kills both single-exception half-fixes.

  Still open, deliberately out of TBD-286's scope (**TBD-335**): an absurd window makes
  `account_balance_forecast_service` emit one response LINE per projected occurrence. Bounding a
  billing period's span at its writer is the honest place for that, and the limit is a product
  decision. Two reasons it is deferred rather than folded in: it needs `start_date` bounded too (an
  **open** row at 2000-01-01 with a successor in 2026 yields a 26-year window by itself), and it
  does not repair rows already stored.

  ⚠ A third reason was offered and is **struck as unsound**: *"any bound tight enough to matter
  (5 years ⇒ 261 weekly occurrences) bites below 500 and re-creates this defect at a lower
  boundary."* It conflates a **validation rejection at the writer** with a **silent truncation at
  the reader**. A span bound returns a 400 and the over-long window never exists — nothing
  truncates, nothing is hidden, `forecast_net` cannot move; this defect was a reader silently
  returning a short list for a window that does exist. Decisively: `generate_due_transactions`
  materialises on `current_cycle_window(cycle_day, today)` (`recurring_service.py:728`), which is
  **roster-independent**, so bounding a period's span changes nothing about what generation creates
  and the conservation property the removed cap broke is not in play. As written it argued against
  ever bounding anything.
- **`recurring_service.MAX_CATCHUP_ITERATIONS` remains an alias — now a vacuous one (TBD-338).**
  F17a pins the aliasing, not the value; nothing depends on 500 in particular. With the collect
  loop's cap gone there is no second walk left to truncate in step with, so the aliasing buys
  nothing and the constant is purely generation's per-run cap. Retiring it is **TBD-338**, not this
  ticket: the cleanup is *exactly* the mutant F17a's AST guard exists to kill, so the fence has to
  be re-aimed in the same change. F17a's docstring records which of its three claims survive; its
  assertions are unchanged here.

## 12. Review fold — the shared iteration budget, and what a mutation audit added

Two independent reviews landed on the branch after the first revision of this document. Both found
real defects; neither ticket-style "here is the one-liner" framing survived contact with the code.

### 12.1 The finding: the shared budget broke conservation for deeply-stale frontiers

`date_utils.occurrences_in_window` spent **one** `max_iterations` budget across its fast-forward loop
and its collect loop. A weekly template whose frontier sat 521 steps before `p_start` produced:

```
frontier 521w back -> forecast_net across scheduler ticks: ['0', '-500.00', '-500.00', '-500.00']
```

`forecast_net` moved `0 → −500.00` on a scheduler tick with **no user action** — precisely the defect
class this PR exists to remove, reintroduced through the helper. Measured threshold: conservation held
through 495 steps back and broke at 496 (`−400.00 → −500.00`, the collect loop truncated mid-window),
fully at ≥500. Across a 20-case deep-staleness matrix over all five frequencies, **11/20 broke**;
weekly (~9.5 years) and biweekly (~19 years) are the realistic ones.

The fixture is reachable. `POST /api/v1/recurring` has **no past-date guard** on `next_due_date`
(`schemas/recurring.py:15` is a bare `datetime.date`), unlike `promote_to_recurring:733`. A
single-digit year typo — 2016 for 2026 — is 521 weekly steps.

**Why the alias did not deliver what §5 claimed** is set out in §5 itself: generation's cap makes
progress and moves the origin; a projection's cap does not. The claim was true of a single snapshot
and false of the invariant it was offered to support. The wording is corrected in `date_utils.py`,
`recurring_service.py` and §5.

**The fix:** drop the iteration budget from the fast-forward loop only. That loop is inherently
bounded — `advance_date` moves strictly forward for every frequency, the `nxt <= d` no-progress guard
is the real defence, and it terminates at `start`. `forecast_plan_service.py:526` already ships
exactly this shape (`while d < p_start and d <= p_end`, no cap). The collect loop keeps its budget;
§11 records why, and what would force its removal.

### 12.2 F17 was fencing the mechanism, not the property

`test_f17_occurrence_walk_matches_generation_walk` asserted `max_iterations=2` behaviour. It pinned
the *shared-budget mechanism* rather than *conservation under truncation* — so it was the one test
that went red against the fix, and it was red for the wrong reason. Split and re-aimed:

- **F17a** keeps the AST alias guard and the month-end iterated-grid assertions, and replaces the
  `max_iterations=2` assertion with the property that actually matters at the unit level: **two
  frontiers on the same weekly grid, one 1 step back and one 900 steps back, must yield the identical
  occurrence list.** Under the shared budget the far one returned `[]`.
- **F17b** is the fence the defect needed: a 521-step-stale weekly frontier, driven through
  `compute_forecast` and three real `generate_due_transactions` runs, asserting `forecast_net` is
  `-500` at **every** tick. Anti-vacuity: it asserts run 1 materialises exactly
  `MAX_CATCHUP_ITERATIONS` rows and leaves the frontier *still* before `p_start` — the intermediate
  state the old code could not project.

An independent mutation audit instrumented the fast-forward's truncation branch and confirmed it never
fired for any test on the branch: neutering the cap was green. F17b closes that coverage hole.

### 12.3 What the mutation audit added (~40 mutations)

| # | Gap | Fence added | Injected → RED |
|---|---|---|---|
| V1 | the probe's `(recurring_id, date)` key was unfenced on **both** dimensions — every fixture was one occurrence per template on a distinct date | **F20** (one weekly template, 1 of 4 occurrences materialised) and **F21** (three templates on one shared date, one materialised) | `if r.id in materialised_ids` → 0 vs 30.00 · key on `date` alone → 0 vs 7.00 |
| V2 | a right-and-wrong-agree fixture: every id in every seed was `1`, so `cat_recurring[r.category_id]` was swappable for `r.account_id` / `r.org_id` | **F22** + `_skew_ids`, decoys on a *different* org so a mis-key cannot resolve | both swaps → wrong `category_id`, `"Unknown"` |
| V3 | `recurring_income` was asserted by **no test in `backend/tests`** | **F23** (overdue income template conserving across generation) | income into `recurring_expense` → 300 vs 0 · income into `cat_recurring` → non-empty breakdown |
| V4 | `is_active` was unfenced repo-wide (pre-existing; this PR rewrote the query) | **F24** (paused 100.00 + live 7.00, same date) | 107.00 vs 7.00 |
| V5 | two docstring/spec overclaims | wording only — see below | — |
| V6 | F17's AST guard was over-specified: three *genuine* aliases went red | widened in F17a | — |
| V7 | decorative assertions | removed / rewritten | — |

**V5, both verified by execution, both corrected in place:**

- **F10b's docstring** claimed it kills "a SECOND `window_end` computed for the recurring query".
  Injecting exactly that leaves F10b **passing** — its single monthly occurrence at `T-35` sits inside
  both candidate horizons and the next is outside both, so the amount cannot discriminate. The
  mutation *is* caught, by F4, F12, G2 and F10c. Docstring re-aimed at what it actually kills.
- **§6** claimed "F3's breakdown-sums-to-totals assertions go red". F3 seeds no recurring templates
  and cannot see the recurring breakdown. Corrected: the real fences are F16, F18, F14 and F20.

**V6** — the AST guard asserted `isinstance(rhs, ast.Name)` and `rhs.id == "MAX_OCCURRENCE_ITERATIONS"`.
That reds three genuine aliases: `import ... as _CAP`, `date_utils.MAX_OCCURRENCE_ITERATIONS` (an
`ast.Attribute`), and `MAX_CATCHUP_ITERATIONS: int = ...` (an `ast.AnnAssign`, which failed with the
misleading "must be bound exactly once"). The inverse defect
(`reference_over_specified_test_false_red`). Widened: it now resolves import aliases, accepts
`Attribute` and `AnnAssign`, and fences on the RHS containing **no `ast.Constant`** — which still reds
a literal `500` and `int(500)`.

**V7** — `F8`'s `assert len(named) == 12` reds on any legitimate new payload field; replaced with a
floor plus explicit named membership. Tautological "fixture preconditions" (`p_start <= calendar_end`,
F13's `p_start + 1 month > window_end`, F14's two bounds) hold by construction of `_calendar_fallback`
/ `_safe_month_anchor`; dropped, with a comment saying so, so the remaining preconditions are all real.

### 12.4 Not folded

- `test_g1_cc_phantom_payments_multiply_per_cycle_on_lapsed_roster` fails on the **5th of every
  month** (`assert 3 == 2`, `payment_day=5`). Verified byte-identical to `main`; inherited, filed
  separately, deliberately untouched here.

### 12.5 Test count

Measured against the final tree, full backend suite, isolated compose project:

```
3547 passed, 12 skipped, 59 warnings in 1403.36s (0:23:23)
```

⚠ **The pre-fold baselines quoted during review do not reconcile with each other**, and this number
supersedes all of them. The PR body said `3492 passed`; one reviewer measured `3513 passed, 12
skipped` on branch head; the mutation audit reported "green across all 3525 backend tests". This fold
adds five new fences and splits F17 into two, i.e. +6 collected tests — which reconciles with none of
the three. Only the number above was measured against the tree that is committed; the earlier figures
should not be carried forward.
