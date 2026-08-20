# TBD-382 — Stacked bar stacks by a dimension; multi-series stops lying

Status: design settled 2026-08-20 by two independent architects plus a
concede-or-defend round and a build-it probe on the one contested structure.

## Phase 0 — the verified defects

Both reproduced against real code before any design.

### Defect A — `stacked_bar` never stacks by a dimension, and its numbers are WRONG

* `frontend/components/reports/widgets/StackedBarWidget.tsx:63` reads
  `dimensions[0]` only. `dimensions[1]` is never referenced in the file.
* Rows go through `mergeSeriesRows` (`frontend/lib/reports/series.ts:175-205`),
  which keys on the primary dimension label and does **last-write-wins**
  (`existing[key] = readNumber(row.value)`).
* `stackId` is gated on `seriesKeys.length >= 2`, i.e. on the number of
  **measures**, never on a dimension.
* **Consequence, shipped today.** The template `cdd-stacked-by-month`
  (`backend/app/reports/templates.py:234-244`) carries
  `dimensions: ["month", "category"]` and a **single** measure. The backend
  returns one row per `(month, category)`. Every category in a month collapses
  onto the month label and only the last survives — rendered as if it were the
  month's total.
* The surviving value is not arbitrary. `_compile`
  (`backend/app/services/reports_query_service.py:354-356`) applies a default
  `ORDER BY value DESC` when the AST carries no `sort`, and this template
  carries none. So **each month's bar shows that month's SMALLEST category,
  labelled as the month.**
* The correct primitive already exists and the sibling `bar` widget uses it:
  `pivotBySecondaryDimension` (`frontend/lib/reports/series.ts:~248`).
* `frontend/components/reports/config/DataTab.tsx:190` gates the "Break down by"
  control to `bar || table`, so a user cannot set a second dimension on a
  `stacked_bar` at all — though templates can and do.

### Defect B — "+ Add series" seeds a duplicate

`frontend/components/reports/config/MeasuresEditor.tsx:54-60` seeds
`{agg:"sum", field: fields[0]}`. Series 1 typically already IS that pair, so on
`line`/`area` the second series draws pixel-identical on top of the first. This
is the owner's "the series does absolutely nothing", exactly.

### Defect C — the two-dimension limit truncates PAIRS, not buckets (found by both architects, in scope)

`limit` caps **rows**, and with two dimensions a row is a `(primary, secondary)`
pair ordered `value desc`. `buildQueryAst`'s bar branch ships
`limit: widget.config.limit ?? 10` (`frontend/lib/reports/useReportQuery.ts:115`)
and `emptyBar` persists `limit: 10` (`widgetKit.tsx:44`). So a bar broken down by
account returns **at most 10 (category, account) pairs in total** — not 10
categories. Every affected bar under-reports its own total. This is live on
`bar` today and would swallow the fix for Defect A.

### Defect D — the palette wraps at 8 and the legend stops disambiguating

`CHART_SERIES` has exactly 8 tokens (`frontend/lib/chart-colors.ts:32-41`) and
`legendColor` does `i % length`. Stacking by **account** rarely exceeds 8;
stacking by **category** — this ticket's whole purpose — routinely does. Two
segments in one bar then carry the same colour with two different legend
labels, and the legend is the only disambiguator. That breaks the PRODUCT.md
WCAG 2.2 AA commitment that visualizations never rely on colour alone.

## The design rulings

### R1 — `stacked_bar` loses the measure-stacking axis entirely

`stacked_bar` becomes a **single-measure** widget. Its only stacking axis is
`config.dimensions[1]`.

Justification, verified against every source: there is **no pair of published
measures on any source whose sum is meaningful**. transactions/recurring
publish `sum(amount)`, `avg(amount)`, `count(id)`; accounts publishes
`sum(balance)`, `avg(balance)`, `count(id)`; credit_utilization publishes
`avg(utilization_pct)`, `sum(outstanding)`, `sum(credit_limit)`, `count(id)`
with `outstanding` a **subset** of `credit_limit`; networth publishes exactly
one. A stack asserts its segments are parts of a whole. `sum`+`avg` is a total
plus a mean of that total; `sum`+`count` is cross-unit;
`sum(outstanding)`+`sum(credit_limit)` double-counts and draws a bar taller
than the credit line. The one legitimate stack — same measure, disjoint filters,
income vs expense — is structurally inexpressible, because `buildSeriesQueryAst`
builds ONE `filters` object for all series
(`frontend/lib/reports/useReportQuery.ts:191-198`).

Therefore the `seriesKeys.length >= 2 -> stackId` branch has never been correct
on any config on any source. This is a deletion of a defect, not of a feature.

