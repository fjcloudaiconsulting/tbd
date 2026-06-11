# Reports v3 — Flexible Canvas & Pluggable Data Sources

**Date:** 2026-06-11
**Status:** Design — pending user review
**Branch:** `feat/reports-v3`
**Supersedes parts of:** `2026-05-22-reports-v2-flexible-canvas.md`, `2026-06-01-reports-simplify-and-complete.md` (v2 canvas/edit model)

## Problem

The current Reports v2 builder has five concrete problems, confirmed from a user screen recording:

1. **Widgets don't respect position/size.** The grid auto-compacts and layout isn't reliably persisted, so widgets jump and the canvas renders as a ragged masonry with gaps.
2. **Opening "Widget settings" reflows the whole canvas.** The settings panel (`ConfigRail`) takes horizontal space → the react-grid-layout container shrinks → every widget resizes/repositions.
3. **Filter model is opaque.** A pinned top card holds report-wide filters (date/accounts/categories) *plus* hidden per-widget overrides, so you can't tell what any widget is actually showing.
4. **No separation between presenting and editing.** The same chrome-heavy canvas is used to view and to build.
5. **Data source is locked to Transactions.** You can't report on accounts, net worth, recurring obligations, or compare anything else the app knows.

## Goals

- Editing a widget never moves or resizes anything on the canvas.
- A widget is **self-describing**: you can see what it queries and filters without opening it.
- Reporting is **pluggable** across app entities, starting with Transactions, Accounts, Net worth, and Recurring.
- Viewing a report is clean; editing is a deliberate mode on the same page.
- Chart values are currency-formatted.

## Non-goals (explicitly out of scope)

- Plans/Scenarios as a report source (folded into the separate Plans redesign).
- Budgets and Forecast Plans as sources **now** — deferred to a phase-2 "over-time" source (their single-period value already lives on their own pages; only cross-period trend would be new).
- Two independent layouts for view vs edit (one layout, chrome stripped).
- Global-only filters / per-widget-only filters (we chose the hybrid below).
- Text-contains filters, settled-date, reconciliation-state, flags, scheduled delivery, collaborative editing.

## Locked decisions (from brainstorm)

| Area | Decision |
|---|---|
| Widget editor | **Anchored popover** beside the clicked widget, portaled over the canvas — never changes canvas width. |
| Filters | **Shared report-level date range** (toolbar) that widgets inherit and may override; **all other filters per-widget**. Every widget shows its effective filters as chips. |
| Data sources (now) | Transactions, Accounts & balances, Net worth over time, Recurring obligations. |
| Data sources (later) | Budgets / Forecast Plans (phase-2 over-time trend), Plans/Scenarios (Plans redesign). |
| View vs Edit | **Same arrangement**, editing chrome stripped in View; **toggle** on the same page (no separate route). |
| Layout policy | **Free 12-column grid, positions honored literally** — no auto-compaction, no auto-movement when widgets are added/removed/resized; gaps allowed. |
| Backend extension | **Source registry / semantic layer** (one class per source behind a common interface). |

---

## Architecture

### Backend — Source registry

A `ReportSource` interface with one implementation per source, registered in a keyed lookup.

```
class ReportSource (Protocol)
    key: str                         # "transactions" | "accounts" | "net_worth" | "recurring"
    label: str
    def dimensions() -> list[Dimension]    # {key, label, kind}
    def measures()   -> list[Measure]      # {key, label, agg, format}
    async def build_rows(db, org_id, query: ReportQuery) -> list[Row]
```

- `Dimension.kind` ∈ {category, account, status, type, tag, time, account_type, …} — drives which filter control the editor renders.
- `Measure.format` ∈ {currency, number, percent} — drives both axis/tooltip formatting and the value formatter.
- `Row` is the existing row shape widgets already consume `{ [dimensionKey]: value, [measureKey]: number }`, so the chart layer is unchanged.

**Implementations:**
- `TransactionsSource` — wraps the existing `reports_query_service` AST compiler verbatim. No *behavior* change, but the request/validation schema does change (see Endpoints + Data model). Dimensions: category, account, status, type, tag, payee, time(month/day). Measures: sum, count, avg.
- `AccountsSource` — balance by account / account_type; pending vs cleared balance; count. Read-only over `accounts` (+ pending tx for pending balance).
- `NetWorthSource` — derived monthly time-series: assets − liabilities per month. **Materially harder than the other sources** and isolated into its own late phase (see Phasing + Open Decisions). The app has **no** asset/liability classification and **no** historical balance snapshots, so the series must be reconstructed from `opening_balance` + cumulative signed transaction deltas per account, bucketed by month, summed by an asset/liability sign that does not exist yet. Traps: transfer legs (use `reportable_transaction_filter()` or double-count), manual-adjustment rows, pending vs settled, and multi-currency accounts (net worth across EUR+USD is undefined without FX). Likely needs a migration (classification) — so the "no schema change" claim does **not** extend to NetWorth.
- `RecurringSource` — committed/upcoming recurring amounts by category and due-month from recurring templates.

