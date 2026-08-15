# TBD-381 — catalog-driven widget editor

**Status:** design settled 2026-08-14, ready to build.
**Owner ticket:** TBD-381 (Epic TBD-380, sequenced first, before TBD-382 and TBD-383).

Two architects worked the design independently, then a third round resolved the
five points they split on. Every ruling below is backed by a read of the real
code; where a ruling contradicts one of the architects, or the ticket, that is
called out. **This document is DoD item 1** — the ticket requires the
architecture ruling be written down before building.

---

## The invariant

Two, because the write side and the read side are different problems.

> **I1 (offer ⇔ honour).** For a widget bound to source `S` with catalog entry
> `E`: a filter control is offered **iff** its field is in `E.filters`; a
> dimension **iff** it is in `E.dimensions`; a measure **iff** the exact
> `(agg, field)` pair is a row of `E.measures`.
>
> **I2 (format follows the measure).** A rendered number is formatted by the
> `format` of the catalog row that produced it. Format is never authored, never
> stored as truth, never guessed.

Sharpenings that the ticket's DoD does not state:

- **I1's measure clause says PAIR, not cross product.** The catalog's atom is a
  measure row. The editor currently models it as two independent selects whose
  cross product is not a subset of the catalog. That mismatch is the root of
  Symptom 3 *and* of the field-only-vs-`(agg, field)` argument in
  `useWidgetMutations.ts:41-52`.
- **I1 constrains SHAPE, not VALUE DOMAIN.** `SourceFilter` publishes
  `field/label/ops/kind` and no value domain. Nothing in the catalog can say
  that `txn_type` means income|expense on `recurring` and
  income|expense|transfer on `transactions`, or that `account_type`'s value is
  an `account_type_id` int. Any claim of "fully catalog-driven filters" is a
  lie about this. Say so in the code.

---

## Ruling 1 — DoD item 1: derive format at RENDER time

**Mutation-time derivation cannot work, and the ticket undercounts the reason.**
The write sites are not three. They are 28, across five files and two languages:

| where | count |
|---|---|
| `config/useWidgetMutations.ts` (`resolveFormat` call sites) | 3 |
| `components/reports/widgetKit.tsx` (draft editor factories) | 5 |
| `app/reports/[id]/page.tsx` (a **second, duplicated** factory set) | 5 |
| `lib/reports/draft.ts` | 1 |
| `backend/app/reports/templates.py` (starter reports, stamped server-side) | 14 |

No frontend resolver can reach the 14 Python ones. The read side is eight lines.

**It is already broken in shipped code**, which settles it empirically rather
than by argument:

- `widgetKit.tsx:119` seeds `format: "number"` for a `sum(amount)` transactions
  sparkline → **every new sparkline renders raw numbers today**.
- `templates.py` `cdd-pie-share` omits `format` → `?? "number"` → a currency pie
  renders unformatted.

Neither goes through a mutation, so neither is reachable by mutation-time
derivation at all.

⚠ **The ticket's proposed render site is wrong.** `formatMeasureValue`
(`lib/reports/series.ts:133`) is a pure function that *takes* `format` as a
parameter, called from nine chart files. The catalog is not in scope there and
cannot be without turning it into a hook. The real derivation point is a new
hook beside `useReportSources`.

**The catalog IS reachable at every render site — verified, not assumed.**
`useReportQuery.ts:47` and `:237` already call `useReportSources()`, and all
eight widget wrappers use one of those hooks. Dashboard tiles are real:
`renderDashboardWidget.tsx:98` `default:` → `renderReportWidget` → the same
components → the same hooks. `WidgetFilterChips.tsx:62` is existing precedent
for reading the catalog at render.

The fact that decides it: `/api/v1/reports/sources` sits behind the **same**
`require_feature(Feature.REPORTS)` gate as `/reports/query`
(`routers/reports.py:88`). A user who cannot fetch the catalog cannot fetch the
data either, so catalog-unavailable and data-unavailable coincide exactly.
Render-time derivation introduces **no new failure mode on the dashboard path**.

### The resolver

At render there is no stale previous value to preserve, so match the exact pair:

1. exact `(agg, field)` in `entry.measures` → its `format`;
2. else `agg ∈ {count, distinct}` → `"number"` (a cardinality is never
   currency — this is what makes a legacy `count(amount)` correct, which
   field-only matching gets **wrong** today);
3. else field-only match → its `format`;
4. else `"number"`.

