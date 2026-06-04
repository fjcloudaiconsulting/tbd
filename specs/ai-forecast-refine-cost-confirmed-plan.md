# Smart Forecast Refinement: Cost-Confirmed Flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two prod bugs in "Apply AI refinement" (10s client abort, 1024-token truncation) and turn it into a configurable, cost-confirmed flow (timeframe + category-scope knobs, a no-LLM cost estimate the user confirms before any tokens are spent).

**Architecture:** A single shared prompt builder feeds both a new no-LLM `/estimate` preflight and the real refine call, so the quoted cost can't drift from what runs. `max_tokens` is sized from the same estimate, eliminating truncation. Frontend gives `/ai/*` a 90s budget; backend per-call httpx timeout rises to 60s. Synchronous; async execution is backlogged.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Pydantic v2 (backend), Next.js 15 + React 19 + TypeScript + SWR (frontend). Tests: pytest (backend), vitest/RTL (frontend).

**Spec:** `specs/ai-forecast-refine-cost-confirmed.md`

**Branch:** create `fix/ai-forecast-refine-cost-confirmed` off `main` before Task 1.

**Test command reminder:** backend tests run in the container —
`docker compose exec backend pytest <path> -v`. If dispatched as a parallel
agent, use an isolated project: `docker compose -p team-<name> exec backend pytest …`
(never the default project). Frontend: `docker compose exec frontend npm test -- <path>`.

---

## File structure

Backend:
- `backend/app/services/ai_forecast_refine_token_estimate.py` — **new**, pure helpers: token heuristic + scope selection + max_tokens sizing. Isolated so it's unit-testable without a DB.
- `backend/app/schemas/ai_forecast.py` — add `timeframe_months`/`scope` to the request; add `ForecastRefineEstimate` response; raise `seasonal`/`anomalies` caps.
- `backend/app/services/ai_forecast_refine_service.py` — shared `_build_refine_prompt`, scope filtering, timeframe slicing, dynamic system prompt, `HISTORY_MONTHS`→12, new `estimate_refine()` entry point, thread params + `max_tokens` into `refine_forecast()`.
- `backend/app/routers/ai_forecast.py` — thread params into `/refine`; add `/estimate`; audit detail + structlog.
- `backend/app/services/ai_providers/anthropic.py` — `CHAT_TIMEOUT_S` 30 → 60.

Frontend:
- `frontend/lib/api.ts` — `/api/v1/ai/*` 90s timeout matcher.
- `frontend/lib/types.ts` — `ForecastRefineEstimate`, request param types.
- `frontend/components/dashboard/AIForecastRefinePanel.tsx` — **new**, the configure→estimate→confirm panel.
- `frontend/components/dashboard/AIForecastRefineToggle.tsx` — open the panel; pass confirmed params to the refine call.

---

## Task 1: Token-estimation + scope helpers (pure functions)

**Files:**
- Create: `backend/app/services/ai_forecast_refine_token_estimate.py`
- Test: `backend/tests/services/test_ai_forecast_refine_token_estimate.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_ai_forecast_refine_token_estimate.py
import pytest
from app.services.ai_forecast_refine_token_estimate import (
    Scope,
    select_categories_by_scope,
    estimate_prompt_tokens,
    estimate_output_tokens,
    max_tokens_for_output_estimate,
)


def test_select_top_n_by_spend_keeps_highest_only():
    # spend_by_cat: {category_id: total_spend}
    spend = {1: 100.0, 2: 5000.0, 3: 50.0, 4: 900.0}
    assert select_categories_by_scope(spend, Scope.TOP_10) == [2, 4, 1, 3]
    assert select_categories_by_scope(spend, Scope.ALL) == [2, 4, 1, 3]
    # top_n truncates; with a tiny n via TOP_10 on 4 items we keep all 4
    top2 = select_categories_by_scope({1: 10.0, 2: 20.0, 3: 30.0}, Scope.TOP_10)
    assert top2 == [3, 2, 1]


def test_estimate_output_tokens_grows_with_category_count():
    few = estimate_output_tokens(category_count=5)
    many = estimate_output_tokens(category_count=40)
    assert many > few
    assert few > 0


def test_max_tokens_never_below_floor_and_covers_estimate():
    # floor is 1024 (today's default); sizing adds headroom above the estimate
    assert max_tokens_for_output_estimate(10) >= 1024
    big = estimate_output_tokens(category_count=40)
    assert max_tokens_for_output_estimate(40) > big


def test_estimate_prompt_tokens_is_char_based():
    short = estimate_prompt_tokens("x" * 350)
    assert short == pytest.approx(100, abs=5)  # ~1 token / 3.5 chars
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_refine_token_estimate.py -v`
Expected: FAIL — `ModuleNotFoundError: ai_forecast_refine_token_estimate`.

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/ai_forecast_refine_token_estimate.py
"""Pure (DB-free) helpers for the cost-confirmed forecast-refine flow.

Kept separate from the service so the heuristic + scope selection are
unit-testable without a database or LLM. The SAME functions back both
the /estimate preflight and the real refine call, so the quoted cost
can't drift from what actually runs.
"""
from __future__ import annotations

