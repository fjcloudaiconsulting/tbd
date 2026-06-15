# AI Dispatch Universal Overspend Gate

**Date:** 2026-06-15
**Status:** design approved (operator + architect-reviewed), building
**Decision basis:** [[project_ai_dispatch_overspend_gate_decision]] (hard-block, no degrade) + 2026-06-15 operator choice (conservative worst-case estimation) + architect pass.

## Problem
Today the AI dispatch layer hard-blocks only the EXHAUSTED case (`cost_so_far >= hard_cap` → `AICapExceeded` → 402 `ai_hard_cap_exceeded`), at TWO identical sites: `call_llm` (`ai_dispatch.py:747-758`) and `_prepare_dispatch` (`:1030-1042`, backing `call_llm_structured` + the stream/embed wrappers). Projected-cost gating exists only for ONE feature (`estimate_refine`, `ai_forecast_refine_service.py:671-786`). So any feature without its own estimate gate can overspend by one final call (or several, via retries). Goal: a UNIVERSAL projected-overspend gate at dispatch, hard-block, conservative.

## Design

### 1. Shared estimator module — `app/services/ai_token_estimate.py` (new, pure)
- `_PROMPT_CHARS_PER_TOKEN` (move the constant here; `ai_forecast_refine_token_estimate.py` re-exports it so the two heuristics cannot diverge — that module's docstring already prizes non-divergence).
- `estimate_prompt_tokens_from_messages(messages: list[dict]) -> int` — char heuristic over message `content`. **Defensive for non-string content**: if `content` is a list of parts (multimodal), sum `len` of text parts and ignore non-text; if missing/None, treat as 0. Never raise on shape.
- `_DEFAULT_MAX_OUTPUT_TOKENS_BY_MODEL: dict[str, int]` keyed by the same model strings as `ai_pricing.MODEL_PRICING`, plus `_GLOBAL_DEFAULT_MAX_OUTPUT_TOKENS` (conservative fallback, e.g. a few-k ceiling — NOT the context window). `default_max_output_tokens_for(model: str) -> int`.

### 2. Projection helper — in `ai_dispatch.py`
`def _projected_cost_cents(model, messages, max_tokens, *, retry_multiplier=1) -> int`:
- `prompt_tokens = estimate_prompt_tokens_from_messages(messages)`
- `completion_tokens = max_tokens if max_tokens else default_max_output_tokens_for(model)`
- `single = estimate_cost_cents(model, prompt_tokens, completion_tokens)` — `estimate_cost_cents` already handles unknown models via its high `_default` pricing, so projection is always computable.
- return `single * retry_multiplier`.
- Wrap the whole computation at the call site in try/except (see §3 fail-closed).

### 3. Gate helper — `_enforce_cap(...)` in `ai_dispatch.py`
Replaces BOTH inline exhausted blocks. Signature carries what each site has: `resolved`, `cost_so_far`, `model`, `messages`, `max_tokens`, `retry_multiplier`, `org_id`, `feature_key`, `capability=None`.
- Short-circuit: if `resolved.hard_cap_cents is None`, return (no cap configured).
- Compute `projected` via `_projected_cost_cents(...)` inside try/except. **Fail-closed**: on ANY exception, set `projected = 0` and `logger.warning("ai.dispatch.cap.projection_failed", ...)` — this degrades to exhausted-only enforcement (never skips the gate, never 500s the hot path).
- **Block predicate (keep the explicit exhausted arm):**
  ```python
  if cost_so_far >= resolved.hard_cap_cents or cost_so_far + projected > resolved.hard_cap_cents:
      logger.info("ai.dispatch.cap.exceeded", org_id=..., feature_key=..., capability=...,
                  cost_so_far=cost_so_far, hard_cap_cents=resolved.hard_cap_cents,
                  projected_cost_cents=projected,
                  reason=("exhausted" if cost_so_far >= resolved.hard_cap_cents else "projected"))
      raise AICapExceeded()
  ```
  The explicit `cost_so_far >= hard_cap` arm guarantees an at-cap org is blocked even if `projected == 0` (fail-closed / empty messages).
- **No ledger row, no audit_event at this layer** (matches the existing exhausted block). `AICapExceeded` is caught by the routers and already audits as a success-precondition (`ai_cap_exceeded`) per [[reference_ai_audit_outcome_semantics]] — reusing the same exception inherits correct semantics; do NOT invent a new reason or 500.

### 4. Wiring both sites
- **`call_llm`** (`:747-758`): it already has `model`, `request_payload` (→ `messages`, `max_tokens`). Replace the inline block with `await _enforce_cap(..., retry_multiplier=1)` (raw chat does not retry).
- **`_prepare_dispatch`** (`:1027-1042`): currently does NOT receive the request payload. Add params so it can project after resolving `model`: thread `messages`, `max_tokens`, `retry_multiplier` (or a small `projection: _ProjectionInput` dataclass) from its callers. Replace the inline block with `_enforce_cap(...)`.
  - **Callers of `_prepare_dispatch`** (`call_llm_structured` + the PR3 stream/embed wrappers): pass their `messages`/`max_tokens`. **Retry multiplier:** `call_llm_structured` retries up to `STRUCTURED_OUTPUT_MAX_RETRIES` and aggregates spend, so it passes `retry_multiplier = STRUCTURED_OUTPUT_MAX_RETRIES + 1` (consistent with "prevent overspend"); chat-only / stream pass `1` unless they also retry. Verify the exact constant + caller signatures in code.

### 5. `estimate_refine` stays as the UI preflight
Unchanged (it gates the UI "Confirm"). Dispatch is now the universal authority its docstring already assumes. Both share the prompt-token heuristic via §1. No unification, no double-ledger (neither writes a ledger row on refusal).

## Accepted tradeoffs (documented, not hidden)
- **Over-block near the cap:** the conservative `max_tokens`/retry-multiplied estimate over-estimates most calls, so calls that would have fit can be refused right at the cap edge. This is the operator-chosen "prevent overspend" posture.
- **Concurrency race:** `cost_so_far` is an unlocked `SUM` over the ledger; two concurrent dispatches each individually fitting can jointly overspend. Out of scope (same caveat `estimate_refine` documents); closing it needs a reservation ledger.

## Tests (`-p team-aigate` isolated stack; never default; never ./pfv migrate)
- **`ai_token_estimate` unit:** prompt heuristic (string + list/multimodal content + missing content → no raise); `default_max_output_tokens_for` (known model → its map value, unknown → global fallback); `estimate_prompt_tokens_from_messages` non-string safety.
- **Gate at BOTH entry points:** `call_llm` and `call_llm_structured`/`_prepare_dispatch`:
  - projected block: `cost_so_far` under cap but `cost_so_far + projected > hard_cap` → `AICapExceeded` (402), NO adapter call, NO ledger row.
  - exhausted block still fires (`cost_so_far >= hard_cap`, `projected == 0`) — the explicit-arm regression.
  - under cap with headroom → call proceeds.
  - `hard_cap_cents is None` → no gate.
  - fail-closed: a payload that makes projection throw → degrades to exhausted-only (warning logged), does not 500.
  - retry multiplier: a structured call whose single-projection fits but `×(retries+1)` exceeds → blocked.
  - unknown model → projects via `_default` pricing (still gates).
- **Router-level:** the projected 402 surfaces as `ai_hard_cap_exceeded` and audits as a success-precondition (spot-check one router, e.g. `ai_categorize`).
- No em-dashes in any user-facing copy ([[feedback_no_em_dashes]]). Follow [[reference_ai_feature_pr_checklist]] + [[reference_ai_dispatch_session_isolation]].

## Files
`app/services/ai_token_estimate.py` (new), `app/services/ai_forecast_refine_token_estimate.py` (re-export the shared constant/heuristic), `app/services/ai_dispatch.py` (`_projected_cost_cents`, `_enforce_cap`, wire `call_llm` + `_prepare_dispatch` + its callers), tests. **No migration.**

## Process
Subagent-driven build with a review gate, then a fleet review before the PR (the established "ship clean" bar). Backend tests in `-p team-aigate`.