⚠ **`useWidgetMutations.ts:41`'s "MATCH ON FIELD ONLY — do NOT add
`&& m.agg === measure.agg`" dies with the approach it defends.** It describes a
mutation-time hazard (preserving a stale format on a resolver miss). Verified
across all five sources: no field maps to two formats today, so step 3 is a safe
backstop rather than a guess.

**Cold-cache cost and its fix:** the catalog becomes a render dependency. Fold
`useReportSources().isLoading` into the `isLoading` returned by `useReportQuery`
/ `useSeriesQueries`; all eight wrappers already render skeletons off that flag.
First-paint only, single shared SWR key.

---

## Ruling 2 — delete `config.format` outright

Not "stop writing but keep reading". **The fallback IS the bug.**

Every write site hardcodes a constant: `"currency"` at `widgetKit.tsx:29,48,69,101`,
`draft.ts:38`, and `templates.py` ×14; `"number"` at `widgetKit.tsx:119`. So a
"pre-catalog fallback" is a constant `"currency"` on essentially every saved
widget. A `credit_utilization` widget saved today carries `format:"currency"` —
the exact stale value that renders 45% as "€45.00". Keeping it as a read input
means shipping the reported bug as a **first-paint flicker** and preserving the
sometimes-derived-sometimes-stored ambiguity that caused the defect.

**No migration is needed — verified.** Every config model in
`backend/app/schemas/report_layout.py` carries `ConfigDict(extra="ignore")`, and
`validate_layout_json` ends `LayoutJson.model_validate(value); return value` —
its docstring says explicitly it must not round-trip through `model_dump` so
unmodeled keys survive. Removing `format` from the models leaves stale saved keys
validating clean and persisting untouched, inert.

**Delete from:** 6 TS interfaces in `lib/reports/types.ts` (`KPIConfig`,
`BarConfig`, `SeriesWidgetConfig`, `PieConfig`, `SparklineConfig`,
`TableConfig`); `resolveFormat` + its 3 call sites in `useWidgetMutations.ts`;
`widgetKit.tsx` ×5; `draft.ts` ×1; `templates.py` ×14; the `WidgetFormat` enum
and 2 field declarations in `report_layout.py`.

---

## Ruling 3 — collapse agg + field into ONE Measure select

Over `entry.measures`, keyed by `m.key`, still persisting `{agg, field}`.

**"Narrow the aggs and snap on field change" is not sufficient — the reachable
state set is NOT identical.** Falsifying sequence, three real UI actions, no
hand-built AST:

1. Add a Line widget → `{agg:"sum", field:"amount"}` on `transactions`.
2. Data source → **Credit utilization**. `setDataset` collapses to
   `entry.measures[0]` = `avg(utilization_pct)`. Valid.
3. Click **+ Add series**.

`MeasuresEditor.add()` (line 57) emits `{agg:"sum", field: fields[0]}` =
`{sum, utilization_pct}` — published **only at `avg`**
(`sources/credit_utilization.py:81`). Guaranteed 422, no UI feedback. That is
TBD-381's own Symptom 3, and "+ Add series" is not a field change, so snapping
never fires.

Collapsing makes an invalid measure **unrepresentable** rather than merely
validated, and turns format into an exact lookup.

**Costs, both real:**
- ~11 assertion sites across 7 test files, mostly a
  `getByLabelText("Aggregation")` → `getByLabelText("Measure")` swap.
- The per-agg tooltips (`AGG_HELP_KEY` → `reports.agg.*`) hang off the agg
  select. Keep one `HelpTooltip` on the Measure select keyed off the **selected
  row's** agg — the content is per-agg, not per-select. Note `distinct` is
  offered by `AGG_OPTIONS` today but published by **no** source, so
  `reports.agg.distinct` goes dead either way.

A persisted pair absent from the catalog (legacy `distinct(id)`) renders as an
extra "(unsupported)" option plus an inline notice. **Never silently rewritten.**

---

## Ruling 4 — one format for shared-axis charts, per-column for Table

**Shared-axis charts (kpi, bar, line, area, stacked_bar, pie, sparkline): one
derived format — unanimous across series, else `"number"`.** These pass a single
format into a single Recharts `tickFormatter` for one `<YAxis>`
(`LineWidgetChart.tsx:47`, `AreaWidgetChart:84`, `StackedBarWidgetChart:53`,
`BarWidgetChart:86`). Putting `measures[0]`'s format on a shared scale does not
merely under-serve series 2 — it **mislabels** it, stamping "€" on ticks for a
series that has no such unit. A tooltip reading "45.0%" against an axis reading
"€45.00" is worse than consistent-and-honest.

