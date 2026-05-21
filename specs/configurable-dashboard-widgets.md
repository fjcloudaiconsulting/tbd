---
name: Configurable Dashboard / Widget System
description: Let users add, remove, and reorder cards on Dashboard (and later other pages) like widgets. Friend feedback 2026-05-09.
type: project
---
**Captured 2026-05-10.** Friend feedback echoed by the user. Not a launch blocker. Sized L-XL depending on scope. Parks for post-launch.

## Concept

Today's Dashboard is a fixed layout (On Track, Forecast, Spending by Category, Budget Progress, Forecast by Category, Recent Transactions). Users want:
- Add or remove cards.
- Reorder cards (drag and drop or up/down arrows).
- Eventually: add cards from a catalog (e.g. the new low-balance-day warning, custom category breakdowns, savings goals).
- Eventually: same widget system on other pages (Accounts, Transactions, etc.).

## Phases

### Phase 0 — Per-account-type consolidation tiles (pre-widget; 2026-05-15 addition)

Owner confirmed 2026-05-15: short-term, ship static consolidated tiles per account type without committing to the widget framework yet. Goal: validate the consolidation is what the user actually wants to see before paying for the configurability infra.

**Tiles to add to Dashboard:**
- Checking subtotal
- Savings subtotal
- Credit-card subtotal (sum of current balances, NOT credit limits)
- Loan subtotal (if loan account type ships — see `project_loan_account_type.md`)
- Investments subtotal (if/when an investment account type lands)

Each tile: total balance + count of accounts of that type + small comparison sparkline if cheap.

**Source data:** `lib/api.ts` already paginates `GET /api/v1/accounts`; group by `account_type.slug` client-side. No new endpoint required.

**Scope discipline:** static cards, no drag/resize, no per-user layout state. If users like the consolidation, the value of widgetizing increases; if they don't use it, we saved ourselves Phase 1's infra cost.

**Cross-cut:** the user's idea is that this concept (consolidation cards / contextual subtotals / configurable widgets) eventually extends beyond Dashboard to other pages (Accounts, Transactions, etc.). Honor in framing: Phase 0 ships only Dashboard tiles; later phases reuse the same idea on other pages once the widget framework exists.

**Effort:** XS-S. Single PR.

### Phase 1 — Dashboard reorder + visibility toggles

- Persist per-user a list of `{widget_id, visible, order}` tuples.
- "Edit dashboard" mode: toggle visibility on each card, reorder via drag-and-drop or up/down buttons.
- Reset-to-default affordance.
- Storage: backend `user_preferences` JSON column or a dedicated `dashboard_layouts` table. Probably JSON column on `users` for v1 simplicity.

### Phase 2 — Widget catalog

- Define a registry of available widgets (existing cards become the seed list).
- Add-widget flow: user picks from catalog, widget appears on dashboard.
- Widget instances may have config (e.g. "Spending by Category — current period" vs "Spending by Category — last 3 months").
- This unlocks the low-balance-day warning as an opt-in widget.

### Phase 3 — Cross-page widgets

- Same primitives applied to Accounts, Transactions, etc.
- Shared widget primitives (header, body, edit-mode chrome).
- Per-page layout persisted independently.

## Open questions

1. **Reorder UX**: drag-and-drop is expected but breaks down on touch. Up/down buttons are cheap and accessible. Probably ship both.
2. **Per-org vs per-user**: per-user is the obvious answer; org-shared dashboards are post-launch.
3. **Mobile**: responsive parity has to be designed in from the start. Widgets at narrow viewports stack 1-up regardless of layout config.
4. **Backend shape**: JSON-on-users is fastest; dedicated table is cleaner if widget configs grow.
5. **Default layout**: every new user gets the current canonical layout. "Reset to defaults" reverts to that.
6. **A11y**: drag-and-drop is notoriously hard for keyboard users. Up/down arrows are the keyboard-friendly answer.

## Cross-references

- `project_low_balance_day_warning.md` (a candidate widget for Phase 2)
- DESIGN.md card system (existing cards become widget instances; no new visual primitives required for Phase 1)

## Why park Phases 1-3

Pre-launch, the canonical layout is opinionated and serves the seed user (finance-savvy spreadsheet operator who wants Plan vs Forecast vs Actual front and center). Configurability is post-launch polish that becomes valuable once enough users want different views to justify the engineering cost. Likely Phase 1 lands as a small post-launch P1 if user demand exists.

**Phase 0 (per-type tiles) is the exception** — owner confirmed 2026-05-15 it's worth shipping pre-launch as a small static-UI step that doesn't require the widget framework.

## 2026-05-15 owner addition — widget motion vocabulary

Owner reinforced the long-term vision: users should be able to **move, resize, and reposition** widgets, not just toggle visibility / reorder. Phase 1 as originally scoped (reorder + visibility) is a subset of what the owner ultimately wants. When Phase 1 lands, the data model should leave room for `width` / `height` per-widget so Phase 2 can add resize without a second migration. Reorder UI in Phase 1 should use a primitive (e.g., `react-grid-layout`) that also supports resize so we don't throw away the implementation when Phase 2 lights up.
