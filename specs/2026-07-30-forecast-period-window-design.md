# One window for the forecast surfaces (TBD-243)

Status: design settled 2026-07-30. Two independent architects, one concede/defend round.
**Both architects reversed their opening position and converged on the same expression.**
Branch `TBD-243-split-bound-horizon`. Backend only. No migration, no schema change, no frontend
change.

---

## 0. The ticket's prescription is OVERRULED. Its defect is real.

TBD-243 asks to *separate* the settled-sum bound from the projection horizon at three sites. **That
is the one thing this PR must not do.** The premise — that these sites derive a period end that
overshoots the successor's window — is true and verified. The prescribed remedy introduces a worse,
quieter defect.

Recorded because the ticket, its title, and spec §7's follow-up sentence
(`2026-07-28-open-period-spend-window-design.md:633-635`) all say "separate them". **This spec
overrules that follow-up.** The PR title must describe what actually ships, not the ticket's name.

Three further corrections to the ticket text:

- **Its stated trigger population is wrong.** It claims adopting the spend window would "zero the
  recurring contribution for lapsed orgs, gutting the forecast tile". On a truly lapsed org the
  recurring contribution is **already zero on `main`** — `forecast_service.py:124-125` requires
  `next_due > today AND <= p_end`, and the fallback `p_end` is in the past, so the conjunction is
  unsatisfiable. Measured: lapsed roster returns `executed=0 pending=0 recurring=0`. Nothing gets
  gutted; the tile is *already* broken. The zeroing the ticket fears attaches to off-grid rosters.
- **It under-counts the uses of `p_end` by five** (`forecast_service.py:155`, `:171`, `:182`,
  `:222`; `account_balance_forecast_service.py:218`, `:302`).
- **It labels the work M–L across three sites.** Under this ruling it is two sites and a small diff.

## 1. The defect, measured

Three rosters, real helpers, run in the isolated stack (today = 2026-07-30):

| roster | open start | calendar fallback | `period_effective_end` | `period_spend_window_end` | main `executed_expense` |
|---|---|---|---|---|---|
| **lapsed, on-grid** | 04-30 | 05-29 | 05-29 | **07-30** | **7.00** — today's 100 dropped |
| **off-grid** | 07-10 | 08-09 | 07-24 | 07-30 | 107.00 |
| **roster tail** | 07-25 | 08-24 | **None** | **None** | 107.00 |

Two distinct live defects:

1. **Lapsed staleness (the bigger one, and not what the ticket is about).** A settled row dated
   today is dropped from `GET /api/v1/forecast`, and a €250 PENDING bill dated today is invisible in
   `GET /api/v1/forecast/account-balances` (`pending_delta` 0.00 where it should be −250.00). The
   lapsed org's forecast tile is months stale in both halves, today, in production.
2. **Off-grid overlap (what the ticket is named for).** Open `[2026-07-10, NULL)` with stub
   `[2026-07-25, 2026-08-24]` gives `p_end = 2026-08-09`, swallowing `07-25 … 08-09` of the stub's
   window. Reachable: a manual UI close sends no date, so `requested = today − 1` and
   `new_start = today`, off-grid unless today is the cycle day — roughly 29 closes in 30.

## 2. Why the split is rejected — the conserved-identity argument

At each site the backward sum and the forward projection are **two halves of one total joined by a
materialisation event**. A recurring template counted in `recurring_*` becomes a transaction counted
in `pending_*` when `generate_due_transactions` materialises it. Crucially, materialisation runs on
`current_cycle_window(cycle_day, today)` (`recurring_service.py:266,275`), which is
**roster-independent** — it does not respect any window this code computes.

With **one** window `W`, an obligation **whose `next_due_date` is in the future** is either in
`(today, W]` before and `[start, W]` after (conserved), or beyond `W` on both sides (conserved at
zero). **Two windows open a gap `(horizon, W]` that the materialisation window reaches into, and the
obligation lands in neither bucket before and one bucket after.** `forecast_net` then moves with no
user action.