import enum
import math

# Char-per-token heuristics. The stack has no tokenizer; these are
# deliberately rough and surfaced to the user as approximate ("≈").
_PROMPT_CHARS_PER_TOKEN = 3.5
_OUTPUT_CHARS_PER_TOKEN = 3.0

# Per-row JSON size assumptions for the output shape (SeasonalAdjustment,
# AnomalyFlag) plus fixed overhead (confidence, summary, braces).
_SEASONAL_CHARS_PER_ROW = 220
_ANOMALY_CHARS_PER_ROW = 180
_FIXED_OUTPUT_CHARS = 600
_OUTPUT_SAFETY_MARGIN = 1.10

# max_tokens sizing: never below today's adapter default; add headroom
# above the estimate so the tool-use JSON can't truncate before the
# required `anomalies` key (the prod bug).
_MAX_TOKENS_FLOOR = 1024
_MAX_TOKENS_BUFFER = 400


class Scope(str, enum.Enum):
    TOP_10 = "top_10"
    TOP_20 = "top_20"
    ALL = "all"


def _limit_for_scope(scope: "Scope") -> int | None:
    if scope is Scope.TOP_10:
        return 10
    if scope is Scope.TOP_20:
        return 20
    return None  # ALL


def select_categories_by_scope(
    spend_by_category: dict[int, float], scope: "Scope"
) -> list[int]:
    """Return category ids ordered by spend desc, truncated to the scope.

    Ties broken by category_id asc for determinism.
    """
    ordered = sorted(
        spend_by_category.keys(),
        key=lambda cid: (-spend_by_category[cid], cid),
    )
    limit = _limit_for_scope(scope)
    return ordered if limit is None else ordered[:limit]


def estimate_prompt_tokens(prompt_text: str) -> int:
    return math.ceil(len(prompt_text) / _PROMPT_CHARS_PER_TOKEN)


def estimate_output_tokens(*, category_count: int) -> int:
    anomalies = category_count // 4
    chars = (
        category_count * _SEASONAL_CHARS_PER_ROW
        + anomalies * _ANOMALY_CHARS_PER_ROW
        + _FIXED_OUTPUT_CHARS
    )
    tokens = math.ceil(chars / _OUTPUT_CHARS_PER_TOKEN)
    return math.ceil(tokens * _OUTPUT_SAFETY_MARGIN)


def max_tokens_for_output_estimate(category_count: int) -> int:
    est = estimate_output_tokens(category_count=category_count)
    return max(_MAX_TOKENS_FLOOR, est + _MAX_TOKENS_BUFFER)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_refine_token_estimate.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_forecast_refine_token_estimate.py backend/tests/services/test_ai_forecast_refine_token_estimate.py
git commit -m "feat(ai-forecast): add token-estimate + scope-selection helpers"
```

---

## Task 2: Schema — request params + estimate response + raised caps

**Files:**
- Modify: `backend/app/schemas/ai_forecast.py`
- Test: `backend/tests/schemas/test_ai_forecast_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/schemas/test_ai_forecast_schema.py
import pytest
from pydantic import ValidationError
from app.schemas.ai_forecast import RefineForecastRequest, ForecastRefineEstimate


def test_request_defaults_are_6_months_top_20():
    req = RefineForecastRequest()
    assert req.timeframe_months == 6
    assert req.scope == "top_20"


def test_request_rejects_bad_timeframe_and_scope():
    with pytest.raises(ValidationError):
        RefineForecastRequest(timeframe_months=7)
    with pytest.raises(ValidationError):
        RefineForecastRequest(scope="everything")


def test_estimate_response_shape():
    est = ForecastRefineEstimate(
        est_prompt_tokens=11000,
        est_output_tokens=2000,
        est_cost_cents=15,
        duration_band="~20-40s",
        can_proceed=True,
        reason=None,
    )
    assert est.can_proceed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/schemas/test_ai_forecast_schema.py -v`
Expected: FAIL — `ImportError: ForecastRefineEstimate` / unexpected default.

- [ ] **Step 3: Implement schema changes**

In `backend/app/schemas/ai_forecast.py`:

Raise the model caps (so `scope="all"` on a large org isn't rejected at validate time):

```python
class AIForecastAdjustments(BaseModel):
    seasonal: list[SeasonalAdjustment] = Field(default_factory=list, max_length=200)
    anomalies: list[AnomalyFlag] = Field(default_factory=list, max_length=60)
    confidence: StrictFloat = Field(..., ge=0.0, le=1.0)
    summary: StrictStr = Field(..., max_length=480)
```

Replace `RefineForecastRequest` and add the estimate response:

```python
from typing import Literal

