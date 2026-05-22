# L4.5 subscription revenue — superadmin Revenue View — design

**Status:** spec authored 2026-05-22, awaiting architect lock on D1-D7. Substrate (subscription model, plans, mock_revenue flag, admin subscription router) already shipped — this spec is the **read-only Revenue View** layered on top, NOT a re-spec of the substrate.
**Date:** 2026-05-22.

**Source:** roadmap L4.5 (`memory/project_roadmap.md`). The substrate row already shows MERGED via the cross-org subscription read service. This spec scopes the platform-revenue surface (MRR / ARR / churn / cohort / AI-cost-vs-revenue) that sits on top of those primitives.

## Summary

* **Substrate already shipped** — `backend/app/services/admin_subscription_service.py` exposes `aggregate_revenue_kpis` (active / trial / past_due / cancelled / signups_7d / trial_expiring_7d / plan_distribution / `mock_mrr` / `mock_arr` / `mock_revenue=True`). `backend/app/routers/admin_subscriptions.py` ships `/api/v1/admin/subscriptions` + `/admin/subscriptions/{id}`. Frontend `/admin/subscriptions` page exists. **This spec adds a new sibling surface** `/admin/revenue` for the time-series + cohort views the existing list page deliberately stays out of.
* **Payment provider** is Paddle (confirmed in `backend/app/services/subscription.py`). L2.2 wires Paddle webhooks → `subscriptions.current_period_*` + a new `subscription_invoices` table. Until L2.2 lands, every dollar reading on the Revenue View is sourced from the existing mock-zero path; the `mock_revenue=true` flag propagates onto every response envelope this spec adds so the FE keeps showing the disclosure.
* **Six metrics surfaced**: MRR, ARR, active orgs by tier, gross churn (count + MRR-weighted), AI cost vs revenue (cost/revenue ratio per tier), trial-to-paid conversion. All derivable from `subscriptions` + `plans` + `organizations` + `ai_usage_ledger.est_cost_cents`. NO new tables. NO new payment-side schema.
* **Strict gating**: every new endpoint runs both the existing `subscriptions.view` permission gate AND the `forbid_impersonation_session` dependency surfaced in the L4.4 spec (`specs/2026-05-22-l4-4-admin-slices.md` §5.3). Reading platform revenue while wearing the skin of a non-superadmin user is a clear smell that we block.
* **Audit on every read**. Unlike most admin GETs which audit only on first-hit-per-window, the Revenue View writes one `admin.revenue.viewed` row per actor per minute (1-min throttle to keep cardinality manageable while still leaving a forensic trail). This is the sensitive-surface premium.
* **Rollout train**: 3 PRs in dependency order — backend revenue service + endpoints (PR 1), frontend page + ASCII-spec wireframe components (PR 2), Paddle cutover swap from `mock_revenue=true` to real (PR 3, gated behind L2.2 webhook delivery).

## Substrate audit (confirmed 2026-05-22)

