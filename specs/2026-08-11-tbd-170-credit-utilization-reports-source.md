# TBD-170 — credit-card utilization as a Reports source

Status: ready to build
Ticket: https://fjconsulting.atlassian.net/browse/TBD-170
Design: two independent architects + one concede-or-defend round (2026-08-11).
Full consensus on all eight forks.

## Phase 0 — half this ticket was already shipped

Verified at file:line before any design. The original description ("utilization
is currently only a quiet text subline on the accounts row", owner feedback
2026-07-22) is **false**.

| Original DoD | Status |
|---|---|
| Utilization as a chart, not text | ✅ shipped — `CreditUtilizationBar.tsx`, a banded Recharts bar on theme tokens |
| Available as a Dashboard tile | ✅ shipped — `dash_cc_utilization`, wired at all four registration sites |
| Available as a Reports source | ❌ **this spec** |
| Forecast CC payment line elsewhere | → split to TBD-378 (needed a product ruling, not code) |

Nothing utilization-shaped exists in the reports layer: `Dataset` has four
members, no source publishes a `percent` measure, and `grep -rni utilization`
over `backend/` returns nothing.

## ⚠ This source is not like the other four

Both architects independently flagged that the ticket's "queried, filtered …
like any other source" is undeliverable. It is the **least** like the others of
anything in the registry:

- no time dimension (F2 — impossible, not merely unscoped)
- no date filter, no status, no category, no tags
- a mandatory currency partition
- a percentage that resists aggregation and bypasses the generic agg machinery

Sizing it as an `accounts.py` clone will under-scope it.

## Rulings

### F1 — Row = one qualifying credit-card account, point-in-time

Dimensions: `account`, `currency`, `account_active`. **All three already exist**
(`reports_enums.py:67,69,70`) and are already keyed in the exhaustive
`DIMENSION_HEADERS` (`series.ts:36,44,45`). **Zero new `Dimension` members.**

No `account_type` dimension: by F5 every row is a credit card, so it is
degenerate — one bar, always.

`account_active` is published as **both dimension and filter**, and the two are
coupled: because F5 keeps inactive cards in the row set by default, a filter
alone would let a user *remove* closed cards but never *see* that the number in
front of them blends live and closed credit lines. PRODUCT.md: "status is data …
never collapse them into a single number when the difference is the whole
point."

### F2 — No time dimension, and none is possible

Established from code, not assumed:

- **Historical balance IS reconstructible** — `networth.py:183-236` rebuilds it
  from `Account.opening_balance` plus settled `balance_contribution_filter()`
  deltas.
- **Historical credit limit is NOT.** `Account.credit_limit` is a plain mutable
  `Numeric(12,2)` (`models/account.py:91-93`) overwritten in place at
  `routers/accounts.py:453-454`. There is no snapshot/history table anywhere in
  `models/`, and a limit change writes no audit row (the only account audit
  event is `account.opening_balance.update`, `routers/accounts.py:352`).

A series would therefore compute `historical_balance / TODAY's limit`. A user
who raised a limit from €2,000 to €10,000 in June would see the whole prior year
retroactively restated to a fifth of what they lived through, with no way to
know. **That is a fabricated number, not a degraded one.**

Publish no `month`/`week`/`day` dimension and **no `date` filter**. A stray
shared-canvas date is dropped by `SHARED_CANVAS_FILTER_FIELDS` (`base.py:40`),
and `sourceSupportsDateFilter` returns false from the catalog with zero FE
change.

Utilization-over-time is a separate ticket whose **first deliverable is a
credit-limit history substrate**, not a chart.

### F3 — Four measures; the percentage is a ratio of sums

| key | field | agg | format |
|---|---|---|---|
| `utilization_pct` | `utilization_pct` | `avg` (nominal) | `percent` |
| `outstanding` | `outstanding` | `sum` | `currency` |
| `credit_limit` | `credit_limit` | `sum` | `currency` |
| `count_cards` | `id` | `count` | `number` |

**Order is load-bearing:** `utilization_pct` must be `measures[0]`, because
`setDataset` resets a switched widget to `entry.measures[0]`
(`useWidgetMutations.ts:179-184`).