Every `build_rows` is org-scoped; the registry never bypasses `org_id`.

**Endpoints:**
- `GET /api/v1/reports/sources` → catalog: each source with `dimensions[]` + `measures[]`. The editor reads this; the frontend hardcodes nothing about a source's shape.
- `POST /api/v1/reports/query` → `{source, measure, dimensions[], filters, date_range, sort, limit}` → dispatch to the source → rows. Replaces the implicit transactions-only query path.
  - **One measure per call (invariant).** The request carries a *single* `measure`, matching today's `ReportsQuery`. The frontend already fans out N parallel calls for multi-series widgets and merges them in `series.ts` (`mergeSeriesRows`/`pivotBySecondaryDimension`, keyed on dimension + `value`). Keeping one-measure-per-call leaves that merge layer untouched — do **not** move multi-measure aggregation server-side.
  - **Response shape preserved verbatim:** `{rows: [{[dim]: …, value: number}], meta}` per call, so all chart/table widgets consume it unchanged.
  - **Request schema is a superset of the legacy AST (Phase 1 change to `reports_query.py`):** the closed `Dataset` enum (single `transactions` member) becomes a `source` discriminator; `Measure._validate_agg_field`'s hardcoded "SUM/AVG require `field='amount'`" rule moves to **per-source** measure validation (Accounts `balance`, NetWorth series are not `amount`). The existing `test_reports_query_service.py` parity suite is **migrated** to the new body shape, not left unchanged.

### Frontend — Canvas, modes, editor

- **Layout engine:** keep react-grid-layout but set `compactType={null}` + `preventCollision` (no `allowOverlap`) so positions are literal and widgets never auto-move; collisions are simply rejected on drop, gaps are allowed. Persist `{x,y,w,h}` per widget in `layout_json`; load on mount; save on drag/resize **stop** only. `Canvas.tsx` today sets *neither* prop (defaults to vertical compaction) — that is the root of bug #1. **Guard `handleLayoutChange`** against react-grid-layout's mount-time/no-op normalization emissions so loading a report does not spuriously mark it `dirty`.
- **No-reflow guarantee:** the editor is a **portaled popover** anchored to the selected widget, so it overlays and never alters the grid container width. Fixes #2. **This is net-new infrastructure** — the repo has no popover/floating/portal primitive and no `@floating-ui`/radix/headless dep. Phase 4 adds `@floating-ui/react` (anchor measurement, flip/shift at viewport edges, outside-click, focus handling) and repositions the popover when the underlying widget moves/resizes. Budget this; it is not a `ConfigRail` swap.
- **View/Edit toggle:** single page, `isEditing` state. View = charts + shared date control + "Edit" button, no handles/gridlines. Edit = drag/resize handles, "+ Add widget", click-a-widget-to-open-popover, "Done" button.
- **Filter model:** report-level date range lives in the toolbar (both modes). Per-widget filters live in the popover. Each widget header renders **filter chips** of its effective filters; the date chip is styled "inherited" vs "override".
- **Editor popover contents:** title · source dropdown (from `/sources`) · measure(s) · dimension(s) · per-widget filters (accounts, categories, status, type, amount, tags) · date inherit-or-override. Changing source swaps the available dimensions/measures from the catalog.
- **Data layer:** `useReportQuery` calls `POST /reports/query` with `widget.source`; `/reports/sources` fetched once via SWR to drive the editor. New widgets copy the previous widget's filters (and date-inherit) so users aren't retyping.
- **Currency formatting:** one shared recharts value formatter keyed on `measure.format` + org currency, applied to tooltips and axes across Bar/Line/Area/StackedBar/Pie/Sparkline/Table. Closes the deferred backlog item.

### Data model / persistence

