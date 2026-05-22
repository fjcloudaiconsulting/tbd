# Plans page, simulation sandbox for life-event planning, design

**Status:** draft, pending architect review 2026-05-22.
**Date:** 2026-05-22.
**Source:** product owner request 2026-05-22. Owner wants a dedicated surface where users can sketch life events (trip, large purchase, retirement) and see month-by-month projected impact on their accounts, WITHOUT touching real transactions, budgets, or recurring templates. The feature has to work end-to-end with no AI dependency; AI (Team E in parallel) is an enhancement that can plug in later.

## Goal

A "Plans" page that is a **simulation sandbox**. Users:

* Create one or more named scenarios (a trip to Lisbon, buying a car in 2027, retirement at 62).
* Each scenario carries plan-type-specific params (destination, duration, daily budget for a trip; target price, down-payment, financing for a purchase; target age, contribution curve for retirement).
* The backend projects month-by-month account balances over a horizon, layering the scenario's cashflows on top of the user's current state (accounts, recurring, budgets).
* The UI shows a chart per account, dip-below-zero alerts, an affordability verdict (green / yellow / red), and suggested adjustments.
* Nothing the user does on this page writes to real transactions, accounts, budgets, or recurring. Sandboxed.

## Substrate audit (what already exists, confirmed 2026-05-22)

* `forecast_service.compute_forecast` returns reportable income/expense aggregates for a single billing period. Anchored to "current period". Useful as a per-period subroutine, not a multi-month projector.
* `account_balance_forecast_service.compute_account_balance_forecast` answers "what will each account's balance be at the end of THIS period?" by summing pending deltas onto the stored balance. Single-period horizon, no scenario overlay. The shape we want to extend.
* `RecurringTransaction` carries frequency + next_due_date + amount + type + account_id + category_id. Perfect generator for the analytic baseline.
* `BillingPeriod` model gives us the period boundaries to align month-month output with.
* `forecast_plans` table is the **per-period budget editor** (categories with planned income / expense, status draft / active). Not what we are designing. Different concept, similar name. **This spec uses `scenarios` for the DB name to avoid the collision** (see Naming below).
* `subscription.Plan` is the billing-tier table at `/api/v1/plans`. Reinforces the naming-collision rationale.
* AppShell has 7 frame-menu items today. Adding Plans + Reports + AI as siblings would push to 10. See Navigation.

## Naming, locked

The word "Plan" is overloaded three ways in this repo:

| Concept | DB | API | UI label today |
|---|---|---|---|
| Subscription tier (billing) | `plans` | `/api/v1/plans` | not user-facing, `/system/plans` admin |
| Per-period budget editor | `forecast_plans` | `/api/v1/forecast-plans` | "Forecast Plans" sidebar item |
| **NEW**: life-event sandbox | `scenarios` (this spec) | `/api/v1/scenarios` | **"Plans"** in the UI |

**Locked, architect-approved 2026-05-22.** Internal name = `scenarios`. User-facing label = **Plans**. Specifically:

* Database table: `scenarios`. SQLAlchemy model class: `Scenario`. Pydantic schemas: `ScenarioCreate`, `ScenarioRead`, etc. Service module: `scenario_engine`, `scenario_service`. Router prefix: `/api/v1/scenarios`.
* Frontend route: `/plans/*`. Page title: "Plans". Frame-menu item label: "Plans".
* The user-visible word "scenario" appears nowhere in the UI. Internal docs and code comments are free to use "scenario" wherever it reads naturally.

A future cleanup PR may rename the **"Forecast Plans"** sidebar label to **"Period Budget"** to free up the word "Plans" entirely. Out of scope here; flagged.

## Schema

### New table, `scenarios`

```sql
CREATE TABLE scenarios (
    id INT NOT NULL AUTO_INCREMENT,
    org_id INT NOT NULL,
    user_id INT NOT NULL,
    name VARCHAR(120) NOT NULL,
    scenario_type ENUM('trip', 'purchase', 'retirement', 'custom') NOT NULL,
    params_json JSON NOT NULL,
    projection_json JSON NULL,
    projection_engine VARCHAR(40) NULL,
    projection_computed_at DATETIME NULL,
    horizon_months INT NOT NULL DEFAULT 24,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_scenarios_org_user (org_id, user_id),
    KEY ix_scenarios_org_active (org_id, is_active),
    CONSTRAINT fk_scenarios_org FOREIGN KEY (org_id)
        REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_scenarios_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
);
```

