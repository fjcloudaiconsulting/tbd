---
name: Dashboard Phase 0 — Balances by Type tile
description: A single opt-in dashboard widget that consolidates account balances per account type, grouped by currency. Phase 0 of the configurable-dashboard-widgets roadmap.
type: project
---

# Dashboard Phase 0 — "Balances by type" tile

**Date:** 2026-07-23. **Roadmap:** Group C item 5 (architect-gated). **Origin:** `specs/configurable-dashboard-widgets.md` Phase 0. **Effort:** M.

## Goal

Give the user one consolidated view of *where their money sits, per account type* on the dashboard. This is the Phase-0 **validation instrument**: ship the consolidation cheaply and see whether users want it, before paying for any per-widget configurability infra (Phases 1-3 stay parked).

## Scope decision (architect + design reviewed, 2026-07-23)

The 2026-05-15 origin spec listed one tile *per* account type and described them as "static cards." Both framings are superseded:

1. **Registered opt-in widget, not a static card.** Since #506 the dashboard is entirely a widget canvas; there is no static region to attach a card to. The most recent tile (`dash_cc_utilization`) is a registered, opt-in widget absent from the seed layout. We follow that precedent exactly.
2. **One consolidated tile, not per-type tiles.** Two independent reviews (architecture + design) unanimously recommended trimming to a single `dash_balances_by_type` tile. A per-type tile is a strict subset of one consolidated row, so it presupposes the validation this phase is meant to run. Per-type tiles are a cheap fast-follow **only if real usage shows demand**. Operator confirmed consolidated-only.

**Ships:** exactly one widget — `dash_balances_by_type`. **Deferred:** per-type subtotal tiles; sparklines (no cheap historical time-series exists in the provider); loan subtotal (no `loan` account type exists yet).

## What the tile shows

One row per **account type that has at least one active account**, ordered assets-first, liabilities last, custom types after:

```
┌────────────────────────────────────────────┐
│ BALANCES BY TYPE                  Accounts  │   header: cardTitle + right-aligned link to /accounts
├────────────────────────────────────────────┤
│  🏛  Checking                 4,210.00 EUR  │   name (text-primary) + amount (right, tabular-nums, text-primary)
│      2 accounts                             │   count subline (text-[11px] text-text-muted)
├────────────────────────────────────────────┤
│  🐷  Savings                 12,000.00 EUR  │
│      1 account                1,500.00 USD  │   multi-currency: one line per currency, stacked, items-start
├────────────────────────────────────────────┤
│  💳  Credit card               -850.00 EUR  │   liability: sign only, text-primary, NO color, NO "(owed)"
│      1 account                              │
└────────────────────────────────────────────┘
```

