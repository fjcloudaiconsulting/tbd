# W1 — Quick Wins Design (2026-06-22)

Part of the 2026-06-22 re-prioritization (`2026-06-22-product-reprioritization.md`). Two **independent** PRs that ship in the same wave:

- **W1a** — Reports multi-select transaction-type filter (frontend-only).
- **W1b** — Founding-members v1 (copy + founder flag + activity tracking + live counter).

---

## W1a — Reports multi-select transaction-type filter

### Goal
Replace the single-select transaction-type control (Income / Expense / Transfer / Any) in the report builder with **checkboxes**, so a report can filter by any combination (e.g. Income **and** Expense, or Transfers only). **None checked = "Any"** (no filter), preserving today's default.

### Key finding: backend already supports it
The transactions source advertises `("eq", "in")` on `txn_type` (`backend/app/reports/sources/transactions.py:43`); the query service compiles `.in_()` (`backend/app/services/reports_query_service.py:147-185`); the query schema validates `op="in"` requires a non-empty list (`backend/app/schemas/reports_query.py:191-194`). A backend test already proves `TXN_TYPE IN [...]` end-to-end (`test_recurring_source.py::test_txn_type_in_filter`). **No backend change.**

### Changes (frontend-only, 4 files + tests)
1. **`frontend/lib/reports/types.ts:148`** — `txn_type?: "income"|"expense"|"transfer"` → `txn_type?: ("income"|"expense"|"transfer")[]`.
2. **`frontend/components/reports/config/FilterEditor.tsx:177-228`** — replace `TxnTypeRadioRow` (radio) with a checkbox group: Income, Expense, Transfer. Empty selection ⇒ `txn_type` undefined (no filter). Keep the existing "Transfer is transactions-only" conditional (recurring source has no transfers — omit the Transfer checkbox for non-transactions sources).
3. **`frontend/lib/reports/resolve.ts:155-157`** — emit `{ field:"txn_type", op:"in", value: widget.txn_type }` only when the array is non-empty (drop the old single-value `op:"eq"`).
4. **`frontend/lib/reports/describe-filters.ts:75-78`** — chip label joins the array, e.g. `"Income, Expense"`.

### Migration / back-compat note
Existing saved reports store `txn_type` as a **string**. Normalize on read: when loading a widget, coerce a string `txn_type` to a one-element array (`typeof x === "string" ? [x] : x`) so old reports keep working. Apply this in the widget-hydration path (wherever `WidgetFilters` is parsed from the persisted `layout_json`). Add a test for the coercion.

### Tests
- Update `frontend/tests/components/reports/widget-filter-chips.test.tsx` (chip now joins an array).
- Update any `FilterEditor`/editor-page test asserting the radio control.
- Add: empty array ⇒ no filter emitted; `["income","expense"]` ⇒ `op:"in"`; string→array hydration coercion.
- Verify the **full** vitest suite passes (per `reference_frontend_full_suite_verification`).

### Out of scope
Status (Settled/Pending) and Amount-range filters — the separate "Reports builder filters" backlog item. This PR is **transaction type only** (what the operator asked for).

---

## W1b — Founding-members v1

### Goal
1. Rephrase "Free in beta" so it reads as a stable-but-free product **and** communicates the founders offer.
2. Permanently flag founding members (`is_founder`), grandfathering existing users.
3. Track per-user activity (`last_active_at`) now; enforce inactivity revoke later (with payments).
4. Show a **live count** of founding members on the apex landing.

Referral discount and inactivity-revoke enforcement are **W2** (payments wave) — not built here.

### Landing copy (4 places)
"Free while in beta" currently appears in:
- `frontend/components/landing/Hero.tsx:48`
- `frontend/components/landing/VsPageLayout.tsx:133`
- `frontend/lib/comparison.ts:158`
- `frontend/tests/comparison-data.test.ts:24` (pinned-string test guard)

Changes:
- **Hero (`Hero.tsx`)** — make the line carry the offer + counter. Proposed (operator to tweak):
  > **"Free for our first 1,000 members — for as long as you use it."** · *{count} founding members so far*
- **Comparison matrix value (`comparison.ts:158`)** → `"Free while we grow"`.
- **`VsPageLayout.tsx:133`** → `"... Free while we grow."`
- **Update the test guard** (`comparison-data.test.ts:24`) to the new string.