Notes:

* **Per-user.** `user_id NOT NULL`. Plans are private to the creator by default. Sharing is a future column flip (`visibility ENUM('private', 'org')`) and out of scope here.
* **Org-scoped enforcement.** Every query filters by `org_id` first, then `user_id`. Matches the "Org-scoped data" rule in CLAUDE.md.
* `params_json` is the typed params blob, see Plan templates below. Pydantic discriminated-union on `scenario_type` validates it on write.
* `projection_json` is the cached last-computed projection. NULL until first simulate. Re-written on each `POST /simulate`. Cheap, lets the list view show a sparkline without re-running the engine.
* `projection_engine` records which engine produced the cached projection (`analytic_v1`, `analytic_v1+ai_assumptions_v1`, etc), so we can invalidate when the engine version bumps.
* `horizon_months` defaults to 24. The column allows up to 480 (40 years) at the storage layer; the **per-`scenario_type` ceiling is enforced at the request validator**, not the column. Caps: 120 months (10y) for trip / purchase / custom; 480 months (40y) for retirement. See Horizon ceiling below.
* `is_active` is the soft-delete flag (matches the rest of the codebase). Active=false hides from list, keeps audit.

### Why a JSON column, not separate tables per type

Three reasons:

* Plan-type schemas evolve fast in v1 (trip params will change as we learn). A JSON column lets the schema iterate without alembic churn for each tweak.
* The params blob is read whole and written whole. We never index into individual fields server-side.
* Pydantic discriminated unions give us write-time validation. Reads are loose.

If a future plan_type needs queryable params (e.g. "list all scenarios with target_balance > 500k"), we extract that field to a column at that point. YAGNI for v1.

## Plan templates, v1

### `trip`

```json
{
  "scenario_type": "trip",
  "destination": "Lisbon, Portugal",
  "start_date": "2026-09-15",
  "duration_days": 10,
  "currency": "EUR",
  "transport_cost": "450.00",
  "accommodation_per_night": "85.00",
  "daily_budget": "70.00",
  "one_off_extras": [
    { "label": "Tickets, Castelo de Sao Jorge", "amount": "15.00", "on_date": "2026-09-17" }
  ],
  "source_account_id": 12
}
```

Engine derivation: a lump-sum expense on `start_date` for `transport_cost + accommodation_per_night * duration_days + daily_budget * duration_days + sum(extras)`, posted against `source_account_id`. Optionally smear daily_budget across the trip days for prettier chart shape, behaviour flagged.

### `purchase` (covers car / house / big-ticket items)

```json
{
  "scenario_type": "purchase",
  "subtype": "car",
  "label": "Replacement car, used Skoda Octavia",
  "target_date": "2027-03-01",
  "currency": "EUR",
  "total_price": "22000.00",
  "down_payment": "8000.00",
  "down_payment_account_id": 12,
  "financing": {
    "principal": "14000.00",
    "annual_rate_pct": "6.5",
    "term_months": 60,
    "first_payment_date": "2027-04-01",
    "payment_account_id": 12
  }
}
```

Engine derivation: lump-sum expense (`down_payment`) on `target_date` against `down_payment_account_id`. Then a monthly amortized expense from `first_payment_date` for `term_months` months, computed via standard mortgage formula `P * r / (1 - (1+r)^-n)`. If `financing` is null, treat as cash purchase (full `total_price` as one lump on `target_date`).

`subtype` is a free-form label hint, not enum, so "boat", "wedding", "kitchen remodel" all fit without code change.

### `retirement`

```json
{
  "scenario_type": "retirement",
  "target_retirement_date": "2048-06-01",
  "currency": "EUR",
  "monthly_contribution": "600.00",
  "contribution_account_id": 23,
  "target_balance": "750000.00",
  "annual_return_pct": "5.0",
  "contribution_curve": [
    { "from": "2026-06-01", "monthly": "600.00" },
    { "from": "2030-01-01", "monthly": "900.00" },
    { "from": "2035-01-01", "monthly": "1200.00" }
  ]
}
```