**The percentage rule, for any group G:**

```
utilization_pct(G) = 100 × Σ outstanding(G) / Σ credit_limit(G)
```

A limit-weighted ratio of sums. At grain=account it reduces exactly to
`creditUtilization()` (`lib/credit.ts:13`), so a **per-card** value in the
report matches a per-card value on the shipped surfaces by construction.

⚠ That guarantee is per-card only, and does **not** extend to aggregates: the
two shipped surfaces do not even read the same population —
`CreditUtilizationWidget.tsx:22-25` reads `activeAccounts` (active only) while
`LiabilityCards.tsx:241` includes inactive. Since this source includes inactive
cards by default (F5.4), an org-wide KPI here can legitimately differ from the
dashboard tile. Do not write a fence asserting they agree in aggregate.

⚠ **An unweighted average is banned and is the defect to fear.** A €200 store
card at 100% and a €20,000 Amex at 5% average to **52.5%**; true combined
utilization is **5.94%**. It produces a *plausible-looking* wrong number, which
is worse than an obviously wrong one.

⚠⚠ **Fence arithmetic must be COMPUTED, not asserted from memory.** An earlier
draft of this spec used cards (−900/1000) and (−100/9000) and asserted the
result is `10.0` "and not `50.0`". The ratio-of-sums is indeed 10.0 — but the
unweighted average of those two cards is **45.56%, not 50%**. The negative
assertion `!= 50.0` would therefore have been **true under both the right and
the wrong implementation**: a vacuous guard inside the very fence written to
kill this defect. Both figures below were computed before being written down:

```
cards (−200/200) and (−1000/20000):
  ratio-of-sums  =  5.9406%      ← correct
  unweighted avg = 52.5000%      ← the defect
  gap            = 46.56 pts
```

The wide gap is deliberate — it leaves no room for a rounding coincidence to
make both assertions pass. `build_rows` computes both sums and divides
in Python; it ignores `measure.agg` for this field (the `net_worth`
nominal-measure precedent, `networth.py:70-72`).

**Why the nominal agg is `avg` and not `sum`:** the declared value is what the
gate admits and what the UI prints. Declaring `sum` would force the gate to
*admit* `sum(utilization_pct)` and *reject* `avg(...)` — inverting the safety
property, so the one word that must never attach to this measure becomes the
only one that validates. It is also user-facing, though less sweepingly than an earlier draft claimed:
`series.ts:161` renders `${HUMAN_AGG[agg]} of ${fieldLabel}` **only on
multi-series widgets** — `series.ts:160` short-circuits with
`if (total === 1) return fieldLabel`, so a bar/kpi/pie shows a bare
"Utilization". `sum` would print **"Sum of Utilization"** on multi-series
widgets only. **The ruling rests on the gate-inversion argument alone**, which
is sufficient; the UI-string argument is corroboration, not the basis.

**The agg gate is required because the base validator does not provide it.**
`validate_against_catalog` (`base.py:73-107`) checks `measure.field` only, never
`measure.agg`, and the editor's agg picker is free — so a hand-rolled or
replayed AST can emit `sum(utilization_pct)`. `validate()` =
`validate_against_catalog(...)` **plus** an explicit agg gate.

**`credit_limit` earns its place; it is not a derivation.** The inverse
`credit_limit = 100 × outstanding / utilization_pct` collapses to 0/0 on exactly
the cards where the limit still matters: a zero-balance or overpaid card yields
`outstanding = 0` and therefore `utilization_pct = 0`, so a card sitting at 0%
with a €5,000 limit has an unrecoverable limit — and F5 puts those cards in the
row set deliberately. A derivation that fails on a member of its own population
is not a derivation.

Headroom and over-limit stay out: both are subtractions of published measures,
readable off a `table` widget.

### F4 — Currency partitioned always; the percentage is a TRAP

Adopt `networth.py:269-292` verbatim. `currency` is **always** in the internal
group key. If the user did not request the `currency` dimension and the
qualifying set spans more than one currency **measured POST-filter**
(`networth.py:197-198,224-225,239-262`; measuring pre-filter would emit a
spurious multi-currency warning for `currency eq "USD"` on a EUR+USD org): **keep** the `currency` key on
every row and set `meta.warning`. Drop the key only when single-currency.