- Empty state (no active accounts at all): quiet "No accounts yet" line linking to `/accounts` (mirror `CreditUtilizationWidget`'s empty state).
- Zero-account types are omitted from the list.

## Data & money rules

Reads `activeAccounts` from `useDashboard()` (`DashboardDataProvider`). **No new backend endpoint, no new fetch.** `activeAccounts` is already `is_active`-filtered.

Grouping and math (all correctness must-fixes from architecture review):

1. **Group by account *type*, keyed on `account_type_id`** (carry `account_type_name` for the label and `account_type_slug` for icon/order). Grouping by the id — not a hardcoded slug allowlist — ensures accounts on **custom (non-system) types** (`account_type_slug === null`) are included; a slug allowlist would silently drop their balances and make the "consolidation" wrong.
2. **Subtotal per (type × currency).** Never sum across currencies. Never net across types within a currency (that would be an accidental net-worth-per-currency figure, out of scope).
3. **`Number(a.balance)` before summing** — the wire value is a string despite the TS `number` type; a raw `+` concatenates.
4. **Liabilities render with their stored sign** (negative), in `text-text-primary`, **no status color, no "(owed)" suffix** — the sign already carries the meaning (house rule at `accounts/page.tsx:1425-1429`; `danger` is scoped to overdraft/past-due status, not a healthy CC balance).
5. **Currency as a code** (`EUR`, `USD`) trailing the amount in `text-text-muted`, matching `formatAmount` (no glyph) and the accounts page.

### Row order

Priority by slug: `checking`(0) → `savings`(1) → `cash`(2) → `investment`(3) → `credit_card`(4) → `loan`(5, future) → custom/null(99). Tie-break by `account_type_name` (locale compare). This keeps assets first, liabilities last, custom types at the end.

### Multi-currency density

- 1 currency: single amount on the row baseline.
- 2 currencies: stacked lines, `items-start`, `gap-0.5` (mirrors `AccountTile`'s two-line right column).
- 3+ currencies (rare): show the **top 2 by magnitude**, then a muted "+N more" that links into `/accounts`. Never scroll inside the tile.

## Visual treatment

Matches the existing tile idiom; **no new visual primitives**.

- Shell: `card` + `cardHeader` + `cardTitle` (`lib/styles.ts`), header title left, "Accounts" link right (`text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary`) — copied from `CreditUtilizationWidget.tsx:60-69`.
- Rows: one `<Link href="/accounts">` per type, separated by `divide-y divide-border-subtle` (the `AccountTile` divided-row idiom), with `AccountTile`'s hover + `focus-visible:outline-2 focus-visible:outline-accent` treatment.
- Left cluster: type icon + name (top line), count subline. Icon renders in **`text-text-secondary`, `strokeWidth={1.5}`, `h-4 w-4` — never brass** (5+ brass icons would violate the One Brass Rule; the accent stays reserved for the one header CTA).
- Right cluster: amount right-aligned, `tabular-nums`, `text-text-primary`; currency code `text-text-muted`.
- Count copy pluralized: "1 account" / "2 accounts", `text-[11px] text-text-muted`.

### Icons (lucide)

| Type | Icon |
|------|------|
| Tile / Add-menu | `Layers` |
| checking | `Landmark` |
| savings | `PiggyBank` |
| cash | `Wallet` |
| investment | `TrendingUp` |
| credit_card | `CreditCard` |
| custom / null slug | `Wallet` (fallback) |

## Accessibility (WCAG 2.2 AA)

- Liability meaning is **not** color-only (color dropped): conveyed by the `-` sign + the type label.
- Per-row `aria-label` spelling the sign, e.g. `"Credit card, 1 account, minus 850.00 EUR"`; decorative icons `aria-hidden`.
- Amount in `text-text-primary` (AA on `surface`); only counts + currency code in `text-text-muted`.
- Rows are focusable links carrying the Pressable-Surfaces focus ring; row height meets the mobile touch floor.

## Implementation surface

New component `frontend/components/dashboard/widgets/BalancesByTypeTile.tsx` (reads `useDashboard()`), plus the standard registered-widget wiring:

**Frontend**
1. `lib/dashboard/widget-types.ts` — add `"dash_balances_by_type"` to `DashboardWidgetType`; add its `DASHBOARD_WIDGET_DEFAULTS` entry. Proposed grid `{ x:0, y:31, w:4, h:8 }` (y sits below the existing defaults; `cc_utilization` ends at y=31, and `addDashTile` recomputes actual placement on insert, so this y is nominal). Implementer tunes `h` against real content.
2. `components/dashboard/AddWidgetMenu.tsx` — add a `DASH_TILES` entry `{ type, label: "Balances by type", description: "Subtotal for each account type, grouped by currency.", Icon: Layers }`. **Also fix the stale header comment** that still says "7 dash_* tiles" (8 today, 9 after this).
3. `components/dashboard/renderDashboardWidget.tsx` — import `BalancesByTypeTile`, add an explicit `case "dash_balances_by_type": return fill(<BalancesByTypeTile />)` (missing arm silently renders blank via the `renderReportWidget` fallthrough).
4. `tests/lib/dashboard/widget-defaults.test.ts` — add the `CANONICAL_GRIDS` + `MIN_CONTENT_H` entries (both are `Record<DashboardWidgetType, …>`, compiler-required).

**Backend** (`backend/app/schemas/dashboard.py`)
5. Add `BALANCES_BY_TYPE = "dash_balances_by_type"` to `DashWidgetType`.
6. Add `class DashBalancesByTypeWidget(_DashWidgetBase)` with `type: Literal[DashWidgetType.BALANCES_BY_TYPE]` and the empty-config default.
7. Register it in the `_DashboardWidget` discriminated `Union`.

**Not touched:** `DEFAULT_DASHBOARD_LAYOUT` seed (opt-in, like `cc_utilization`) → the FE↔BE seed grid-sync test and `test_dashboard.py`'s `len(types) == 7` stay green.

## Testing

- **Component unit tests** (`BalancesByTypeTile`): grouping by type; per-(type×currency) subtotals; `Number()` coercion (string balances sum, not concatenate); signed liability rendered with sign + no color class; **custom/null-slug type included**; multi-currency stacking (2 currencies) and 3+ → top-2-plus-more; zero-account type omitted; empty-state when no accounts; count pluralization; row order.
- **Backend** schema-validation test: a layout containing `dash_balances_by_type` validates; the widget round-trips through `validate_dashboard_layout_json`.
- Keep `widget-defaults.test.ts` and the backend seed/grid tests green.
- Verify set: `vitest run`, `npx tsc --noEmit`, `eslint . --quiet`, `check-design-tokens.sh`, backend `pytest tests/routers tests/schemas` (isolated stack).

## Visual validation

Operator validates the tile **live in an isolated stack before any commit of the implementation** (this is a visual change). Design iterations happen against the running tile; only then does the PR go up.

## Out of scope / deferred

- Per-type subtotal tiles (fast-follow if usage validates).
- Sparklines / historical comparison.
- Loan subtotal (until a `loan` account type ships).
- Any net-worth / cross-type / cross-currency total (that is the separate NetWorthSource report item).
- Seeding the tile into the default layout, drag/resize config, per-user layout beyond what the canvas already provides.
