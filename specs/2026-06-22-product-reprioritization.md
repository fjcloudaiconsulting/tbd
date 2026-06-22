# Product Re-Prioritization — 2026-06-22

**Trigger:** Operator review of Monarch Money's UI ("I'm jealous of how beautiful their web UI looks"). Decision to re-organize priorities around: more color + better charts, a fully customizable dashboard (widgets including ones sourced from reports), continued Reports improvement (especially mobile), richer transaction filtering in Reports, moving AI work to the top, and rephrasing "Free in beta." The Plans refactor is **parked**.

This document is the strategy/sequencing source of truth for this pivot. Each workstream gets its own implementation spec → plan → build cycle.

---

## Library research findings (verified June 2026)

Current stack: `recharts@3.8.1`, `@dnd-kit/core@6.3.1`, `react-grid-layout@1.5.0`, on Next 16 (App Router) + React 19.

**Charts — keep Recharts; add `@nivo/sankey` for the cash-flow Sankey only.**
- Recharts is the best fit for *this* design system: it accepts `fill: var(--token)` / `stroke` directly (satisfies the No-Off-Token rule without a canvas/`getComputedStyle` bridge), has `accessibilityLayer` on by default (WCAG 2.2 AA), React-19 + best SSR story, MIT.
- "More color + beauty" (donut with hover, gradient fills, smooth `monotone` area for net worth) is achievable in Recharts **today** via tokens — no library swap.
- The one weak spot is the Monarch-style **Sankey cash-flow** diagram. Add **`@nivo/sankey`** (MIT, React 19, SVG, token-friendly) scoped to that single widget. (ECharts is richer but canvas + ~150 KB + JS-token theming; Unovis is a viable SVG alternative.)
- **Avoid:** Tremor (React 18 only, dormant, fights token rule), Victory (broken on React 19), TanStack react-charts (beta), Muuri (abandoned).

**Dashboard grid — migrate to `gridstack.js` v12.6 for mobile.**
- `react-grid-layout@1.5.0` works but is mobile-weak (no first-class touch, open edge-resize bug, no keyboard/ARIA).
- Since mobile/touch is a hard requirement, **gridstack.js** has the best touch story (native touch drag *and* resize), clean `save()/load()` JSON that maps onto the existing `layout_json` persistence pattern, ~23 KB zero-dep MIT.
- Caveats we own: (1) run a **React-19 spike first** (no explicit React-19 peer declaration; load client-only via `next/dynamic({ ssr:false })`); (2) build a **keyboard/ARIA layer** for WCAG 2.2 AA.
- Safer-but-weaker fallback if the spike fails: upgrade `react-grid-layout` 1.5 → 2.2 (React-19 rewrite, still desktop-first touch + no ARIA).

---

## Workstreams & sequence

Operator-chosen sequence: **quick wins first → visual refresh → customizable dashboard**, with the founders program pulled forward (time-sensitive vs. live ads) and the AI agent specced as its own XL track.

| # | Workstream | Size | Status |
|---|---|---|---|
| **W1** | Quick wins: Reports multi-select txn-type filter + landing copy + founding-members v1 | S + M | **Spec'd 2026-06-22** → `2026-06-22-w1-quick-wins-design.md` |
| **W2** | Founding-members program (full): referral 30%-off-year-1 + inactivity revoke | M–L | Deferred to payments wave; founder *flag + counter* ship in W1 |
| **W3** | Visual/color chart refresh (Recharts + tokens; donut, gradients, smooth area; `@nivo/sankey` cash-flow) + Reports mobile pass | M | Needs spec |
| **W4** | Customizable dashboard (gridstack.js migration + widget framework + "add widget from report") | L | Needs spec; gridstack React-19 spike first |
| **W5** | AI assistant (agentic): natural-language → *every* user UI action, Plans wired-but-off, consistency (seeded) | XL | Needs its own brainstorm + spec |
| — | ~~Plans refactor~~ | — | **PARKED** (operator decision) |

### Locked decisions for the founders program (W1/W2)
- **Soft cap** — 1,000 is a marketing number; everyone in the window gets the offer, counter counts *up*. No hard-stop gating logic.
- **Flag founders from now**, and **grandfather all existing users** (`is_founder` default `1`) — the friends testing today are the most-founding members.
- **Track activity now, enforce later** — stamp `last_active_at`; the revoke-on-30-days-inactivity logic ships with payments (no scheduler built prematurely).
- **Referral**: referred user gets **30% off year 1** when charging begins — designed/activated in the payments wave, not now.
- **Counter delivery**: hardened **public count-only** endpoint (no token — the apex is a static export, so any token would be exposed in the browser; the count is non-sensitive by design). Excludes a config username list (e.g. `pfv_smoke_l05`).

### W5 AI agent — scope captured (for its later brainstorm)
Goal: an AI assistant that can perform **all actions available to users in the UI** via natural language, with **consistent answers** (seed the model where it helps). Plans actions **wired but left off** for now. This is an agentic tool-registry over the service layer with **preview-confirm-before-write** semantics and per-user auth scoping. XL; its own spec.

---

## Out of scope / parked
- Plans refactor (parked).
- Payments (L2.x) remain parked; W2 referral discount + founder inactivity-revoke ride on it.