1. **Subscription model.** `backend/app/models/subscription.py` defines `Plan(id, slug, name, price_monthly, price_yearly, max_users, retention_days, features, is_active, is_custom, sort_order)` and `Subscription(id, org_id UNIQUE, plan_id, status, billing_interval, trial_start, trial_end, current_period_start, current_period_end)`. `SubscriptionStatus` ∈ {`trialing`, `active`, `past_due`, `canceled`}. `BillingInterval` ∈ {`monthly`, `yearly`}. `org_id` is `UNIQUE` — one subscription per org, so org-to-subscription joins are 1:1 (no need to dedupe).
2. **Aggregator already exists.** `admin_subscription_service.aggregate_revenue_kpis` returns the KPI envelope used by the existing pulse strip. We REUSE this — the Revenue View's `/summary` endpoint calls it directly, then layers cohort / time-series math on top via new sibling functions in the same service module.
3. **Mock-revenue propagation.** `mock_revenue: True`, `mock_mrr: "0.00"`, `mock_arr: "0.00"` are hard-pinned strings in the existing aggregator. The new endpoints' envelopes carry the same flag. The cutover to real values happens in `aggregate_revenue_kpis` once L2.2 ships — the FE doesn't need to know.
4. **AI usage cost.** `ai_usage_ledger.est_cost_cents` (integer, MySQL `BigInt`) is the per-call cost estimate written by `ai_dispatch.call_llm`. The Revenue View aggregates `SUM(est_cost_cents) GROUP BY org_id` per period, joins to `subscriptions.plan_id` for the "cost vs revenue by tier" tile. Cost is in CENTS — the FE converts to dollars at the boundary.
5. **Plans table is the price source of truth.** `plans.price_monthly` and `plans.price_yearly` are `Numeric(10, 2)` dollars. MRR per subscription is computed as `plan.price_monthly` if `billing_interval=monthly`, else `plan.price_yearly / 12`. ARR is `MRR * 12`. No prorations in v1 — Paddle webhooks (when they land) will populate `subscriptions.current_period_start/end` and a future invoices table; until then the plan-derived MRR is a deterministic approximation.
6. **Audit event taxonomy.** `audit_events` table per the L4.4 lock #24 has no `target_user_id` column; revenue reads are platform-scoped, so the audit row's `target_org_id` is NULL and the `detail` JSON carries the view metadata. `audit_service.record_audit_event` opens its own AsyncSession (commit-independent).
7. **forbid_impersonation_session dependency.** Introduced by `specs/2026-05-22-l4-4-admin-slices.md` §5.3 as a sibling of `require_permission`. The L4.4 list of surfaces that already pull this in includes `/api/v1/admin/audit`, `/admin/users`, `/admin/orgs`, `/admin/analytics`, `/admin/roles`, `/admin/subscriptions`, `/admin/announcements`, `/admin/ai-usage`. **All four `/admin/revenue/*` endpoints below ALSO pull this in.** If L4.4 hasn't landed by the time this PR train starts, PR 1 of this spec adds the dependency itself (and the L4.4 train consumes it from this side instead).
8. **Permission.** `subscriptions.view` already exists and is on the superadmin short-circuit (see `app/auth/permissions.py`). The Revenue View reuses this permission — no new entry. If L4.8 ships before we want to give finance team members revenue-view-without-org-write, they'll get `subscriptions.view` via the role editor.

## Design

### 1. Problem & scope

**In scope:**

* Superadmin-only `/admin/revenue` page. Sibling to `/admin/subscriptions`.
* Six metrics tiled across the page: MRR, ARR, active orgs by tier, gross churn (count + MRR-weighted), AI cost vs revenue, trial-to-paid conversion.
* Time-range filter (30 days / 90 days / 365 days). Default 30 days.
* Time-series chart for MRR + churn over the selected range.
* `mock_revenue` disclosure banner on the page when the flag is true (i.e., before L2.2 cutover).
* Every read writes an `admin.revenue.viewed` audit row (throttled 1-per-actor-per-minute).
* Impersonation-blocked: this entire surface returns 403 to impersonation tokens.

**Out of scope (explicitly):**

* Customer-facing billing UI (already hidden per `specs/2026-05-21-hide-billing-ui-until-payment.md`).
* Payment dispute workflows. Disputes flow through Paddle's dashboard, NOT through the Revenue View.
* Paddle webhook handling itself. L2.2 owns this; the Revenue View consumes the resulting `subscriptions.current_period_*` columns and a future `subscription_invoices` table.
* Refund issuance, plan changes initiated by admin, billing-cycle adjustments. Those are write surfaces; this spec ships read-only.
* Cohort retention curves beyond simple trial-to-paid conversion. Cohort retention is L4.12.
* Revenue forecasting. Forecasting is a Reports v2 concern (see `specs/2026-05-22-reports-v2-flexible-canvas.md`); the Revenue View ships actuals only.

### 2. Data sources

Every metric is derivable from already-existing tables. Zero schema changes.

