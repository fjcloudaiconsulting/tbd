# Reports filter bundle — design (STATUS canvas cascade + dynamic "Next cycle")

**Date:** 2026-07-13
**Branch:** `feat/reports-status-cascade` (PR1); PR2 branches from main after PR1 merges
**Status:** approved (brainstorm + both architects)

## Problem

Two operator-requested Reports filter features (2026-06-24/25):

1. **STATUS cascade.** The Settled/Pending status filter shipped **widget-only** (#509);
   it must cascade to the **canvas tier** and the **dashboard chart cards**.
2. **"Next cycle" date preset.** A new relative date option meaning the org's **NEXT
   billing cycle** (not a calendar month). Chosen semantics: **DYNAMIC** — re-resolved
   to the current upcoming cycle every render, never frozen to absolute dates.

## Delivery

**Two PRs, no hard dependency, status first** (PR2 carries the real risk weight — a new
AST concept — so PR1 stays a near rubber-stamp). The only shared file is
`backend/app/schemas/report_layout.py`, where PR1 adds `CanvasFilters.status` and PR2
adds `CanvasDateRange.preset` — disjoint fields, trivial merge.

## Architecture note (governs both features)

`POST /api/v1/reports/query` is stateless and receives a **fully-resolved AST**. The
canvas→widget cascade, the widget-override merge, and source-pruning all run
**client-side** in `frontend/lib/reports/resolve.ts`. The backend never sees "canvas"
vs "widget" — only a flat `filters` list, with `org_id` injected at the router. The
sync compiler `compile_ast_to_query` has no `db` handle.

---

## Feature 1 — STATUS canvas cascade (PR1)

### Backend
- **`schemas/report_layout.py`** — add `status: Optional[TxnStatus]` (closed literal
  `"settled"|"pending"`) to the persistence `CanvasFilters` (it is `extra="forbid"`, so
  a canvas status currently 422s at `ReportCreate`/`ReportUpdate`). Dashboards reuse this
  same validator, so this one edit also satisfies the dashboard-chart-cards requirement.
- **`reports/sources/base.py`** — add `"status"` to `SHARED_CANVAS_FILTER_FIELDS`. This
  is the **safety net**: a canvas status stamped on a source that doesn't publish it
  (accounts/recurring) is dropped at build time rather than 422'd in
  `validate_against_catalog`. Verified the drop is real (those sources ignore unpublished
  fields), not a crash.
- **No compiler change.** `reports_query_service` already maps `STATUS → Transaction.status`
  and applies it origin-agnostically; transactions publishes `status` as `eq`-only, which
  matches what the resolver emits. Keep canvas status single-select (`op:"eq"`).

### Frontend
- **`lib/reports/types.ts`** — add `status?: TxnStatus` to `CanvasFilters` (already on
  `WidgetFilters`).
- **`lib/reports/resolve.ts` (the critical correctness item):** add a
  `sourceSupportsStatusFilter(sources, dataset)` gate mirroring the existing
  `sourceSupportsDateFilter`, thread a `sourceSupportsStatus` arg through `resolveFilters`
  and both `useReportQuery` builders, and change the status emission to:
  ```ts
  const status = widget?.status ?? (sourceSupportsStatus ? canvas?.status : undefined);
  if (status) out.push({ field: "status", op: "eq", value: status });
  ```
  **`pruneFiltersToSource` does NOT protect the cascade** (it only sanitizes persisted
  widget filters on a source switch). Without this gate, a canvas `Settled` leaks onto
  non-transaction widgets and 422s. The backend `SHARED_CANVAS_FILTER_FIELDS` entry is the
  net; this gate is the primary suppression.
- **Override plumbing:** widen `isFieldOverridden` to treat `"status"` like `"date_range"`,
  and generalize `valuesEqual` so a scalar (status) compares by `===` while `date_range`
  keeps the `{start,end}` compare. In `describe-filters.ts`, compute the cascaded status
  (`widget ?? canvas`), gate it on `sourceSupportsStatus`, and set the `overridden` flag so
  a widget status differing from the canvas status reads as an override.
- **Surfaces:**
  - `components/reports/CanvasFiltersBar.tsx` — drop the existing reusable `StatusFilter`
    under the date block, bound to `value.status`, `ariaPrefix="Canvas status"`.
  - **Dashboard** — it has **no canvas filter UI today** (it persists `canvas_filters_json`
    but renders only `DashboardPeriodNav`). Reuse `CanvasFiltersBar` **status-scoped** (hide
    the date block via a prop, to avoid a second date control colliding with
    `DashboardPeriodNav`) inside `CustomDashboard`'s customize mode, wired to the existing
    `canvasFilters`/`setCanvasFilters` state (marks dirty). Chart cards inherit via
    `renderDashboardWidget → renderReportWidget`.
  - Keep the widget `StatusFilter` gated behind `dataset === "transactions"`; add the
    override pill.

### Tri-state decision (locked)
`undefined` = **inherit canvas**. The 3-way All/Settled/Pending widget control can only
**narrow** an inherited status (symmetric with how canvas date works); there is **no
"force All that ignores the canvas"** in v1. If product later needs force-All, add a 4th
"Inherit" option and make `undefined` the inherit sentinel — deferred.

### Tests (Feature 1)
- Canvas status emits onto transactions widgets; the `sourceSupportsStatus` gate suppresses
  it on accounts/recurring (no 422).
- Widget status overrides canvas status; the override pill shows.
- Persistence round-trips a canvas status (`ReportCreate`/`Update` no longer 422).
- Dashboard customize-mode status control writes `canvas_filters_json`; a chart card inherits.

---

## Feature 2 — dynamic "Next cycle" preset (PR2)

Server-authoritative cycle math; the token is resolved **server-side at query time** so it
does not depend on materialized future `BillingPeriod` rows (verified: `GET /billing-periods`
returns only existing rows) and does not re-implement `_snap_to_cycle`'s month-length clamps
in TypeScript.

### Backend (Design A — relative token in the AST, resolve-before-validate)
- **`services/billing_service.py`** — new pure, DB-free helper
  `next_cycle_window(cycle_day, today) -> (start, end_inclusive)`:
  ```
  cur_start, _ = current_cycle_window(cycle_day, today)
  next_start   = _snap_to_cycle(cur_start + relativedelta(months=1), cycle_day)
  following    = _snap_to_cycle(next_start + relativedelta(months=1), cycle_day)
  return next_start, following - timedelta(days=1)
  ```
  Month-length clamps via `_snap_to_cycle`; **no drift** (re-derived from `today` each call);
  inclusive `[start, end]`, gap-free with the following cycle.
- **`reports/reports_enums.py`** — new closed `RelativeDateToken` enum (`next_cycle`), a
  **shared atom** used by both the persisted preset and the AST value so they cannot drift.
- **`schemas/reports_query.py`** — add `RELATIVE = "relative"` to `FilterOp`; in
  `Filter._validate_value` add a branch requiring `field is DATE` and `value ∈ RelativeDateToken`.
- **`routers/reports.py`** — a shared async pre-pass
  `resolve_relative_date_filters(db, org_id, filters)` that rewrites each
  `{field:date, op:relative, value:next_cycle}` → `{field:date, op:between, value:[start,end]}`
  using `next_cycle_window(org.billing_cycle_day, today)`. Run it **before** `source.validate()`
  in `_run_source_query` **AND** in the Sankey branch — both paths share the same `List[Filter]`,
  and omitting Sankey is the one easy silent bug. After the pre-pass, `validate_against_catalog`
  and the compiler see only an absolute `between` → **no source-catalog or compiler change**.
- Date filters compare against `effective_period_date_expr()` (`coalesce(settled_date, date)`),
  consistent with cash-basis (#453) — unchanged.
- **Persistence:** add `preset: Optional[RelativeDateToken]` to `CanvasDateRange`
  (`report_layout.py`, `extra="forbid"`). Widget-level `filters` is `Optional[dict]`
  (`extra="ignore"`), so a widget-level token passes through with no schema change.

### Frontend
- **`lib/reports/types.ts` / `CanvasDateRange`** — additive optional field
  `preset?: PresetKey` (NOT a discriminated union — keeps the ~dozen absolute `start`/`end`
  readers working; old blobs simply have no `preset`). Frozen calendar presets keep writing
  absolute `{start,end}`; only a relative preset writes `{preset}` with no start/end.
- **`lib/reports/date-presets.ts`** — add `"next_cycle"` to `PresetKey`. `buildPresetRanges`
  stays calendar-only and does **not** compute next_cycle. Clicking the chip writes
  `{preset:"next_cycle"}` directly; `matchPreset` returns `"next_cycle"` when
  `value.preset === "next_cycle"` **before** the absolute-equality loop.
- **`lib/reports/resolve.ts`** — when a date range carries `preset:"next_cycle"`, emit
  `{field:"date", op:"relative", value:"next_cycle"}` (the token travels to the backend;
  `resolveFilters` stays synchronous — no FE billing fetch). `pickDateRange` treats a
  preset-bearing range as "has a value" so a widget token override wins over the canvas.
- **`components/reports/filters/DatePresetChips.tsx`** — add `{key:"next_cycle", label:"Next cycle"}`;
  active-state keys off `value.preset`.
- **`lib/reports/describe-filters.ts`** — `dateLabel` returns `"Next cycle"` when the range
  carries the token. **v1 chip shows the label only** (the authoritative window lives
  server-side); echoing the resolved `MMM D – MMM D` on the query response is a deferred
  nice-to-have.

### Label (locked)
Chip label **"Next cycle"**, aria/tooltip **"Next billing cycle"**. Never "Next month" —
the app's domain language is "period/billing cycle", and these windows are calendar-decoupled
when `billing_cycle_day > 1`.

### Scope
Reports **canvas + widget** date presets only. The dashboard keeps `DashboardPeriodNav` for
its period; `next_cycle` is **not** added to the dashboard period nav in this effort.

### Tests (Feature 2)
- `next_cycle_window` unit: month-length clamps (cycle_day=31 into Feb), inclusive
  boundaries, gap-free, no drift across calls.
- Relative filter resolves to the correct absolute window at query time — on **both** the
  reports-query and Sankey paths.
- `FilterOp.RELATIVE` validation: rejects a non-DATE field and an unknown token.
- FE: the chip writes `{preset:"next_cycle"}`; `matchPreset` highlights it; the AST emits
  `op:"relative"`; persistence round-trips the token; `describe` shows "Next cycle".

### Expected semantic (not a bug)
A future cycle is dominated by pending/scheduled rows (pending → `settled_date` null →
`effective = date`), so **"Next cycle" + Status=Settled reads near-empty**. Correct; worth a
one-line UX note in the chip/help copy if it confuses.

---

## Out of scope
- Making `this_month`/`last_month` cycle-based (they stay calendar).
- `next_cycle` on the dashboard `DashboardPeriodNav` (Reports-only this effort).
- A 4th "force All" status option (tri-state = inherit-or-narrow in v1).
- Backend echoing the resolved window on the `/query` response (deferred nice-to-have).
- Excluding transfer legs / adjustments from reports queries
  (`reportable_transaction_filter` is a pre-existing backlog gap, not introduced here).