> **⚠ Correction (2026-07-30, review fold). The dichotomy above is not exhaustive, and the
> conservation claim is narrower than this section originally stated.**
>
> There is a third case, `[p_start, today]`. An **overdue** template (`next_due_date <= today`) is
> excluded from `recurring_*` by `forecast_service.py:165`'s own `> today` gate, but
> `generate_due_transactions` materialises it into `pending_*` regardless. `forecast_net` therefore
> moves 0 → −100.00 across generation for an overdue template **on any window**, this design's
> included.
>
> **This break is INHERITED, not introduced.** Measured on `main` at the same commit: 0 → −100.00 on
> a healthy on-grid roster, where this PR is byte-identical to `main`. Measured on this design:
> 0 → −100.00 on both a healthy on-grid roster and a lapsed one. It is a property of the `> today`
> gate, not of the window, and it was pinned by
> `test_g2_guard_overdue_template_breaks_conservation_on_both_designs`.
>
> **⚠ SUPERSEDED by TBD-260** (`specs/2026-07-30-forecast-overdue-recurring-design.md`). The `> today`
> gate is gone; `recurring_*` now bounds the OCCURRENCE by `[p_start, window_end]` and an
> anti-double-count probe keyed on `(recurring_id, date)` suppresses occurrences that are already
> rows. The third case is closed and conservation is **general**. That guard is now the fence
> `test_g2_overdue_template_conserves_forecast_net_on_both_rosters`, asserting
> `(−100.00, −100.00)` on both rosters. See the "Accepted residuals" entry below for why both of this
> section's hand-off claims — "product question" and "bound it below by `p_start`" — were wrong.
>
> **This does NOT rescue the split.** The break the split introduces is on a **future-dated**
> template — the case one window genuinely does conserve and two windows do not — so it is a
> *separate* break that this design does not have and F4 still fences. The ground for overruling the
> ticket stands; it is simply narrower than "conservation is a property of using one window". The
> honest statement is: **conservation holds for templates due in the future; it holds under one
> window and fails under the split. For overdue templates it fails under both, for a reason that
> predates this PR.** Whether the overdue case is worth repairing is a separate question — see §5.
> *(TBD-260 answered it: yes. Conservation is now general and the "honest statement" above is history.)*

Both architects proposed a split; both then reproduced the break against their own design.

**Break in the split design** (`horizon = min(derived, fallback)`, `window = max(derived, today)`),
verified by execution — the mirror case, off-grid **late** successor, `fallback < derived`:

```
p_start 07-10, cycle_day 25 -> derived 08-24, fallback 08-09, today 08-15
horizon 08-09, window 08-24, gap (08-09, 08-24]
materialisation window [07-25, 08-24] reaches into the gap
template due 08-20:  recurring needs due <= horizon(08-09) -> excluded
                     pending   needs date <= window(08-24) -> included after generation
forecast_net 0 -> 100.  CONSERVATION BREAK.       main: conserved.
```

The split design is also **self-inconsistent** on that roster: it reports `period_end = 08-24` while
its projection stops at 08-09.

`test_forecast_parity_after_generate` **cannot** catch this: its org seeds no `BillingPeriod`, so
`get_current_period` auto-creates a roster **tail**, where the split collapses. Green there was
offered as the design's strongest check and was worth nothing — the vacuity pattern applied to the
reasoning itself.