| Metric | Tables | Columns | Notes |
|---|---|---|---|
| MRR | `subscriptions`, `plans` | `subscriptions.status`, `subscriptions.billing_interval`, `subscriptions.plan_id`, `plans.price_monthly`, `plans.price_yearly` | Sum over `status=active`. `monthly` rows contribute `price_monthly`; `yearly` rows contribute `price_yearly / 12`. Trial subs contribute 0. |
| ARR | derived | — | `MRR * 12`. Single SELECT, no separate query. |
| Active orgs by tier | `subscriptions`, `plans`, `organizations` | `subscriptions.status`, `subscriptions.plan_id`, `plans.slug`, `organizations.id` | `COUNT(DISTINCT org_id) GROUP BY plan_id` filtered to `status IN ('active','trialing')`. INNER JOIN to `plans` for the label. |
| Gross churn (count) | `subscriptions` | `subscriptions.status`, `subscriptions.updated_at` | `status=canceled` AND `updated_at` in selected range. v1 uses `updated_at` as a churn-date proxy because `canceled_at` doesn't exist; L2.2 may add a dedicated column, in which case PR 3 swaps the proxy out. |
| Gross churn (MRR-weighted) | `subscriptions`, `plans` | same + `plans.price_monthly/_yearly` | Same filter as count; sum the plan-derived MRR per cancelled subscription. |
| AI cost vs revenue | `ai_usage_ledger`, `subscriptions`, `plans` | `ai_usage_ledger.org_id`, `ai_usage_ledger.dispatched_at`, `ai_usage_ledger.est_cost_cents`, `subscriptions.plan_id`, `plans.slug` | `SUM(est_cost_cents) GROUP BY org_id` over selected range, joined to `subscriptions` then `plans`. Ratio per tier is `total_cost_cents / total_mrr_cents`. |
| Trial-to-paid conversion | `subscriptions` | `subscriptions.trial_start`, `subscriptions.trial_end`, `subscriptions.status` | Within the selected range: count subs whose `trial_end` fell in range. Of those, count subs whose `status='active'`. Conversion = active / total. |
| Time-series (MRR over time) | `subscriptions`, `plans` | `subscriptions.created_at`, `subscriptions.updated_at`, `subscriptions.status` | Daily snapshot is reconstructed from `created_at` + `updated_at` + status; see D5 — this is the metric whose v1 implementation needs an architect lock. |

**`mock_revenue` propagation.** Every endpoint's envelope carries the flag. The flag is true when **any** dollar-denominated field in the response was sourced from `aggregate_revenue_kpis` (which hard-pins to 0 until L2.2). The flag is false ONLY when L2.2 has cut over `aggregate_revenue_kpis` to real values AND the new revenue endpoints have been re-verified against Paddle's reports. The cutover is a single boolean in service code (no FE change).

### 3. API surface

All endpoints under `/api/v1/admin/revenue/*`. Every endpoint:

* Pulls `Depends(require_permission("subscriptions.view"))`.
* Pulls `Depends(forbid_impersonation_session)` (the L4.4 §5.3 dependency).
* Audits via `audit_service.record_audit_event(event_type="admin.revenue.viewed", detail={endpoint, range, ...})`, throttled 1-per-actor-per-minute (process-local, like the existing user-search throttle).

#### 3.1 `GET /api/v1/admin/revenue/summary`

Pulse strip — the four headline numbers + the flag.

```
Permission: subscriptions.view
Impersonation: blocked

Response 200:
{
  "mrr_cents": int,                    # 0 when mock_revenue=true
  "arr_cents": int,                    # = mrr_cents * 12
  "active_orgs": int,                  # COUNT subscriptions WHERE status=active
  "trialing_orgs": int,                # COUNT subscriptions WHERE status=trialing
  "past_due_orgs": int,                # COUNT subscriptions WHERE status=past_due
  "cancelled_last_30d": int,           # COUNT subscriptions WHERE status=canceled AND updated_at >= now-30d
  "mrr_weighted_churn_cents_30d": int, # sum of plan-derived MRR over the cancelled-in-last-30d set
  "trial_to_paid_30d_pct": float,      # conversion over the trailing 30d window
  "mock_revenue": bool,                # propagated from aggregator
  "generated_at": iso8601_utc
}
```

