# Org-level Forecast + Budget toggles (TBD-197) — implementation spec v3

**Status:** ready to build.
**Date:** 2026-08-05 (v3).
**Supersedes:** `specs/2026-05-17-forecast-budget-toggles.md` and
`specs/forecast-budget-enable-disable.md`. Do not resurrect the latter's key names
`feature.forecasts` / `feature.budgets` — they sit in the RESERVED `feature.`
OrgSetting namespace.

**Why v3 exists.** v1 was rejected (11 blocking). v2 folded those and **introduced
11 more** — net zero. The findings clustered in one place every round: **all three
designs tried to make the dashboard feature-aware**, and each failed differently
(layout filtering destroyed saved layouts; widget-list filtering missed the desktop
path; in-shell notices had no shell to render in on mobile). Both architects then
independently withdrew their own rulings and adopted a **radical subtraction**:
the dashboard learns nothing at all.

v3 is smaller than v1. The whole frontend is now one exported constant, one nav
filter, two page notices, and three fetch skips.

---

## 1. What ships

An org admin can turn **Forecast** and **Budgets** off for their organization.
Both ship **ON** for every existing and new org. Turning one off closes its API
routes, hides its nav entry, and replaces its page with a one-line notice.

Dashboard tiles for a disabled feature fall back to **their own existing empty
states**. Nothing is deleted; the user's layout is untouched; re-enabling restores
everything.

---

## 2. Substrate — proven by build, unchanged since v1

**System 1 (`feature_gate.py`) owns this**, not the L4.11 plan-entitlement
substrate. `feature_catalog.py` is untouched, so `ALL_FEATURE_KEYS` stays at 4 and
none of the 15 test files pinning that set go red. Verified empirically: the
backend kernel was built and run — **8/8 fences RED against their named mutants,
full suite 3758 passed, zero regressions.**

- `config.py`: `feature_forecast: bool = True`, `feature_budgets: bool = True`.
  Precedent in production at `config.py:251` (`feature_custom_dashboard`).
- `Feature` enum + `_ENV_FLOOR` entries for both.
- **No `default=True` on `PlanFeatures`.** `feature_service.py:34-35`'s fail-closed
  docstring and `test_feature_service.py:97-110`'s exact-dict assertion stay true
  and unmodified. (Second reason: `_fetch_plan_features:53-61` joins
  Subscription→Plan with **no status filter**, so a churned org would otherwise
  have kept both features forever.)

⚠ `_ENV_FLOOR[feature]()` is `[]` indexing — a missing key is a KeyError/500, not
a fail-closed `False`. Fenced by F1b.

---

## 3. The mask lives INSIDE `resolve_feature`

Org intent is stored separately from operator intent, because one slot with two
writers is what broke v2: `admin_features.py:281-290` lets a superadmin grant one
org a feature against a global `"off"`, and an org admin's "enable" deleting that
row silently flipped the feature **True → False**, unrecoverably.

```
OrgSetting("orgpref.<feature>", "off")   <- org admins, off-only, never "on"
OrgSetting("feature.<feature>", on|off)  <- superadmins only (unchanged)
SystemSetting("feature.<feature>")       <- global (unchanged)
env floor                                <- unchanged
```

**`resolve_feature()` BECOMES the masked, tenant-facing answer** — same name, same
signature. The unmasked three-level chain becomes `_resolve_platform_feature()`,
module-private.

This is the fix for v2's worst defect. v2 said "resolve the three, then mask",
which made masking a *call-site* obligation; a builder who masked only
`/auth/status` would have shipped nav hiding, a page notice, and **every backend
route still open**, with a fully green test table. Making the safe resolver the
default name means no call site can be missed.

**All four call sites, exhaustively:**

