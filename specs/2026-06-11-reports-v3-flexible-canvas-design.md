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
- `TransactionsSource` — wraps the existing `reports_query_service` AST verbatim. No behavior change; it just moves behind the interface. Dimensions: category, account, status, type, tag, payee, time(month/day). Measures: sum, count, avg.
- `AccountsSource` — balance by account / account_type; pending vs cleared balance; count. Read-only over `accounts` (+ pending tx for pending balance).
- `NetWorthSource` — derived monthly time-series: assets − liabilities per month over the date range. Logic fully hidden behind `build_rows` (no single-table AST).
- `RecurringSource` — committed/upcoming recurring amounts by category and due-month from recurring templates.

Every `build_rows` is org-scoped; the registry never bypasses `org_id`.

**Endpoints:**
- `GET /api/v1/reports/sources` → catalog: each source with `dimensions[]` + `measures[]`. The editor reads this; the frontend hardcodes nothing about a source's shape.
- `POST /api/v1/reports/query` → `{source, measures[], dimensions[], filters, date_range}` → dispatch to the source → rows. Replaces the implicit transactions-only query path.

### Frontend — Canvas, modes, editor

- **Layout engine:** keep react-grid-layout but set `compactType={null}` + `preventCollision` (or `allowOverlap` off) so positions are literal. Persist `{x,y,w,h}` per widget in `layout_json`; load on mount; debounced save on drag/resize **stop** only. This fixes problems #1 and #4 of v2's behavior.
- **No-reflow guarantee:** the editor is a **portaled popover** anchored to the selected widget (Popover/floating primitive), so it overlays and never alters the grid container width. Fixes #2.
- **View/Edit toggle:** single page, `isEditing` state. View = charts + shared date control + "Edit" button, no handles/gridlines. Edit = drag/resize handles, "+ Add widget", click-a-widget-to-open-popover, "Done" button.
- **Filter model:** report-level date range lives in the toolbar (both modes). Per-widget filters live in the popover. Each widget header renders **filter chips** of its effective filters; the date chip is styled "inherited" vs "override".
- **Editor popover contents:** title · source dropdown (from `/sources`) · measure(s) · dimension(s) · per-widget filters (accounts, categories, status, type, amount, tags) · date inherit-or-override. Changing source swaps the available dimensions/measures from the catalog.
- **Data layer:** `useReportQuery` calls `POST /reports/query` with `widget.source`; `/reports/sources` fetched once via SWR to drive the editor. New widgets copy the previous widget's filters (and date-inherit) so users aren't retyping.
- **Currency formatting:** one shared recharts value formatter keyed on `measure.format` + org currency, applied to tooltips and axes across Bar/Line/Area/StackedBar/Pie/Sparkline/Table. Closes the deferred backlog item.

### Data model / persistence

- `layout_json` already exists and is strictly validated (PR #424). Widgets are JSON, so **no DB migration** is needed for the new widget fields.
- Widget gains `source: str`. Pre-launch, no back-compat shim: existing saved widgets are treated as `source:"transactions"` by hard default in the reader, no data migration.
- Report-level filter shape changes: `canvas_filters_json` keeps **date_range only**; accounts/categories move to per-widget. Per the pre-launch no-data-migration policy, any accounts/categories previously stored at canvas level are **dropped**, not migrated.
- New sources (Accounts/NetWorth/Recurring) are read-only queries — no schema changes.

## Error handling

- `/reports/query` validates `source` against the registry → 400 on unknown source; validates requested dimensions/measures belong to that source → 400 with the offending key.
- A widget whose source/measure no longer resolves renders an inline "this widget needs attention" state (not a crashed canvas), and the popover surfaces what's invalid.
- Net worth / recurring with an empty range return an empty series, not an error.

## Testing

- **Backend:** per-source `build_rows` tests — org-scoping isolation, each dimension/measure correctness, empty-range behavior; `/sources` catalog shape; `/query` dispatch + 400s. `TransactionsSource` must pass the existing reports query tests unchanged (parity proof).
- **Frontend (full `vitest run`, not single-file — per the project's full-suite rule):** popover opens without changing grid geometry (no-reflow assertion); layout persists literally across save/reload; filter chips reflect effective filters; view/edit toggle strips/adds chrome; source-swap repopulates editor dropdowns; currency formatter output.

## Phasing (each phase = its own PR, conventional-commit title, subagent-driven)

1. **Backend registry + TransactionsSource parity** + `/sources` + `/query` + tests. (No frontend change; transactions behavior identical.)
2. **Frontend data-layer swap** to `/query` + `/sources` catalog; source dropdown in the editor.
3. **Canvas layout fix** — literal positions, reliable persistence, no auto-movement.
4. **View/Edit toggle + popover editor** replacing `ConfigRail`; filter chips + shared-date inherit/override model.
5. **AccountsSource, NetWorthSource, RecurringSource** + editor wiring per source.
6. **Currency formatting** across all widgets.

Phases 1–4 are the core redesign; 5 adds the new sources; 6 is the polish item. 5 and 6 are independently shippable.