**Corollary that settles it.** The split forces every anti-double-count guard onto the window
anyway: the loan `already_paid` probe (`account_balance_forecast_service.py:211-219`, whose code
comment at `:207-210` already warns "DO NOT add a status filter here or a pending loan payment
double-counts against pending_delta"), and CC's `p_k_owned`. Once every guard is on the window, the
horizon buys nothing but the gap.

## 3. The design: ONE value

```python
today = today if today is not None else datetime.date.today()      # ONE clock read per request

if period.end_date is not None:
    window_end = period.end_date                                   # closed: verbatim, never floored
else:
    derived = await period_spend_window_end(db, org_id, period, today=today)
    window_end = derived if derived is not None else (
        p_start + relativedelta(months=1) - datetime.timedelta(days=1)   # roster tail
    )
```

`window_end` is used for **every** backward sum **and** every forward horizon at the site. There is
no second value and no second name.

This is `period_spend_window_end` verbatim, with the roster tail falling back to the calendar
expression. It adds **no fourth derived-end shape** — the boundary model's three shapes are
untouched, which is why no differential fence is owed.

**The tail fallback is forced, not chosen:** `forecast_service.py:135` and `:182` run
`while d <= p_end`, which cannot terminate on `None`. It also makes `period_end` non-null trivially,
so no schema change is needed.

**The clock must be injected and read once.** `compute_forecast` and
`compute_account_balance_forecast` gain `*, today: datetime.date | None = None`, and
`forecast_service.py:119`'s existing `date.today()` must consume the **same** value. Two independent
clock reads in one computation is the straddle trap TBD-240's D6 exists to prevent, and
`test_forecast_parity_after_generate` asserts a conservation invariant **across two calls** — a
bucket that moves because the clock ticked between them breaks it.

### 3.1 Behaviour by roster, verified

| roster | main | ships as |
|---|---|---|
| closed period | `end_date` | **byte-identical** |
| healthy on-grid | fallback == derived | **identical** |
| roster tail | fallback | **byte-identical** |
| **lapsed** | stale past date | **floors at today — the staleness fix** |
| **off-grid, successor not yet started** | overshoots into stub | **stops at successor − 1 — the overlap fix** |
| **off-grid, late successor** | conserved | **conserved** (the split was not) |

### 3.2 Sites

**IN — two sites.** `forecast_service.py:61` (uses at `:77`, `:88`, `:102`, `:113`, `:124`, `:135`,
`:155`, `:171`, `:182`, `:222`) and `account_balance_forecast_service.py:67` (uses at `:96`, `:170`,
`:218`, `:240`, `:302`). The category-breakdown queries at `:155`/`:171` **must use the same local**
as the totals or the breakdown stops summing to the total.

**OUT — `forecast_plan_service.populate_from_sources` (`:440`). Comment only, zero code change.**
It is a **write** path: its window feeds `master_monthly["__current__"]`, which is divided by
`len(months)` as **one month slot**. Widening it to today on a lapsed org counts three months of
spend as one month and inflates every `planned_amount`. That entanglement is a separate design
question (weighting the current slot by elapsed months), not a bound/horizon question. `:512` is a
loop-**termination** guard (`d < p_start and d <= p_end`; given non-inversion the second clause is
dead on every reachable input) and must not be rewired — binding it to a `None` raises `TypeError`
in a `while` header, and binding it to an earlier date silently truncates to zero. `:611` mixes
SETTLED+PENDING and feeds a suggestion, not a reported actual.

**The existing comment at `forecast_plan_service.py:311-317` must be updated in this PR.** It
currently tells the reader the third expression is deliberate and points at spec §7's "named
follow-up". That follow-up is overruled; the comment must say so and point here.

### 3.3 Response contract

**`period_end` emits `window_end`. Schema unchanged: required, non-null, no `Optional`, no second
field.**

`period_end` is **not** read-free, contrary to the ticket's framing: `ai_forecast_refine_service.py`
puts `baseline["period_end"]` into the LLM prompt payload at `:267` and re-emits it at `:356`/`:649`
as `RefinedForecastResponse.period_end`, a required `StrictStr`. Zero *frontend* reads is true
(six declarations, no consumers; the only live `.period_end` read repo-wide is
`recurring/page.tsx:429-430`, a different payload) — zero live reads is not. This also kills
"delete the field".

A second field is scope creep: spec §7 already declined the analogous `BudgetResponse.window_end`
because nothing consumes it. If the divergence needs observing, it belongs in a log line.

## 4. Test plan

Coverage baseline, verified: **two of the three sites have ZERO coverage of the fallback branch.**
All 39 tests in `test_account_balance_forecast_service.py` seed a CLOSED period (`:138-139`); every
`populate_from_sources` test seeds a closed period. The only test touching an open fallback row is
`test_forecast_parity_after_generate.py:47-80`, whose org is a roster **tail**.

Every test is labelled `fence` (fails against a named wrong implementation) or `guard` (regression
net). Inject `today` explicitly; never let the service read the wall clock in a test. Anchor
fixtures to `today ± n` (`reference_wall_clock_date_bomb_tests`).

| # | type | fixture | assertion | wrong implementation killed |
|---|---|---|---|---|
| **F1** | fence | **lapsed**: open `[T−3mo, NULL)`, historic stubs; SETTLED 100 dated `T`, 7 dated `T−3mo+3d` | `executed_expense == 107`; `period_end == T` | `main` (returns 7.00 / `T−3mo+1mo−1d`) |
| **F2** | fence | **lapsed**, PENDING 250 dated `T`, via `compute_account_balance_forecast` | `pending_delta == -250.00`; `expected == 750.00` | `main` (0.00 / 1000.00) |
| **F3** | fence | **off-grid, successor not started** (Case B): open `[T−20, NULL)`, stub `[T−5, T+25]`; SETTLED 7 @`T−19`, 50 @`T`, 100 @`T+3`; PENDING 9 @`T+3` | `executed_expense == 57`; `pending_expense == 0`; **and** the category breakdown at `:155`/`:171` agrees | bound left on the fallback (`T+11`) → 157; `<` for `<=` → 7; totals rebound but `:155`/`:171` missed → caught only by the category assert |
| **F4** | **fence — the conservation fence** | **off-grid, LATE successor** (Case A): `p_start` off-grid such that `fallback < derived`; template due inside `(fallback, derived]` and inside the materialisation window | `forecast_net` **unchanged** across `generate_due_transactions`; the template counted exactly once on each side | **the split design** (`horizon = min(derived, fallback)`, `window = max(derived, today)`) → net moves 0 → 100. Red against the split, green against this design. **No test in the repo can catch this today.** |
| **F5** | fence | **roster tail**: single open row, no successor; SETTLED far past the fallback. Driven through the ROUTER (`TestClient`) so `response_model` is the gate | 200 not 500; `period_end == p_start+1mo−1d`; totals equal the pre-change values | `window_end = period_spend_window_end(...)` with no `None` arm → `ArgumentError` / `TypeError` / `AttributeError`. Verified: SQLAlchemy **refuses to compile** `col <= None` (it is not a silent zero), and `None.isoformat()` raises |
| **F6** | fence | **closed** period `[T−90, T−60]`; SETTLED row dated `T` | row NOT counted; `period_end == T−60` | flooring hoisted **above** the `period.end_date is not None` check → re-opens reported history for every org. The single most plausible refactoring slip |
| **F7a/F7b** | fence pair | lapsed fixture, injected `today == derived` and `today == derived + 1d` | floor does **not** fire / **does** fire | `max` written as `>` vs `>=`, or applied unconditionally. **A boundary pinned from one side is not pinned.** |
| **F8** | guard | **healthy on-grid** at `today = p_start` and `today = calendar_end` | **named fields** equal the pre-change values | any implementation that moves the healthy fleet |
| **F9** | ~~guard~~ **fence** *(rewritten in the review fold — see below)* | **lapsed** org, untracked loan whose `first_payment_date` is `T−40`, i.e. **past the calendar fallback** and inside the widened window | the emitted list is exactly `[{pmt, T−40}]`, and the source's expected balance drops by `pmt` | the loan synthesizer left on the calendar fallback (`p_end = p_start+1mo−1d`) → `loan_payments == []` |
| **F10** | fence | `compute_forecast(..., today=T)` with `T = real_today − 40d`; SETTLED @`T` and @`T+1` | `T` included, `T+1` excluded | dropping `today=today` on the helper call; leaving a bare `date.today()` at `:119` (two clocks) |
| **F11** | fence | **lapsed** org, untracked loan (F9's fixture) **plus a reciprocal payment-in leg dated exactly `window_end`** | `loan_payments == []`; source's expected == its balance | the `already_paid` probe left on the calendar fallback → phantom on top of a recorded payment. Also kills `<` for `<=` on the probe's own upper bound |
| **F12** | fence | **off-grid, LATE successor** (`window_end = T+19`, future); one row ON `window_end` and one on `window_end+1` in **every** bucket: settled income/expense, pending income/expense, a recurring template due exactly `window_end` | each bucket counts the boundary row and excludes the row past it, totals **and** per-category | `<` for `<=` at any of the nine `forecast_service` upper bounds (settled income/expense, pending income/expense, recurring gate, recurring loop, and the three per-category equivalents) |
| **G1** | guard **+ fence** | **lapsed** org; CC `close_day=10, payment_day=5, payment_day_relative_month=1`, balance −900.00, three 300.00 charges, no payment legs, source 5000.00 | **TWO** past-dated phantom payments summing to 600.00; source 5000.00 → 4400.00 | *(guard role)* pins the §5 CC residual as it actually behaves. *(fence role)* the CC synthesizer left on the calendar fallback → `cc_payments == []` |
| **G2** | guard | an **overdue** template (`next_due <= today`) on a healthy on-grid roster **and** on a lapsed one | `forecast_net` 0 → −100.00 across `generate_due_transactions` on both | nothing — green on `main` too, by design. Records the §2 correction so it is not rediscovered |

**F9 was vacuous as originally shipped and has been rewritten.** Its assertion was
`len(payments) == 1` against a fixture whose `first_payment_date` sat *inside* the calendar
fallback. `synthesize_account_loan_payment` returns `dates[0]` — a 0-or-1 element list **by
construction** — so the assertion could not detect the multiplicity it claimed to pin, and the
fixture kept it green with the loan horizon left on the fallback as well. It was the **eighteenth**
instance of this repo's signature defect. The rewrite moves `first_payment_date` past the fallback
and asserts the emitted list and the money that moved; the multi-date span the window now covers is
asserted through `due_loan_payment_dates` as an explicit fixture precondition.

**Three of the five `account_balance_forecast_service` sites shipped unfenced** and were caught in
the same fold: the loan `already_paid` probe, the CC synthesis horizon and the loan synthesis
horizon each stayed green under 86 forecast/balance tests when reverted to the calendar fallback one
at a time. F9, F11 and G1 now fire on exactly one of those three injections each.

**F4 and F3 are the two that do not exist in any form today** and are the reason this PR is safe to
ship. **F8 carries the highest vacuity risk** — it passes under almost every implementation; label it
`guard`, never count it as coverage. **F8 also carries the highest over-specification risk**: assert
**named fields**, never a full-payload dict comparison across two clock reads
(`reference_over_specified_test_false_red`).

**Boundary sweep, mandatory.** Flip `<= window_end` to `< window_end` **one site at a time** across
both files and re-run the forecast/balance suites. Before the review fold this left **8 of 11**
upper bounds green (`forecast_service.py` settled-income, pending-income, pending-expense, the
recurring gate, the recurring loop, all three per-category queries, and
`account_balance_forecast_service`'s loan `already_paid` probe). F12 and F11 close all eight; the
sweep now goes RED at every one of the eleven. *A boundary pinned from one side is not pinned* —
F3's `pending_expense == 0` assertions are `0 == 0` and could never have detected any of this.

**Injection gate, mandatory:** for every `fence`, inject the named wrong implementation, observe
**RED**, restore, and record the command and output. Green-against-unmodified-`main` is this repo's
most-repeated defect (17 instances). **No AST guard** — the conservation fence F4 is strictly
stronger and cannot miss a construction shape, which is the failure recorded in
`reference_analysis_domain_vs_display_window` §3.

**`test_forecast_parity_after_generate.py:78` must stay green, UNMODIFIED.** Its org is a roster
tail, where this design is byte-identical to `main`. If the implementer finds it must be edited,
that is a **stop signal**, not a licence — changing a test to make a change pass is how a fence
becomes decoration.

## 5. Residuals, announced

**This PR changes user-visible numbers.** It must say so; framing it as a refactor would guarantee a
support ticket.

> For a billing period that has not been closed on schedule, the forecast and month-end account
> projection now count through today instead of stopping at a date in the past. Affected
> organisations will see previously-missing settled and pending amounts appear.

Three behaviour changes to name explicitly:

1. **Lapsed orgs: recorded sums widen** to run through today (measured 7.00 → 107.00). This is the
   largest change and the real deliverable.
2. **Off-grid orgs: the window narrows.** ⚠ *Corrected in the review fold — the original wording
   ("narrows to the successor's start") was true only for the case §3.1's table already qualifies,
   `successor not yet started`.*

   The window is `max(derived, today)` on an open row, so which of the two bounds wins depends on
   where today sits:

   - **Successor has not started yet** (`derived >= today`): the window narrows to
     `successor_start − 1`. This is the overlap fix TBD-243 is named for.
   - **Successor has ALREADY started** (`derived < today`): **the floor overrides and the window
     runs to today, which is LATER than the successor's start — overlap remains.** Measured: open
     `[T−20, NULL)`, successor `[T−5, T+25]`, `derived = T−6`, shipped `period_end = T`. **Six days
     of the successor's window are still double-counted** (down from 10 on `main`, whose
     `period_end` was `T+9`). The overlap is *reduced*, not eliminated, and this PR must not claim
     otherwise. It is the same residual as the first bullet under "Accepted residuals" below, and it
     shrinks to zero as `BillingCloseJob` converges the roster.

   In **both** sub-cases the org **loses its forward recurring projection** on the open period
   (100 → 0), because `next_due > today AND <= window_end` is unsatisfiable once `window_end` is not
   past today. *(TBD-260 removed the `> today` conjunct; the projection is now bounded by
   `window_end` alone, so on a lapsed roster the open period keeps whatever occurrences fall in
   `[p_start, window_end]` — which, with `window_end == today`, is the overdue ones. The forward loss
   described here still holds for occurrences past today; it is the roster boundary, not the clock.)*
   The amount is not destroyed — it appears on the successor's forecast, where it
   belongs. Both candidate designs paid this identically; it is the unavoidable price of respecting
   the roster boundary at all.
3. **Roster-tail and closed and healthy on-grid orgs: unchanged.**

**Accepted residuals:**

- On an unclosed roster the open period's forecast now overlaps its historic stubs' own forecast
  views — the same residual TBD-240 §2.3 accepted for budgets, shrinking to zero as
  `BillingCloseJob` converges.
- **Phantom projections on a lapsed roster (F9's and G1's subject).** `due_cycles_in_horizon` and
  `due_loan_payment_dates` scan `[p_start, W]` with no `> today` gate, so on a lapsed org they walk a
  months-long window and emit past-dated projected payments. `already_paid` and `p_k_owned` suppress
  this for any obligation with a recorded leg; an **untracked** obligation still projects phantoms.
  Pinned by F9 (loan) and G1 (CC), not fixed here. Do **not** add a `> today` gate to `:170`/`:240` —
  CC and loan use clock-free dedupe rather than `forecast_service`'s `> today` gate, and a past-due
  but genuinely unpaid instalment **must** still be projected; adding the gate would delete real
  obligations.

  > **⚠ The CC case is larger and more visible than the loan case, and the original text announced
  > only the loan case.** Corrected in the review fold.
  >
  > A loan emits **at most one** phantom per period — `synthesize_account_loan_payment` returns
  > `dates[0]`, a 0-or-1 element list by construction. A credit card emits **one per cycle**:
  > `due_cycles_in_horizon` returns every cycle whose `payment_date` falls in the window, and the
  > widened window spans several. Measured on a lapsed roster (open `[T−3mo, NULL)`, stubs at
  > `T−2mo`/`T−1mo`; card `close_day=10, payment_day=5, payment_day_relative_month=1`, balance
  > −900.00, three 300.00 charges, no payment legs, source Checking 5000.00):
  >
  > | | `main` | ships as |
  > |---|---|---|
  > | projected CC payments | none | **`2026-06-05` 300.00 and `2026-07-05` 300.00 — both past-dated** |
  > | source `expected_month_end_balance` | 5000.00 | **4400.00** |
  >
  > The multiplication is **bounded by the outstanding balance**, not unbounded: `s_prev` threads
  > each synthesized outflow forward inside `synthesize_account_cc_payments`, so the projected
  > payments sum to the balance owed at the last projected close (600.00 of charges closed), never
  > to a multiple of it. That bound is what makes this announceable rather than a defect.
  >
  > It is user-visible: `frontend/components/dashboard/widgets/CreditUtilizationWidget.tsx:94` renders
  > "Next payment … on `<date>`" straight off this list, so a lapsed org sees a payment date weeks
  > in the past. **The PR description must say so.** Still no `> today` gate, for the reason above.

- ~~**An overdue recurring template moves `forecast_net` across `generate_due_transactions`, on any
  window.**~~ **RESOLVED by TBD-260** → `specs/2026-07-30-forecast-overdue-recurring-design.md`.

  The original text: the `> today` gate at `forecast_service.py:165` keeps an overdue template out of
  `recurring_*` while `generate_due_transactions` puts it into `pending_*`; measured 0 → −100.00 on
  this design and on `main`; pinned by G2; not fixed here.

  **Two things in that hand-off were wrong, and the follow-up says so:**

  1. *"a product question about what 'upcoming' means, not a bounds question."* It is a **bounds
     question**, and this very document already ruled the opposite way for the loan and CC surfaces
     at §5 (*"a past-due but genuinely unpaid instalment must still be projected; do **not** add a
     `> today` gate"*), while `forecast_plan_service.populate_from_sources:509-536` already ships the
     clock-free fast-forward. Three surfaces to one. `forecast_service.py:165` was the outlier.
  2. The follow-up ticket TBD-260 prescribed bounding the query *"below by `p_start`"*
     (`next_due_date >= p_start`). That is a **null fix**: `next_due_date` is a **frontier**, not an
     occurrence date, so it leaves the ticket's own headline scenario broken in the same direction
     and by the same amount (0 → −100.00). The bound has to be on the **occurrence**.

  What shipped: the recurring query lost its lower bound entirely, the occurrence grid is walked
  iteratively with `date_utils.occurrences_in_window` over `[p_start, window_end]`, and an
  anti-double-count probe keyed on `(recurring_id, date)` — generation's own create-condition,
  negated — suppresses occurrences that are already rows. `forecast_net` is now conserved across
  `generate_due_transactions` on **every** roster, not only for future-dated templates.

  G2 was rewritten from a guard into a fence (`(−100.00, −100.00)` on both rosters, values asserted,
  never `before == after`). F8 was promoted from guard to fence: with no clock predicate left in the
  recurring path, the whole payload is now identical across two injected clock values. F4's "scope of
  the conservation claim" paragraph was rewritten — conservation is general now, not narrow. F12
  gained the lower-bound coverage it never had (it pinned nine upper bounds and zero lower bounds).

  TBD-260 also fixed a **live double-count on `main`** as a side effect: `promote_to_recurring` sets
  `tx.recurring_id` on the source row and the UI sends `next_due_date == tx.date` for a future-dated
  row, so `main` counted such a transaction in `recurring_*` **and** in `pending_*`. Unfenced before;
  fenced now by F18/F16.

- **`ai_forecast_refine_service` labels an N-month window "monthly". NOT fixed here — it needs a
  design round, exactly like `populate_from_sources` (§3.2).**

  `refine_forecast` calls `compute_forecast` and puts `period_start`, `period_end` and
  `forecast_expense` into the LLM payload (`:266-269`) under a system instruction that calls it
  "their baseline **monthly** forecast" (`:212`), alongside a `{timeframe}`-month history of
  **per-month** aggregates. `_apply_adjustments` then multiplies each baseline category by the
  model's 0.5–1.5 seasonal factor and reports the product as `refined_forecast_expense`.

  Measured on a lapsed roster: `main` sent a **31-day** window with `forecast_expense` 7.00; this
  design sends a **91-day** window with 357.00, still labelled monthly. **No test covers the prompt
  payload's window at all.**

  **Ruled: record, do not fix.** Relabelling the prompt is not the low-risk half it looks like. The
  arithmetic mismatch is not in the label — it is that a 3-month bucket is handed to a model asked to
  infer seasonality from 1-month history rows, and the multiplier it returns is applied to that
  bucket verbatim. Making the prompt honest changes the model's output distribution on a path with
  **no deterministic assertions anywhere**, which is a behaviour change dressed as a copy edit.
  Normalising the window properly (divide the current bucket by elapsed months? clamp the AI path's
  window to one month? weight the history?) is the same entanglement — an N-month window landing in a
  one-month slot — that §3.2 used to keep `populate_from_sources` out of scope, and it deserves the
  same treatment: its own design round.

  **Follow-up ticket, one line:** *`ai_forecast_refine_service` hands `compute_forecast`'s
  period window to the LLM as a "monthly" baseline and applies monthly-history seasonal multipliers
  to it; on a lapsed org that window is now ~3 months, so the refined forecast is scaled from a
  bucket the prompt misdescribes.*

- **`billing_service.get_current_period` reads the wall clock in its auto-create arm** (`:103`,
  `datetime.date.today()`), and takes no `today` parameter. For an org with **no** open period row,
  the start date of the row it creates is therefore not governed by an injected clock, so
  `compute_forecast(..., today=T)` is not fully authoritative on that path. **Zero production
  impact** — production always passes the real clock — but it does weaken injected-clock tests for
  orgs with an empty roster, and any future test that injects `today` and expects the auto-created
  period to follow it will be quietly wrong. Not fixed here: threading `today` through
  `get_current_period` touches every caller and belongs with the roster work (TBD-235), not with a
  window change.

- **Roster-tail divergence between `budget_service._compute_spent` and the forecast surfaces is
  pre-existing and unchanged by this PR.** `_compute_spent` treats a `None` derived end as
  **unbounded**; the forecast surfaces substitute the calendar fallback. The two therefore disagree
  on a roster tail, before and after this change alike. Its right home is the named follow-up at the
  end of this section — **delete the calendar fallback once the roster is guaranteed converged** —
  because the divergence exists only because the fallback exists. No separate ticket.

**Explicitly out:** `populate_from_sources` (§3.2, own follow-up); any `> today` gate on CC/loan; any
new response field; any schema or `Optional` change; roster repair / `ensure_future_periods`
re-anchoring (TBD-235); the pre-#588 snapshot backfill (TBD-240 D9); any migration.

**Named follow-up, worth a ticket.** The calendar fallback is a fourth definition of a period end,
invented independently at three call sites, and it exists for exactly one reason: `period_effective_end`
returns `None` on a roster tail. If the roster were guaranteed converged (TBD-241's `BillingCloseJob`
plus TBD-235's anchor repair), a live org would always have a successor, the fallback could be
**deleted outright**, and `window_end` would simply be the derived end. That is the subtraction that
beats this refactor; its blocker is roster convergence, not this code. Do not attempt it now —
deleting the fallback today makes `period_end` null for every fresh org.

## 6. Sequencing

One PR, two sites: they feed two tiles on one dashboard screen, and shipping one without the other
puts a visible contradiction between them on a lapsed org. Tests first at each step.

1. Inject `today` into both entry points, resolve once. (F10)
2. Introduce `window_end` initialised to the **old fallback verbatim**. Pure rename, zero behaviour
   change, whole suite green — **this is the checkpoint.**
3. Rebind every predicate at both sites to `window_end`, including `:155`/`:171` and `:218`. (F3, F8)
4. Switch the open-row branch to `period_spend_window_end` with the `None` arm. (F1, F2, F5, F6, F7)
5. Add the conservation fence and the phantom guard. (F4, F9)
6. Update the `forecast_plan_service.py:311-317` comment.

## 7. Sign-off record

Two independent architects, full authority, 2026-07-30, plus one concede/defend round. **Both opened
on different designs and both withdrew them.**

- Architect 1 opened on the split (two locals, `horizon = min(derived, fallback)`), then reproduced
  the conservation break against their own design on a measured roster and conceded. They also
  retracted their own "gate zero" argument as self-inflicted vacuity, and observed that their
  `:218` fix had been solving a problem the split itself created.
- Architect 2 opened on `max(calendar_end, derived, today)`, then withdrew it on the ground that it
  **does not fix the defect TBD-243 is named for** — on the ticket's own roster it keeps swallowing
  the successor's window — and is therefore "a different ticket that should not wear this number".
  They also found the mirror-case conservation break (Case A) that Architect 1 had not, and
  **refuted a hypothesis put to them by the coordinator** rather than adopting it (the
  `derived <= today` gap is not real: the recurring gate's own `> today` already excludes it).

Convergence was on the expression neither opened with. Unanimous on: the ticket's causal claim being
refuted; the lapsed staleness being the deliverable; `populate_from_sources` out of scope; closed
rows never floored; `period_end` required non-null with no second field; and both fences F3 and F4
shipping regardless of design.

## 8. Correctness-review fold (2026-07-30, post-PR)

An independent correctness review of PR #596 ran the real services and found the design's test plan
and its residual section both weaker than they read. Every finding below was reproduced against a
running stack before it was folded, and every fence added carries an injection gate.

**Test-quality defects fixed:**

- **F9 was vacuous** — `len(payments) == 1` against a synthesizer that returns a 0-or-1 element list
  by construction, on a fixture where the horizon could be reverted to the calendar fallback and the
  test stayed green. Eighteenth instance of this repo's signature defect. Rewritten as a real fence
  (§4).
- **Three of the five `account_balance_forecast_service` sites shipped unfenced** — the loan
  `already_paid` probe and BOTH synthesis horizons each stayed green across 86 forecast/balance tests
  when reverted one at a time. F9, F11 and G1 now fire on exactly one of the three injections each.
- **Eight of eleven upper bounds were pinned from one side only** — closed by F11 and F12 (§4's
  boundary sweep).

**Over-claims corrected:**

- §2's conservation claim was too strong; it omits the overdue-template case, which breaks on
  `main` too.
- §5's "the window narrows to the successor's start" held only for `derived >= today`; when the
  successor has already started the floor wins and overlap remains.
- §5 announced past-dated phantoms for the loan case only; the CC case multiplies per cycle and is
  rendered on the dashboard.

**Residuals newly recorded, not fixed:** the overdue-template conservation break (own ticket),
`ai_forecast_refine_service`'s "monthly" label on an N-month window (own ticket),
`get_current_period`'s wall-clock read in its auto-create arm, and the pre-existing roster-tail
divergence with `budget_service._compute_spent`.

**No design ruling was overturned.** One window still ships; the split is still rejected; the
ticket's prescription is still overruled — on a narrower but sound ground.
