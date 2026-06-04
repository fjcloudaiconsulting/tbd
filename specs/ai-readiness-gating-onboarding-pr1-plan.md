# AI Readiness — PR1 (provider-aware gating) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the three AI surfaces render by true state — hidden (not entitled) / "Set up AI" CTA (entitled, no provider) / live action (entitled + configured) — backed by one authenticated AI-status source, and hard-block the routes with 412 when no provider is configured.

**Architecture:** A new authenticated `GET /api/v1/ai/status` returns per-feature `{entitled, configured}`, computed from `feature_service.get_features` + `ai_routing_service.get_routing_for_feature` via a single canonical entitlement↔routing↔UI mapping. The frontend reads it through a `useAiStatus()` SWR hook; a shared `<SetUpAiCta>` renders the role-aware not-configured state on each surface. Forecast + budget routes gain a 412 precondition (categorize already has it).

**Tech Stack:** FastAPI + SQLAlchemy async + Pydantic v2 (backend); Next.js 15 + React 19 + SWR + TS (frontend). Tests: pytest / vitest+RTL.

**Spec:** `specs/ai-readiness-gating-onboarding.md` (note: this plan corrects the spec's `/auth/status` location to a dedicated authenticated `/api/v1/ai/status` — `/auth/status` is public/pre-auth and has no org context).

**Branch:** `feat/ai-readiness-gating-onboarding` (already exists, rebased on `main`; the Spec B + backlog docs are already committed here).

**Test command reminder:** backend `docker compose exec backend pytest <path> -v`; frontend `docker compose exec frontend npm test -- <path>`; types `docker compose exec frontend npx tsc --noEmit`. Default compose project (sequential). Keep tests minimal (operator is compute-capped).

---

## File structure

Backend:
- `backend/app/services/ai_feature_map.py` — **new**: canonical `AI_FEATURE_MAP` triple (entitlement key, routing name, UI id) + `entitlement_to_routing` / iteration helper. Single source of truth for the name mismatch.
- `backend/app/services/ai_status_service.py` — **new**: `get_ai_feature_status(db, *, org_id) -> dict[str, dict[str,bool]]`.
- `backend/app/routers/ai_status.py` — **new**: authenticated `GET /api/v1/ai/status`.
- `backend/app/schemas/ai_status.py` — **new**: `AIFeatureState`, `AIStatusResponse`.
- `backend/app/main.py` — register the new router.
- `backend/app/routers/ai_forecast.py`, `backend/app/routers/ai_budget.py` — add the 412 no-provider precondition.

Frontend:
- `frontend/lib/types.ts` — `AIFeatureState`, `AIStatus`.
- `frontend/lib/hooks/use-ai-status.ts` — **new**: `useAiStatus()` SWR hook.
- `frontend/components/ai/SetUpAiCta.tsx` — **new**: shared role-aware "Set up AI" CTA.
- `frontend/app/budgets/page.tsx`, `frontend/app/transactions/page.tsx`, `frontend/components/dashboard/AIForecastRefineToggle.tsx` — 3-state wiring.

---

## Task 1: canonical feature mapping + drift guard

**Files:**
- Create: `backend/app/services/ai_feature_map.py`
- Test: `backend/tests/services/test_ai_feature_map.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_ai_feature_map.py
from app.services.ai_feature_map import AI_FEATURE_MAP, ui_to_routing
from app.auth.feature_catalog import FeatureKey  # Literal of entitlement keys
from app.models.org_ai_routing import ROUTABLE_FEATURE_NAMES
import typing


def test_map_keys_align_with_catalog_and_routing():
    catalog_keys = set(typing.get_args(FeatureKey))
    for ent_key, routing_name, ui_id in AI_FEATURE_MAP:
        assert ent_key in catalog_keys, f"{ent_key} missing from feature catalog"
        assert routing_name in ROUTABLE_FEATURE_NAMES, f"{routing_name} not routable"


def test_ui_to_routing_resolves_and_rejects_unknown():
    assert ui_to_routing("forecast") == "smart_forecast"
    import pytest
    with pytest.raises(KeyError):
        ui_to_routing("nope")
```

- [ ] **Step 2: Run, expect FAIL**

Run: `docker compose exec backend pytest tests/services/test_ai_feature_map.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/services/ai_feature_map.py
"""Canonical mapping for the 3 user-facing AI features.

The entitlement key (feature catalog), the routing feature name (dispatch),
and the UI id all differ. This is the ONE place that triple lives; any drift
is a bug (guarded by tests/services/test_ai_feature_map.py).
"""
from __future__ import annotations

# (entitlement_key, routing_name, ui_id)
AI_FEATURE_MAP: tuple[tuple[str, str, str], ...] = (
    ("ai.autocategorize", "categorize_transactions", "categorize"),
    ("ai.forecast", "smart_forecast", "forecast"),
    ("ai.budget", "smart_budget", "budget"),
)


def ui_to_routing(ui_id: str) -> str:
    for _ent, routing, ui in AI_FEATURE_MAP:
        if ui == ui_id:
            return routing
    raise KeyError(ui_id)
```

(Confirm `FeatureKey` is importable from `app.auth.feature_catalog` and is a
`typing.Literal`; if its location differs, import the actual key set the catalog
exposes. `ROUTABLE_FEATURE_NAMES` is in `app/models/org_ai_routing.py`.)

- [ ] **Step 4: Run, expect PASS**

Run: `docker compose exec backend pytest tests/services/test_ai_feature_map.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_feature_map.py backend/tests/services/test_ai_feature_map.py
git commit -m "feat(ai): canonical AI feature mapping + drift guard"
```

---

## Task 2: AI status service + authenticated endpoint

**Files:**
- Create: `backend/app/services/ai_status_service.py`, `backend/app/schemas/ai_status.py`, `backend/app/routers/ai_status.py`
- Modify: `backend/app/main.py` (register router)
- Test: `backend/tests/routers/test_ai_status.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/routers/test_ai_status.py
import pytest
from app.services import ai_status_service


@pytest.mark.asyncio
async def test_get_ai_feature_status_shape(monkeypatch):
    async def fake_features(db, org_id):
        return {"ai.autocategorize": True, "ai.forecast": True, "ai.budget": False}

    async def fake_routing(db, *, org_id, feature_name):
        return (1, "claude-x") if feature_name == "smart_forecast" else None

    monkeypatch.setattr(ai_status_service.feature_service, "get_features", fake_features)
    monkeypatch.setattr(
        ai_status_service.ai_routing_service, "get_routing_for_feature", fake_routing
    )
    out = await ai_status_service.get_ai_feature_status(None, org_id=1)
    assert out["categorize"] == {"entitled": True, "configured": False}
    assert out["forecast"] == {"entitled": True, "configured": True}
    assert out["budget"] == {"entitled": False, "configured": False}
```

- [ ] **Step 2: Run, expect FAIL**

Run: `docker compose exec backend pytest tests/routers/test_ai_status.py -v`
Expected: FAIL — `ai_status_service` missing.

- [ ] **Step 3: Implement the service**

```python
# backend/app/services/ai_status_service.py
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_routing_service, feature_service
from app.services.ai_feature_map import AI_FEATURE_MAP


async def get_ai_feature_status(
    db: AsyncSession, *, org_id: int
) -> dict[str, dict[str, bool]]:
    """Per-feature {entitled, configured} keyed by UI id. `configured` is only
    evaluated when entitled, so an un-entitled org costs zero routing lookups.
    """
    features = await feature_service.get_features(db, org_id)
    out: dict[str, dict[str, bool]] = {}
    for ent_key, routing_name, ui_id in AI_FEATURE_MAP:
        entitled = bool(features.get(ent_key, False))
        configured = False
        if entitled:
            routing = await ai_routing_service.get_routing_for_feature(
                db, org_id=org_id, feature_name=routing_name
            )
            configured = routing is not None
        out[ui_id] = {"entitled": entitled, "configured": configured}
    return out
```

(Verify `feature_service.get_features(db, org_id)` and
`ai_routing_service.get_routing_for_feature(db, *, org_id, feature_name)`
signatures match; adjust the call if positional/kw differs.)

- [ ] **Step 4: Implement schema + endpoint + register**

```python
# backend/app/schemas/ai_status.py
from pydantic import BaseModel, StrictBool


class AIFeatureState(BaseModel):
    entitled: StrictBool
    configured: StrictBool


class AIStatusResponse(BaseModel):
    categorize: AIFeatureState
    forecast: AIFeatureState
    budget: AIFeatureState
```

```python
# backend/app/routers/ai_status.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.ai_status import AIStatusResponse
from app.services.ai_status_service import get_ai_feature_status

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


@router.get("/status", response_model=AIStatusResponse)
async def ai_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_ai_feature_status(db, org_id=current_user.org_id)
```

In `backend/app/main.py`, import and `app.include_router(ai_status.router)`
alongside the other AI routers (match the existing registration style).

- [ ] **Step 5: Run, expect PASS**

Run: `docker compose exec backend pytest tests/routers/test_ai_status.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/ai_status_service.py backend/app/schemas/ai_status.py backend/app/routers/ai_status.py backend/app/main.py backend/tests/routers/test_ai_status.py
git commit -m "feat(ai): authenticated GET /api/v1/ai/status (entitled+configured per feature)"
```

---

## Task 3: 412 no-provider precondition on forecast + budget routes

**Files:**
- Modify: `backend/app/routers/ai_forecast.py` (the `/refine` handler), `backend/app/routers/ai_budget.py` (the `/rebalance` handler)
- Test: extend `backend/tests/routers/test_ai_forecast_refine.py`

Behavior: before invoking the service, if no routing is configured for the
feature, raise `HTTPException(412, {"code": "ai_provider_not_configured", ...})`.
Other (runtime) fallbacks in the service are untouched.

- [ ] **Step 1: Write the failing test** (forecast; mirror existing fixtures)

```python
# add to backend/tests/routers/test_ai_forecast_refine.py
@pytest.mark.asyncio
async def test_refine_returns_412_when_no_provider_configured(session_factory):
    seed = await _seed_org_with_data(session_factory, enable_ai_forecast=True)

    async def resolver(_f):
        return await _get_user(session_factory, seed["owner_id"])

    app = _make_app(session_factory, resolver)
    client = TestClient(app)
    # No routing configured for this org -> precondition fails.
    resp = client.post("/api/v1/ai/forecast/refine",
                       json={"timeframe_months": 6, "scope": "top_20"})
    assert resp.status_code == 412
    assert resp.json()["detail"]["code"] == "ai_provider_not_configured"
```

(If the existing seed already configures routing, ensure this test path has
NONE. The existing `no routing -> baseline` test will need updating to the new
412 contract — update it to assert 412 instead of the baseline fallback.)

- [ ] **Step 2: Run, expect FAIL** (currently returns 200 baseline)

Run: `docker compose exec backend pytest tests/routers/test_ai_forecast_refine.py -k "412" -v`

- [ ] **Step 3: Implement** — in `ai_forecast.py` `refine_forecast_endpoint`, before the `refine_forecast(...)` call:

```python
from app.services import ai_routing_service
from app.services.ai_forecast_refine_service import ROUTING_KEY  # "smart_forecast"

routing = await ai_routing_service.get_routing_for_feature(
    db, org_id=current_user.org_id, feature_name=ROUTING_KEY
)
if routing is None:
    raise HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail={"code": "ai_provider_not_configured",
                "message": "Configure an AI provider to use this feature."},
    )
```

Apply the same precondition in `ai_budget.py`'s rebalance handler using its
routing key (`"smart_budget"`). (Use each router's existing routing-key constant
if present; otherwise the literal.)

Also update the pre-existing forecast "no routing -> baseline" router test to
expect 412 (the contract changed by design).

- [ ] **Step 4: Run, expect PASS**

Run: `docker compose exec backend pytest tests/ -k "ai_forecast or ai_budget" -q`

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_forecast.py backend/app/routers/ai_budget.py backend/tests/routers/test_ai_forecast_refine.py
git commit -m "feat(ai): 412 ai_provider_not_configured on forecast+budget when no provider routed"
```

---

## Task 4: frontend `useAiStatus` hook + shared `SetUpAiCta`

**Files:**
- Modify: `frontend/lib/types.ts`
- Create: `frontend/lib/hooks/use-ai-status.ts`, `frontend/components/ai/SetUpAiCta.tsx`
- Test: `frontend/tests/components/SetUpAiCta.test.tsx`

- [ ] **Step 1: Types**

```typescript
// frontend/lib/types.ts
export interface AIFeatureState { entitled: boolean; configured: boolean }
export interface AIStatus { categorize: AIFeatureState; forecast: AIFeatureState; budget: AIFeatureState }
```

- [ ] **Step 2: Hook** (match the app's existing SWR hook style under `frontend/lib/hooks/`; if none exist, this is the pattern)

```typescript
// frontend/lib/hooks/use-ai-status.ts
import useSWR from "swr";
import { apiFetch } from "@/lib/api";
import type { AIStatus } from "@/lib/types";

export function useAiStatus() {
  // SWR dedupes so all surfaces share one request. AI is opt-in/non-critical,
  // so failures resolve to undefined (surfaces hide rather than error).
  const { data } = useSWR<AIStatus>("/api/v1/ai/status", (url) => apiFetch<AIStatus>(url), {
    shouldRetryOnError: false,
    revalidateOnFocus: false,
  });
  return data;
}
```

- [ ] **Step 3: Write the failing test for SetUpAiCta**

```tsx
// frontend/tests/components/SetUpAiCta.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { SetUpAiCta } from "@/components/ai/SetUpAiCta";

describe("SetUpAiCta", () => {
  it("admin sees a link to settings", () => {
    render(<SetUpAiCta role="owner" />);
    const link = screen.getByRole("link", { name: /set up ai/i });
    expect(link).toHaveAttribute("href", "/settings/ai-providers");
  });
  it("member sees an ask-admin message, no link", () => {
    render(<SetUpAiCta role="member" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/ask your.*admin/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 4: Run, expect FAIL**

Run: `docker compose exec frontend npm test -- tests/components/SetUpAiCta.test.tsx`

- [ ] **Step 5: Implement the component**

```tsx
// frontend/components/ai/SetUpAiCta.tsx
import Link from "next/link";

const ADMIN_ROLES = new Set(["owner", "admin"]);

export function SetUpAiCta({ role, className }: { role: string | null; className?: string }) {
  if (role && ADMIN_ROLES.has(role)) {
    return (
      <Link href="/settings/ai-providers" className={className}>
        Set up AI
      </Link>
    );
  }
  return (
    <span className={className} aria-disabled="true">
      Set up AI (ask your organization admin)
    </span>
  );
}
```

(Match the existing button/link Tailwind classes used by the surfaces; the
caller passes `className`. Keep copy em-dash-free.)

- [ ] **Step 6: Run, expect PASS** + commit

Run: `docker compose exec frontend npm test -- tests/components/SetUpAiCta.test.tsx`
```bash
git add frontend/lib/types.ts frontend/lib/hooks/use-ai-status.ts frontend/components/ai/SetUpAiCta.tsx frontend/tests/components/SetUpAiCta.test.tsx
git commit -m "feat(ai): useAiStatus hook + shared SetUpAiCta (role-aware)"
```

---

## Task 5: budget rebalance surface — 3-state

**Files:**
- Modify: `frontend/app/budgets/page.tsx` (the Suggest-rebalance button block, ~lines 229-237)
- Test: `frontend/tests/app/budgets-ai-gate.test.tsx` (one focused test)

Replace the ungated button with the 3-state. Read `useAiStatus()` + `useAuth().user?.role`.

- [ ] **Step 1: Implement** — replace the existing block:

```tsx
// near other hooks in budgets/page.tsx:
const ai = useAiStatus();
const role = /* from useAuth() */ null;
const budgetAi = ai?.budget;

// in the toolbar, replacing the old `{isCurrentPeriod && budgets.length > 0 && (<button…/>)}`:
{isCurrentPeriod && budgets.length > 0 && budgetAi?.entitled && (
  budgetAi.configured ? (
    <button onClick={() => setRebalanceOpen(true)} className="<existing classes>"
            data-testid="suggest-rebalance-btn">
      Suggest rebalance
    </button>
  ) : (
    <SetUpAiCta role={role} className="<existing classes>" />
  )
)}
```

(Get `role` from the auth context — find how `budgets/page.tsx` or a sibling
reads the current user; reuse that. Keep the existing button classes verbatim.)

- [ ] **Step 2: Failing test → implement → pass** (one test, mocking the hook)

```tsx
// frontend/tests/app/budgets-ai-gate.test.tsx — mock useAiStatus + auth, render the toolbar
// Assert: not-entitled -> no suggest-rebalance-btn and no Set up AI;
//         entitled+!configured -> "Set up AI" shown, no suggest-rebalance-btn;
//         entitled+configured -> suggest-rebalance-btn shown.
```

Mock `@/lib/hooks/use-ai-status` and the auth hook. Mirror the mocking style of
existing `frontend/tests/app/budgets-*.test.tsx`.

- [ ] **Step 3: tsc + commit**

```bash
docker compose exec frontend npx tsc --noEmit
git add frontend/app/budgets/page.tsx frontend/tests/app/budgets-ai-gate.test.tsx
git commit -m "feat(budgets): provider-aware 3-state gating for Suggest rebalance"
```

---

## Task 6: transactions categorize surface — 3-state + delete the bespoke probe

**Files:**
- Modify: `frontend/app/transactions/page.tsx` (the `ai.autocategorize` probe ~254-274; the `SuggestCategoryButton` render sites ~1470, ~1771)

- [ ] **Step 1: Implement** — delete the `/api/v1/subscriptions` probe + its state; derive visibility from `useAiStatus()`:

```tsx
const ai = useAiStatus();
const categorizeAi = ai?.categorize;
// remove: the useEffect that fetched /api/v1/subscriptions and set the
// `ai.autocategorize` boolean, plus that piece of state.
```

At each `SuggestCategoryButton` site, branch:
```tsx
{categorizeAi?.entitled && (
  categorizeAi.configured
    ? <SuggestCategoryButton ... />
    : <SetUpAiCta role={role} className="<existing inline classes>" />
)}
```

(Keep the button's existing props/behavior; only the wrapper changes. Reuse the
same `role` source as Task 5.)

- [ ] **Step 2: Run the existing transactions tests** (ensure the probe removal didn't break them) → fix mocks if they referenced `/subscriptions`:

Run: `docker compose exec frontend npm test -- tests/app/transactions-page.test.tsx`

- [ ] **Step 3: tsc + commit**

```bash
docker compose exec frontend npx tsc --noEmit
git add frontend/app/transactions/page.tsx
git commit -m "feat(transactions): provider-aware gating for Suggest category; drop bespoke subscriptions probe"
```

---

## Task 7: forecast refine surface — 3-state

**Files:**
- Modify: `frontend/components/dashboard/AIForecastRefineToggle.tsx`

Today the toggle hides on a 403 from the estimate call. Add the up-front state
read so an entitled-but-unconfigured org sees the CTA instead of the live toggle.

- [ ] **Step 1: Implement** — at the top of the component:

```tsx
const ai = useAiStatus();
const forecastAi = ai?.forecast;
const role = /* same role source */ null;

// after the existing `if (!visible || gateBlocked) return null;`:
if (forecastAi && !forecastAi.entitled) return null;
if (forecastAi && forecastAi.entitled && !forecastAi.configured) {
  return <SetUpAiCta role={role} className="<existing toggle button classes>" />;
}
// else: existing toggle/panel flow (entitled + configured, or status still loading)
```

(Keep the existing estimate/confirm/refine flow for the configured case. The
existing 403 self-hide stays as a backstop.)

- [ ] **Step 2: Run the toggle test** (update mocks if needed so `useAiStatus` returns configured for the happy-path tests):

Run: `docker compose exec frontend npm test -- tests/components/dashboard/ai-forecast-refine-toggle.test.tsx`

- [ ] **Step 3: tsc + commit**

```bash
docker compose exec frontend npx tsc --noEmit
git add frontend/components/dashboard/AIForecastRefineToggle.tsx frontend/tests/components/dashboard/ai-forecast-refine-toggle.test.tsx
git commit -m "feat(dashboard): provider-aware 3-state for AI forecast refine"
```

---

## Task 8: full-suite verification + PR

- [ ] **Step 1:** `docker compose exec backend pytest tests/ -q` (note the 4 pre-existing auth/session-grace failures that also fail on main; everything else green).
- [ ] **Step 2:** `docker compose exec frontend npm test` then `docker compose exec frontend npx tsc --noEmit` — green/clean.
- [ ] **Step 3: Manual smoke (local, has the Anthropic credential):** with a provider configured, all 3 surfaces show the live action. Temporarily remove the default routing (or test an org without it) → surfaces show "Set up AI"; a direct `POST /ai/forecast/refine` returns 412 `ai_provider_not_configured`.
- [ ] **Step 4: Open PR** titled `feat(ai): provider-aware gating + "Set up AI" CTA across AI surfaces`. Concise body, no test-plan section.

---

## Self-review (coverage vs spec)

- `ai` status source of truth → Tasks 1-2 (corrected to authenticated `/api/v1/ai/status`). ✓
- Canonical mapping + drift guard → Task 1. ✓
- 3-state on all 3 surfaces + role-aware CTA → Tasks 4-7. ✓
- Budget-rebalance gate fix → Task 5. ✓
- Delete bespoke `/subscriptions` probe → Task 6. ✓
- 412 enforcement (forecast+budget; categorize already) → Task 3. ✓
- No migrations; tests minimal. ✓
- Deferred to PR2: help tooltips, `/docs` section, provider doc links, label rename. (Not in this plan.)