#### 3.2 `GET /api/v1/admin/revenue/by-tier`

Per-plan breakdown — the "active orgs by tier" view.

```
Permission: subscriptions.view
Impersonation: blocked

Response 200:
{
  "rows": [
    {
      "plan_id": int,
      "plan_slug": str,
      "plan_name": str,
      "active_count": int,             # status IN ('active','trialing')
      "active_paid_count": int,        # status='active' only
      "mrr_cents": int,                # sum of plan-derived MRR for active_paid set
      "share_of_mrr_pct": float,       # mrr_cents / total_mrr_cents * 100
      "ai_cost_cents_30d": int,        # SUM(ai_usage_ledger.est_cost_cents) WHERE org has this plan AND dispatched_at >= now-30d
      "cost_vs_revenue_pct": float     # ai_cost_cents_30d / mrr_cents * 100 (or null if mrr_cents == 0)
    },
    ...
  ],
  "total_mrr_cents": int,
  "total_active_orgs": int,
  "mock_revenue": bool,
  "generated_at": iso8601_utc
}
```

#### 3.3 `GET /api/v1/admin/revenue/timeseries?range=30d|90d|365d`

Daily MRR series over the selected range.

```
Permission: subscriptions.view
Impersonation: blocked
Query: range ∈ {30d, 90d, 365d}, default 30d

Response 200:
{
  "range": "30d" | "90d" | "365d",
  "points": [
    { "date": "YYYY-MM-DD", "mrr_cents": int, "active_orgs": int, "cancelled_orgs": int },
    ...
  ],
  "mock_revenue": bool,
  "generated_at": iso8601_utc
}
```

Number of points = 30, 90, or 365 (one row per day in the range, oldest first).

#### 3.4 `GET /api/v1/admin/revenue/ai-cost-vs-revenue?range=30d|90d|365d`

Detail page for the cost-vs-revenue tile.

```
Permission: subscriptions.view
Impersonation: blocked
Query: range ∈ {30d, 90d, 365d}, default 30d

Response 200:
{
  "range": "30d" | "90d" | "365d",
  "total_ai_cost_cents": int,
  "total_revenue_cents": int,          # MRR * range_in_months
  "ratio_pct": float,                  # total_ai_cost_cents / total_revenue_cents * 100; null when revenue is 0
  "by_org": [                          # top 20 by cost; rest aggregated as "other"
    {
      "org_id": int,
      "org_name": str,
      "plan_slug": str,
      "ai_cost_cents": int,
      "mrr_cents": int,
      "cost_vs_revenue_pct": float
    },
    ...
  ],
  "other_orgs": {                      # rolled-up tail
    "count": int,
    "ai_cost_cents": int,
    "mrr_cents": int
  },
  "mock_revenue": bool,
  "generated_at": iso8601_utc
}
```

Per-org row reveals an org name to the superadmin — this is fine because the surface is already superadmin-gated. The 20-row cap protects the response payload size; the long tail is rolled into the `other_orgs` summary so the totals reconcile.

### 4. UI surface