**Scope limit, load-bearing.** This applies ONLY to stacking. `line`, `area` and
`table` keep multi-measure unchanged: two lines are plotted, not summed, and
that comparison is exactly what Epic TBD-380 A.3 asks for; table columns are not
summed either. DoD 3 stays live for those three types.

**Filed separately, NOT fixed here:** `AreaConfig.stacked` stacks N measures by
the identical never-correct mechanism. It defaults to `Boolean(stacked)` = off
(`StyleTab.tsx:81`) whereas `stacked_bar` defaults on (`stacked !== false`), so
it is a real but lower-severity hazard. Widening this ticket to it was rejected.

### R2 — `stacked?: boolean` finally acquires its documented meaning

`types.ts:311-316` already documents it as "the user explicitly wants grouped
(side-by-side) bars". Post-fix it flips the **break-down** between stacked and
grouped. Its old meaning ("don't stack the measures") was a no-op on every real
config.

### R3 — the two-dimension limit rule

When `dimensions.length > 1`, the AST limit becomes `MAX_LIMIT` (500) and
`config.limit` becomes a **client-side cap on PRIMARY buckets**, applied after
the pivot. This fixes Defect C on `bar` as a side effect.

**Default when `config.limit` is absent on the 2-dimension path: 100 primaries**,
for both `bar` and `stacked_bar`. Rationale: `limit` on a 2-dimension query never
meant "primary buckets" before this change (it meant pairs), so there is no prior
primary-cap semantics to preserve. `emptyBar` writes `limit: 10` explicitly, so
factory-made bars are unaffected; the default only reaches configs carrying no
limit at all, which is exactly the already-cloned-template case that must render
all 12 of its months. The 1-dimension path is untouched (`bar` keeps `?? 10`,
which is a genuine bucket cap today).

**Ordering of the client-side primary cap — three branches, decided by the
PRIMARY dimension first and `sort` second:**

1. **Primary is a time dimension (`month`/`week`/`day`)** -> sort primaries
   **chronologically ascending by label FIRST**, then keep the most-recent N (the
   **tail**, matching the precedent already set at
   `backend/app/reports/sources/networth.py:277-284`). Never rank by total.
2. **`sort.by === "dimension"` on a non-time primary** -> keep backend order, take
   the first N.
3. **Otherwise** (`sort.by === "value"`, or `sort` absent on a non-time primary)
   -> order primaries by row total desc, take N.

⚠ **Branch 1 must sort BEFORE capping, and this is the whole point.** "Keep
backend order and take the first N" is NOT chronological: with `sort` absent the
compiler applies `ORDER BY value DESC` over `(primary, secondary)` **pairs**
(`backend/app/services/reports_query_service.py:354-356`), so first-seen primary
order is "months ranked by their single largest category". Capping that drops an
arbitrary subset of months out of the middle of the series — precisely the
outcome this guard exists to prevent. Sorting after capping merely re-orders a
set that already lost the wrong members.

### R4b — colour is assigned from a STABLE ordering, not arrival order

`pivotBySecondaryDimension` mints `s0..sN` in **first-seen** order and
`legendColor(i)` indexes that order, while the compiler defaults to
`ORDER BY value DESC` — so arrival order is a function of the values, and
Groceries is gold this month and violet next. R5's top-7-by-total ranking is
*still* data-dependent: a category crossing the rank boundary swaps hue and can
drop into "Other" and back between adjacent loads.

Assign the colour index from a **stable ordering of the secondary label**
(alphabetical), not arrival order. "Other" is pinned to the neutral, outside the
index. A category changing its own colour between two loads is the same false
assertion of identity that R5 exists to remove, on the time axis — and this
ticket is what makes stacking-by-category the headline use, so it owns the fix.

Full identity-stable colour (hash label -> slot, stable across differing label
sets) is a fair follow-up.

### R5 — the "Other" fold

**The fold fires only when `distinctSecondaryCount > CHART_SERIES.length`
(i.e. > 8).** When it fires, keep the top `CHART_SERIES.length - 1` (7) by grand
total and sum the remainder into a final "Other" segment.

⚠ Stating the guard as `> 8` is load-bearing and is NOT what a naive mirror of
`topNWithOther` produces. `topNWithOther` (`series.ts:351-361`) is
`if (rows.length <= topN) return rows` with `topN` as the head size, so mirroring
it with `topN = 7` folds at exactly 8 — changing a chart that renders perfectly
today with 8 distinct colours. The head size is 7; the trigger is 8.