**Table is per-column, IN THIS TICKET.** The objection that this needs
`SeriesConfig.format` (which is `extra="forbid"`, so a 422) is **factually
correct but irrelevant**: once format derives at render, nothing is persisted.
`TableWidget.tsx` already holds `measuresConfig` and formats cell-by-cell at
`:246` and `:264`, so it is `measuresConfig.map(m => formatForMeasure(entry, m.measure))`
and an index. Zero backend change, no follow-up.

**CSV needs nothing** — `seriesCsv.ts` and `TableWidget`'s `csvDataset` emit raw
numbers, not formatted strings.

Author-side affordance: when chosen series' formats disagree, show an inline
notice ("mixed units — values render as plain numbers"). Do **not** forbid the
mix. The stacked-by-secondary-dimension path (`StackedBarWidget.tsx:71-77`) uses
one measure across all segments and is never ambiguous.

---

## Ruling 5 — filters are SUBTRACTIVE. No `kind` registry, no new controls

**A `kind → component` registry is unsound on today's catalog.** The same field
name carries different kinds across sources:

- `transactions.py:40` — `SourceFilter("amount", …, "amount")`
- `recurring.py:72` — `SourceFilter("amount", …, "number")`
- `accounts.py:57` — `SourceFilter("balance", …, "number")`

`kind` is documented at `sources/base.py:25` as a **"control hint"** — a hint,
not a normalized taxonomy. Keying dispatch on it means the field `amount`
resolves to different registry entries depending on the source.

**The field-keyed map already exists and is already correct:**
`FILTER_KEY_TO_SOURCE_FIELD` (`lib/reports/resolve.ts:281`), whose own comment
calls it "the single source of truth for the WidgetFilters↔source-field
mapping", already consumed by `pruneFiltersToSource`.

So `FilterEditor` renders each existing control **iff**
`entry.filters.some(f => f.field === FILTER_KEY_TO_SOURCE_FIELD[key])`.

This also generalizes the two ad-hoc one-field helpers
`sourceSupportsDateFilter` / `sourceSupportsStatusFilter` (`resolve.ts:135,161`,
consumed by `WidgetFilterChips.tsx:62` and `useReportQuery.ts:48`) into one
`sourceSupportsField(sources, dataset, field)`. Real consolidation; a registry
would be indirection over a map that already works.

### The gate is `resolveFilters`, not the control

A hidden control still leaks via persisted config and via the canvas cascade.
`resolveFilters` emits a field only if published. Mutation-time pruning stays,
as hygiene (don't accumulate junk, don't lie in the chips), not as the net.

### Shared-canvas semantics — preserved, not regressed

- `sources/base.py:37` sets `SHARED_CANVAS_FILTER_FIELDS = {date, account_id, category_id, status}`;
  `validate_against_catalog` **raises** for any other unpublished field.
  Gating client-side drops the same fields the server drops, plus the ones the
  server would 422. Strictly safer, never louder.
- ⚠ **The empty-catalog bias is load-bearing.** `sourceSupportsField` must
  return "unknown → allow everything" when `sources` is empty, exactly as the
  two existing helpers do. Invert it and a cold SWR cache silently strips every
  filter, rendering unfiltered totals.
- ⚠ **Do not gate on `ops`.** No source publishes `relative` in its date ops;
  the backend resolves `relative` → `between` **before** validating
  (`_resolve_relative_date_filters`). An op-aware resolver would silently kill
  the `next_cycle` preset. Read `ops` only to **assert in a test** that each
  control's emitted ops are a subset of what every publisher allows.
- The pill stays bespoke: `isFieldOverridden` is a property of the two
  canvas-shared fields, not of a `kind`. Only the date and status controls take
  `canvasFilters`.
- The date control correctly **disappears** on `accounts` / `recurring` /
  `credit_utilization` (none publish `date`), which finally makes the editor
  agree with the resolver and the chips.

### Free win, and a deletion

- **Restores a false negative:** `recurring.py:72` publishes `amount`, but
  `FilterEditor.tsx:158` gates `AmountRangeFilter` on
  `allowTransfer = dataset === "transactions"`. Subtractive gating **gains**
  recurring the amount control it already supports — existing component, no new
  surface.
- **Delete** `allowTransfer` and its three blocks, and the `hasIllegalTransfer`
  self-heal effect at `FilterEditor.tsx:230-245` — dead under a catalog-driven
  `txn_type` gate.