New page at `/admin/revenue`. Sibling of `/admin/subscriptions` in the admin nav. Page composition:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  The Better Decision     [Dashboard] [Admin v]                  [Bell] [Avatar]│
├──────────────────────────────────────────────────────────────────────────────┤
│  Admin > Revenue                                                              │
│                                                                               │
│  ┌─ Mock data disclosure (shown when mock_revenue=true) ─────────────────┐   │
│  │ ⓘ  Revenue values are mocked at $0 until Paddle integration ships.    │   │
│  │    Org counts, churn counts, and AI cost are real.                    │   │
│  └────────────────────────────────────────────────────────────────────────┘   │
│                                                                               │
│  Time range:  [ 30 days ▼ ]   ( 30 days | 90 days | 365 days )               │
│                                                                               │
│  ┌─ Pulse strip (4 tiles) ──────────────────────────────────────────────┐   │
│  │  MRR        ARR        Active orgs    Churn (30d)                    │   │
│  │  $0.00      $0.00      147            3 orgs  $0.00                  │   │
│  └────────────────────────────────────────────────────────────────────────┘   │
│                                                                               │
│  ┌─ MRR over time ──────────────────┐  ┌─ Active orgs by tier ──────────┐   │
│  │                                  │  │ Free     104  ────────         │   │
│  │       ╭───────────╮              │  │ Pro       38  ──────           │   │
│  │      ╱             ╲             │  │ Business   5  ──               │   │
│  │     ╱               ╲___         │  │                                 │   │
│  │   ─                     ─        │  │ Total MRR: $0.00                │   │
│  └──────────────────────────────────┘  └──────────────────────────────────┘   │
│                                                                               │
│  ┌─ AI cost vs revenue (last 30 days) ──────────────────────────────────┐   │
│  │ Total AI cost: $14.27        Total revenue: $0.00     Ratio: —       │   │
│  │                                                                       │   │
│  │  ┌─ Top orgs by AI cost ────────────────────────────────────────┐   │   │
│  │  │ Org              Plan      AI cost    MRR      Cost/Rev     │   │   │
│  │  │ acme-corp        Pro       $4.12     $0.00    —             │   │   │
│  │  │ widgets-inc      Business  $2.83     $0.00    —             │   │   │
│  │  │ ...                                                          │   │   │
│  │  └────────────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                               │
│  ┌─ Trial-to-paid conversion ──────────────────────────────────────────┐   │
│  │ Last 30d: 14 trials ended · 9 converted to paid · 64.3%             │   │
│  └────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

**States:**

* **Loading**: skeleton placeholders on each tile (existing `Skeleton` primitive).
* **Empty** (no subscriptions exist): the four pulse tiles show 0; MRR-over-time chart says "No subscription history in this range"; by-tier table shows the plans table with zero rows of counts; AI cost tile shows the actual cost number (the ledger may have data even with no subscriptions, in early dev).
* **Mock-data** (the steady state pre-launch): the blue disclosure banner at the top is shown. All dollar figures render as `$0.00`. Tooltip on the disclosure links to the Paddle cutover ticket (D2 below).
* **Error**: standard `ErrorAlert` primitive; "Failed to load revenue data. Try refreshing." with a retry button.

**Time-range filter behavior:** changing the range refetches all four endpoints in parallel. The pulse strip's `cancelled_last_30d` and `mrr_weighted_churn_cents_30d` are HARD-CODED to 30 days regardless of the range filter — those numbers are "snapshot at the moment" tiles, not range-dependent. The range applies only to the timeseries chart, the AI cost table, and the trial-to-paid number (which lives inside the conversion tile). D6 below asks the architect to confirm this asymmetric range-scoping.

### 5. Audit logging

This is a sensitive surface. We audit reads, not just writes.

```
Event type:      admin.revenue.viewed
Throttle:        1 per actor per 60s (process-local; restart resets — acceptable for v1)
Actor:           the superadmin
target_org_id:   NULL (platform-scoped, not org-scoped)
detail: {
  endpoint: "/summary" | "/by-tier" | "/timeseries" | "/ai-cost-vs-revenue",
  range:    "30d" | "90d" | "365d" | null,           # null on /summary and /by-tier
  mock_revenue: bool                                   # snapshot the flag at view time
}
```

The throttle window is keyed `(actor_user_id, endpoint)` so a flurry of clicks across the four endpoints in one page render = at most 4 audit rows per minute, not 4-per-second. Refreshes inside the window are silent. First hit in each new window writes.