| Site | Uses | Note |
|---|---|---|
| `feature_gate.py:118` (`require_feature`) | `resolve_feature` (masked) | **The site v2's defect left open.** Zero edit. |
| `auth.py:238-240` (`/auth/status`) | `resolve_feature` (masked) | Zero edit. |
| `admin_features.py:233` (`list_org_features` — what the superadmin UI renders) | `_resolve_platform_feature` **+ the orgpref row, as separate fields** | Fixes the operator-visibility gap. |
| `admin_features.py:295` (`set_org_feature` write echo) | same shape | Read and write must not disagree. |

`list_org_features` returns `{feature, override, org_preference, effective}`
(`:235-237`). Without `org_preference` an operator sees `override: inherit,
effective: false, global on` and cannot explain it.

⚠ `feature_gate.py:3-12`'s module docstring describes three levels. Rewrite it to
describe two questions ANDed, or it becomes the next stale-comment trap.

### The endpoint

```
PUT /api/v1/settings/features/{feature}     body: {"enabled": bool}
  gate:    _require_admin (settings.py:61-66)
  feature: Literal["forecast", "budgets"]   <- allow-list; never reaches
                                               reports / plans / custom_dashboard
  enabled=false -> upsert OrgSetting("orgpref.<feature>", "off")
  enabled=true  -> DELETE that row
  response: {feature, enabled}   (the re-resolved effective value)
  audit:    org.config.feature.set on both branches
```

Escalation is impossible **by construction**: the org layer has no `"on"` value to
write. `_require_admin`, not `require_org_owner` — `org_permissions.py:28-33`
reserves owner for *"tenant-scoped destructive operations"*, and the neighbouring
manual-balance toggle (which rewrites account balances) is admin-gated at
`settings.py:957`.

**Required:** extend `RESERVED_SETTINGS_PREFIX` (`settings.py:53`) to a tuple
including `"orgpref."` — `str.startswith` accepts a tuple, so `:93` and `:173` work
unchanged. ⚠ The 403 detail strings at `:96` and `:176` hardcode *"The 'feature.'
settings namespace…"*, which becomes a lie for `orgpref.` keys. Update them.

⚠ `admin_features._upsert_org_setting:108-118` has **no IntegrityError retry**,
unlike `settings.py:139-155`, and `org_settings` has `uq_org_settings_org_key`.
Concurrent double-PUT → 500. Copy the retry.

⚠ Add `resolve_features(list[Feature], org_id, db)` doing one `IN` query per level.
`/auth/status` goes from 3 features to 5, each now reading two keys.

⚠ This endpoint sits beside a previously fleet-caught **Critical auth bypass**. It
must go through `/security-review`, reading the fixed code independently.

---

## 4. Backend gates

| Surface | File | Gate |
|---|---|---|
| `/api/v1/budgets` (8 handlers) | `budgets.py:19` | router-level `BUDGETS` |
| `/api/v1/ai/budget/*` | `ai_budget.py` | router-constructor `BUDGETS` |
| `/api/v1/forecast-plans` (12) | `forecast_plans.py:18` | router-level `FORECAST` |
| `/api/v1/ai/forecast/*` | `ai_forecast.py` | router-constructor `FORECAST` |
| `GET /api/v1/forecast` | `forecast.py:16` | **handler-level** `FORECAST` |
| `GET /api/v1/forecast/account-balances` | `forecast.py:26` | **UNGATED — §5** |
| `POST /api/v1/budgets/from-forecast` | `budgets.py:51` | additional handler-level `FORECAST` |
| `/api/v1/auth/status` | `auth.py:237-241` | add `forecast` + `budgets` |

⚠ **Neither AI gate is a router-level list today.** `ai_budget.py:45` is a **route
decorator** `dependencies=[...]`; `ai_forecast.py:66` is a **handler signature
parameter**. Put the System-1 dep on the `APIRouter(...)` **constructor** — FastAPI
solves constructor deps before both forms, so a forecast-disabled org gets 404
rather than a 403 advertising an AI upsell.