- **Keep** the Transfer member of the txn_type control and
  `include_non_reportable` gated on `dataset === "transactions"`. The catalog
  publishes ops and kinds, never enum **values**, and
  `include_non_reportable` is a query mode, not a filter field. Comment both as
  known catalog gaps rather than pretending otherwise.

---

## Collision boundary — AMENDED

⚠ The ticket's boundary contradicts its own DoD-1: render-time format requires
editing the eight widget wrappers that read `widget.config.format`, and the
boundary assigns them to TBD-382.

**The overlap is smaller than it looks.** `widget.config.format` is read in
exactly eight files, one identical line each
(`const format = widget.config.format ?? "number";`):
`KPIWidget:44`, `BarWidget:105`, `LineWidget:58`, `AreaWidget:59`,
`StackedBarWidget:63`, `PieWidget:59`, `SparklineWidget:63`, `TableWidget:117`.

The six `*Chart.tsx` files receive `format` as a **prop** and never read
`widget.config` — zero edits. `renderReportWidget.tsx` threads only `currency` —
zero edits. `series.ts` keeps `formatMeasureValue`'s signature — zero edits.

**Ruling: TBD-381 takes format derivation end-to-end.** Deferring is incoherent
— if TBD-381 stops writing `format` but cannot edit the readers, every widget
falls to `?? "number"` and **currency reports lose their symbols in production
between the two PRs**, a worse regression than the percent bug being fixed.
TBD-382 keeps `renderReportWidget.tsx`, `series.ts` and all six `*Chart.tsx`
untouched by this PR.

---

## Corrections to the ticket

1. **Severity understated.** "Offers transaction filters that silently do
   nothing" is true only for `category_id` (a shared-canvas field, dropped).
   `txn_type` and `tag_name` on `networth`/`accounts`/`credit_utilization` are a
   hard **422**, surfaced as a bare "Couldn't load" with no explanation. The
   net-worth fence must assert **both**.
2. **The render site named in DoD-1 does not exist** as a viable target
   (`formatMeasureValue` takes `format` as a parameter).
3. **"The editor largely does not consult it"** — `DataTab.tsx` already drives
   the source list, both dimension selects and the measure *field* options off
   the catalog. The gaps are precisely filter controls, agg options, and format.
4. **"Three write sites"** — 28, including 14 in Python.
5. **`validate_against_catalog` checks measure FIELD but never AGG**, so today's
   4-agg cross product yields nonsense (`avg(id)`, `count(amount)`) rather than
   422s. Narrowing is a correctness fix, not only UX.
6. **Effort:** `effort-m` holds only with the deferrals below. Without them, L.

---

## Explicitly NOT in this ticket

- A `kind → component` registry (unsound until `kind` is normalized).
- New controls for published-but-unrepresented fields: `currency`,
  `account_type`, `account_active`, `frequency`, `recurring_active`, `balance`.
  Separate additive-filters ticket.
- Branching on `ops` at runtime.
- Deduplicating the two widget-factory sets (`widgetKit.tsx` vs
  `app/reports/[id]/page.tsx`, near-verbatim copies already drifting). Same
  "one concept, N write sites" disease this ticket cures — file it.
- Backend catalog changes: `kind` normalization, publishing value domains, and
  agg-aware `validate_against_catalog`. File all three.

---

## Fences

Per the ticket's own warning: **drive the real UI event, never a hand-built
AST.** A test constructing `{agg:"sum", field:"outstanding"}` directly passes
against the buggy lookup, because the shipped UI cannot produce that shape —
a green fence over a live bug, which already happened once on TBD-170.

1. **Net-worth sparkline offers no category control** (was a silent drop) **and
   no txn_type or tag control** (was an unexplained 422). Both directions.
2. **A `recurring` widget offers the amount control** — the restored false
   negative.
3. **"+ Add series" on `credit_utilization` produces a valid AST**, driven by
   firing the change event on the single Measure select. The two-select shape
   that produced the old bug will no longer exist to reproduce, so the fence
   asserts the emitted AST.
4. **A `credit_utilization` KPI renders `45.0%`, not `€45.00`** — derived at
   render, with `config.format` absent from the widget entirely.
5. **A legacy widget carrying `format:"currency"` on a percent measure renders
   as a percent** — proves the stale key is inert rather than a fallback.
6. **A Table with a currency column and a count column formats each column
   independently.**
7. **Empty catalog (cold cache) does NOT strip filters** — the load-bearing
   allow-everything bias.
8. `ops` assertion: every control's emitted ops ⊆ published ops for every source
   publishing that field.

Injection gate applies to all of them: confirm RED against the named wrong
implementation, then restore and confirm green.