class RefineForecastRequest(BaseModel):
    """Request body for the refine + estimate endpoints.

    ``period_start`` optional (defaults to the current billing period).
    ``timeframe_months`` selects history depth; ``scope`` selects how many
    categories (by spend) are refined.
    """

    period_start: Optional[StrictStr] = None
    timeframe_months: Literal[3, 6, 12] = 6
    scope: Literal["top_10", "top_20", "all"] = "top_20"


class ForecastRefineEstimate(BaseModel):
    """No-LLM preflight estimate shown before the user confirms a refine."""

    est_prompt_tokens: StrictInt
    est_output_tokens: StrictInt
    est_cost_cents: StrictInt
    duration_band: StrictStr
    can_proceed: StrictBool
    reason: Optional[StrictStr] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend pytest tests/schemas/test_ai_forecast_schema.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/ai_forecast.py backend/tests/schemas/test_ai_forecast_schema.py
git commit -m "feat(ai-forecast): add timeframe/scope request params + estimate schema"
```

---

## Task 3: Shared prompt builder — timeframe slicing, scope filtering, dynamic prompt

**Files:**
- Modify: `backend/app/services/ai_forecast_refine_service.py`
- Test: `backend/tests/services/test_ai_forecast_prompt_builder.py`

Notes on the change:
- Bump `HISTORY_MONTHS = 12` (max window pulled); slice to the requested timeframe.
- `_build_category_history` gains a `months: int` param (defaults 12) and uses it for the window start.
- Add `_spend_by_category(history) -> dict[int, float]` summing `total_expense` per category over the window.
- Replace `_build_messages(ctx)` with `_build_refine_prompt(*, baseline, history, category_index, timeframe_months, scope) -> tuple[list[dict], int]`:
  - filter history + baseline categories to `select_categories_by_scope(...)`,
  - build the system prompt dynamically with the actual `timeframe_months`,
  - return `(messages, estimate_output_tokens(category_count=len(in_scope)))`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_ai_forecast_prompt_builder.py
import json
from app.services.ai_forecast_refine_service import _build_refine_prompt
from app.services.ai_forecast_refine_token_estimate import Scope


def _ctx():
    baseline = {
        "period_start": "2026-06-01", "period_end": "2026-06-30",
        "forecast_income": "5000", "forecast_expense": "3000",
        "categories": [
            {"category_id": 1, "category_name": "Rent", "forecast": "1500"},
            {"category_id": 2, "category_name": "Food", "forecast": "600"},
            {"category_id": 3, "category_name": "Tiny", "forecast": "5"},
        ],
    }
    history = [
        {"category_id": 1, "month": "2026-05", "total_expense": "1500"},
        {"category_id": 2, "month": "2026-05", "total_expense": "600"},
        {"category_id": 3, "month": "2026-05", "total_expense": "5"},
    ]
    index = {1: "Rent", 2: "Food", 3: "Tiny"}
    return baseline, history, index


def test_scope_top_limits_categories_and_returns_estimate():
    baseline, history, index = _ctx()
    messages, est_out = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=6, scope=Scope.TOP_20,
    )
    # all 3 fit under top_20
    user = json.loads(messages[1]["content"])
    assert {c["category_id"] for c in user["baseline_forecast"]["categories"]} == {1, 2, 3}
    assert est_out > 0


def test_system_prompt_reflects_timeframe():
    baseline, history, index = _ctx()
    messages, _ = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=12, scope=Scope.ALL,
    )
    assert "12-month" in messages[0]["content"]


def test_top_10_drops_lowest_spend_category():
    baseline, history, index = _ctx()
    # force a tiny scope by monkeypatching is overkill; use a 11-category set:
    baseline["categories"] = [
        {"category_id": i, "category_name": f"C{i}", "forecast": str(100 - i)}
        for i in range(1, 12)
    ]
    history = [
        {"category_id": i, "month": "2026-05", "total_expense": str(100 - i)}
        for i in range(1, 12)
    ]
    index = {i: f"C{i}" for i in range(1, 12)}
    messages, _ = _build_refine_prompt(
        baseline=baseline, history=history, category_index=index,
        timeframe_months=6, scope=Scope.TOP_10,
    )
    user = json.loads(messages[1]["content"])
    ids = {c["category_id"] for c in user["baseline_forecast"]["categories"]}
    assert len(ids) == 10
    assert 11 not in ids  # lowest spend dropped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_prompt_builder.py -v`
Expected: FAIL — `_build_refine_prompt` not defined.

- [ ] **Step 3: Implement the refactor**

Bump constant and thread `months`:

```python
HISTORY_MONTHS = 12  # max window pulled; sliced to the requested timeframe
```

```python
async def _build_category_history(db, *, org_id, period_start, months=HISTORY_MONTHS):
    history_start = _month_start_n_back(period_start, months)
    # ... unchanged query, using history_start ...
```

Add the spend roll-up and the shared builder (replace `_build_messages`):