⚠ **Import collision.** `ai_budget.py:22` and `ai_forecast.py:32` already bind
`require_feature` from `app.auth.feature_deps` (System 2, takes a `str`). A plain
import of `feature_gate.require_feature` rebinds the name and breaks the existing
dep at import time. **Alias it** (`require_product_area`).

⚠ Widening `Feature` widens `admin_features.py:134` from 3 rows to 5. The frontend
label maps `OrgFeatureGateCard.tsx:12` and `FeatureFlagsCard.tsx:12` should be
widened for correct labels — but **this does not break `tsc`** (v2 claimed it did;
both cards type server data through `apiFetch<T>()`, which is unvalidated at
runtime, and both already carry a `?? gate.feature` fallback at
`OrgFeatureGateCard.tsx:117,130` / `FeatureFlagsCard.tsx:111,123`). Consequence of
skipping is cosmetic: two admin cards render raw slugs.

---

## 5. Loan / Credit-Card: `/forecast/account-balances` stays ungated

`account_balance_forecast_service.py:50-63` imports no `ForecastPlan` and no
`Budget`; it synthesizes credit-card statement cycles and loan amortization. It is
an account-projection engine mounted under a `/forecast` URL prefix.
`LoanPayoffTile` and `CreditUtilizationWidget` keep working with zero edits.

⚠ Do not apply service-provenance mechanically: `forecast_service.py:15-22` also
imports no `ForecastPlan`, yet `GET /forecast` **is** gated. The distinction is
consumer-side — `forecastProjection` has one consumer (`OnTrackWidget.tsx:16,27`)
which compares it against `forecastPlan` from `/forecast-plans/current`.

⚠ **Standing trap.** An ungated route inside an otherwise-gated area invites the
next engineer to "fix the inconsistency" by moving the dep to `forecast.py:12`,
silently breaking Loans and Credit Cards. Required: a loud module docstring on
`forecast.py`, **and** fence F7.

⚠ **Landmine, record it here:** `AccountMonthEndForecast.tsx:128-137` returns a
bare `<p>Loading…</p>` when `forecast` is null. Anyone who later skips
`loadAccountMonthEndForecast` gets a permanent false loading state. This is the
single reason a blanket "let every tile render empty" rule would have failed, and
the only reason it does not apply is that this loader keeps running.

---

## 6. Frontend — the whole of it

### 6.1 One exported default

There are **three** hand-maintained `features` literals: `AuthProvider.tsx:149`
(`useState` init), `:376` (inside `logout()`), and a partial third at
`AppShell.tsx:290` (`features ?? { reports: false, plans: false }`). The new keys
default **`true`** while `reports`/`plans` default **`false`** — mixed polarity
across three copies is a guaranteed drift bug.

```ts
export const DEFAULT_FEATURES = {
  reports: false, plans: false, customDashboard: false,   // opt-in, ship off
  forecast: true, budgets: true,                          // ship on
} as const;
```

Use it at all three sites. `logout()` writes `setFeatures(DEFAULT_FEATURES)`.

**Two-state boolean. No tri-state.** v2 introduced an "unknown" third state; it is
unobservable, because every consumer uses `features?.x === false`
(`app/reports/page.tsx:47,140`) or truthiness (`dashboard/page.tsx:156`), both of
which collapse unknown into `true`. All three of v2's fences passed a two-state
build, so they fenced nothing.

⚠ Amend the comment at `AuthProvider.tsx:140,147-148`, which states the
false-until-`/status` rule as universal. It is correct only for opt-in flags.

**Accepted cost:** a booting or just-logged-out client briefly shows Forecast and
Budgets nav items even for an org that disabled them. A flash, not a leak — every
route is closed server-side by `require_feature`. The alternative (defaulting
false) flashes them *missing* for 100% of orgs on every cold load.

### 6.2 Nav

`AppShell.tsx:94-103` carries `/budgets` and `/forecast-plans` in `baseNavItems`
**unconditionally**. `buildNavItems` (`:135`) filters only Reports and Plans.
Extend it to drop both when off. Fenced by F15 — **v1 and v2 had no fence for this
at all**, so an implementation that never touched `buildNavItems` passed the entire
suite while shipping a nav link to a 404.