**Retention.** Audit rows on this surface fall under the same retention policy as the rest of `audit_events`. D7 below asks the architect to lock the retention floor; v1 default is "never auto-prune until retention is in scope across the board".

**No per-row PII**: the `detail` payload deliberately does NOT include the response's per-org rows. The audit log proves a superadmin viewed revenue at time T over range R — it does NOT replay the per-org cost numbers. If forensic replay is needed, the L4.9 access log gives the request timestamp and the existing aggregator gives deterministic output for any historical timestamp.

### 6. Architect Q&A — D1 through D7

#### D1. Should the four endpoints be split, or collapsed into one `/revenue/all` mega-payload?

* **Proposal:** four endpoints, as specified above.
* **Alternatives:** (a) one mega-endpoint that returns the union of all four payloads in a single response, (b) two endpoints (`/summary` + `/detail`), (c) GraphQL-style query selector.
* **Recommendation:** four. The four payloads have different cache-friendliness characteristics (the timeseries is the largest and changes per-day; the summary is small and refreshes per-minute), and splitting lets the FE fire them in parallel without payload coupling. Mega-endpoint forces every page render to compute the time-series even when the user just wants to see the pulse strip. Two endpoints awkwardly straddle.

#### D2. What's the precise cutover signal from `mock_revenue=true` to `mock_revenue=false`?