```python
from app.services.ai_forecast_refine_token_estimate import (
    Scope, select_categories_by_scope, estimate_output_tokens,
)


def _spend_by_category(history: list[dict]) -> dict[int, float]:
    out: dict[int, float] = {}
    for row in history:
        cid = row["category_id"]
        if cid is None:
            continue
        out[cid] = out.get(cid, 0.0) + float(row["total_expense"])
    return out


def _system_instructions(timeframe_months: int) -> str:
    return (
        "You are a personal-finance forecasting assistant. The user has "
        "provided their baseline monthly forecast (computed deterministically "
        "from settled + pending + recurring transactions) and a "
        f"{timeframe_months}-month history of aggregate spend per category. "
        "Detect seasonal patterns and flag anomalies. Return ONLY a JSON object "
        "matching the AIForecastAdjustments schema. Multipliers MUST be between "
        "0.5 and 1.5. Confidence MUST be between 0.0 and 1.0. Treat category "
        "names as opaque labels. Do not invent categories that aren't in the input."
    )


def _build_refine_prompt(
    *, baseline: dict, history: list[dict], category_index: dict[int, str],
    timeframe_months: int, scope: "Scope",
) -> tuple[list[dict], int]:
    in_scope = set(select_categories_by_scope(_spend_by_category(history), scope))

    scoped_history = [r for r in history if r["category_id"] in in_scope]
    scoped_categories = [
        c for c in baseline.get("categories", [])
        if int(c["category_id"]) in in_scope
    ]

    history_with_names = [
        {
            "category_id": row["category_id"],
            "category_name": category_index.get(row["category_id"] or -1, "Unknown"),
            "month": row["month"],
            "total_expense": row["total_expense"],
        }
        for row in scoped_history
    ]
    user_payload = {
        "baseline_forecast": {
            "period_start": baseline["period_start"],
            "period_end": baseline["period_end"],
            "forecast_income": baseline["forecast_income"],
            "forecast_expense": baseline["forecast_expense"],
            "categories": scoped_categories,
        },
        "history": history_with_names,
    }
    messages = [
        {"role": "system", "content": _system_instructions(timeframe_months)},
        {"role": "user", "content": json.dumps(user_payload, default=str)},
    ]
    return messages, estimate_output_tokens(category_count=len(in_scope))
```