**Execution order is `cap primaries, THEN fold secondaries`.** Ranking must only
ever see rendered data. Fold-first lets a secondary that appears solely under a
primary the cap is about to drop win a legend entry and a palette slot while
contributing 0 to every rendered bar, while a real, visible secondary is buried
in "Other". Both orders keep bar totals exact, so no arithmetic fence catches the
difference — it has to be specified.

**"Other" renders in a neutral, never a categorical hue: `var(--color-border-strong)`
(measured 3.31:1 dark / 3.32:1 light against `bg-surface`), and is pinned LAST in
both stack order and legend order** so position is a second channel. Falling
through to `CHART_SERIES[7]` would paint "not a category" in Overdue Coral, the
danger hue — asserting a status that is not there, the same class of false
assertion the fold exists to remove. The `PieWidgetChart.tsx:69` precedent uses
`var(--color-border)`, which measures 1.35:1 and fails WCAG 1.4.11; copy the
shape of that precedent, not its token, and file the Pie fix as a follow-up.

**The CSV export carries the RAW, UNFOLDED columns.** PRODUCT.md's line-item
visibility principle requires every total to have a path to its constituent rows,
and the fold is a truncation the user did not choose. 11 distinct secondary
values means 12 CSV columns (label + 11), not 9.

* **The bar total stays EXACT.** Sum(top 7) + Sum(tail) = Sum(all), and this
  holds under zero-backfill: a primary carrying none of the top-7 values simply
  has its whole total in "Other". Dropping the tail instead would under-report
  the bar — the same wrong-number class this ticket exists to kill.
* **Strict no-op at <= 8 distinct values**, including at exactly 8: same colours,
  same legend order, same segments. Only configs already rendering a colour
  collision change at all.
* Applies to `bar` as well as `stacked_bar`. Leaving `bar` colliding would fix a
  WCAG violation on one widget while stepping over the identical one next door.
* Threshold is **derived**, not chosen: it is exactly where the palette runs out.

⚠ **R5 does NOT close the WCAG commitment on its own, and must not claim to.**
It removes *duplicate* colour; it does not remove *confusable* colour. Measured
on the real `globals.css` tokens (Vienot CVD simulation + CIEDE2000): chart-1 vs
chart-7 is ΔE 3.1 under light-theme deuteranopia, 3.3 under protanopia; chart-3
vs chart-5 is ΔE 2.9 under dark-theme tritanopia. Below ~5 at a 10px swatch
reads as the same colour. So a fully-compliant 8-segment stack still hands a
CVD user two indistinguishable segments. The second-channel work (pattern fill,
or segment-order-matched legend) is a NAMED FOLLOW-UP, not something this ticket
discharges.

### R6 — query path converges

`stacked_bar` moves to `useReportQuery` — one query. With one measure there is
nothing to fan out (`useSeriesQueries` with N=1 is one query wrapped in
`Promise.all`), so this deletes a code path rather than changing perf.
`useSeriesQueries` stays untouched for `line`/`area`/`table`.

`mergeSeriesRows` and `pivotBySecondaryDimension` must **NOT** be unified. They
answer different questions — merge N responses on one known key, vs pivot one
response on two keys discovering the secondary values — and only one needs the
null-prototype guard against `__proto__`. Unifying yields one function with a
mode flag and a third bug.

### R7 — Defect B's fix: seed the next unused (agg, field) PAIR

The bug is the **vocabulary**, not the default. `measureFieldOptionsFor`
de-duplicates the catalog down to distinct *fields* and throws the `agg` away,
but the catalog's unit of truth is the `(agg, field)` pair. Field-only seeding
cannot produce a distinct series on a single-field source.

1. Seed the first **catalog** `(agg, field)` pair not already present in
   `measures`.
2. When the catalog's pairs are exhausted, **disable the button** with a reason.
   A control that refuses is honest; a control that seeds a duplicate is the bug.
3. While the source catalog has not resolved (`fieldOptions === undefined`),
   the button is disabled — R7 is defined in terms of catalog pairs and has no
   meaning before they exist.

⚠ **There is deliberately NO agg-rotation fallback.** An earlier draft rotated
`agg` over the last series' field when catalog pairs ran out, on the stated
premise that `validate_against_catalog` checks the measure field only and never
the pair (`backend/app/reports/sources/base.py:80-85`). **That premise is false,
and the fallback is unsafe three separate ways:**

* **It 422s.** `validate_against_catalog` is not the only validator.
  `CreditUtilizationSource.validate`
  (`backend/app/reports/sources/credit_utilization.py:197-206`) calls it and then
  enforces the pair against an exhaustive `_DECLARED_AGG` map (`:101-106`), whose
  comment says it exists *precisely because* the shared helper never checks the
  agg. A 4-series credit_utilization widget rotating onto `(sum, id)` raises
  `ValueError` -> 422 -> the whole widget errors out.