### 6.3 Pages

A shared `FeatureDisabledNotice` renders inside the **client island** of both
pages, reading `useAuth().features`.

- `app/budgets/page.tsx` — client already (`"use client"` line 1).
- `ForecastPlansClient.tsx` — `app/forecast-plans/page.tsx` takes **zero changes**;
  `serverFetch`'s null contract (`:109-115`) turns the 404 into `initialPlan = null`
  and the island mounts normally.

Small centered `card`, `cardTitle` heading, one `text-sm text-text-secondary` line
— *"Budgets isn't enabled for {org}. An organization admin can turn it back on in
Settings → Planning tools."* — plus, for admins only, a single `btnLink`. **No
`btnPrimary`**: no brass on a page whose message is absence.

Also hide the three "From Forecast" affordances on `/budgets` when Forecast is off:
header button `:321-326`, next-period seed `:567-569`, empty-state prose `:588`.

### 6.4 Fetch skips — the only dashboard mechanism

In `DashboardDataProvider.tsx` only, skip `loadBudgets` (`:534-535`),
`loadForecastProjection` (`:591`), `loadForecastPlan` (`:639-640`).

**Do NOT skip `loadAccountMonthEndForecast` (`:619`)** — §5.

This is load-bearing, not optional: without it the gated routes 404, `apiFetch`
throws, and `loadForecastProjection`'s catch sets `projectionFailed = true`, which
`OnTrackTile.tsx:200-232` renders as an **error with a retry button**. A deliberate
org setting must never render as a failure.

**Legacy shell** (`dashboard/page.tsx`) keeps plain conditional hiding at
`:633-644`+`:1121-1170`, `:796-822`, `:855-878`, `:1061-1119`, `:820`, **and needs
its own fetch skips**: `:286-289` and `:312-315` put `/api/v1/budgets` and
`/forecast-plans/current` inside `Promise.all`, and a rejection replaces the whole
page with *"Failed to load dashboard data"* (`:407`). Fenced by G6.

### 6.5 The dashboard learns NOTHING

**No widget, tile, `WidgetShell`, `AddWidgetMenu`, `Canvas`, `renderDashboardWidget`,
or `dashboard.py` layout-seed code changes.** Disabled tiles fall back to their own
existing empty states, all verified present and non-crashing:

| Widget | Null state | Where |
|---|---|---|
| `OnTrackTile` | "No plan for this period. Set one up →" | `:170-197` |
| `ForecastBarsWidget` | "No forecast for this period. Set one up." | `:83-92` |
| `BudgetBarsWidget` | "No budgets for this period. Add one." | `:96-104` |
| `AccountForecastWidget` | **not gated** — renders real data (§5) | — |

**Accepted costs, deliberately not fixed here:**
- Those empty states link to `/forecast-plans` and `/budgets`, which now show the
  notice. A dead-end click that explains itself in one line — a discovery path, not
  a defect.
- A user whose canvas is mostly forecast/budget tiles sees several "no data" cards
  that do not say *why*. The nav absence and the Settings card carry the
  explanation.
- A new org seeded with Budgets off (`dashboard.py:107-113`) sees an empty Budget
  tile.

⚠ **Do not "fix" this copy in this ticket.** Making a widget feature-aware to
improve its empty-state wording is exactly how the cluster restarts. File it.

---

## 7. Settings UI

A **"Planning tools"** card on `/settings/organization` with two switches. Not
"Features" — that collides with `OrgFeatureGateCard.tsx:88` and
`FeatureOverridesCard.tsx`, the fault line memory warns against conflating.

Placed **below** the operational cards and **above** the Danger Zone.
Body: *"Turn off the parts of the app your household doesn't use. Nothing is
deleted, and turning a tool back on restores everything."*
Do **not** use "Not available on your plan" — upsell copy on a settings page.