Engine derivation: monthly income (sign positive) into `contribution_account_id`. `contribution_curve` is optional, when present it OVERRIDES `monthly_contribution` for any month >= `from`. Compound interest on the contribution account at `annual_return_pct / 12` per month. The affordability verdict checks "projected balance at `target_retirement_date` vs `target_balance`."

### `custom`

```json
{
  "scenario_type": "custom",
  "label": "Sabbatical year, no salary 2028",
  "events": [
    { "kind": "income_off", "from": "2028-01-01", "to": "2028-12-31", "recurring_id": 7 },
    { "kind": "one_off_expense", "on_date": "2028-01-15", "amount": "3000.00", "account_id": 12, "label": "Tickets" }
  ]
}
```

Custom is the escape valve. It exposes the raw event primitives the engine consumes: `income_off` / `expense_off` (mute a real recurring for a date range), `recurring_on` (add a synthetic recurring), `one_off_income` / `one_off_expense`, `transfer`. Power-user only, surfaces in the UI behind a "Custom scenario" template.

## Horizon ceiling

Horizon caps split by `scenario_type`:

* **trip / purchase / custom**: max **120 months** (10 years).
* **retirement**: max **480 months** (40 years).

The DB column `horizon_months` allows the full 480 range; the cap is enforced by a Pydantic validator on the simulate-request payload (and the same validator runs on scenario create / patch). Backend cost is linear in horizon, sub-ms per month for the analytic engine.

Validator sketch:

```python
# backend/app/schemas/scenario.py

from pydantic import BaseModel, model_validator

_HORIZON_CAP_BY_TYPE = {
    "trip": 120, "purchase": 120, "custom": 120, "retirement": 480,
}

class SimulateRequest(BaseModel):
    engine: str = "analytic"
    options: dict = {}

    @model_validator(mode="after")
    def _cap_horizon(self, info):
        scenario_type = info.context["scenario_type"]  # injected by router
        cap = _HORIZON_CAP_BY_TYPE[scenario_type]
        if info.context["horizon_months"] > cap:
            raise ValueError(f"horizon_months exceeds cap for {scenario_type} ({cap})")
        return self
```