⚠ The percentage *looks* exempt because it is dimensionless. It is not:
aggregating sums currency into **both** numerator and denominator —
`€4,000 / (€10,000 + $10,000) = 20%` is an FX conversion at exactly 1.00. And
because the output is a percent, the standing "never sum across currencies"
reflex does not fire. **`utilization_pct` gets no exception.**

Inherited wart, stated: a no-dimension KPI on a multi-currency org returns N
rows, not 1. Networth behaves the same. The alternative is a fabricated number.

**Sign.** `outstanding` is published **positive**, inverting the repo-wide
"liabilities stored negative" convention, to match `creditUtilization()`. This
must be documented at the top of the source file: a user placing accounts
`sum_balance` and cc `outstanding` on one canvas sees the same card twice with
opposite signs. That is precisely why the field is named `outstanding` and not
`balance`. Renders as an unsigned magnitude, never colour-coded.

### F5 — Qualification

1. **`AccountType.slug == "credit_card"`.** Never `AccountType.name` — types are
   per-org renameable rows, and a user-created "Credit Card" has `slug = NULL`.
2. **Loans excluded.** Do not reuse `LIABILITY_SLUGS` (`{credit_card, loan}`); a
   loan's `principal_amount` is contractual principal, not a revolving limit.
   "Utilization" is a category error on a loan.
3. **No limit (`credit_limit IS NULL` or `<= 0`) → excluded from rows, counted
   in `meta.warning`.** `creditUtilization` returns 0% for a limitless card
   (`credit.ts:15`), so including them renders a wall of 0% bars for merely
   unconfigured cards — reading as "you're doing great" — and drags every group
   ratio down. Both shipped surfaces already exclude them from the bar and show
   their existence separately; Reports has no such affordance, so the count goes
   in the warning. **Silent exclusion is not acceptable.**
4. **Inactive cards included by default; filterable.** A closed card with a
   balance still consumes utilization and still needs paying. Rejected: a hidden
   `is_active = true` predicate — the closed-AST contract exists so the AST *is*
   the whole query, and a hidden server-side predicate makes the same AST return
   rows a reader cannot predict.
5. **Overpaid (`balance > 0`) → `outstanding = 0`, 0%.** Never `-balance`:
   harmless at grain=account, silently deflates the group ratio when grouped.
6. **Over-limit is NOT clamped.** `utilizationPct` returns 125% uncapped; the
   clamp to 100 exists only in the bar's visual domain. >100% is the one state
   that matters — do not hide it.

⚠ **Dialect.** Express the clamp as
`case((Account.balance < 0, -Account.balance), else_=0)` — the idiom at
`accounts.py:69` and `networth.py:125`. **Never `func.greatest`:** it is
MySQL-only, the source tests build on `sqlite+aiosqlite`
(`test_report_sources_endpoint.py:38`), and it fails the suite outright.
Measured: `sqlite3.OperationalError: no such function: greatest`. Already
documented at `transaction_service.py:1074`. Note the risk direction — this is a
**build-breaker in CI**, not a silent prod defect.

### F6 — Rendering: no new `WidgetType`

**`CreditUtilizationBar` cannot be reused.** It takes `{name, balance,
creditLimit, currency}` — raw per-account *inputs*. Every arm of
`renderReportWidget` renders from `QueryRow[]`, and a `QueryRow` carries exactly
one `value`. The AST cannot carry the *(balance, limit)* pair the bar needs, and
making it do so means adding a second measure column to a response contract all
four existing sources share.

So a utilization report is an ordinary **`bar`** / `table` / `kpi` widget. No new
`WidgetType`, no `report_layout.py` Literal, no `renderReportWidget` arm, no
`widgetKit` factory, no picker entry. Additive at the query layer only.