### Backend — migration `066`
Add to `backend/app/models/user.py` (after the existing boolean flags / optional timestamps, ~lines 70 / 102):
- `is_founder: bool` — `mapped_column(Boolean, nullable=False, server_default="1")`. **`server_default="1"` grandfathers all existing rows** as founders. New users get it explicitly at registration.
- `last_active_at: Optional[datetime]` — `mapped_column(DateTime(timezone=True), nullable=True)`. NULL until first stamped.

Migration `066_*` (next number after `065`): add both columns; `is_founder` server-default `1` covers the backfill.

### Registration
`backend/app/routers/auth.py` user creation (~line 304-314): set `is_founder=True` explicitly (alongside `is_superadmin=is_first_user`). Soft cap ⇒ **no gating** — every registration is a founder during the window.

### Expose the flag
`backend/app/schemas/auth.py` `UserResponse` (~line 64): add `is_founder: bool`; populate in the `_user_response()` helper (`auth.py:~98`). Lets the app surface a "Founding member" badge later.

### Activity tracking (cheap, throttled)
There is **no** existing per-user activity timestamp. Stamp `last_active_at` in `backend/app/deps.py get_current_user` (the one path every authenticated request already passes through), **throttled** to avoid a hot-path write:
- Only `UPDATE users SET last_active_at = now()` when `last_active_at` is NULL or older than a threshold (e.g. 1 hour). ⇒ ≤1 write/user/hour.
- Keep it fire-and-forget relative to the request (don't fail the request if the stamp write fails).
- Threshold via config (`LAST_ACTIVE_STAMP_THROTTLE_SECONDS`, default 3600).

This is enough signal for the later inactivity rule; no scheduler is built now.

### Live counter — hardened public endpoint
The apex is a **static export** (no server runtime; client-side fetch only). A bearer token would be exposed in the public bundle, so we use a **public, count-only** endpoint (the number is non-sensitive by design).

- **New public router** `backend/app/routers/public_stats.py` (or extend an existing public surface): `GET /api/v1/public/founder-count` → `{ "count": <int> }`. No auth.
- **Count query**: `is_founder = 1 AND is_active = 1`, **excluding** a configurable username list `FOUNDER_COUNT_EXCLUDE_USERNAMES` (CSV env, default `pfv_smoke_l05`). Parse like the existing CSV env settings in `config.py`.
- **Cache**: Redis with a short TTL (e.g. 300 s) keyed `public:founder_count`; fall back to a direct COUNT if Redis is unavailable (never 500 — return a sane number or the cached/last value).
- **Rate-limit**: apply the existing slowapi limiter (mirror other public routes), generous but bounded.
- **CORS**: add the apex origins (`https://thebetterdecision.com`, `https://www.thebetterdecision.com`) to `BACKEND_CORS_ORIGINS` (`config.py:148` default for dev, and the prod value in `.do/app.yaml`). GET only is already allowed.
- **Frontend (apex Hero)**: client-side `fetch` to `https://app.thebetterdecision.com/api/v1/public/founder-count`, render the count; **build-time fallback** number (`NEXT_PUBLIC_FOUNDER_COUNT_FALLBACK`) shown if the fetch fails or while loading, so the page never shows a broken/empty counter. Must be covered by the apex build path filter (`apex-deploy.yml paths:`) — see `reference_ga_tag_gateway` / the #466 lesson about apex deploy-path omissions.

### Tests
- Backend: register sets `is_founder=True`; `UserResponse` carries `is_founder`; migration round-trips; founder-count endpoint returns the right number and **excludes** the configured username; cache hit/miss; never-500 on Redis down. Run in an isolated `-p team-<name>` compose project (per CLAUDE.md).
- `last_active_at` throttle: stamps when stale, skips when fresh.
- Frontend: comparison-data test guard updated; Hero renders the fallback when fetch fails and the live number when it resolves.

### Operator follow-ups (post-merge)
- Confirm the apex CORS origin reaches prod (`.do/app.yaml`) and the counter loads on the live apex.
- Decide the final Hero wording.

---

## Wave-level final task (operator, after W1a + W1b merge)

**Review the live Google Ads copy for messaging consistency.** The Search campaign launched 2026-06-22 ("Search – EU – Forecasting+Competitors", 4 ad groups) was written against the old "Free while in beta" positioning. Once the landing copy changes to the founders framing ("Free while we grow" / "Free for our first 1,000 members"), the RSA headlines/descriptions, callouts, and sitelinks must be reviewed and aligned so the ad → landing message-match holds (and so the founders offer can be surfaced in the ads themselves if desired). Operator/console-only — no code. See `project_google_ads_launch`.
