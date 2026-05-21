---
name: UI/UX Historical Log
description: Completed-item history only. All forward-looking work migrated to project_roadmap.md (single source of truth) on 2026-04-20.
type: project
---
# UI/UX Historical Log

**All open/active items now live in `project_roadmap.md`.** This file is kept for historical reference — which items shipped in which PR. When adding new UI backlog items, put them in the roadmap under the appropriate phase (L3 Core UX, L4 Polish, or P-series post-launch).

## Completed

1. ✅ Tooltip text colors (Remaining/Spent/Over budget) — PR #55
2. ✅ Clickable bar charts → filter transactions — PR #55
4. ✅ Auto-description for transfers — earlier PR
5. ✅ Transaction descriptions mandatory — earlier PR
6. ✅ Replace all `window.confirm()` with ConfirmModal (12 callsites) — PR #55
7. ✅ Responsive / mobile layout (hamburger sidebar) — PR #55
8. ✅ Show all active accounts on dashboard — PR #55
10. ✅ Dashboard balances refresh on settle/unsettle — PR #55
11. ✅ Dashboard Forecast card colors match Forecast page — PR #55
14. ✅ Dashboard Budget bar chart semantic colors — PR #55
15. ✅ Dashboard Forecast bar chart colors/legend — PR #55
22. ✅ Budget transfer form keeps spent/amount visible — PR #55
24a. ✅ Import page: CategorySelect type-ahead — PR #55
24b. ✅ Transfer form: optional category picker — PR #55
29. ✅ Mobile responsive sweep — PR #63 (data tables) + PR #64 (everything else)
33. ✅ Multi-select + bulk delete transactions — PR #65

## Open — see `project_roadmap.md`

| Previous backlog # | Now lives at |
|---|---|
| #3 Transfer exclusion from spending chart | L3.5 |
| #9 CC pending after payment | L3.4 |
| #12 Dashboard Forecast explanation | L3.7 |
| #13 CC pending in forecast investigation | Technical Debt |
| #16 Donut charts for dashboard | Technical Debt |
| #17 Composite index for import dedup | P1.5 |
| #18 pytest infrastructure | Technical Debt |
| #19 User manual / help | L5.3 |
| #20 Quick tour / onboarding | L3.3 |
| #21 Demo seed opt-in | L3.3 |
| #23 Notification preferences | P1.2 |
| #24c Inline "Add category" | L3.6 |
| #25 System Admin Dashboard | **L4.2–L4.8** (promoted to launch blocker) |
| #26 OpenTelemetry | P7.1 |
| #27 User locale preferences | P2.* |
| #28 Header & footer redesign | L5.4 |
| #30 Privacy policy page | L1.2 |
| #31 Footer link to privacy policy | L5.4 |
| #32 Migrate /settings/security to SettingsLayout | Technical Debt |
| #34 Reset org data | **L3.1** (bumped to first L3 item) |
| #35 Auth hardening round 2 | L1.1 |
| #36 Strict CSP nonce middleware | Technical Debt |
| #37 SEO baseline | L5.1 |
| #38 Cloudflare HSTS + authenticated ZAP | L1.5 + L1.6 |
