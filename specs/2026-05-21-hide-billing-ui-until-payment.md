# Hide plan / billing UI from customers until payment platform is wired — design

**Status:** design only, awaiting architect review. Not yet implemented.
**Date:** 2026-05-21.
**Source:** operator request 2026-05-21 (session memory: today's CAPTCHA implementation session). Goal: ship a single global kill switch that hides the entire plan / billing customer-facing surface until the real payment platform is wired, without disturbing admin / operator views. Trial subscriptions still get created in the backend (the `create_trial` call in the register handler), they just aren't shown to customers.

## Goal

Add one boolean env var, `BILLING_UI_ENABLED`, that controls whether customers see ANY plan / trial / billing UI. When `false` (the new pre-launch default):

* Trial banner in the AppShell header does not render.
* `/settings/billing` renders an explanatory empty state instead of the plan grid.
* "Billing" tab disappears from the settings nav.
* Landing page (`/`) drops the "14-day free trial, no credit card required" marketing line.

When `true` (after payment platform is wired):

* All three customer touchpoints return to today's behavior.

Admin / operator surfaces under `/admin/*` and `/system/*` are unaffected in both states.

## Substrate (what already exists, riding the captcha gate pattern)

* `backend/app/config.py` — `Settings` already carries `captcha_required: bool = False` as a control-plane flag exposed via `/api/v1/auth/status`. Same shape will work here.
* `backend/app/routers/auth.py:157` — `/api/v1/auth/status` already returns `{ needs_setup, captcha_required }`. Extend with `billing_ui_enabled`.
* `frontend/components/auth/AuthProvider.tsx:134` — auth restore reads `/auth/status` for `needs_setup`. Already a natural mount-time fetch we can extend.
* `frontend/components/auth/RegisterPageBody.tsx` — captcha precedent: a client component reads `captcha_required` from `/auth/status` on mount and conditionally renders. We'll use the same shape for billing UI gating.
* Owner-only auth at `backend/app/auth/org_permissions.py:27-40` already protects `/api/v1/subscriptions` and `/api/v1/plans`. Defense in depth at the backend is **not** part of this spec (see Out of scope).

## Customer-facing surface (inventory)

Confirmed by a code survey on 2026-05-21:

| File | What it renders | Action when flag is off |
|---|---|---|
| `frontend/components/ui/TrialBanner.tsx` | Persistent trial badge in AppShell header (countdown / "Upgrade" / "Free Plan" CTAs). | Render nothing. |
| `frontend/components/AppShell.tsx:33,461` | Imports + places `<TrialBanner />` in the header. | No edit needed — the conditional lives inside TrialBanner. |
| `frontend/app/settings/billing/page.tsx` | Plan grid, current-plan card, upgrade / downgrade / cancel buttons. | Render explanatory empty state: heading + body copy "Billing isn't available yet. We will let you know when subscriptions launch." Keep the page accessible by URL but skip the data-fetch effects. |
| `frontend/components/SettingsLayout.tsx:13` | "Billing" tab entry in settings nav (owner-only). | Filter the entry out of the rendered nav tabs. |
| `frontend/app/page.tsx:48` | Marketing line "Create your free account and start making better decisions with your money. 14-day free trial, no credit card required." | Conditionally drop the second sentence. See "Landing page constraint" below. |
| `frontend/components/onboarding/OnboardingPageBody.tsx:373` | Single mention of "billing" in a feature description ("plan for what is next, and stay on top of every billing"). | Review during implementation — likely refers to transaction billing periods, not subscription billing. If transaction-billing, leave. If sub-billing, drop. |

## Admin / operator surface (preserved, no edits)

| File | Why it stays |
|---|---|
| `frontend/app/admin/subscriptions/page.tsx`, `[id]/page.tsx` | Operator KPIs + per-sub detail. Calls `/api/v1/admin/subscriptions/...`. |
| `frontend/app/system/plans/page.tsx` | System-admin plan CRUD. Calls `/api/v1/plans/all`. |
| `frontend/app/admin/orgs/page.tsx`, `[id]/page.tsx` | Org list with `subscription_status` / `trial_end`. Plan changes via `ChangePlanModal`. |
| `frontend/components/admin/ChangePlanModal.tsx` | Reusable in admin surfaces. |

## Critical design tension — the landing page

`/app/page.tsx` ("Create your free account…") is shared between:

1. The **app-host** deploy (App Platform, `app.thebetterdecision.com`) — there it's redirected to `/login` by `frontend/proxy.ts:106-120`, so the trial copy is never seen here.
2. The **apex** deploy (S3 + CloudFront, `thebetterdecision.com`) — there it IS the public landing. Per the apex build (`next.config.apex.ts`), this is a static export, NOT a Next.js runtime that can read `/auth/status`.

The flag can't be threaded at request time on the apex landing. Two options:

* **Option A (recommended): hardcode the copy edit.** Drop the "14-day free trial, no credit card required." sentence from `/app/page.tsx` in the same PR. When the payment platform is wired and `BILLING_UI_ENABLED=true`, a follow-up PR puts the sentence back. Pro: simple, one-line edits both ways. Con: the trial copy lives in code, not behind a runtime flag.
* **Option B: build-time `NEXT_PUBLIC_BILLING_UI_ENABLED` env var.** Apex build reads it at static-export time, branches the copy. Pro: matches the "single flag" model. Con: two flags to manage (backend env + frontend build-time), and changes still need a rebuild + redeploy of apex.

Spec recommends **Option A**. Operator review.

## Architecture

```
.env / .do/app.yaml
      │
      ▼
backend Settings.billing_ui_enabled: bool = False  ──┐
                                                     │
                                          /api/v1/auth/status
                                                     │
                       { needs_setup, captcha_required, billing_ui_enabled }
                                                     │
                                                     ▼
                          frontend AuthProvider reads on mount
                                                     │
                       passed through context to consumers
                                                     │
                       ┌─────────────────────────────┼─────────────────────────────┐
                       ▼                             ▼                             ▼
                 TrialBanner.tsx            settings/billing/page.tsx      SettingsLayout.tsx
                  (returns null)            (renders empty state)          (filters tab list)
```

## Files to touch

### Backend

* `backend/app/config.py` — add `billing_ui_enabled: bool = False` to `Settings`. Place under a new `# Billing UI` block following the existing CAPTCHA block, with a comment explaining the rollout intent (flip to true when the payment platform is wired).
* `backend/app/routers/auth.py` (`/status` endpoint, ~line 157) — extend the response shape:
  ```python
  return {
      "needs_setup": user_count == 0,
      "captcha_required": app_settings.captcha_required,
      "billing_ui_enabled": app_settings.billing_ui_enabled,
  }
  ```
* `.env.example` — add `BILLING_UI_ENABLED=false` with the explanatory comment.
* `ENVIRONMENT.md` — document the var (mirrors the CAPTCHA_* table rows: required-when, defaults per env, value in dev / preview / prod, what breaks when set wrong).
* `.do/app.yaml` — add `BILLING_UI_ENABLED: "false"` under the backend service envs. Same scope (`RUN_AND_BUILD_TIME`).

### Frontend

* `frontend/components/auth/AuthProvider.tsx` — extend the `/auth/status` fetch to capture `billing_ui_enabled`. Add a new field on `AuthContextValue` (`billingUiEnabled: boolean`, default `false` until status resolves). Update `useAuth()` consumers as needed.
* `frontend/components/ui/TrialBanner.tsx` — at the top of the render, `if (!billingUiEnabled) return null;`.
* `frontend/components/SettingsLayout.tsx:13` — filter the `billing` tab entry out of the rendered list when `!billingUiEnabled`.
* `frontend/app/settings/billing/page.tsx` — when `!billingUiEnabled`, short-circuit the data-fetching effects and render the empty state. Suggested copy: "Billing isn't available yet" heading + "We will let you know when subscriptions launch." body. No data calls, no plan grid.
* `frontend/app/page.tsx:48` — apply Option A copy edit: drop the trial sentence (until operator chooses Option B).
* `frontend/components/onboarding/OnboardingPageBody.tsx:373` — review-only; likely no change.

### Tests

* **Backend unit:** add a `/auth/status` integration test that asserts the new `billing_ui_enabled` field is present with the configured value. Cover both default-false and explicit-true via monkeypatch.
* **Frontend Vitest:** for each of the three gated components, add a test that confirms the hidden-state render path:
  * `TrialBanner.test.tsx` — when `billingUiEnabled=false`, returns null (no DOM output).
  * `SettingsLayout.test.tsx` — when `billingUiEnabled=false`, the rendered tab list does NOT include "Billing".
  * `settings-billing-page.test.tsx` — when `billingUiEnabled=false`, the empty state heading is in the DOM and `/api/v1/subscriptions` was not called.
* **Frontend AuthProvider:** extend the existing `/auth/status` mock in `tests/components/auth-provider.test.tsx` to return `billing_ui_enabled` and assert it flows into context.

## Rollout

1. Merge this PR with `BILLING_UI_ENABLED=false` in `.do/app.yaml`. Deploy. Customer-facing billing surface disappears in prod; admin/operator surfaces unchanged.
2. When the payment platform is wired (separate workstream, no ETA today):
   * Single-line commit flips `.do/app.yaml` to `"true"`.
   * Companion commit reverts the `/app/page.tsx` trial-copy edit (Option A) OR sets `NEXT_PUBLIC_BILLING_UI_ENABLED=true` and rebuilds apex (Option B, if chosen).
   * Deploy.

Rollback is symmetric — flip the env back to `false` and redeploy.

## Out of scope

* Backend API gating. `/api/v1/subscriptions`, `/api/v1/plans`, etc. stay reachable as today. They are owner-only and don't leak much; no UI calls them when the flag is off because the gated components don't render their data-fetch effects. Adds zero defense-in-depth but adds a layer of state coupling between the env var and route registration that makes rollback non-trivial.
* `User.subscription_status`, `subscription_plan`, `trial_end` fields in `/auth/me`. Backend keeps populating them from DB. Frontend code that reads them is in the three hidden components, so no extra plumbing is needed. If we ever expose these elsewhere on customer surfaces, we revisit.
* Onboarding tour copy review beyond a quick read. The single "billing" mention at `OnboardingPageBody.tsx:373` is most likely about transaction billing periods (the app's domain concept), not subscription billing. Confirm during implementation; if it IS sub-billing, drop it in this PR; otherwise leave.
* `frontend/app/page.tsx` apex build mechanics. Option A (hardcoded copy edit) keeps this PR small. Option B (`NEXT_PUBLIC_BILLING_UI_ENABLED`) is a follow-up if the operator wants the toggle pattern there too.

## Open questions for architect

1. **Landing copy** — operator picked "hide the trial line." Spec recommends Option A (hardcoded copy edit). Architect: agree, or push toward Option B (build-time NEXT_PUBLIC_ flag for apex)?
2. **Billing tab nav filtering** — `SettingsLayout.tsx:13` currently lists tabs with `minRole: "owner"` gating. Should the billing entry also carry a `minFlag: "billing_ui_enabled"` shape so future flags can ride the same filter pattern, or just inline the conditional for this one case? Spec leans toward inline today (one consumer, no precedent).
3. **Empty-state copy review** — proposed body: "Billing isn't available yet. We will let you know when subscriptions launch." Operator copy convention is no em-dashes (`feedback_no_em_dashes`). Architect: any other copy lint concerns?

## Naming + memory file pointers

* Env var: `BILLING_UI_ENABLED` (positive form: `true` = visible).
* Config field: `billing_ui_enabled: bool = False`.
* `/auth/status` JSON key: `billing_ui_enabled`.
* Frontend context field: `billingUiEnabled: boolean`.
* `[[project_bot_signup_captcha]]` — same operator-flag pattern as `CAPTCHA_REQUIRED`.
* `[[reference_do_spec_sync.md]]` — DO spec is authoritative on every deploy; the env var must land in `.do/app.yaml`, not only in the DO console.