- `layout_json` / `canvas_filters_json` are `JSON` columns, so **no DB migration for widget storage**. But "no DB migration" ≠ "no schema change": the strict Pydantic validators in `report_layout.py` and `reports_query.py` **must change** in Phase 1 (see below). NetWorth's classification likely *does* need a DB migration (Open Decisions).
- **`source` lives on the widget `config`, not the envelope.** `_WidgetBase` is `extra="forbid"` with a regression test (`test_reject_extra_widget_envelope_key`); adding `source` there would 422. The config models are `extra="ignore"` and already hold `dataset`, so `source` slots next to it forward-compatibly. Phase 1 still must **widen the closed `Dataset` enum and relax the required `dataset` field** so non-transactions widgets validate, while keeping the `extra="forbid"` envelope test green.
- Pre-launch, no back-compat shim: a widget config with no `source` is read as `source:"transactions"` by hard default.
- **Report-level filters: `canvas_filters_json` surfaces only `date_range` in the UI**; accounts/categories move to per-widget. The backend `CanvasFilters` schema (`extra="forbid"`) **keeps** the `account_ids`/`category_ids` fields modeled-but-tolerated so any saved report still carrying them does not 422 on next PATCH; the v3 UI simply stops reading/writing them. (Pre-launch we could instead remove the fields and wipe old rows, but tolerate-and-ignore is lower-risk and needs no data touch.) The cascade code (`resolve.ts`, `CanvasFiltersBar.tsx`, `resolve.test.ts`) is edited to drop the account/category tier.
- New sources Accounts/Recurring are read-only queries — no schema change. NetWorth is the exception (classification).
- **Formatter precedence:** a widget's explicit `config.format` (if set) wins; otherwise the registry `Measure.format` is the default. Documented so the two don't diverge.

## Error handling

- `/reports/query` validates `source` against the registry → 400 on unknown source; validates requested dimensions/measures belong to that source → 400 with the offending key.
- A widget whose source/measure no longer resolves renders an inline "this widget needs attention" state (not a crashed canvas), and the popover surfaces what's invalid.
- Net worth / recurring with an empty range return an empty series, not an error.

## Testing

- **Backend:** per-source `build_rows` tests — org-scoping isolation, each dimension/measure correctness, empty-range behavior; `/sources` catalog shape; `/query` dispatch + 400s. `TransactionsSource` must reproduce the existing reports-query results (parity proof) — the parity suite is migrated to the new `/query` body shape but its expected outputs are identical.
- **Frontend (full `vitest run`, not single-file — per the project's full-suite rule):** popover opens without changing grid geometry (no-reflow assertion); layout persists literally across save/reload; filter chips reflect effective filters; view/edit toggle strips/adds chrome; source-swap repopulates editor dropdowns; currency formatter output.

## Phasing (each phase = its own PR, conventional-commit title, subagent-driven)

1. **Backend registry + TransactionsSource parity** — `ReportSource` interface + registry, `TransactionsSource` wrapping the existing compiler, the `reports_query.py` schema widening (`source` discriminator + per-source measure validation), `/sources` + `/query` endpoints, migrated parity tests. (No frontend change; transactions behavior identical.) Includes the `report_layout.py` change to accept `source` on widget config + relax `dataset`.
2. **Frontend data-layer swap** — `useReportQuery` → `POST /reports/query` with `widget.source`; fetch `/sources` once via SWR. **Data layer only — no editor UI change** (the source dropdown lands in Phase 4 with the new editor, to avoid throwaway work in `ConfigRail`, which Phase 4 replaces).
3. **Canvas layout fix** — `compactType={null}` + `preventCollision`, literal `{x,y,w,h}` persistence, save-on-stop, `handleLayoutChange` mount-time guard. No auto-movement.
4. **View/Edit toggle + popover editor** — add `@floating-ui/react`; replace `ConfigRail` with the portaled anchored popover (incl. the **source dropdown**); filter chips; shared-date inherit/override model; drop canvas account/category tier in `resolve.ts`/`CanvasFiltersBar`.
5. **AccountsSource + RecurringSource** + editor wiring — the two cheap, single-table read-only sources.
6. **NetWorthSource** — isolated. Gated on the Open Decisions below (asset/liability classification + currency). Carries its own migration if classification becomes a column. Do not start until those decisions are made.
7. **Currency formatting** across all widgets (formatter precedence: `config.format` › `Measure.format`).

Phases 1–4 are the core redesign. 5 adds the cheap sources. 6 (NetWorth) is isolated and decision-gated. 7 is polish. 5, 6, 7 are independently shippable.

## Open Decisions (block Phase 6 only — core build proceeds without them)

1. **Asset vs liability classification.** No such flag exists. Options: (a) hardcoded slug map for the 5 system types (`checking/savings/investment/cash` = asset, `credit_card` = liability) + a per-type default for user-created custom types; (b) a new `kind`/`is_liability` column on `account_types` (migration) with admin/UI to set it. (a) is faster and ships NetWorth without a migration; (b) is correct long-term for custom types.
2. **Multi-currency net worth.** Accounts carry a `currency`. Summing EUR+USD balances is meaningless without FX. Options: (a) NetWorth assumes a single-currency org and sums raw (document the limitation); (b) out-of-scope until an FX/rates feature exists. Recommend (a) for now.
