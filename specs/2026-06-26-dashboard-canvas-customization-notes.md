# Dashboard canvas customization — architecture & gotchas (2026-06-26)

Developer notes for the customizable dashboard's drag/resize/add behaviour and
the bug-fix wave that made it production-ready. Pairs with the user manual at
`frontend/app/docs/dashboard/page.tsx` and the W4 project memory
(`project_w4_tierB_customizable_dashboard`).

## The shared canvas and its two placement modes

`components/reports/Canvas.tsx` wraps `react-grid-layout` (RGL) and is shared by
**both** Reports and the dashboard. It now takes a `compact?: boolean` prop:

| Mode | `compactType` | `preventCollision` | Used by | Behaviour |
|------|---------------|--------------------|---------|-----------|
| literal (default, `compact={false}`) | `null` | `true` | Reports | Widgets never auto-compact; a drag/resize into an occupied cell snaps back. Authored position/size is honoured verbatim (Reports v3 #442). |
| compact (`compact`) | `"vertical"` | `false` | Dashboard | Dragging a tile over another displaces it; tiles float up to fill gaps (phone-style rearrange). You **cannot** leave an empty gap above a tile. |

Reports deliberately stays literal — flipping it to compact would re-compact
every existing saved report on next render (intentional gaps collapse). The prop
keeps the two surfaces independent. If Reports is ever switched to compact, do it
as its own decision with a migration note.

Grid constants (both modes): **12 columns**, `rowHeight={60}`, `margin={[12,12]}`.
A tile of grid height `h` occupies `h*60 + (h-1)*12` px.

## The height-fill chain (the bug that floated the resize handle)

RGL sizes each grid item to a **fixed pixel box** and draws the resize handle at
that box's corner. For the visible card to reach the handle, the card must fill
the box. That requires `h-full` at **every** level between the RGL grid item and
the tile's card:

```
RGL grid item (fixed px height, draws the handle)
└─ <div data-testid={`widget-${type}`} className="h-full">   ← CustomDashboard renderWidget wrapper
   └─ WidgetShell (flex h-full flex-col; flex-1 body)         ← shared chrome
      └─ <div className="h-full [&>*]:h-full">{tile}</div>    ← renderDashboardWidget fill wrapper
         └─ tile card (e.g. <div className="card …">)         ← forced to fill by [&>*]:h-full
```

Two breaks existed and were fixed in this wave:
1. The `data-testid` wrapper (added in Phase 3 for tests) lacked `h-full`, so
   `WidgetShell` collapsed to content height → handle floated below the card.
2. The tile cards are content-height (the finance tiles don't stretch like a
   chart). `renderDashboardWidget` wraps each `dash_*` tile in
   `<div className="h-full [&>*]:h-full">` so the single-root card fills the box.

Report-cloned widgets already fill via their own `h-full` roots, so the
`renderDashboardWidget` default arm does **not** wrap them.

**Why Reports never hit this:** in Reports, `WidgetShell` is the grid item's
*direct* child (no intermediate wrapper) and its widgets (charts) are `h-full`.

## Recent Transactions: scroll + page size

The recent-tx tile holds a fixed page of rows that can exceed its cell. To keep
it contained at any size:
- The card is `flex h-full flex-col`; the **rows live in a `flex-1 overflow-y-auto`
  region**; the header, sort-row and pager are `shrink-0` (pinned). It scrolls
  inside the card instead of overflowing onto the canvas.
- **Page size is user-selectable (10/25/50/100)** via the shared `Pagination`
  selector. The size is **provider state** in `DashboardDataProvider`
  (`pageSize` + `setPageSize`, which resets to page 0); `loadPageTransactions`
  reads it and re-fetches when it changes. It is session-only (resets on reload).
  Persisting it per-dashboard would mean storing it in the widget config and the
  save flow — deferred.
- Default cell height is `h=9` (~636px, ~10 rows) so the default shows ~10 rows
  without scrolling.

## Default layout — keep in sync

The default 7-tile layout exists in **two** places that MUST match:
- backend seed: `backend/app/routers/dashboard.py` `DEFAULT_DASHBOARD_LAYOUT`
  (served by `GET /api/v1/dashboard` auto-create and `GET /api/v1/dashboard/default`).
- frontend factory: `frontend/lib/dashboard/widget-types.ts`
  `DASHBOARD_WIDGET_DEFAULTS`.

`backend/tests/routers/test_dashboard.py::test_default_layout_contains_seven_dash_tiles`
pins the exact grid coords, so it catches drift between the two.

## Tests

- `tests/components/dashboard/CustomDashboard.addwidget.test.tsx` — regression
  guard that the canvas widget wrapper carries `h-full` (the height-chain fix).
- `tests/components/dashboard/chart-widgets.test.tsx` — Recent Transactions
  rows/pager/sort/status-toggle + the page-size selector (10–100) and that the
  pager shows even when rows fit one page.
- `tests/components/dashboard/dashboard-widget-registry.test.tsx` and the mocks
  carry the full `DashboardData` shape (incl. `setPageSize`).