**Switch form, applied to the new controls only:**
- `role="switch"` + `aria-checked`, immediate mutation, **no confirm dialog** (the
  change is non-destructive).
- Visible "Enabled"/"Disabled" text span — never colour alone
  (`settings/organization/page.tsx:749-751` is the model).
- `focus-visible:ring-2 focus-visible:ring-accent/30`.
- Theme-token knob, **not `bg-white`** (`check-design-tokens.sh:59` only bans
  `text-white`/`text-black`, so `bg-white` passes CI while violating *No Off-Token*).
- 44px hit area (`h-6 w-11` is 24px, under the DESIGN.md:280/341 floor).
- No shadow on the knob at rest (*State-Only Shadow Rule*).

**Deleted from v2: the shared switch primitive and its four-switch migration.**
That migration was the extraction's entire justification and had no fence; it is a
refactor riding a feature ticket. File separately, along with the pre-existing
One Brass Rule violation on that page (five `btnPrimary` at `:471,:552,:669,:763,:796`
against DESIGN.md:217's "at most twice").

*Architect dissent recorded:* one architect preferred reusing the existing
`aria-pressed` button + `ConfirmModal` (`:751-772`, `:942-961`) for zero new
pattern. Overruled: both architects previously ruled the change non-destructive,
and a confirm dialog on a non-destructive toggle is worse UX than a seventh switch.

⚠ **Global-deny state.** `/auth/status` returns only a resolved boolean, so a
global `"off"` and an org opt-out are indistinguishable **at page load**. The
"Off — set by your administrator" badge is therefore **write-response-only**: it
renders when a `PUT {enabled:true}` returns `enabled:false`, and not on load. v2
presented it as a complete replacement for `locked_by_plan`; it is not. Specified
here so the load state is deliberate rather than forgotten.

⚠ Measure contrast against `globals.css`, not DESIGN.md — its frontmatter hexes are
stale and would produce a false finding (TBD-320).

---

## 8. Tests

**Every fence must name the row it writes.** No fence may say "budgets off". This
is the rule that would have caught v2's worst defect.

### PR 1 — backend

| id | Writes / state | Observes | Expects | Mutant killed |
|---|---|---|---|---|
| F1 | nothing anywhere | `resolve_feature` both | `True` | env floor `False` |
| F1b | `_ENV_FLOOR` entry removed | `resolve_feature` | KeyError, not silent `False` | missing enum entry |
| **F2a** | `OrgSetting(org,"orgpref.budgets","off")` | `GET /api/v1/budgets` | **404** | **mask applied only in `auth.py`** — B1's exact exploit |
| F2b | same | `/auth/status` | `features.budgets is False` | mask deleted from resolver |
| F2c | `SystemSetting("feature.budgets","off")` | `GET /api/v1/budgets` | 404 | (models `test_feature_gate.py:78`) |
| F2d | same | `/auth/status` | `False` | |
| **F2e** | `SystemSetting off` + `OrgSetting("feature.budgets","on")`, org sends `{enabled:true}` | resolve + row | effective **True**, **grant row intact** | v2's design, which destroyed the grant |
| F2f | `OrgSetting("feature.budgets","on")` + `OrgSetting("orgpref.budgets","off")` | resolve | **False** | mask deleted |
| F3 | `PUT {enabled:false}` then `{true}` | rows | `orgpref` written then deleted; **`feature.budgets` never written by this endpoint** | any write to the `feature.` namespace |
| F4 | `PUT /settings/features/reports` | — | 422 | allow-list omitted / typed `str` |
| F4b | generic `PUT /api/v1/settings` key `orgpref.budgets` | — | 403 | prefix tuple not extended |
| F5 | non-admin member | PUT | 403 | gate omitted |
| F6 | `orgpref.budgets=off`, **`ai.budget: False`** | `/ai/budget/rebalance` | **404 not 403** | System-1 dep after the `ai.budget` dep |
| F6c | `budgets` on, `ai.budget: False` | same | **403** (control) | — |
| G1 | — | `/auth/status` | all five keys, correctly resolved | |
| G2 | — | `test_feature_service.py` | green, **unmodified** | proves the fail-closed invariant was not spent |
| G3 | both PUT branches | `audit_events` | one row each | |

⚠ **F6's non-vacuity requires `ai.budget: False`.** Its natural home,
`test_ai_budget_router.py`, sets `"ai.budget": True` in every test — written there
the mutant **passes**.

⚠ `backend/tests/conftest.py` has **no DB/session fixture**; every suite hand-rolls
in-memory SQLite + `dependency_overrides` (~30 lines per new file).

⚠ `test_settings_feature_namespace.py:195` patches `feature_gate.app_settings` with
a bare `MagicMock` setting only two attributes — any new `_ENV_FLOOR` entry
resolved in that block returns a truthy MagicMock. Will silently fake-pass anyone
adding assertions there.

### PR 2 — backend

| id | Writes | Observes | Expects | Mutant killed |
|---|---|---|---|---|
| F7 | `orgpref.forecast=off` | `/forecast/account-balances` **and** `/forecast` | **200** and **404** | dep moved to `forecast.py`'s router → breaks Loan + CC |
| F8 | `orgpref.forecast=off` | `/budgets/from-forecast` and `GET /budgets` | 404 and 200 | cross-feature gate omitted |

⚠ **F8's control needs a seed.** `create_budgets_from_forecast` raises
`ValidationError` with no `ForecastPlan`, and in a bare `FastAPI()` test app it
**propagates** rather than becoming the production 400. Seed `get_current_period`
+ a `ForecastPlan`.

### Frontend

| id | Asserts | Mutant killed |
|---|---|---|
| **F15** | `budgets:false` → the "Budgets" nav item is **ABSENT** | **never touching `buildNavItems`** — `baseNavItems` carries it unconditionally, so v1 and v2 both passed while shipping a link to a 404 |
| F13 | `budgets:true` → nav item and page body **present** (control) | a gate that hides unconditionally |
| F16 | `DEFAULT_FEATURES.forecast === true && .budgets === true && .reports === false` | a future reader "correcting" the polarity split |
| F12 | `budgets:false` → `apiFetch` never called for `/api/v1/budgets`; `forecast:false` → never for `/forecast` or `/forecast-plans`; **always** for `/forecast/account-balances` | both the do-nothing build and over-gating |
| G5 | `FeatureDisabledNotice` renders on `/budgets` when off, contains no `btnPrimary` | |
| G6 | legacy shell, `budgets:false` → no whole-page error banner | `Promise.all` rejection |
| F14 | switch has accessible name, `role="switch"`, **both** `aria-checked` states, visible Enabled/Disabled text | colour-only state |

⚠ **F12's positive clause** is suppressed by `loadAccountMonthEndForecast`'s own
`!realPeriodStart || !isCurrentSelectedPeriod` early return (`:607-612`) — the
fixture **must** select the current period or it goes falsely RED.

⚠ **F14 must scope by container id.** `getByRole("switch")` now matches ≥6 nodes on
that page (`SchedulerSettingsCard.tsx:209,245,302`, `SmartRulesSection.tsx:82`).
Never index positionally (TBD-313).

**Deleted from v2 and why:** the tri-state fences F9/F9b/F9c (all passed a
two-state build); F10 (asserted "no `WidgetShell` content", which dies with the
notice, and was RED against the correct implementation); F11's CC-chip assertion
(inherited F12's early-return trap for coverage F12 already gives); G4 (vacuous —
`loadForecastProjection`'s existing `!realPeriodStart` branch already sets
`projectionFailed = false` against unmodified `main`).

---

## 9. PR sequence

**PR 1 — substrate + Budgets, end to end.** `Feature` enum widened once (both
keys), both env floors, both `/status` keys, `_resolve_platform_feature` + the mask
inside `resolve_feature`, batched `resolve_features`, the `PUT` endpoint,
`RESERVED_SETTINGS_PREFIX` tuple + corrected 403 strings, `admin_features`
`org_preference` field, `DEFAULT_FEATURES`, nav filter, `FeatureDisabledNotice`,
`/budgets` notice, `loadBudgets` skip, legacy Budget Progress hide + fetch skip,
the Planning tools card with one switch.

⚠ **The mask must land in the same PR as the endpoint**, never before — it changes
every existing `resolve_feature` caller's behaviour at once.

*After PR 1:* Budgets is fully toggleable; Forecast untouched. Shippable alone.

**PR 2 — Forecast, end to end.** Remaining routers, the `forecast.py` handler dep,
the cross-feature gate, `ForecastPlansClient` notice, the three "From Forecast"
sites, legacy forecast surfaces, `loadForecastProjection` + `loadForecastPlan`
skips (**not** account-balances), second switch.

**Run serially** — both touch `AuthProvider.tsx`, `AppShell.tsx`,
`settings/organization/page.tsx`, `DashboardDataProvider.tsx`.

**Left alone deliberately:** `settings.py:117-126` (`FORECAST_INPUT_GRANULARITY_KEY`,
knowingly orphaned), `scenarios.py:270` (behind `Feature.PLANS`, env floor `False`,
unreachable today), `categories.py:168` (`forecast_items` count is true and has
zero non-test frontend references), admin/org-data counts (operator-facing).

---

## 10. Known one-way door: pricing

This design bets Forecast and Budgets are **table stakes, not upsells**. The seeded
tiers (`023_plans_and_subscriptions.py:85-113`) differentiate Free from Pro on
seats, retention and the **AI** capabilities; both features are in both tiers, and
every key ever stored in `plans.features` has been an AI capability.

If that is wrong: add both keys to `FeatureKey`/`PlanFeatures` (paying the 15-test
exact-set blast radius then), migrate every `orgpref.` row into
`org_feature_overrides`, and rebuild the plan-entitlement guard. Use a **data**
migration stamping `True` into `plans.features` (precedent:
`028_plan_features_and_org_overrides.py:59-68`), **not** `default=True` on the
model. Roughly 1-2 days plus a data migration — a costed decision, not a wall.

---

## 11. Findings rejected, with evidence

**"Delete the five legacy dashboard gate sites; the component gate covers them."**
REJECTED. `OnTrackWidget.tsx:4` is a thin wrapper importing `OnTrackTile` at `:11`;
legacy imports `OnTrackTile` **directly** at `dashboard/page.tsx:30`.

**"Delete the System-1 gates on the two AI routers; the `ai.*` dep already denies."**
REJECTED. Those deny orgs **without the AI entitlement**. A Pro org has
`ai.forecast: True`, so a Pro org that disabled Forecast would still reach
`POST /api/v1/ai/forecast/refine`. The justification given was UI reachability,
which contradicts the same architect's earlier position that the server gate exists
for direct API callers and stale tabs. Demonstrated empirically by the build probe.

**"`OrgFeatureGateCard.tsx:12` is a closed union; widening `Feature` breaks `tsc`."**
REJECTED (v2 claim). Both cards type server data through `apiFetch<T>()`, which
TypeScript never validates at runtime, and both already carry `?? gate.feature`
fallbacks. The real consequence is cosmetic.

### Design history

| Round | Outcome |
|---|---|
| v1 | 11 blocking. Substrate ruling survived; dashboard design (layout filtering) destroyed saved layouts |
| Build probe | Backend kernel proven: 8/8 fences RED against mutants, 3758 green, zero regressions |
| v2 | Folded v1's 11, **introduced 11 more**. Dashboard redesigned twice more, wrong both times |
| v3 | Both architects withdrew their own rulings; dashboard gating **deleted entirely** |
