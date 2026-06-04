# Smart Forecast refinement: reliability fix + configurable, cost-confirmed flow

- Status: Draft (awaiting user review)
- Date: 2026-06-04
- Scope: Spec A of the "make AI real" wave. Sibling Spec B (AI-readiness
  gating + provider onboarding) is tracked separately. The AI chat /
  internal-MCP assistant is a separate backlogged project.

## Problem

The "Apply AI refinement" button (Smart Forecast refinement, LAI.2, PR #371)
is a shipped, real feature but is broken in production. A single user click
(`request_id 7dcf14a8…`, org 1, 2026-06-04 07:23 UTC) exposed two independent
bugs:

1. **Client timeout too short.** The frontend aborts at
   `DEFAULT_TIMEOUT_MS = 10s` (`frontend/lib/api.ts:2`); the refine endpoint is
   not on the 45s recovery allowlist and passes no override, so it gets a hard
   10s ceiling and surfaces `"Request timed out. Try again."`
   (`frontend/lib/api.ts:285`). But a single synchronous Anthropic call took
   **~19s** in prod, and the dispatch can do up to 3 sequential attempts
   (~39s observed). The browser gives up long before the backend finishes — so
   the feature can **never** succeed through the UI as wired.

2. **`max_tokens` truncation.** The structured dispatch passes no `max_tokens`,
   so the Anthropic adapter falls back to `DEFAULT_CHAT_MAX_TOKENS = 1024`
   (`backend/app/services/ai_providers/anthropic.py:46,235`). The response
   schema requires `seasonal[]` (one row **per category**) + `anomalies[]` +
   `confidence` + `summary` (`ai_forecast_refine_service.py:96-105`). For a real
   org (11,319-token prompt, many categories) the tool-use JSON exceeds 1024
   output tokens and is cut off **before the `anomalies` key**, failing schema
   validation on all 3 attempts → `ai.dispatch.structured.exhausted` → silent
   fallback to the baseline forecast with `ai_applied=False`. Proof: logged
   `completion_tokens: 3072` = exactly 3 × 1024 (the dispatcher sums tokens
   across attempts at `ai_dispatch.py:1110`).

Net: even with unlimited client time, refinement currently exhausts retries and
silently returns the plain baseline. Both bugs must be fixed.

### Product gap

Beyond the bugs, the one-click action spends the org's tokens (real money) with
no visibility or control. The owner wants the user to **knowingly choose how
much to analyze and confirm the cost before any tokens are spent**.

## Goals

- Refinement reliably succeeds for the default configuration.
- The user controls two cost levers and sees an estimated cost/time **before**
  any tokens are spent, then explicitly confirms.
- Preserve the existing fallback contract: the refine endpoint never 5xxs;
  any dispatch failure returns the baseline forecast.

## Non-goals / deferred

- **Background/async execution** of the refine call — deferred to the backlog
  (the motivating case for it is the "All categories on a very large org"
  worst case below). v1 stays synchronous.
- **Provider-gating + onboarding** (only show AI features once a provider is
  configured, provider token doc links, budget-rebalance gate fix) — Spec B of
  this wave, separate PR, full backend enforcement.
- **Platform-native AI provider** — stub, gated off (`AI_NATIVE_ENABLED=false`).
- **AI chat / internal-MCP assistant** — separate backlogged project.

## UX flow

The one-click toggle becomes a configure-then-confirm panel, kept lightweight so
the happy path is still essentially one decision:

1. User clicks **Apply AI refinement** → an inline panel/modal opens,
   pre-filled with the recommended defaults as dropdowns.
2. The panel **auto-calls the estimate endpoint** (~1-2s, no LLM call) and
   displays **≈ tokens, ≈ cost, and a rough time band** (e.g. "~20-40s").
3. Changing either dropdown re-fetches the estimate live.
4. **Confirm** runs the real refine call (spinner sets the time expectation).
   **Cancel** closes it. No tokens are spent until Confirm.

If the estimate reports it cannot proceed (no provider/routing configured, or
insufficient history), the panel shows a friendly message instead of a Confirm
button. (Provider-not-configured messaging is deepened in Spec B.)

## The two knobs

| Knob | Default | Options | Controls |
|---|---|---|---|
| **Timeframe** | **6 months** | 3 / 6 / 12 months | Prompt (input) size |
| **Category scope** | **Top 20 by spend** | Top 10 / Top 20 / All | Output size (the `seasonal[]` row count that truncated) |

Defaults are pre-selected, so a user who does not care just clicks Confirm.

## Backend design

### New preflight endpoint

`POST /api/v1/ai/forecast/refine/estimate`

- Accepts the same params as refine: `{ period_start?, timeframe_months, scope }`.
- Builds the **same prompt** the refine call will build (see shared builder),
  runs **no LLM call**, and returns:
  `{ est_prompt_tokens, est_output_tokens, est_cost_cents, duration_band,
  can_proceed, reason? }`.
- **Always returns 200** (read-only preflight). `can_proceed=false` + `reason`
  when routing is missing or history is insufficient — the frontend then
  suppresses Confirm rather than letting the user spend on a doomed call.
- No separate rate limit and no cap spend (it touches no LLM). It reuses the
  same gate (`require_feature("ai.forecast")`) as refine.

### Single shared prompt builder (critical — prevents cost lying)

Refactor the current inline `_build_messages(ctx)` into:

```
_build_refine_prompt(baseline, history, category_index, timeframe_months, scope)
    -> (messages, est_output_tokens)
```

Both the estimate endpoint and the refine call invoke this one function, so the
quoted cost cannot drift from what actually runs. `est_output_tokens` is derived
deterministically from the in-scope category count (see heuristic). The refine
call derives `max_tokens` from the **same** `est_output_tokens` — one source of
truth, no separate lookup table.

### Fix the truncation bug

The refine dispatch passes `max_tokens = est_output_tokens + buffer` (buffer
~400 tokens for JSON structural growth, floored at the old 1024 so we never go
below today's behavior). With output sized to the chosen scope, the JSON can no
longer truncate before `anomalies`, so the retries that the truncation was
forcing disappear and the happy path is a single call.

### Token estimation heuristic

The stack has no tokenizer (no tiktoken/Anthropic counter). Use a char-based
heuristic, surfaced to the user as an **approximate** ("≈") figure:

- Prompt tokens: estimate from the built prompt string length (~1 token / 3.5
  chars).
- Output tokens: from the in-scope shape — per `seasonal` row ~220 chars; assume
  ~1 anomaly per 4 in-scope categories at ~180 chars; ~600 chars fixed overhead
  (`confidence`, `summary`, braces); convert at ~1 token / 3 chars (round up);
  add a ~10% safety margin.
- Cost: reuse `estimate_cost_cents(model, est_prompt_tokens, est_output_tokens)`
  with the org's routed model.
- Duration band: coarse mapping from scope (e.g. Top 10 → "~15-25s", Top 20 →
  "~20-40s", All → "may take 60s+").

Anthropic's free `count_tokens` API is a possible v2 accuracy improvement; not
in scope here (it would add a per-estimate network call and is provider-specific).

### Category scope selection

Add a helper that ranks categories by spend over the chosen window and selects
Top 10 / Top 20 / All. It must filter **both** the baseline categories sent in
the prompt and the history, so the model is never asked to adjust an
out-of-scope category. Out-of-scope categories keep their baseline value.

### Parameters, validation, history

- Add `timeframe_months` and `scope` to the refine request schema with Pydantic
  validation: `timeframe_months ∈ {3, 6, 12}` (default 6), `scope ∈
  {top_10, top_20, all}` (default top_20). The estimate endpoint shares the
  same request model.
- Keep `HISTORY_MONTHS` as the **maximum** window the query pulls (12). The
  service slices to the requested timeframe. If the org has less history than
  requested, refine still runs on what exists; the estimate reports the actual
  window used.
- The `SYSTEM_INSTRUCTIONS` prompt currently hardcodes "6-month history" — build
  it dynamically from `timeframe_months` so the prompt stops lying when the user
  picks 3 or 12.

### Fallback contract (unchanged)

The refine endpoint still returns 200 with the baseline forecast and a
`fallback_reason` on any dispatch failure (no routing, cap exceeded, capability
unsupported, structured-output exhausted, validation failure). No new 5xx paths.

### Audit + observability

- Refine audit row captures the chosen `timeframe_months` and `scope`.
- Emit a structlog event when the user-confirmed params reach the backend
  (e.g. `ai.forecast.refine.confirmed_params` with timeframe, scope,
  est_cost_cents) so the next prod investigation can verify intent vs outcome.
- Estimate requests are logged (structlog) but need not write an audit row
  (read-only, spends nothing).

## Timeout changes

The interaction between the client timeout, the per-call backend httpx timeout,
and the retry loop is the second root cause. Resolution:

- **Frontend:** add an `/api/v1/ai/*` path matcher in `frontend/lib/api.ts`
  (alongside the existing recovery-path matcher) granting a **90s** budget,
  replacing the 10s default for all AI dispatch endpoints. This is an
  infrastructure concern (AI calls are slow), not feature logic, so it lives in
  the path matcher rather than a per-call override.
- **Backend:** raise `CHAT_TIMEOUT_S` 30 → **60s** in the Anthropic adapter so a
  single legitimate large call is not cut short.
- **Leave the global structured-retry cap (architect-lock #13) untouched** — it
  is shared by categorize and budget. Correctly sizing `max_tokens` removes the
  truncation that was forcing all 3 retries, so the default path is a single
  ~20-40s call that fits comfortably inside 90s.

### Honest limitation

Synchronous execution plus a slow LLM means a pathological **"All categories on
a very large org"** request can still approach the 90s client budget (and, if it
needed retries, exceed it). The estimate's duration band warns the user before
they opt into that path, and this exact case is the motivation for the
backlogged async/background-job work. v1 makes the **default** path reliable and
makes the slow path an informed, opt-in choice.

## Data flow

```
[panel opens] -> POST /ai/forecast/refine/estimate {timeframe, scope}
   -> _build_refine_prompt (no LLM) -> {est tokens, est cost, duration band, can_proceed}
[user adjusts knob] -> re-estimate (live)
[Confirm] -> POST /ai/forecast/refine {timeframe, scope}
   -> _build_refine_prompt (same) -> call_llm_structured(max_tokens=est_output+buffer)
   -> success: refined forecast (ai_applied=true)
   -> any failure: baseline forecast (ai_applied=false, fallback_reason)
```

## Testing (TDD)

- A failing test reproducing the 1024 truncation with a Top-20-sized response
  schema, that passes once `max_tokens` is sized from the estimate.
- Estimate/refine **prompt-consistency** test: identical inputs (timeframe,
  scope, period) produce identical `messages` from the shared builder.
- Scope-selection test: Top 10 / Top 20 / All select the right categories by
  spend and filter both baseline and history.
- Timeframe slicing + insufficient-history behavior.
- Dynamic system prompt reflects the chosen timeframe.
- Frontend: `/ai/*` path matcher returns the 90s budget; estimate→confirm flow
  renders cost and only enables Confirm when `can_proceed`.
- Fallback contract preserved: dispatch failures still return baseline, no 5xx.

## Files (rough)

- `backend/app/services/ai_forecast_refine_service.py` — shared
  `_build_refine_prompt`, scope selection, timeframe slicing, dynamic prompt,
  `max_tokens` sizing, estimate helper.
- `backend/app/routers/ai_forecast.py` — new `/estimate` endpoint; pass
  params + `max_tokens`; audit detail.
- `backend/app/schemas/ai_forecast.py` — request params + estimate response
  schema.
- `backend/app/services/ai_providers/anthropic.py` — `CHAT_TIMEOUT_S` 30 → 60.
- `frontend/lib/api.ts` — `/ai/*` 90s path matcher.
- `frontend/components/dashboard/AIForecastRefineToggle.tsx` (+ a small
  estimate/confirm panel component) — the configure→estimate→confirm flow.