* **It mints meaningless measures.** `_measure_expr`
  (`backend/app/services/reports_query_service.py:244-263`) maps
  `MeasureField.ID -> Transaction.id` and applies whatever agg it is handed, so
  rotation produces `COALESCE(SUM(transactions.id), 0)` — the sum of primary
  keys — with no error, formatted as a plain number by
  `formatForMeasure`'s field-only backstop. That is a new silent-wrong-number
  vector introduced by the fix, which is the exact class this ticket exists to
  kill. Same on `accounts` and `recurring`.
* **On `networth` it reproduces Defect B verbatim** — the one source the
  fallback was introduced to rescue. `NetWorthSource.build_rows` **ignores
  `measure.agg` and `measure.field` entirely** (`networth.py:67-72` docstring,
  and the body at `:149-293` never reads `query.measure`), so seeding
  `avg(net_worth)` returns byte-identical rows and series 2 draws on top of
  series 1. "The series does absolutely nothing", exactly.

So `networth` (one published pair) correctly lands on step 2 with the button
disabled as soon as it has one series, and transactions with all three pairs
present likewise.

Scoped to `line`/`area`/`table` — `stacked_bar` no longer offers the control.

**Copy and surface (register-checked against `WidgetPicker`'s voice):**
"Every measure this source publishes is already a series." Rendered through the
already-imported `HelpTooltip` (`MeasuresEditor.tsx:8`), NOT as a `title`
attribute (not keyboard-reachable, not reliably announced) and NOT as a new
`text-xs text-text-muted` line (that would be a new inline surface, the same
category R10 defers, and would drag R7 into needing visual approval).

Disabled treatment reuses the shipped primitive `styles.ts:28`
(`disabled:cursor-not-allowed disabled:opacity-60`); the reason renders at normal
contrast OUTSIDE the dimmed control.

⚠ **The "+ Add series" button currently has NO focus state** —
`MeasuresEditor.tsx:149-156` is `hover:` only. DESIGN.md's *Pressable-Surfaces
Rule* requires a visible Brass Tally focus state on anything pressable, and
PRODUCT.md commits to visible focus on every interactive element. R7 rewrites
this button's state model, so it is fixed in the same edit:
`focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30`.

### R8 — persisted shape: write `measures` ONLY

`_MultiSeriesConfig.measures` is `Field(min_length=1)`
(`backend/app/schemas/report_layout.py:156`), bound to `stacked_bar` through the
discriminated union, and `backend/app/schemas/dashboard.py` reuses the same
model. So:

* `stacked_bar` stays single-measure in the EDITOR but keeps writing back
  `measures` as a **length-1 array**. Writing `config.measure` singular is a
  missing required field -> 422 on the next save, on BOTH the reports and
  dashboard paths.
* **Never dual-write a convenience `measure` key alongside `measures`.**
  `validate_layout_json` (`report_layout.py:317-337`) validates and returns
  **verbatim** — it deliberately never round-trips through `model_dump`, so
  `extra="ignore"` does NOT strip unmodelled keys from what is persisted. A
  stray `measure` would live in the DB forever as a second source of truth, free
  to drift from `measures[0]`. That would be a new silent-wrong-number vector
  introduced by the fix.
* Legacy entries beyond index 0 are left in the JSON, never rewritten at render.

### R9 — template changes

`cdd-stacked-by-month` gets `sort: {by: "dimension", dir: "asc"}` and an explicit
`limit`. Both ship as bugfix: the sort changes no data and there is no state in
which "months in spend order" is the intended reading of a time axis; the limit
is a no-op on any window holding <= 12 months.

The **12-month window is a DESIGN CHANGE and goes to the operator**,
non-blocking. Resolved ambiguity: the ask is **(a) canvas-wide**
(`canvas_filters_json`), not a per-widget `WidgetFilters.date_range` override.
A widget-only override would leave the canvas date control reading "This month"
while one panel showed twelve — a filter that does not govern what is on screen,
contradicting status-is-data and quiet-by-default, and forcing the panel to
declare its own window, which is itself the new inline surface R10 defers.
Canvas-wide means the pie stops meaning "this month's category share" and the
table stops meaning "this month's top categories", so the panels want retitling
too — which is exactly why it is the operator's call and not mine. `category_deep_dive` carries
`canvas_filters_json: {"date_range": this_month}`, so grouping by month over a
one-month window is structurally a ONE-BAR chart. Once `sort` lands that panel
is no longer *wrong* — it is a coherent single month's stack — which is what
makes the window an improvement rather than a fix. If the operator declines, the
ticket still closes; the fences prove DoD 2, not the template.

**What the operator must SEE to decide:** two side-by-side screenshots of the
WHOLE `category_deep_dive` canvas on a seeded org with >= 12 months of data —
today's one-bar version and the 12-month version, all three panels visible, at
the panel's real `w:12 h:4` grid size. The decision turns on the pie/table
semantic change and on whether 12 bars x 8 segments with `interval={0}` x-ticks
and a wrapping legend actually reads at that height. Prose cannot answer either.

### R10 — deferred, with a condition

The **truncation notice** (surfacing `meta.truncated`, which nothing in the app
reads today) is a new inline surface = design change. Deferred to a follow-up.
This is only safe BECAUSE R3 removes the common truncation case AND R5 sums the
tail rather than dropping it. **If EITHER R3 or R5 is cut from scope, the notice
comes back**, or the ticket ships a chart that silently under-reports the very
total it was opened to correct. `MAX_LIMIT` 500 is not infinity, so a `guard`
asserts `meta.truncated` is at least PLUMBED to the widget even while unrendered,
making the follow-up a render change rather than a re-plumb.

### R11 — accessibility fixes on the surfaces this ticket touches

All four restore commitments already written in PRODUCT.md or DESIGN.md, so all
four are bugfixes needing no approval.

* **Segment separator.** WCAG 2.2 AA 1.4.11 wants 3:1 against adjacent colours
  for graphical parts required to understand the content. Measured across all 28
  palette pairs, segment-vs-segment is 1.05-1.59 (dark) and from 1.02 (light) —
  nowhere near. Today `stacked_bar` renders one bar and has no adjacency; this
  ticket makes adjacency the widget's entire purpose. Add
  `stroke="var(--color-surface)" strokeWidth={1}` to each `<Bar>`, the idiom
  already shipped at `PieWidgetChart.tsx:62`. Surface-vs-chart-N measures
  5.97-9.48 dark and 3.13-5.70 light, all >= 3:1. Applies to `BarWidgetChart`
  too, which is already sliced and already has the gap.
* **Legend swatch bound.** Swatch-vs-surface measures 3.13 (chart-1), 3.19
  (chart-7), 3.30 (chart-3) in light theme — passing with no margin at 10x10px,
  unbordered. Add `ring-1 ring-border` so the swatch's shape is bounded
  independently of its fill.
* **Text alternative.** The legend `<ul>` has no accessible name and no
  relationship to the chart, so a screen-reader user meets a bare list of
  category names after a chart with no text alternative at all. Add `aria-label`
  on the `<ul>` naming both dimensions, and `role="img"` + `aria-label` on the
  chart container. (Note `StackedBarWidget.tsx:102` currently puts `aria-label`
  on a role-less `<div>` — invalid ARIA-in-HTML, silently a no-op.)
* **Typographic rung.** `BarWidget.tsx:191` uses `text-[11px]`; DESIGN.md defines
  exactly three body rungs and there is no 11px one. Fix to `text-xs` on the
  legend this ticket is already modifying. `check-design-tokens.sh` scans colours
  only and cannot see this. The other sites (`MeasuresEditor.tsx:69`, the two
  charts' `fontSize: 11` axis ticks) are pre-existing and out of this diff —
  filed, not fixed.

### R12 — reduced motion: disable chart animation outright

PRODUCT.md commits verbatim: "`prefers-reduced-motion` is respected for any
non-essential motion (page transitions, **chart animations**)." `globals.css:382`
implements it as a CSS block zeroing `animation-duration`/`transition-duration`.
Recharts animates through `react-smooth`'s rAF loop writing inline attributes per
frame — **not** a CSS animation or transition — so that block never reaches it.

**Ruling: `isAnimationActive={false}` on both bars.** `bar` loses its 220ms
entrance animation.

Measured, not asserted: within `components/reports/widgets/`, **five of six charts
already disable animation** — `AreaWidgetChart.tsx:101`, `LineWidgetChart.tsx:69`,
`SparklineWidgetChart.tsx:39`, `PieWidgetChart.tsx:63` and the deleted
`StackedBarWidgetChart.tsx:74`. `BarWidgetChart` was the family's lone OUTLIER,
not the norm, so converging is the smaller behavioural delta.

The alternative — gating on `matchMedia("(prefers-reduced-motion: reduce)")` —
needs a shared hook (the repo's only one is module-private inside
`components/Tooltip.tsx:87`), a new test, and still leaves the other five reports
charts inconsistent. Disabling is unconditionally reduced-motion-correct for zero
new machinery.

⚠ This is visible to users who did NOT request reduced motion, so it is called
out in the PR body as a judgement call the operator may reverse.

**Filed, not fixed:** `components/dashboard/widgets/BudgetBarsWidget.tsx:70,86`
and `ForecastBarsWidget.tsx:63,71` still animate in JS at 220ms with no
reduced-motion gate. Live a11y defects, outside this diff.

### R13 — the duplicate widget factory is a second site and must not be missed

`frontend/app/reports/[id]/page.tsx:105-141` defines its OWN `emptyMultiSeries`,
byte-identical to `frontend/components/reports/widgetKit.tsx:57-84`, plus its own
`emptyKPI`/`emptyBar`/`emptyWidget` (`page.tsx:181-206`), and it is **live** —
`page.tsx:384` calls the local `emptyWidget`, while `/reports/new` uses
widgetKit's copy. Any change to the `stacked_bar` seed applied to one module only
means a stacked bar created from the saved-report editor differs from one created
in the draft editor. This is the repo's signature half-fix-leaves-a-door shape.

**Both factory sites also seed `sort: {by:"value", dir:"desc"}` on a `month`
primary** (`widgetKit.tsx:64-65` and the twin). Under R3 branch 3 that would
render every newly created stacked bar's month axis in spend order — the
arrangement R9 calls out as never the intended reading of a time axis. R9 fixes
the template and R3 branch 1 fixes already-cloned reports; the factory is the third site
and nothing else covers it. **Seed `sort: {by:"dimension", dir:"asc"}` when the
seeded primary is a time dimension, at BOTH sites.**

### R14 — DoD item 3's "reordering" is unsatisfiable as written

The ticket's DoD 3 asks that adding, removing **or reordering** a series visibly
changes the chart. `MeasuresEditor.tsx:54-65` offers `add` and `remove` only —
**there is no reorder control anywhere.** Adding one is a new interaction flow,
i.e. a design change requiring approval, and it is out of this ticket's scope.
Adding and removing are fenced; reordering is filed as a follow-up and called out
in the PR body so the operator can overrule.

## Fences

Every test below is a `fence` (fails against a named wrong implementation)
unless marked `guard` (regression net). Each fence names what it kills.

| # | Fence | Kills |
|---|---|---|
| F1 | **Mount the widget** with a 2-dimension query response (2 months x 3 categories, one pair missing) and assert the RENDERED segments and the CSV totals. Assert the month total is **not** the smallest category. | last-write-wins `mergeSeriesRows`. ⚠ A unit test of `pivotBySecondaryDimension` alone is VACUOUS — that function is already correct on `main` (`series.ts:267-329`) and never touches `mergeSeriesRows`. It must mount. |
| F2 | `stacked_bar` with `dimensions:["month","category"]` and ONE measure -> >=2 series keys reach the chart; DOM legend lists the category names | reading `dimensions[0]` only; gating `stackId` on `measures.length >= 2` |
| F3 | `runQuery` called **exactly once**, AST carries `dimensions:["month","category"]` and `limit: 500` | keeping the `useSeriesQueries` fan-out; leaving the AST limit at `config.limit` |
| F4 | Legacy `measures:[sum(amount), sum(amount)]` -> one series, no crash, **persisted config unmutated by render** | blind `measures[1]` indexing; rewriting config at render |
| F5 | `stacked:false` + secondary dim -> `stackId === undefined`; unset/`true` -> `"stack"` | hard-coding `stackId="stack"`; the current `< 2` gate |
| F6 | 2 dims + `sort:{by:"value"}` + `limit:10`, **12 primaries supplied** -> at most 10 primary labels survive, chosen by summed total | shipping `limit:10` on a 2-dim query (Defect C); raising the AST limit without the client-side cap |
| F7 | 2 dims, primary is `month`, **`sort` absent**, **backend rows delivered in value-desc order**, **primaries (14) > limit (12)** -> the 12 **most recent** months survive, in ascending chronological order, with **no gaps** | ⚠ the "keep backend order, take first N" reading, which drops an arbitrary subset of months. A fixture with primaries <= limit is VACUOUS here — both implementations agree. |
| F8 | Sliced CSV headers/rows agree with the rendered chart for a 2-secondary case | repairing the chart but leaving the export on the collapsed single column |
| F9 `guard` | The measures array reaching `useWidgetFormat` after the pivot is still length-1 and still the real measure; transactions `sum(amount)` formats as currency | feeding pivoted `s0..sN` keys into the format resolver. (Relabelled from `fence`: the named wrong implementation is a type error and unreachable, so this is green on `main` and cannot be injection-tested.) |
| F10 | `DataTab` for `stacked_bar` renders "Break down by", writes `dimensions[1]`, clears on "None" | leaving the gate at `bar \|\| table` |
| F11 | `DataTab` for `stacked_bar` renders a single measure editor (no `measure-add`); editing writes back `config.measures[0]`, **array length 1, key still `measures`**, and **no `measure` key present** | (a) re-opening the deleted fork; (b) "simplifying" to `config.measure` -> 422 on next save; (c) dual-writing both keys. ⚠ The implementation trap is flipping `isMultiSeries` (`controlConstants.ts:108-117`), which is also the type guard `setSeries` early-returns on (`useWidgetMutations.ts:90`) — flipping it routes writes to `setSingleMeasure` and `config.measure`. |
| F12 | `cdd-stacked-by-month` carries `sort:{by:"dimension",dir:"asc"}`, an explicit `limit`, **exactly one measure**, and still validates against `LayoutJson` | shipping the fix on a spend-ordered axis; a future template edit adding a second measure (`measures` has `min_length=1` and **no max**) |
| F13 | `+ Add series` on transactions `[sum(amount)]` seeds `avg(amount)`; on **`networth`** the button is **disabled** | the duplicate seed (Defect B); the agg-rotation fallback, which on networth returns byte-identical rows because `build_rows` ignores agg/field |
| F14 | `[sum(amount), avg(amount)]` -> seeds `count(id)`; all three present -> **disabled** despite `MAX_SERIES=5` | de-duping only against `measures[0]`; any agg-rotation fallback (which would leave it enabled and seed `sum(id)`) |
| F15 | Both `renderReportWidget` and `widgetKit.renderWidgetByType` mount the same component for `stacked_bar` | updating one routing site and not the other |
| F16 | Parameterized over **{9, 11}** distinct secondary values -> exactly 8 series keys, 8th labelled "Other" in the neutral token, pinned last, and the 8 segments sum to the raw values | the `i % 8` collision; a fold that drops the tail; "Other" taking `CHART_SERIES[7]` (the danger hue) |
| F17 | Parameterized over **{7, 8}** distinct secondary values -> no "Other", and **the label->colour mapping is identical to today** | ⚠ a fold whose trigger is `>= 8` instead of `> 8` (off-by-one that repaints a correct 8-colour chart). A 5-value fixture is VACUOUS. Asserting key COUNT alone misses the reorder — assert colour-per-label. |
| F18 | Adding a series to a **Line** widget changes the **RENDERED output and the CSV values**, not merely the `measure` payloads | ⚠ an inert renderer; a fence reading `config.measures.length`; **and the next-order trap** — asserting the two `runQuery` payloads DIFFER goes green on networth, where differing payloads return identical rows |
| F19 `guard` | 1 dimension, `limit` absent, `stacked_bar` -> AST limit is not silently 10 | the `?? 100` -> `?? 10` cell change when `stacked_bar` moves into the bar branch |
| F20 | `emptyWidget("stacked_bar")` is **identical** from `widgetKit.tsx` and from `app/reports/[id]/page.tsx`, and both seed `sort:{by:"dimension",dir:"asc"}` for a time primary | the half-fix door: fixing one factory site and leaving the live duplicate, so the saved-report editor and the draft editor create different widgets |
| F21 | 11 primaries with `limit:10`, where a secondary appears **only** under the dropped 11th primary with a large value -> that secondary is **absent** from the legend, and a real visible secondary is **not** in "Other" | fold-before-cap ordering, which awards a legend entry and a palette slot to a secondary contributing 0 to every rendered bar |
| F22 | 11 distinct secondary values -> CSV carries **12 columns** (label + 11 raw), while the chart shows 8 | applying the fold to the export, severing the only drill path from "Other" to its rows |
| F23 | The same rows fed in **reversed** order produce an **identical** label->colour mapping | first-seen colour assignment, under which a category changes hue between two loads |
| F24 | 2 dims, `config.limit` **absent**, 12 primaries supplied -> all 12 render | the unspecified default; reusing `?? 10` from the bar branch and cutting an already-cloned template's months |
| F25 | `stacked_bar` with **no** secondary dimension and one measure -> renders one series, no legend, no crash | the degenerate case falling through the new sliced path |
| F26 | **Removing** a series from a Line widget removes the rendered line and its CSV column | DoD 3's removal half, which no fence otherwise covers |
| F27 | `matchMedia("(prefers-reduced-motion: reduce)")` matched -> `isAnimationActive === false` on the chart | inheriting `animationDuration={220}` as live motion for a user who asked for none |
| F28 | Each rendered `<Bar>` carries `stroke="var(--color-surface)"` | adjacent segments with no separator, all palette pairs measuring under 1.6:1 |
| F29 | The type->component routing tables in `render-report-widget.test.tsx` and `dashboard-widget-registry.test.tsx` assert each type carried **its OWN** widget through (stub echoes `data-widget-type`), not merely that some stub rendered | ⚠ the silent collapse: after the merge two rows both point at `bar-widget-stub`, so retargeting the row compiles, passes, and certifies STRICTLY LESS than before. This is the "fence records coverage, not path" class. |
| F30 | The config-shape adapter is fenced through the **CSV header**, using a measure whose field LABELS differently (`count`/`id` -> "Row count" vs the default "Amount") | ⚠⚠ the vacuous adapter test. Asserting `runQuery` received the right `measure` is GREEN against a hardcoded adapter, because `buildQueryAst` reads `config.measures[0].measure` itself — a downstream path masks the mutant entirely. Measured: hardcoding the adapter left 8/8 passing. **Anyone implementing this will write that test and believe it.** |
| F31 | `config.stacked` reaches the chart: `vi.mock` the `BarWidgetChart` module (this DOES intercept the `next/dynamic` import) with a stub emitting `data-stacked`, and assert both `true` and `false` | ⚠ hardcoding `stacked = true` — which deletes the ENTIRE remaining semantic difference between `bar` and `stacked_bar` — left 406/406 green, because `stacked` only manifests inside a `next/dynamic` recharts subtree that jsdom collapses to 0x0. The gap pre-dates TBD-382, but R15 concentrates the whole type difference into this one boolean. |

## Verification protocol

Per fence: RED before implementation, green after, then **re-introduce the named
wrong implementation and confirm RED again**, and record the evidence. Mutation
testing is not a substitute — it cannot see a stale fixture, an unvaried
argument, a self-comparing assertion, or a fixture on which the right and wrong
implementations agree.

Full gate before the PR: `npx tsc --noEmit`, `eslint --quiet`, the FULL
`vitest run`, and `frontend/scripts/check-design-tokens.sh`.

## R15 — component structure: DELETE the duplicate, settled by building it

**Delete `StackedBarWidget.tsx` and `StackedBarWidgetChart.tsx`; route
`stacked_bar` through `BarWidget`/`BarWidgetChart`.** Keep the `stacked_bar`
TYPE — the enum member, the discriminated union at `report_layout.py:242-253`,
the WidgetPicker entry and the template all stay. Deleting the type would be a
data migration over `reports`, `report_versions` and dashboard layouts for zero
gain.

The two architects produced a clean position SWAP on this — each conceded to the
other — which this project's memory flags as information asymmetry rather than
resolution. It was settled by BUILDING it, not by a third reading round.

Measured outcome: after R1, only **six expressions** discriminate the two types,
five of them contiguous in an 11-line block at the top of a 277-line file;
everything below is type-agnostic, and `BarWidgetChart` has **zero**
discriminating expressions. Source goes 4 files -> 2, 560 lines -> 427.
`buildQueryAst` already reads `config.measures[0]?.measure` for `stacked_bar`, so
the query layer needed NO change. Three of the five `vi.mock` sites were dead
weight (they existed only to stop a module-scope import dragging recharts in).

Option B's `breakdown.ts` would therefore have contained essentially the whole
widget, leaving two ~40-line shells differing by a testid, a title and a boolean.

**Known wart, accepted:** the merged component is not type-agnostic, and
`stacked` lives on `StackedBarConfig` only. Hoisting it onto `BarConfig` would
hand `bar` a grouped mode nobody asked for. If follow-ups later give the two
types genuinely different behaviour again, that 11-line block grows and this
ruling ages badly. That is the failure mode to watch.

**The `next/dynamic` loading testid** cannot be parameterized off `widget.type`
(the `loading` element is built at module scope with no props). Mint one dynamic
wrapper per prefix via a factory; the import specifier stays a literal in each so
the bundler still emits one shared chunk. Note this is NOT an Option A cost —
Option B has the identical problem.

## Visual-approval consequence of R15

Routing `stacked_bar` through `BarWidget` relocates its legend from recharts'
in-SVG `<Legend>` to the DOM `<ul>` below the chart, which consumes height inside
a fixed `h:4` grid cell, and swaps its tooltip surface. Neither PRODUCT.md nor
DESIGN.md specifies legend placement, so there is no "already supposed to look
like this" to restore — which is the definition of a design decision rather than
a bugfix.

**Therefore R15 requires operator visual approval BEFORE the PR opens.** One
screenshot of the rebuilt `cdd-stacked-by-month` panel at its real grid size
discharges it. R12's animation change is called out in the same pass.