**Anti-drift moves to the math layer.** The band thresholds and copy are inline
literals at `CreditUtilizationBar.tsx:50-58` (`over > 0`, `>= 75`, "High", "Over
limit"). Extract into `frontend/lib/credit.ts` as `utilizationBand(...)` and
have `CreditUtilizationBar` consume it. **That extraction — not component reuse
— is what satisfies "must not drift."**

**Do not band-colour the report bar.** `BarWidgetChart` paints one flat fill;
DESIGN.md reserves `chartColor.over` as a narrow carve-out, and both DESIGN.md
and PRODUCT.md require colour to be paired with a label. `CreditUtilizationBar`
complies because every band pairs colour with text; a report bar has no per-bar
text slot, so band colour alone would be a WCAG 2.2 AA regression. The percent
on the axis and tooltip carries the signal.

⚠ **A live bug this ticket must fix.** `SourceMeasure.format` exists
(`base.py:33`), the endpoint ships it (`routers/reports.py:351`), the FE types it
(`types.ts:453`) — **and nothing reads it.** Format is hardcoded per widget type
at creation (`widgetKit.tsx:29,48,69,101`, `draft.ts:38`) and `setDataset` never
touches it. `formatMeasureValue` renders `percent` correctly (`series.ts:139`)
but nothing ever sets it. It has never fired because **no percent measure has
ever existed**. Consequence: switching a widget to this source shows
**"€45.00" for 45%**. Fix in `setDataset`, where `resetMeasure` is already built
from `firstMeasure` — carry `firstMeasure.format` onto `config.format`.

Two render notes: the source must return utilization on the **0–100** scale
(`series.ts:139` is `${value.toFixed(1)}%`; a 0–1 return renders "0.5%"), and a
percent measure has **no Y-axis clamp** — a 140% card blowing past 100 is
correct and must not be "fixed".

## Changes

### Backend

| file | change |
|---|---|
| `schemas/reports_enums.py:15-18` | `Dataset` += `CREDIT_UTILIZATION = "credit_utilization"` |
| `schemas/reports_enums.py:31-41` | `MeasureField` += `UTILIZATION_PCT`, `OUTSTANDING`, `CREDIT_LIMIT` |
| `schemas/reports_query.py:60` | `NUMERIC_MEASURE_FIELDS` += all three, else `Measure._validate_agg_field` 422s every agg on them before the source is reached |
| `reports/sources/credit_utilization.py` | **NEW** — source + `register(...)` |
| `reports/sources/__init__.py:26-29` | import so it self-registers |

### Frontend

| file | change |
|---|---|
| `lib/reports/types.ts:28` | `Dataset` union += `"credit_utilization"` |
| `lib/reports/types.ts:32-38` | `MeasureField` union += all three |
| `lib/reports/series.ts:63-70` | `MEASURE_FIELD_LABELS` — exhaustive `Record`, **compile error** until keyed |
| `components/reports/config/DataTab.tsx:40-45` | `DATASET_FALLBACK_LABELS` — exhaustive `Record`, **compile error** until keyed |
| `components/reports/config/useWidgetMutations.ts` | the `format` fix — **not one line**: derive from the *resulting* measure in `setDataset`, AND thread the catalog entry into `setSingleMeasure`/`setSeries` (`:37-40`, `:53-72`) so the measure-change path is covered too. See (6) |
| ~~`lib/credit.ts` extract `utilizationBand`~~ | **DROPPED — out of scope.** The thresholds exist in exactly one place today and the report bar is not band-coloured, so the extraction would have a single consumer. The ticket's "must not drift" constraint guards a risk that does not exist |

### Exact-set assertions that BREAK (F7)

- `tests/schemas/test_reports_enums_consistency.py:14-16` — `{d.value for d in Dataset} == {...}`. **Hard break**, update.
- `tests/schemas/test_measure_numeric_validation.py:19-24` — `NUMERIC_MEASURE_FIELDS == {AMOUNT, BALANCE, NET_WORTH}`. **Hard break**, update; the test's *name* also becomes a lie.
- `tests/services/test_report_sources_registry.py:110-118` — `enum_values == registered`. Green only if the enum and the registry import land **together**. **Keep it a strict `==`** — it is the good fence.
- `tests/services/test_report_sources_registry.py:82-91` — `known_kinds` allowlist. **Binding constraint, not an edit:** publish only `account`, `currency`, `boolean`, `number`. Do **not** invent a `"percent"` kind.
- `tests/routers/test_report_sources_endpoint.py:157-158` — every source must publish a non-empty filter list. Binding: this source cannot ship filterless.

### Verified NON-hazards (do not touch)

`DIMENSION_HEADERS` (no new `Dimension`), `report_layout.py` `WidgetType` and the
discriminated `Widget` union (no new widget type), `FILTER_KEY_TO_SOURCE_FIELD`,
`resolve.ts` `sourceSupportsDateFilter`, `templates.py`,
`lib/dashboard/widget-types.ts`, `_run_source_query` (registry dispatch, no
switch).

## The source contract

```python
key   = "credit_utilization"
label = "Credit utilization"

dimensions: account (account) | currency (currency) | account_active (boolean)
measures:   utilization_pct (avg, percent)  ← MUST be measures[0]
            outstanding (sum, currency)
            credit_limit (sum, currency)
            count_cards (count, number)
filters:    account_id (in) | currency (eq,in) | account_active (eq)
            NO date filter

validate(q) = validate_against_catalog(self, q)
            + EXHAUSTIVE agg pin (see below)

build_rows:
  FROM accounts JOIN account_types ON account_types.id = accounts.account_type_id
  WHERE accounts.org_id = :org_id
    AND account_types.slug = 'credit_card'
    AND accounts.credit_limit IS NOT NULL AND accounts.credit_limit > 0
    AND <every compiled query.filter>                  -- ⚠ see (1)
  GROUP BY <requested dims> + accounts.currency if not already requested  -- (5)
  SELECT SUM(case(balance < 0, -balance, else_=0)) AS outstanding,
         SUM(accounts.credit_limit)                 AS credit_limit,
         COUNT(*)                                   AS card_count
  then in Python per group, select `value` by requested measure -- ⚠ see (2)
  then sort + slice IN PYTHON                                   -- ⚠ see (4)
  meta.warning: joined notices                                  -- ⚠ see (5)
```

### (1) ⚠ Filters MUST be compiled. Publishing is not honouring.

Copy the `_apply_filter` loop verbatim from `accounts.py:102-150` /
`accounts.py:186-189`. **`validate_against_catalog` ACCEPTS a published field
without applying it** (`base.py:93-102`), so an uncompiled filter is not
rejected — it is silently ignored.

Failure without this: a widget with `filters=[{field:"currency", op:"eq",
value:"USD"}]` on a EUR+USD org returns **both** currencies, no error. Same for
`account_active eq false` — closed cards still appear. A fence suite with no
filter assertions ships this green. **Every published filter gets its own
fence** (F-13).

### (2) ⚠ The measure → `value` mapping must be explicit.

`QueryRow` carries exactly ONE `value` (`reports_query.py:290-293`), and
`accounts.py:177` + `:84-99` select the column by `query.measure` (`:239` is
merely `d["value"] = val`). An implementation
that only ever emits the utilization number returns it for **every** requested
measure.

Failure without this: a KPI with `measure={agg:"count", field:"id"}` — which
`validate_against_catalog` admits, because it checks field membership only —
renders "3 cards" as `42.7`.

Required switch, computed per group:

| requested `MeasureField` | `value` |
|---|---|
| `UTILIZATION_PCT` | `100 × Σoutstanding / Σcredit_limit` |
| `OUTSTANDING` | `Σoutstanding` |
| `CREDIT_LIMIT` | `Σcredit_limit` |
| `ID` (count_cards) | `COUNT(*)` |

⚠ **The switch must end in an explicit `else: raise`.** It is total over the
four published fields today, but a fifth added later would otherwise fall
through to whichever branch is last — silently returning the wrong number,
which is the identical failure class this section exists to kill.

### (3) ⚠ The agg pin must be EXHAUSTIVE, not utilization-only.

Gating only `utilization_pct` leaves the same inversion one field over. With
`OUTSTANDING`/`CREDIT_LIMIT` in `NUMERIC_MEASURE_FIELDS`,
`Measure._validate_agg_field` passes `{agg:"avg", field:"outstanding"}`,
`validate_against_catalog` passes it, and `build_rows` — which only ever SUMs —
returns the total. Two cards at €900 and €100 render as **"average outstanding =
€1,000."**

Pin **every** measure to its declared agg via a `{field: agg}` map. Anything
else raises → 422.

### (4) ⚠ Sort and limit happen in PYTHON, not SQL.

`utilization_pct` exists only after the Python division, so there is no `value`
column to `ORDER BY` / `LIMIT` on (contrast `accounts.py:199-220`). Applying
`.limit()` in SQL keeps an **arbitrary** N rows and the Python sort then orders
the wrong ones: 150 qualifying cards with `limit=100` can silently drop the
highest-utilization cards — the exact rows the report exists to surface.

Sort then slice in Python, per `networth.py:267,282-284`, **with an explicit
tiebreaker** — `accounts.py:213-218` adds `func.min(Account.id).asc()` precisely
"so truncation on ties is stable", and a Python sort without one makes
"top 100 of 150" nondeterministic. F-17's fixture must request
`dimensions:["account"]` (without a dimension the query returns ONE row, not
150), and must guarantee distinct utilizations and unique account names (the
group key is `Account.name`, `accounts.py:64`). ⚠ **The `sort.by="dimension"`-with-zero-dimensions check must live in
`validate()`, not `build_rows`.** `_run_source_query` wraps only `validate()` in
its try (`routers/reports.py:213-216`); `build_rows` is called OUTSIDE it, so
copying `accounts.py:202-205` verbatim turns user input into a **500** instead
of a 422.

### (5) `meta.warning` is a single `Optional[str]` — compose, don't overwrite.

`reports_query.py:279-287`. Both the multi-currency notice and the
excluded-cards notice can apply to the same query, and a second assignment would
silently discard the first — telling the user about currencies and never about
the excluded card, violating "silent exclusion is not acceptable."

Collect notices into a list and `" ".join(...)` once. Fenced by F-16 with
**both** conditions true.

Also: `GROUP BY` adds `currency` only **if not already requested**, or the group
key carries a duplicate column.

Reuse `accounts.py:67-70`'s `case((Account.is_active.is_(True), "Active"),
else_="Inactive")` verbatim for the `account_active` label so the two sources
cannot drift on the strings.

## Fences

Each names the wrong implementation it kills. Backend →
`tests/services/test_credit_utilization_source.py` (new); frontend → alongside
the existing reports tests.

| # | Fence | Kills |
|---|---|---|
| F-1 | Two same-currency cards (−200/200) and (−1000/20000), no dimension → **`approx(5.9406, abs=1e-3)`**, and explicitly **`!= approx(52.5, abs=1e-3)`**. ⚠ The `abs=` is mandatory: `1200/20200*100 = 5.940594059…`, which clears pytest's default `rel=1e-6` by **5.9e-12** — a Decimal-vs-float division order in the implementation flips it RED against correct code. The 46.56-point gap to 52.5 means `abs=1e-3` costs nothing | unweighted `avg` of per-card ratios — the plausible-looking wrong number |
| F-2 | EUR card at 90% + USD card at 10%, `currency` NOT requested → **two rows**, each keyed by currency, `meta.warning` set. Never one row | the F4 trap: treating a percent as currency-free, an implicit 1.00 FX |
| F-3 | `{agg: "sum", field: "utilization_pct"}` → ValueError → 422 | relying on `validate_against_catalog` alone, which checks the field but never the agg |
| F-4 | Parity with `creditUtilization` (`credit.ts:13-18`) **over the limit>0 population only** — incl. `balance > 0 → 0%` and the 0–100 scale | a backend returning 0–1 (renders "0.5%") and an overpaid card going negative |
| F-5 | A CC with `credit_limit IS NULL` and a nonzero balance appears in **no** row **and** is counted in `meta.warning`. Fixture is **single-currency** | including it at 0% (which `credit.ts:15` hands you), dragging every group ratio down; and equally, dropping it in silence |
| F-8 | Switching a bar widget to this source + `utilization_pct` leaves `config.format === "percent"`; **and** a retained `count(id)` measure does **not** become `"percent"`; **and** changing the measure to `outstanding` within the source flips format back to `"currency"` | the live bug (45% as "€45.00") **and both inverse bugs the naive fix introduces** — see (6) |
| F-10 | `{d.key for d in src.dimensions()} == {"account","currency","account_active"}` and no `date` in `filters()` | someone "helpfully" adding `month` by reusing networth's reconstruction against today's `credit_limit` — a chart that rewrites last January on every limit increase |
| F-12 | Over-limit card returns **>100** uncapped from the source | clamping the report to 100, hiding the one state that matters |
| **F-13** | **One per published filter:** `currency eq "USD"` on a EUR+USD org returns USD rows only; `account_active eq false` returns closed cards only; `account_id in [x]` returns that card only | **filters published but never compiled** — the single highest-value fence here, because nothing else in the suite would notice |
| **F-14** | **One measure per requested `MeasureField`, same fixture:** `utilization_pct`, `outstanding`, `credit_limit` and `count_cards` each return their own number | a `build_rows` that emits the utilization figure for every measure — "3 cards" rendered as `42.7` |
| **F-15** | `{agg:"avg", field:"outstanding"}` → 422 | a non-exhaustive agg pin: `build_rows` only SUMs, so "average outstanding" would return the total |
| **F-16** | Multi-currency org **that also holds a limitless card** → `meta.warning` contains **both** substrings | a second assignment silently overwriting the first |
| **F-17** | 150 qualifying cards, `limit=100`, sort value desc → the returned 100 are the **top 100 by utilization** | `.limit()` applied in SQL before the Python division, silently dropping the highest-utilization cards |

### Dropped — these were not fences

- ~~F-6 (executes on SQLite)~~ — implied by every other backend fence; the whole
  suite is `sqlite+aiosqlite`. The `func.greatest` prohibition stays in
  Constraints, but it needs no fence of its own.
- ~~F-7 (registry lockstep)~~ and ~~F-11 (`known_kinds`)~~ — these are **existing
  tests that already pass on unmodified `main`**. They are real constraints on
  the implementation and are listed under "exact-set assertions", but calling
  them fences of *this* change is exactly the "passes against unmodified main"
  vacuity class. Verified: `{account, currency, boolean, number}` are all
  already in the allowlist.
- ~~F-9 (single band definition)~~ — **its stated rationale is false.** The
  thresholds exist in exactly ONE place today (`CreditUtilizationBar.tsx:50-58`);
  both shipped surfaces render that same component, and grep finds no second
  copy. Since F6 forbids band-colouring the report bar, the extracted
  `utilizationBand` would have exactly one consumer. **The `utilizationBand`
  extraction is therefore an optional refactor, not a requirement of this
  ticket** — the "must not drift" constraint inherited from the ticket text
  guards against a risk that does not exist. Dropped from scope; a literal
  "only place `>= 75` is written" test would also trip on the doc comment at
  `CreditUtilizationBar.tsx:15`.

### (6) ⚠ The `format` fix is NOT one line, and the naive version is a new bug

`setDataset` only **resets** the measure when the field is unpublished
(`useWidgetMutations.ts:189-190, 223-228, 239-241`), and `id` is published by
**every** source (`count_rows` / `count_accounts` / `count_cards`). So an
unconditional `config.format = firstMeasure.format` mis-formats a *retained*
measure:

- `accounts` + `count(id)` → switch to credit_utilization → format becomes
  `"percent"` → **4 cards renders as "4.0%"**.
- Symmetrically, credit_utilization → accounts writes `"currency"` → "€4.00".

Derive from the **resulting** measure, not `measures[0]`, and **match on FIELD
ONLY**:

```ts
entry.measures.find(m => m.field === measure.field)?.format ?? cfg.format
```

⚠⚠ **Do NOT add `&& m.agg === measure.agg`.** An earlier draft of this spec did,
and it is **actively wrong**: the Field select emits `{...measure, field}`,
carrying the previous agg over unchanged (`SingleMeasureEditor.tsx:53-55`). With
the conjunct, picking "Outstanding" while agg is `avg` looks up
`(outstanding, avg)` — which this catalog publishes at `sum` only — misses,
falls through to `?? cfg.format`, and **preserves the stale `"percent"`**, so
€1,234.56 renders as "1234.6%": verbatim the bug this fix exists to kill.
Format is a pure function of field in all five catalogs (`transactions.py:33-35`,
`accounts.py:47-49`, `recurring.py:60-62`, `networth.py:71`, and this one), so
the agg conjunct can only ever produce misses.

⚠ **F-8 must drive the actual UI event — the field select — not hand-build the
measure.** A test calling `setSingleMeasure({agg:"sum", field:"outstanding"})`
directly goes GREEN against the buggy conjunct, because the shipped UI cannot
produce that AST. That is a green fence over a live bug.

⚠ `SourceCatalogMeasure.format` is typed `string` (`types.ts:453`) while
`config.format` is the union `"currency" | "number" | "percent"`
(`types.ts:220`). Narrow with a guard, never a bare cast.

### ⚠ SCOPE — subtracted to TBD-379

Two spec rounds found their defects clustering here, in code **this ticket did
not cause**: `SourceMeasure.format` has been shipped-and-unread since Phase 5,
and is only visible now because `utilization_pct` is the first `percent` measure
in the codebase. Continuing to correct it inside this ticket was growing the
diff without improving the source.

**In scope here — the minimal correct single-measure path:** `setDataset` and
`setSingleMeasure`, field-only match, so a `bar` / `kpi` / `table` widget on this
source renders `45.0%` and not `€45.00`.

**Moved to TBD-379:**

- the multi-series path — `SeriesConfig` has no `format` field at all
  (`types.ts:248-251`), so "which of N series sets the single `config.format`"
  is an undecided product question, not wiring;
- narrowing the agg picker per catalog. `AGG_OPTIONS`
  (`controlConstants.ts:23-28`) is catalog-independent and
  `MeasuresEditor.tsx:46,56-58` `add()` hardcodes `agg:"sum"` with
  `fields[0]` — which, because F3 puts `utilization_pct` first, makes
  "Add series" emit exactly the AST the new agg pin 422s, with no UI feedback.
  **Known dead-end, accepted for this ticket, fenced by nothing here.** Stated
  in the PR body rather than discovered by a reviewer;
- `MEASURE_FIELD_LABELS` doubling as the catalog-free `FIELD_OPTIONS` fallback
  (`controlConstants.ts:40-42`), which already leaks `balance` / `net_worth`
  into a transactions Field picker today;
- evaluating **render-time** derivation in `formatMeasureValue` (one read site)
  instead of mutation-time (three write sites) — which would delete this class
  rather than patch instances of it.

## Verification protocol

Three legs per fence: RED before implementation → green after → **RED again**
against the named wrong implementation → restore from a `cp` backup (never
`git checkout --`) → confirm green. A fence whose third leg cannot be
demonstrated is decoration and must be deleted or reworked.

Full backend suite. Frontend changes here, so **all four** of `tsc --noEmit`,
`eslint --quiet`, full `vitest run`, and `check-design-tokens.sh`.

⚠ **Visual-validation gate.** This is a new user-facing surface. Build it, run
the app, and get explicit operator approval **before** opening the PR.

## Sequencing

1. Backend enums + `NUMERIC_MEASURE_FIELDS` + fix the two hard-breaking
   exact-set tests. The registry lockstep test is RED here — expected.
2. The source + registry import. Registry test goes green. Land F-1…F-5,
   F-10, F-12, **F-13…F-17** (filters, measure→value, exhaustive agg pin,
   composed warning, Python sort+slice).
3. Frontend unions + labels — TypeScript tells you when you are done
   (two exhaustive `Record`s are compile errors until keyed).
4. The `format` fix, both paths, and F-8's three assertions. Existing
   `credit.test.ts` and `credit-utilization-widget.test.tsx` must stay green
   **unchanged** — this ticket no longer touches `lib/credit.ts`.

## Out of scope — file as follow-ups

1. Utilization over time — blocked on a credit-limit history substrate.
2. A catalog-driven `FilterEditor`. Published filters have no UI control today;
   the `accounts` source's have been unreachable since Phase 5.
3. A banded per-account report widget (would need a new `WidgetType` across six
   files).
