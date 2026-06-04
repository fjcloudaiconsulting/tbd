# AI refinement tweaks (backlog)

Ideas to refine the existing AI features (categorize / forecast refine / budget
rebalance). Each needs its own brainstorm → spec when picked up. Filed
2026-06-04 during the "make AI real" wave (Spec A #394/#395 shipped).

## 1. Expose AI generation parameters to the user (seed, temperature, etc.)

Operator idea: let users tune the model's generation parameters — a **fixed seed**
(to reduce run-to-run deviation / make suggestions reproducible), **temperature**,
top_p, etc. Surface these with **proper documentation and warnings** so a user
only changes them deliberately (e.g. "lower temperature = more consistent, less
creative; a fixed seed makes repeated runs return the same suggestion").

Notes / open questions for the eventual spec:
- Where do these live? Per-org settings (like `forecast_input_granularity`) vs
  per-request (in the estimate/refine panel) vs per-provider-routing.
- Provider support varies: Anthropic supports `temperature`/`top_p` but **not a
  seed** (as of now); OpenAI supports `seed`. The adapter layer
  (`ai_providers/*`) would need to pass these through `chat_structured`, and the
  UI must only offer params the routed provider supports.
- Sensible defaults + guardrails (clamp temperature to a safe range).
- Reproducibility caveat: even with a seed, providers don't guarantee identical
  output across model versions.
- Pairs with the cost-confirmed flow ([[ai-forecast-refine-cost-confirmed]]):
  these are "advanced" knobs behind a disclosure, not front-and-center.

## 2. Forecast refine: review-then-apply (instead of auto-preview + Revert)

Today "Apply AI refinement" shows the AI-refined forecast immediately as an
**ephemeral on-screen overlay** (no data is persisted; "Revert to baseline" just
clears local React state). It works, but the UX reads as "it applied without
asking." The budget-rebalance feature already does **per-row accept/skip** in its
modal — forecast refine could adopt the same review-then-apply pattern, or at
minimum frame the result clearly as a *preview* of suggestions.

Notes:
- Confirm whether "apply" should ever *persist* into the forecast plan, or stay a
  display overlay. Currently it's display-only; if persistence is desired that's
  a bigger change (write to `forecast_plan_items`).
- Per-category accept/skip would mirror `BudgetRebalanceModal`'s pattern for
  consistency across AI features.
- Low risk today (nothing is mutated), so this is UX-clarity, not a data-safety
  fix.

## Related backlog

- Async/background refine execution (the synchronous-timeout worst case) — noted
  in [[ai-forecast-refine-cost-confirmed]].
- `estimate_refine` spend-cap pre-check (don't show can_proceed=true when the org
  is at its hard cap).
- AI chat / internal-MCP assistant — separate project
  (`specs/ai-assistant-mcp-chat-backlog.md`).