(Validator binds against the scenario row's stored `horizon_months` and `scenario_type`; the router injects both into the validation context. Same pattern is reused by the `Scenario` create / patch validator so a row cannot be persisted with an over-cap horizon either.)

## Simulation engine, non-AI baseline

### Interface

```python
# backend/app/services/scenario_engine/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

@dataclass
class WorldState:
    """Snapshot of the user's current finances, frozen at simulation time."""
    accounts: list["AccountSnapshot"]          # current balances, type, currency
    recurring: list["RecurringSnapshot"]       # active recurring templates
    history: list["MonthlyCashflowPoint"]      # last 12 months net cashflow, per account

@dataclass
class SimulationRequest:
    scenario: "Scenario"
    state: WorldState
    horizon_months: int
    options: dict   # engine-specific knobs (e.g. {"smooth_with_regression": true})

@dataclass
class SimulationResult:
    engine_name: str               # "analytic_v1"
    computed_at: datetime
    horizon_months: int
    per_account_series: list["AccountSeries"]   # month-by-month projected balance per account
    alerts: list["DipAlert"]                    # dip-below-zero events
    verdict: "AffordabilityVerdict"             # green | yellow | red + reason
    suggestions: list["Suggestion"]             # zero or more adjustment hints

class ScenarioEngine(ABC):
    name: str

    @abstractmethod
    def simulate(self, req: SimulationRequest) -> SimulationResult: ...
```

### Analytic baseline algorithm

For each month `m` in `[1..horizon_months]`:

1. **Seed**: take each account's current balance.
2. **Apply real recurring**: for each active `RecurringTransaction` in `state.recurring` whose `next_due_date` falls in month `m`, add (income) or subtract (expense) `amount` on `account_id`. Advance the recurring date by its `frequency` cadence (`advance_date` from `app.services.date_utils`, the same helper Forecast already uses).
3. **Apply scenario overlays**: from `scenario.params_json`, generate scenario-specific cashflows for month `m`:
   * `trip`: lump-sum expense on `start_date`.
   * `purchase`: down_payment on `target_date`, then amortized monthly expenses.
   * `retirement`: monthly contribution (with curve overrides), plus compound interest on `contribution_account_id` at `(annual_return_pct/100)/12`.
   * `custom`: replay the `events` array.
4. **Carry forward**: post-month balance becomes the seed for month m+1.
5. **Emit a series point** per account per month, plus alerts whenever a balance crosses zero downward.

### Optional, linear regression overlay

Knob: `options.smooth_with_regression = True`. The analytic engine then computes a least-squares fit on `state.history` (last 12 months of per-account net cashflow) and adds the projected month-over-month delta as a synthetic "drift" line in each month's calc. Surface in the chart as a dotted line ("trend-adjusted projection") next to the deterministic line.

Implementation lives in `app/services/scenario_engine/regression.py`, called by the analytic engine when the knob is set. Numpy is already a transitive dep, no new package needed. (If it is not, fall back to a hand-rolled OLS for two-coefficient fit; the math is six lines.)

### Why this is "the baseline"

It uses only deterministic math (compound interest, fixed recurring patterns, amortization formula). No model calls. No external services. Reproducible. Fast (sub-100ms for 5 accounts x 60 months in benchmark estimates). Works offline, works on a free org plan, works when Team E's AI tier is disabled.

## Simulation engine, AI path (layered)

The AI path is **additive**, not a replacement. It wraps the analytic engine via a pipeline:

```
AIEngine.simulate(req):
    adjusted_req = ai_tier.adjust_assumptions(req)   # may bump recurring amounts, seasonalize, smooth anomalies
    base_result  = AnalyticEngine().simulate(adjusted_req)
    annotated    = ai_tier.annotate(base_result, req.state)   # may add Suggestions, refine verdict prose
    return annotated.with_engine_name("analytic_v1+ai_assumptions_v1")
```

The contract `ai_tier.adjust_assumptions` and `ai_tier.annotate` are the seams to Team E's AI Tier abstraction. The AI Tier owns model routing, billing, prompt versioning. Plans is a consumer.

If the AI Tier is disabled (org plan does not include AI, env flag off, AI Tier service unavailable):

* `AIEngine.simulate` falls back to `AnalyticEngine.simulate` directly, returns the deterministic result.
* The UI reflects this with a small badge ("Analytic projection" vs "AI-enhanced projection"). No silent degradation.

Output shape is **identical** in both paths. UI is engine-agnostic. The only thing AI changes is the numbers and the quality of suggestions.

## Output shape (`projection_json`)

```json
{
  "engine_name": "analytic_v1",
  "computed_at": "2026-05-22T09:15:00Z",
  "horizon_months": 24,
  "currency": "EUR",
  "per_account_series": [
    {
      "account_id": 12,
      "account_name": "Main checking",
      "currency": "EUR",
      "points": [
        { "month": "2026-06", "projected_balance": "4200.00" },
        { "month": "2026-07", "projected_balance": "4350.00" },
        ...
      ]
    }
  ],
  "alerts": [
    {
      "account_id": 12,
      "month": "2026-09",
      "projected_balance": "-120.00",
      "trigger": "trip_lump_sum",
      "severity": "warn"
    }
  ],
  "verdict": {
    "color": "yellow",
    "headline": "Trip is feasible but cuts close",
    "reason": "Main checking dips to -120 EUR in Sep 2026 after trip lump-sum. Recovers by Oct."
  },
  "suggestions": [
    { "action": "shift_start_date", "by_days": 30, "expected_outcome": "Main checking stays above 200 EUR throughout trip month." },
    { "action": "reduce_daily_budget", "by_amount": "10.00", "expected_outcome": "Removes dip entirely." }
  ]
}
```

### Affordability verdict thresholds (analytic baseline)

* **Green**: no account dips below zero across the horizon. No alert with severity = critical.
* **Yellow**: any account dips below zero by less than 10% of its starting balance, or stays negative for < 30 days. Recoverable.
* **Red**: any account dips below zero by 10%+ OR stays negative for 30+ days OR the retirement scenario falls short of `target_balance` by > 10%.

Thresholds live as constants in `app/services/scenario_engine/verdict.py`. The AI path can override with model-judged reasoning.

## API

```
GET    /api/v1/scenarios                       list current user's scenarios
POST   /api/v1/scenarios                       create
GET    /api/v1/scenarios/{id}                  read
PATCH  /api/v1/scenarios/{id}                  update (name, params, horizon_months, is_active)
DELETE /api/v1/scenarios/{id}                  soft-delete (is_active=false)
POST   /api/v1/scenarios/{id}/simulate         run engine, return + cache projection
POST   /api/v1/scenarios/compare               body: {scenario_ids: [int]} returns side-by-side projections
```

### `POST /scenarios/{id}/simulate`

* Body: `{ "engine": "analytic" | "ai_enhanced", "options": {...} }`. Defaults to `analytic`.
* Sync 200 response carrying the full `SimulationResult` shape above. Writes the result to `scenarios.projection_json`.
* When `engine = "ai_enhanced"` AND the org has AI Tier enabled AND `Accept: text/event-stream`, the endpoint streams SSE events: `assumption_step`, `partial_series`, `final_result`. Otherwise, falls back to sync JSON even for the AI path. v1 only requires sync JSON; SSE is the reservation, not the v1 deliverable.
* **No background job queue.** Analytic baseline is fast enough to return synchronously. AI path will be slower but still sub-10s in expected cases, so SSE is the right tool over a job/poll loop.

### `POST /scenarios/compare`

* Body: `{ "scenario_ids": [4, 7, 9] }`. Max 3 ids enforced.
* Returns: `{ "scenarios": [ {scenario_id, projection: SimulationResult} ] }`. Re-uses cached `projection_json` when available; runs the analytic engine when not.

## Sandboxing guarantee

The scenario engine **reads** from `accounts`, `recurring_transactions`, `transactions` (history only). It **writes** only to `scenarios.projection_json`. No other table is mutated. Specifically NOT:

* Not `transactions` (no synthetic rows created).
* Not `accounts` (no balance mutations).
* Not `budgets`, not `forecast_plans` (the budget editor).
* Not `recurring_transactions` (the user's real recurring templates are untouched).

Backend enforcement is contract-level (the engine has no `db.add` calls outside the `Scenario` model). Test guards in PR 1 assert "running simulate produces zero deltas in the transactions table." See Tests.

"Apply this scenario as real transactions" is **out of scope for v1**. The button can come later as a separate spec; the design avoids closing the door on it (the engine output already enumerates discrete cashflow events, which is what an apply step would consume).

## Comparison view

Once the user has 2 or 3 scenarios:

* Pick 2 or 3 from the list.
* `POST /scenarios/compare` returns each projection.
* UI renders overlaid lines per account (one color per scenario) plus a verdict-comparison row ("Scenario A: green, Scenario B: red because retirement gap of 80k EUR").
* Hard max 3 scenarios at once. Beyond that the chart becomes illegible.

Comparison is **PR 3 or later**. PR 1 ships the single-scenario simulator only.

## UX shape

```
+------------------------------------------------------------------------------+
|  PLANS                                                                       |
|  Plan one-off life events. Nothing here touches your real transactions.      |
+------------------------------------------------------------------------------+

  [+ New plan]   Templates:  [Trip]  [Purchase]  [Retirement]  [Custom]

  YOUR PLANS
  +------------------------------------+
  | Lisbon trip (Sep 2026)             |  Verdict: YELLOW  | Horizon 24mo |
  | Replacement car (Mar 2027)         |  Verdict: GREEN   | Horizon 36mo |
  | Retirement at 62                   |  Verdict: RED     | Horizon 240mo|
  +------------------------------------+
  | [Compare 2-3 plans]                                                     |
  +------------------------------------+
```

Click into a plan:

```
+----------------------------------+------------------------------------------+
|  PARAMS                          |  PROJECTION (24 months)                  |
|                                  |                                          |
|  Name [Lisbon trip       ]       |   chart: balance line per account       |
|  Destination [Lisbon     ]       |   alert markers at dip points           |
|  Start date  [2026-09-15 ]       |                                          |
|  Duration    [10 days    ]       |                                          |
|  Daily budget [70.00 EUR ]       |   Verdict: YELLOW                        |
|  Transport   [450.00 EUR ]       |   "Cuts close in Sep 2026."             |
|  Account     [Main check ]       |                                          |
|                                  |   Suggestions:                           |
|  Engine: [Analytic v]            |   - Shift start by 30 days -> green     |
|  [Re-simulate]                   |   - Reduce daily budget by 10 -> green  |
+----------------------------------+------------------------------------------+
```

Re-simulate is debounced (400ms) on every params change. Backed by the same `POST /simulate` endpoint.

## Overlap, Forecast vs Plans

Address explicitly because the two surfaces will look superficially similar.

| Question | Forecast | Plans |
|---|---|---|
| **What does it answer?** | "Where is THIS billing period heading if nothing changes?" | "What if I do X over the next N months?" |
| **Time horizon** | One billing period | 1 to 120 months (trip / purchase / custom), 1 to 480 months (retirement) |
| **User intervention** | Passive, no intervention possible | Active, scenario params are the intervention |
| **Persistence** | Stateless aggregate query | Named, persistent `scenarios` rows |
| **Mutates state?** | No | No (sandboxed) |
| **Reuses what?** | n/a | Reuses Forecast's recurring-projection logic as a subroutine. Reuses the Recharts chart components from `AccountMonthEndForecast.tsx`. |

The analytic engine **calls into** `account_balance_forecast_service` for the "month 0 starting balance" step and **reuses** `advance_date` for recurring cadence. Same math, longer horizon, with scenario overlays. No duplicate code.

## Overlap, AI Tier vs Plans

Plans is a **consumer** of the AI Tier abstraction Team E is designing, not a sibling product. Concretely:

* AI Tier owns: model routing, prompt management, per-org AI budget, AI feature flags, the `ai_tier.*` SDK.
* Plans owns: scenario CRUD, the analytic engine, projection cache, UI.
* Plans calls AI Tier through a narrow interface (`adjust_assumptions`, `annotate`) and tolerates AI being unavailable. When AI is OFF, Plans is fully functional with the analytic engine.

When Team E ships the SDK:

* Plans adds `AIEngine` as a wrapper around `AnalyticEngine`.
* The UI gets a small "Engine: Analytic / AI-enhanced" picker, gated on `org.has_ai_tier`.
* No new endpoint, no schema change. The `engine` query param on `POST /simulate` already accommodates it.

## Navigation

Plans is a new top-level frame-menu item, sibling of Forecast Plans (which stays at its current `/forecast-plans` path). The two surfaces share components (the Recharts forecast renderer used by `AccountMonthEndForecast.tsx`) but live at separate routes and serve different intents (per-period budget editor vs multi-month sandbox). Reports and AI Tier are owned by their own specs and not addressed here.

## Phased rollout

| PR | Scope | LOC estimate |
|---|---|---|
| **PR 1** (this spec → land) | Schema migration, `Scenario` model + Pydantic, scenarios CRUD router, analytic engine baseline, `trip` and `purchase` templates, single-scenario simulator UI (params left + chart right), sandboxing tests, frame-menu integration. NO retirement, NO comparison, NO AI. | ~1800 backend, ~1400 frontend |
| **PR 2** | Add `retirement` template (compound interest, contribution curve), regression-overlay knob (`smooth_with_regression`), affordability-verdict refinement, suggestions for purchase scenarios. | ~600 backend, ~400 frontend |
| **PR 3** | Comparison view (`POST /compare`, side-by-side UI, max 3 scenarios). | ~400 backend, ~700 frontend |
| **PR 4** | AI engine wrapper, gated on Team E's SDK landing. SSE upgrade on `POST /simulate`. Engine-picker UI. | ~500 backend, ~300 frontend |

Plans MVP is PRs 1 through 4. Total v1-through-MVP (PR 1+2+3): roughly 3300 backend / 2500 frontend LOC. AI path adds 800 LOC.

## Tests

### Backend (PR 1)

* `tests/services/scenario_engine/test_analytic.py`
  * Trip lump-sum lands on `start_date` against `source_account_id`.
  * Purchase lump-sum lands on `target_date`; financing amortization is correct (compare to hand-computed numbers for a known P/r/n).
  * Recurring expenses are picked up through the horizon, advancing by frequency.
  * Verdict thresholds (green / yellow / red) trigger on synthetic input.
  * Sandboxing guard: running `simulate` produces **zero** new rows in `transactions`, `recurring_transactions`, `accounts`, `budgets`, `forecast_plans`. Snapshot row counts before/after.
* `tests/routers/test_scenarios.py`
  * CRUD round-trip with Pydantic discriminator validating params per scenario_type.
  * `POST /simulate` writes `projection_json` and `projection_computed_at`.
  * Other users in the same org cannot read/edit this user's scenarios (per-user scoping).
  * Other orgs entirely cannot read this scenario.
  * Horizon-cap validator: `trip` / `purchase` / `custom` with `horizon_months > 120` → 422; `retirement` with `horizon_months > 480` → 422; `retirement` with `horizon_months = 480` → 200.
* `tests/services/scenario_engine/test_regression.py` (PR 2)
  * OLS fit on a known 12-point linear series returns the expected slope within 1e-6.

### Frontend (PR 1)

* `frontend/tests/app/plans-page.test.tsx`
  * Lists current user's scenarios; empty state when none.
  * "New plan" with template = Trip opens param form with the right fields.
  * Param edit triggers debounced re-simulate (mock the API; assert 1 call after 400ms idle).
  * Chart renders one line per account; alert markers appear when API returns alerts.
  * Verdict badge color matches API verdict color.

## Open questions for architect

1. **Per-user vs per-org default visibility** — spec says private-to-creator. Confirm.
2. **`apply this scenario` button** — left as future scope. Confirm out-of-scope for v1.

(Resolved 2026-05-22: naming locked to `scenarios` + "Plans"; navigation is a new top-level frame-menu item with no Planning umbrella and no Forecast repath; horizon ceiling split 120 / 480 by `scenario_type`.)

## Out of scope (explicit)

* Apply-scenario-as-real-transactions button (future PR, separate spec).
* Sharing scenarios across users in an org (future, `visibility` column).
* Multi-currency arithmetic across accounts (assume each scenario operates in one currency for v1; cross-currency stays in the scenario's source account currency).
* Goal tracking ("I am 64% of the way to my retirement target") as a persistent widget on Dashboard — that is a Reports concern.
* Probabilistic / Monte-Carlo simulation. Analytic baseline is deterministic; AI path will add judgment but not stochastic sampling.
* Tax modeling. Retirement projections ignore tax. Spec calls this out in tooltip on the retirement template.
* Importing recurring templates as scenario events (would be useful for `custom`, but YAGNI for v1; users hand-author events).

## Naming + cross-references

* Backend: `backend/app/models/scenario.py`, `backend/app/schemas/scenario.py`, `backend/app/routers/scenarios.py`, `backend/app/services/scenario_engine/{base.py,analytic.py,regression.py,verdict.py,ai.py}`, `backend/app/services/scenario_service.py`.
* Frontend: `frontend/app/plans/page.tsx`, `frontend/app/plans/[id]/page.tsx`, `frontend/components/plans/PlansList.tsx`, `frontend/components/plans/PlanEditor.tsx`, `frontend/components/plans/ProjectionChart.tsx` (reusing Recharts setup from `AccountMonthEndForecast.tsx`), `frontend/components/plans/templates/{TripParams,PurchaseParams,RetirementParams,CustomParams}.tsx`.
* `[[2026-05-17-forecast-budget-toggles]]` — adjacent surface for the Forecast vs Budgets carve; this spec carves Plans on the same axis.
* `[[forecast-budget-enable-disable]]` — same neighborhood.
* `[[reference_do_spec_sync.md]]` — no new env vars introduced; no `.do/app.yaml` change required for PR 1. PR 4 (AI path) will introduce AI-Tier credentials, owned by Team E's spec.