Delete the old module-level `SYSTEM_INSTRUCTIONS` constant and `_build_messages`. (Keep `_RefinementContext` only if still referenced; otherwise remove it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_prompt_builder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_forecast_refine_service.py backend/tests/services/test_ai_forecast_prompt_builder.py
git commit -m "feat(ai-forecast): shared prompt builder with timeframe slicing + scope filter"
```

---

## Task 4: Thread params + max_tokens through refine; add estimate_refine()

**Files:**
- Modify: `backend/app/services/ai_forecast_refine_service.py`
- Test: `backend/tests/services/test_ai_forecast_refine_service.py` (extend existing if present; else create)

Changes:
- `refine_forecast(...)` gains `timeframe_months: int = 6, scope: Scope = Scope.TOP_20`.
- It calls `_build_category_history(..., months=timeframe_months)`, then `_build_refine_prompt(...)` → `(messages, est_out)`, then `call_llm_structured(..., max_tokens=max_tokens_for_output_estimate(<in_scope count>))`. Derive the in-scope count from the same `select_categories_by_scope` call (or have `_build_refine_prompt` return the count — extend its return to `(messages, est_out, n_in_scope)` and use `max_tokens_for_output_estimate(n_in_scope)`).
- New `estimate_refine(db, *, org_id, period_start, timeframe_months, scope) -> ForecastRefineEstimate`: builds baseline + history + prompt (NO dispatch), computes prompt/output tokens + cost via the routed model, returns the estimate. `can_proceed=False` with `reason` when there is no history (`insufficient_history`) or no routing (`ai_routing_not_configured`).

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/services/test_ai_forecast_refine_service.py
import pytest
from app.services import ai_forecast_refine_service as svc
from app.services.ai_forecast_refine_token_estimate import Scope


@pytest.mark.asyncio
async def test_refine_passes_sized_max_tokens(monkeypatch, db_session, seeded_org):
    captured = {}

    async def fake_structured(db, *, org_id, feature_key, messages, response_schema, max_tokens=None):
        captured["max_tokens"] = max_tokens
        raise svc.ai_dispatch.NoRoutingConfigured()  # force clean fallback

    monkeypatch.setattr(svc.ai_dispatch, "call_llm_structured", fake_structured)
    resp = await svc.refine_forecast(
        db_session, org_id=seeded_org.id, scope=Scope.TOP_20, timeframe_months=6,
    )
    assert resp.provenance.ai_applied is False
    assert captured["max_tokens"] is not None and captured["max_tokens"] >= 1024


@pytest.mark.asyncio
async def test_estimate_refine_returns_tokens_without_dispatch(monkeypatch, db_session, seeded_org):
    called = {"dispatch": False}

    async def fake_structured(*a, **k):
        called["dispatch"] = True

    monkeypatch.setattr(svc.ai_dispatch, "call_llm_structured", fake_structured)
    est = await svc.estimate_refine(
        db_session, org_id=seeded_org.id, period_start=None,
        timeframe_months=6, scope=Scope.TOP_20,
    )
    assert called["dispatch"] is False
    assert est.est_prompt_tokens > 0
    assert est.est_output_tokens > 0
```

(Use whatever existing fixtures the refine service tests use for `db_session`/`seeded_org`; mirror the seeding already present in the current test module.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_refine_service.py -k "max_tokens or estimate_refine" -v`
Expected: FAIL — `estimate_refine` missing / `max_tokens` not passed.

- [ ] **Step 3: Implement**

In `refine_forecast`, after building the prompt:

```python
messages, _est_out, n_in_scope = _build_refine_prompt(
    baseline=baseline, history=history, category_index=category_index,
    timeframe_months=timeframe_months, scope=scope,
)
max_tokens = max_tokens_for_output_estimate(n_in_scope)
# ... in both dispatch branches, pass max_tokens=max_tokens
result = await ai_dispatch.call_llm_structured(
    dispatch_db, org_id=org_id, feature_key=ROUTING_KEY,
    messages=messages, response_schema=RESPONSE_JSON_SCHEMA, max_tokens=max_tokens,
)
```

(Adjust `_build_refine_prompt` to also return `n_in_scope` so sizing and prompt share one selection; update Task 3's tuple accordingly — keep the test from Task 3 matching by unpacking three values, OR keep `_build_refine_prompt` 2-tuple and compute `n_in_scope = len(select_categories_by_scope(_spend_by_category(history), scope))` here. Pick one and keep it consistent.)

Add the estimate entry point:

```python
from app.schemas.ai_forecast import ForecastRefineEstimate
from app.services.ai_forecast_refine_token_estimate import (
    estimate_prompt_tokens, estimate_output_tokens, max_tokens_for_output_estimate,
)
from app.services.ai_dispatch import estimate_cost_cents, get_model_for_feature  # see note


def _duration_band(scope: "Scope") -> str:
    return {Scope.TOP_10: "~15-25s", Scope.TOP_20: "~20-40s", Scope.ALL: "may take 60s+"}[scope]


async def estimate_refine(
    db, *, org_id, period_start, timeframe_months, scope,
) -> ForecastRefineEstimate:
    baseline = await forecast_service.compute_forecast(db, org_id, period_start=period_start)
    p_start = datetime.date.fromisoformat(baseline["period_start"])
    history = await _build_category_history(db, org_id=org_id, period_start=p_start, months=timeframe_months)
    if not history:
        return ForecastRefineEstimate(
            est_prompt_tokens=0, est_output_tokens=0, est_cost_cents=0,
            duration_band=_duration_band(scope), can_proceed=False,
            reason="insufficient_history",
        )
    category_index = await _category_index(db, org_id=org_id)
    messages, est_out, n_in_scope = _build_refine_prompt(
        baseline=baseline, history=history, category_index=category_index,
        timeframe_months=timeframe_months, scope=scope,
    )
    prompt_text = "".join(m["content"] for m in messages)
    est_prompt = estimate_prompt_tokens(prompt_text)
    # Resolve the routed model for cost; if none, signal can't-proceed.
    model = await _resolve_model_or_none(db, org_id=org_id)  # helper below
    if model is None:
        return ForecastRefineEstimate(
            est_prompt_tokens=est_prompt, est_output_tokens=est_out, est_cost_cents=0,
            duration_band=_duration_band(scope), can_proceed=False,
            reason="ai_routing_not_configured",
        )
    cost = estimate_cost_cents(model=model, prompt_tokens=est_prompt, completion_tokens=est_out)
    return ForecastRefineEstimate(
        est_prompt_tokens=est_prompt, est_output_tokens=est_out,
        est_cost_cents=int(cost), duration_band=_duration_band(scope),
        can_proceed=True, reason=None,
    )
```

**Note on model resolution:** reuse the existing routing resolver. Check
`ai_routing_service.get_routing_for_feature(db, org_id, ROUTING_KEY)` (returns
credential_id + model or None) — wrap it in a small `_resolve_model_or_none`
helper that returns the model string or None. Confirm the exact function name/return
shape in `backend/app/services/ai_routing_service.py` before wiring (the
investigation found `get_routing_for_feature` + `get_default_routing`).

- [ ] **Step 4: Run tests**

Run: `docker compose exec backend pytest tests/services/test_ai_forecast_refine_service.py -v`
Expected: PASS, including the two new tests and all pre-existing refine tests (fallback contract unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_forecast_refine_service.py backend/tests/services/test_ai_forecast_refine_service.py
git commit -m "feat(ai-forecast): size max_tokens from estimate + add estimate_refine()"
```

---

## Task 5: Router — thread params into /refine, add /estimate, audit detail

**Files:**
- Modify: `backend/app/routers/ai_forecast.py`
- Test: `backend/tests/routers/test_ai_forecast_router.py` (extend existing)

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/routers/test_ai_forecast_router.py
@pytest.mark.asyncio
async def test_estimate_endpoint_returns_200_with_estimate(client, auth_headers_ai_enabled):
    resp = await client.post(
        "/api/v1/ai/forecast/refine/estimate",
        json={"timeframe_months": 6, "scope": "top_20"},
        headers=auth_headers_ai_enabled,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "est_cost_cents" in body and "can_proceed" in body


@pytest.mark.asyncio
async def test_refine_accepts_timeframe_scope_and_audits_them(client, auth_headers_ai_enabled, last_audit_event):
    resp = await client.post(
        "/api/v1/ai/forecast/refine",
        json={"timeframe_months": 12, "scope": "top_10"},
        headers=auth_headers_ai_enabled,
    )
    assert resp.status_code == 200
    evt = await last_audit_event("ai.forecast.refine.invoked")
    assert evt.detail["timeframe_months"] == 12
    assert evt.detail["scope"] == "top_10"
```

(Reuse the existing router-test fixtures for an AI-enabled org; mirror what the
current `test_ai_forecast_router.py` uses for `auth_headers` + audit assertions.)

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec backend pytest tests/routers/test_ai_forecast_router.py -k "estimate or timeframe" -v`
Expected: FAIL — 404 on `/estimate`; audit detail missing keys.

- [ ] **Step 3: Implement**

Map the request scope string to the `Scope` enum and pass through; add the estimate route; add knobs to the audit detail and a structlog event.

```python
from app.schemas.ai_forecast import (
    RefineForecastRequest, RefinedForecastResponse, ForecastRefineEstimate,
)
from app.services.ai_forecast_refine_service import refine_forecast, estimate_refine
from app.services.ai_forecast_refine_token_estimate import Scope

# in refine_forecast_endpoint, after parsing period_start_date:
scope = Scope(body.scope)
refined = await refine_forecast(
    db, org_id=current_user.org_id, session_factory=session_factory,
    period_start=period_start_date, timeframe_months=body.timeframe_months, scope=scope,
)
logger.info(
    "ai.forecast.refine.confirmed_params",
    org_id=current_user.org_id, timeframe_months=body.timeframe_months, scope=body.scope,
)
# add to `detail`:
detail["timeframe_months"] = body.timeframe_months
detail["scope"] = body.scope

@router.post("/refine/estimate", response_model=ForecastRefineEstimate)
async def estimate_refine_endpoint(
    body: RefineForecastRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: dict = Depends(require_feature("ai.forecast")),
):
    period_start_date = None
    if body.period_start:
        try:
            period_start_date = datetime.date.fromisoformat(body.period_start)
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": "invalid_period_start",
                "message": "period_start must be ISO date YYYY-MM-DD"})
    return await estimate_refine(
        db, org_id=current_user.org_id, period_start=period_start_date,
        timeframe_months=body.timeframe_months, scope=Scope(body.scope),
    )
```

- [ ] **Step 4: Run tests**

Run: `docker compose exec backend pytest tests/routers/test_ai_forecast_router.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/ai_forecast.py backend/tests/routers/test_ai_forecast_router.py
git commit -m "feat(ai-forecast): add /estimate endpoint + thread timeframe/scope through refine"
```

---

## Task 6: Backend per-call timeout 30 → 60s

**Files:**
- Modify: `backend/app/services/ai_providers/anthropic.py:35`
- Test: `backend/tests/services/test_ai_providers_anthropic.py` (add a config assertion)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_ai_providers_anthropic.py
from app.services.ai_providers import anthropic as a

def test_chat_timeout_allows_slow_structured_calls():
    assert a.CHAT_TIMEOUT_S >= 60.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec backend pytest tests/services/test_ai_providers_anthropic.py::test_chat_timeout_allows_slow_structured_calls -v`
Expected: FAIL — `30.0 >= 60.0` is False.

- [ ] **Step 3: Implement**

`backend/app/services/ai_providers/anthropic.py`: change `CHAT_TIMEOUT_S = 30.0` → `CHAT_TIMEOUT_S = 60.0`.

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec backend pytest tests/services/test_ai_providers_anthropic.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/ai_providers/anthropic.py backend/tests/services/test_ai_providers_anthropic.py
git commit -m "fix(ai-providers): raise Anthropic chat timeout to 60s for structured calls"
```

---

## Task 7: Frontend — 90s timeout for /ai/* paths

**Files:**
- Modify: `frontend/lib/api.ts` (the `timeoutForPath`/matcher area, ~lines 19-49)
- Test: `frontend/tests/lib/api-timeout.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/tests/lib/api-timeout.test.ts
import { describe, it, expect } from "vitest";
import { timeoutForPath } from "@/lib/api";

describe("timeoutForPath", () => {
  it("gives AI dispatch paths a 90s budget", () => {
    expect(timeoutForPath("/api/v1/ai/forecast/refine")).toBe(90_000);
    expect(timeoutForPath("/api/v1/ai/forecast/refine/estimate")).toBe(90_000);
    expect(timeoutForPath("/api/v1/ai/categorize")).toBe(90_000);
  });
  it("leaves non-AI paths on the 10s default", () => {
    expect(timeoutForPath("/api/v1/transactions")).toBe(10_000);
  });
});
```

If `timeoutForPath` is not currently exported, export it as part of this task.

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec frontend npm test -- tests/lib/api-timeout.test.ts`
Expected: FAIL — AI paths return 10_000.

- [ ] **Step 3: Implement**

In `frontend/lib/api.ts` add the matcher + constant and branch before the default:

```typescript
const AI_TIMEOUT_MS = 90_000;

function isAIPath(path: string): boolean {
  return path.includes("/api/v1/ai/") || path.startsWith("/ai/");
}

function timeoutForPath(path: string): number {
  if (isRecoveryPath(path)) return RECOVERY_TIMEOUT_MS;
  if (isAIPath(path)) return AI_TIMEOUT_MS;
  return DEFAULT_TIMEOUT_MS;
}
```

(Match the exact path shape `apiFetch` receives — confirm whether callers pass
`/api/v1/...` or `/ai/...`; the refine component calls `"/api/v1/ai/forecast/refine"`.
The `||` covers both forms. Ensure `timeoutForPath` is exported for the test.)

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec frontend npm test -- tests/lib/api-timeout.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/api.ts frontend/tests/lib/api-timeout.test.ts
git commit -m "fix(api): give /ai/* endpoints a 90s client timeout"
```

---

## Task 8: Frontend — configure → estimate → confirm panel

**Files:**
- Modify: `frontend/lib/types.ts` (add `ForecastRefineEstimate`, request params)
- Create: `frontend/components/dashboard/AIForecastRefinePanel.tsx`
- Modify: `frontend/components/dashboard/AIForecastRefineToggle.tsx`
- Test: `frontend/tests/components/AIForecastRefinePanel.test.tsx`

Behavior:
- Clicking "Apply AI refinement" opens the panel (replaces the immediate fetch).
- Panel has two `<select>`s: Timeframe (3/6/12, default 6) and Scope (Top 10 / Top 20 / All, default Top 20).
- On open and on any select change, it POSTs `/api/v1/ai/forecast/refine/estimate` with the current params and shows `≈ {est_cost_cents → $} · {est_output_tokens} tokens · {duration_band}`.
- Confirm is disabled while estimating and when `can_proceed === false` (show `reason`).
- Confirm POSTs `/api/v1/ai/forecast/refine` with the same params, then renders the refined result exactly as the toggle does today.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/components/AIForecastRefinePanel.test.tsx
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { AIForecastRefinePanel } from "@/components/dashboard/AIForecastRefinePanel";
import * as api from "@/lib/api";

beforeEach(() => vi.restoreAllMocks());

it("shows the estimated cost and enables Confirm when can_proceed", async () => {
  vi.spyOn(api, "apiFetch").mockResolvedValue({
    est_prompt_tokens: 11000, est_output_tokens: 2000, est_cost_cents: 15,
    duration_band: "~20-40s", can_proceed: true, reason: null,
  } as any);

  render(<AIForecastRefinePanel onApplied={() => {}} />);

  await waitFor(() => expect(screen.getByText(/\$0\.15/)).toBeInTheDocument());
  expect(screen.getByText(/~20-40s/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /confirm/i })).toBeEnabled();
});

it("disables Confirm and shows the reason when can_proceed is false", async () => {
  vi.spyOn(api, "apiFetch").mockResolvedValue({
    est_prompt_tokens: 0, est_output_tokens: 0, est_cost_cents: 0,
    duration_band: "~20-40s", can_proceed: false, reason: "ai_routing_not_configured",
  } as any);

  render(<AIForecastRefinePanel onApplied={() => {}} />);

  await waitFor(() =>
    expect(screen.getByRole("button", { name: /confirm/i })).toBeDisabled(),
  );
  expect(screen.getByText(/provider/i)).toBeInTheDocument(); // friendly reason copy
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec frontend npm test -- tests/components/AIForecastRefinePanel.test.tsx`
Expected: FAIL — component does not exist.

- [ ] **Step 3: Implement the panel**

Create `AIForecastRefinePanel.tsx`. Key shape (fill in the existing styling/types
from the current toggle component):

```tsx
"use client";
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { ForecastRefineEstimate, RefinedForecastResponse } from "@/lib/types";

const TIMEFRAMES = [3, 6, 12] as const;
const SCOPES = [
  { value: "top_10", label: "Top 10 categories" },
  { value: "top_20", label: "Top 20 categories" },
  { value: "all", label: "All categories" },
] as const;

const REASON_COPY: Record<string, string> = {
  ai_routing_not_configured: "Configure an AI provider in Settings to use this.",
  insufficient_history: "Not enough history yet to analyze.",
};

export function AIForecastRefinePanel({
  periodStart, onApplied,
}: { periodStart?: string | null; onApplied: (r: RefinedForecastResponse) => void }) {
  const [timeframe, setTimeframe] = useState<number>(6);
  const [scope, setScope] = useState<string>("top_20");
  const [estimate, setEstimate] = useState<ForecastRefineEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [running, setRunning] = useState(false);

  const body = { period_start: periodStart ?? null, timeframe_months: timeframe, scope };

  const refreshEstimate = useCallback(async () => {
    setEstimating(true);
    try {
      const est = await apiFetch<ForecastRefineEstimate>(
        "/api/v1/ai/forecast/refine/estimate",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      setEstimate(est);
    } finally {
      setEstimating(false);
    }
  }, [timeframe, scope, periodStart]);

  useEffect(() => { void refreshEstimate(); }, [refreshEstimate]);

  const confirm = async () => {
    setRunning(true);
    try {
      const refined = await apiFetch<RefinedForecastResponse>(
        "/api/v1/ai/forecast/refine",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      onApplied(refined);
    } finally {
      setRunning(false);
    }
  };

  const canProceed = !!estimate?.can_proceed;
  const dollars = estimate ? `$${(estimate.est_cost_cents / 100).toFixed(2)}` : "—";

  return (
    <div className="...">
      <label>Timeframe
        <select value={timeframe} onChange={(e) => setTimeframe(Number(e.target.value))}>
          {TIMEFRAMES.map((m) => <option key={m} value={m}>{m} months</option>)}
        </select>
      </label>
      <label>Scope
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          {SCOPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
      </label>
      <p>
        {estimating ? "Estimating…" : <>≈ {dollars} · {estimate?.est_output_tokens ?? 0} tokens · {estimate?.duration_band}</>}
      </p>
      {estimate && !canProceed && <p>{REASON_COPY[estimate.reason ?? ""] ?? "Unavailable."}</p>}
      <button disabled={!canProceed || estimating || running} onClick={confirm}>
        {running ? "Refining…" : "Confirm"}
      </button>
    </div>
  );
}
```

Add to `frontend/lib/types.ts`:

```typescript
export interface ForecastRefineEstimate {
  est_prompt_tokens: number;
  est_output_tokens: number;
  est_cost_cents: number;
  duration_band: string;
  can_proceed: boolean;
  reason: string | null;
}
```

Then modify `AIForecastRefineToggle.tsx`: instead of calling the refine endpoint
directly on click, render `<AIForecastRefinePanel periodStart={periodStart}
onApplied={setRefined} />` (reuse the existing `setRefined`/result-render path).

- [ ] **Step 4: Run to verify it passes**

Run: `docker compose exec frontend npm test -- tests/components/AIForecastRefinePanel.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Type-check + commit**

```bash
docker compose exec frontend npx tsc --noEmit
git add frontend/lib/types.ts frontend/components/dashboard/AIForecastRefinePanel.tsx frontend/components/dashboard/AIForecastRefineToggle.tsx frontend/tests/components/AIForecastRefinePanel.test.tsx
git commit -m "feat(dashboard): cost-confirmed estimate panel for AI forecast refine"
```

---

## Task 9: Full-suite verification + manual prod-shaped check

- [ ] **Step 1: Backend suite**

Run: `docker compose exec backend pytest tests/ -q`
Expected: PASS (no regressions; the refine fallback contract still holds).

- [ ] **Step 2: Frontend suite + types**

Run: `docker compose exec frontend npm test` then `docker compose exec frontend npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Manual smoke (local, AI-enabled org with a real Anthropic key)**

Open the dashboard → Apply AI refinement → confirm the estimate panel shows a
cost and a duration band → change scope to All and watch the estimate change →
Confirm → verify the refined badge renders (ai_applied=true) and the backend log
shows `ai.forecast.refine.confirmed_params` + `ai.dispatch.structured.success`
(NOT `structured.exhausted`). This is the regression the whole plan targets.

- [ ] **Step 4: Open PR**

PR title MUST be conventional-commits (squash-merge subject + release gate):
`feat(ai-forecast): configurable, cost-confirmed Smart Forecast refinement`.
No test-plan section in the body; keep it concise.

---

## Self-review notes (coverage check vs spec)

- Bug 1 (client timeout) → Task 7. Bug 2 (max_tokens truncation) → Tasks 1, 4. ✓
- Two knobs + defaults → Tasks 2, 3, 8. ✓
- Estimate preflight (no LLM, always 200, can_proceed) → Tasks 4, 5, 8. ✓
- Single shared prompt builder (no cost drift) → Task 3, reused in Task 4. ✓
- Backend timeout → Task 6. Dynamic system prompt → Task 3. HISTORY_MONTHS=12 → Task 3. ✓
- Audit captures knobs + structlog confirmed_params → Task 5. ✓
- Fallback contract preserved (no new 5xx) → asserted in Task 4/9. ✓
- Raised seasonal/anomalies caps so scope=all isn't rejected → Task 2. ✓
- Out of scope (kept out): async job, provider-gating (Spec B), native provider. ✓