* **Proposal:** flip happens inside `aggregate_revenue_kpis` (and any new sibling functions added by this train) when **all three** are true: (1) L2.2's Paddle webhook handler has been live in prod for at least 72 hours; (2) `subscriptions.current_period_start/end` are non-null on at least N% of `status=active` rows (architect to pick N — proposal: 95%); (3) a one-shot reconciliation job has verified our derived MRR is within 1% of Paddle's reported MRR for the trailing 30 days. The flag flip is a code change (single line), committed as PR 3 of this train.
* **Alternatives:** (a) env-var toggle; (b) per-org subscription column `billing_active=true`; (c) automatic flip when `current_period_start` is non-null on any subscription.
* **Recommendation:** code-change cutover. The env-var route has been bitten before (see `reference_do_spec_sync.md`). The per-row column is a data-plane change for a UX concern. Automatic flip on first non-null `current_period_start` is too eager — a single test fixture in prod (which shouldn't happen, but) would flip the world. Hard-coded cutover keyed to an explicit verification job is the conservative path.

#### D3. Should churn be measured by `updated_at` (proxy) or wait for a real `canceled_at` column?

* **Proposal:** use `updated_at` as the proxy in v1; revisit when L2.2 adds `canceled_at`.
* **Alternatives:** (a) add `canceled_at NULLABLE` to `subscriptions` in PR 1 of this train; (b) defer the entire churn metric until L2.2.
* **Recommendation:** `updated_at` proxy in v1. It's wrong-by-design in the sense that ANY update to a cancelled subscription (e.g., back-dating a refund row in some imagined future) would re-stamp `updated_at` — but the only path that touches a cancelled subscription today is the cancel action itself, so the proxy is exact. When L2.2 introduces other updates (e.g., reactivation), it ALSO introduces `canceled_at` because the lifecycle naturally requires it; PR 3 of this train swaps the proxy then. Adding `canceled_at` from this train is over-shooting scope.

#### D4. The 1-per-minute audit throttle — is that the right cardinality target?

* **Proposal:** 60 seconds, process-local, keyed `(actor_user_id, endpoint)`. Up to ~24 audit rows per actor per hour assuming 4 endpoints × 1 view-per-min cap.
* **Alternatives:** (a) no throttle — write a row per request, ~thousands of rows/hour; (b) longer window, e.g. 5 min, ~4-5 rows/hour; (c) per-actor (not per-actor-per-endpoint), ~1 row/minute total.
* **Recommendation:** 60s, per-actor-per-endpoint. The L4.4 user-search throttle uses the same shape and the per-endpoint key gives forensic granularity (we want to be able to ask "did anyone hit `/ai-cost-vs-revenue` between 10:00 and 11:00?" without grepping detail JSON). The audit-volume math is fine: even ~100 superadmins (impossible at this scale) × 4 endpoints × ~60 windows/hour = 24K rows/hour worst-case, still trivial.

#### D5. Time-series implementation — daily snapshot table, or on-the-fly reconstruction?

* **Proposal:** on-the-fly reconstruction from `subscriptions.created_at` + `updated_at` + `status`. For each day in the range, replay the cumulative state of every subscription as-of that day and sum MRR.
* **Alternatives:** (a) materialized daily snapshot table `revenue_snapshots(date, mrr_cents, active_orgs, ...)` written nightly by a cron; (b) cached aggregation refreshed every N minutes; (c) Redis-cached on-the-fly with 5-min TTL.
* **Recommendation:** on-the-fly with a 5-min Redis cache (per `range`). On-the-fly is correct for a small-volume product (sub-1K subscriptions for v1). The snapshot table is the right answer at >100K subscriptions, but we'd be building it speculatively. Redis cache is a 5-line addition once the reconstruction is shown to be slow; if perf is fine without it, drop it. The reconstruction itself is one SELECT scanning `subscriptions` once per range (not per day) — see D5-sketch below.
* **D5-sketch (so architect can sanity-check the math):**
```python
# Single SELECT scans all subs once. For each sub, generate the list of
# (date, mrr_cents_delta) events: +mrr at created_at, -mrr at updated_at
# if status transitioned to canceled. Aggregate by date for the daily
# series. This is O(N subscriptions) regardless of range length.
events = []
for sub in subs:
    mrr_cents = _plan_mrr_cents(sub.plan_id, sub.billing_interval)
    if sub.status == SubscriptionStatus.CANCELED:
        events.append((sub.created_at.date(), +mrr_cents))
        events.append((sub.updated_at.date(), -mrr_cents))
    else:
        events.append((sub.created_at.date(), +mrr_cents))
# Fold events into the daily series for the requested range.
```

#### D6. Asymmetric range scope on the pulse strip — pulse always 30d, timeseries respects filter?

* **Proposal:** pulse strip is "now" plus a hard-coded 30-day churn delta; timeseries and AI cost respect the range filter.
* **Alternatives:** (a) all tiles respect the range filter (so churn becomes "in the last 90/365 days"); (b) all tiles are "now" snapshots; (c) range filter has a sub-tile within the pulse strip.
* **Recommendation:** asymmetric (Proposal). The pulse strip is meant to answer "what's the state of the platform right now?" — comparing the headline 30-day churn against varied time windows muddies the read. The timeseries and AI cost are clearly range-dependent. We can revisit if user feedback says "I want 90-day churn in the pulse strip"; until then asymmetric is the cleaner read.

#### D7. Audit-event retention for `admin.revenue.viewed` — never-prune, or scoped?

* **Proposal:** never auto-prune in v1 (matches the rest of `audit_events`).
* **Alternatives:** (a) 90-day retention (drop rows older than 90d via a nightly job); (b) 1-year retention; (c) tier-based (revenue audit events kept forever, other audit events pruned).
* **Recommendation:** never-prune until a platform-wide retention policy exists (which is a separate spec entirely). Auditors are likely to want every revenue-view event for the life of the company. The row size is small (~1KB) and the per-row write rate is low (~24/actor/hour cap from D4). Storage is not a near-term concern.

### 7. PR train breakdown

**PR 1 — `revenue-service-and-endpoints`.** Backend only.
* Add four new functions in `admin_subscription_service.py` (or a new sibling `admin_revenue_service.py`): `aggregate_revenue_summary`, `aggregate_revenue_by_tier`, `aggregate_revenue_timeseries(range)`, `aggregate_ai_cost_vs_revenue(range)`.
* Add new router `app/routers/admin_revenue.py` with the four endpoints under `/api/v1/admin/revenue/*`.
* Each endpoint pulls `require_permission("subscriptions.view")` AND `forbid_impersonation_session` (if L4.4 hasn't shipped, this PR contributes the dependency).
* Add `admin.revenue.viewed` audit event with the 60s throttle.
* Add Pydantic schemas in `app/schemas/admin_revenue.py`.
* Tests: per-endpoint contract tests, throttle test, permission test, impersonation-block test, mock_revenue propagation test, by-tier with no plans test, timeseries with empty subscriptions test.
* Independent: zero FE impact.

**PR 2 — `/admin/revenue` page.** Frontend only.
* New page `frontend/app/admin/revenue/page.tsx`.
* New components: `RevenuePulseStrip`, `MRROverTimeChart`, `ByTierTable`, `AICostVsRevenuePanel`, `TrialToPaidTile`.
* New API client functions in `frontend/lib/api.ts` for the four endpoints.
* Admin nav: add "Revenue" entry between "Subscriptions" and "Audit". Permission gate `subscriptions.view`.
* `mock_revenue` disclosure banner component.
* Loading skeletons; empty / error states.
* Independent: PR 1 ships the endpoints, this PR consumes them. Reverts cleanly (just deletes the page).

**PR 3 — Paddle cutover.** Service-code change.
* After L2.2 has landed and the 72-hour-and-95% verification (D2) has run.
* Flip `mock_revenue: True` → `False` in `aggregate_revenue_kpis` AND the four new sibling aggregators.
* Replace `mock_mrr: "0.00"` / `mock_arr: "0.00"` with the actual computed values.
* Swap the `updated_at` proxy for `canceled_at` if L2.2 added it (D3).
* Tests: re-verify all the existing PR 1 tests now expect mock_revenue=false; add a verification test asserting our derived MRR matches a captured Paddle export within 1%.
* Independent: PR 1 + PR 2 ship the surface; this PR is the data swap. Reverts cleanly to mock-zero.

### 8. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Revenue data is sensitive — a leak exposes the company's financial state. | Surface is superadmin-gated AND impersonation-blocked. The audit log writes a row per (actor, endpoint, minute) so any access leaves a trail. Per-org rows on `/ai-cost-vs-revenue` are NOT replayed into audit detail. |
| R2 | Mock-vs-real cutover (D2) is a one-shot flag flip — if Paddle data is wrong, the FE shows wrong dollars to every superadmin. | Three-gate cutover (72h, 95% coverage, ±1% reconciliation). PR 3 is a single tiny code change easy to revert. The `mock_revenue` flag flow stays in place even after cutover so we can re-enable mock-mode if Paddle suddenly returns garbage. |
| R3 | Audit-event volume — even at the 60s throttle, we add ~24 rows/actor/hour. | At superadmin headcount of single digits, this is ~tens of thousands of rows per year. `audit_events` is indexed and small per-row. Retention policy (D7) is a future concern, not a blocker. |
| R4 | Impersonation-blocked path means a non-superadmin can never see revenue, but a superadmin impersonating an org owner also can't. | This is intentional — a superadmin debugging a user's experience should not see platform revenue while wearing that skin. The Exit-impersonation button is one click away. |
| R5 | Time-series reconstruction (D5) might be slow at >1K subscriptions. | 5-min Redis cache per range. If perf bites, add it in a follow-up PR with a single decorator. Worst case, we materialize a daily snapshot table (still scoped out for v1). |
| R6 | The `updated_at` proxy for churn date (D3) breaks the day L2.2 adds reactivation. | We accept this — D3 explicitly says PR 3 of this train swaps the proxy when L2.2 adds the real column. The window of risk is the lifetime of the proxy, which is bounded by the L2.2 timeline. |
| R7 | `/ai-cost-vs-revenue` reveals per-org names — even though gated, accidentally leaking the response payload to logs would expose customer identity tied to revenue. | The structlog redactor already drops response bodies above a size threshold. The per-org rows are not echoed into audit `detail`. Standard precautions; no new mitigation needed. |
