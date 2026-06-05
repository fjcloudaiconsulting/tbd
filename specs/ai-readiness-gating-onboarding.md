# AI readiness: gating, "Set up AI" CTA, discoverability & onboarding

- Status: Draft (awaiting user review)
- Date: 2026-06-04
- Scope: Spec B of the "make AI real" wave. Sibling to the merged Spec A
  (forecast-refine reliability, #394) and the in-flight #395 (lenient LLM-output
  validation). The AI chat / internal-MCP assistant remains a separate backlog
  project (`specs/ai-assistant-mcp-chat-backlog.md`).

## Problem

The three shipped AI features are scattered, undiscoverable, and not clearly
gated:

- **Auto-categorize** ("✨ Suggest category", transactions edit row) is hidden
  unless `ai.autocategorize` is entitled — so an org without that flag never
  learns it exists.
- **Forecast refine** ("Apply AI refinement", dashboard) — the label is vague
  and has no help.
- **Budget rebalance** ("Suggest rebalance", budgets toolbar) renders
  **ungated** — it shows even when the org isn't entitled and no provider is
  configured, then dead-ends.

Worse, **entitlement and provider-configured are independent layers**: a feature
flag can be ON while the org has no routed AI provider, so an affordance appears
but the action fails. There's no single signal for "is this feature actually
usable", no guidance to set up a provider, and no in-app docs.

## Goals

- One server-computed source of truth for each AI feature's real state.
- Each AI surface renders one of three clear states, consistently.
- When a feature is entitled but no provider is configured, guide the user to
  set one up (role-aware), instead of hiding it or letting it dead-end.
- Make the features discoverable and self-explanatory (help + docs).
- Smooth provider onboarding (where to get an API key).

## Non-goals / deferred

- A central "AI hub" page (chose in-context + docs instead).
- Payments / plan-upsell messaging for not-entitled orgs (payments parked).
- The platform-native managed provider (still a stub, gated off).
- Live config invalidation: `/auth/status` is read at mount, so after
  configuring a provider the user reloads to see surfaces flip CTA → live.
  Documented limitation, acceptable for v1.
- AI chat / internal-MCP assistant (separate backlog project).

## Decisions (frozen)

1. **Not-configured state → "Set up AI" CTA** (not hidden), role-aware.
2. **Discoverability via in-context help tooltips + a `/docs` section** (reuse
   the existing help system); no central hub.
3. **Hard-block all 3 routes with 412** on the *no-provider precondition* —
   forecast/budget join categorize in returning `412 ai_provider_not_configured`
   when routing is missing. Genuine *runtime* AI failures (cap exceeded, LLM
   error, invalid schema) still return 200 + a usable baseline (the Spec A/#395
   fallback contract is preserved). Frontend gating means users never hit the
   412 from the UI; it's contract-hardening for direct/stale callers.
4. **Two focused PRs** (functional core, then discoverability).

## Architecture

### Foundation — `ai` block on `GET /api/v1/auth/status`

`/auth/status` is already fetched once at mount by `AuthProvider`. Add an `ai`
map giving each feature's true state:

```json
"ai": {
  "categorize": {"entitled": true,  "configured": false},
  "forecast":   {"entitled": true,  "configured": true},
  "budget":     {"entitled": false, "configured": false}
}
```

- `entitled` ← `feature_service.get_features(db, org_id)[<entitlement_key>]`.
- `configured` ← `ai_routing_service.get_routing_for_feature(db, org_id,
  <routing_name>) is not None`. Only evaluated when `entitled` is true, so an
  un-entitled org costs zero routing lookups; a fully-entitled org costs ≤3
  indexed lookups on a call that already runs once per session.

### Canonical feature mapping (kills the key-name drift)

The entitlement key, routing name, and UI id differ
(`ai.autocategorize`/`categorize_transactions`/`categorize`). Put the single
canonical triple in **one** backend module (e.g.
`backend/app/auth/ai_feature_map.py`), consumed by the status helper and any
route that needs it, plus a tiny **drift-guard test** asserting every
entitlement key is in the feature catalog and every routing name is in
`ROUTABLE_FEATURE_NAMES`. The frontend mirrors the UI ids only.

### Frontend — the 3-state pattern (reused on all 3 surfaces)

`AuthProvider` exposes `ai` (from status) and the existing `user.role`. Each AI
affordance branches:

| State | Condition | Renders |
|---|---|---|
| Hidden | `!entitled` | nothing |
| **Set up AI** | `entitled && !configured` | a CTA. **owner/admin** → links to Settings → AI Providers. **member** → disabled control + tooltip "Ask your org admin to set up an AI provider." |
| Live | `entitled && configured` | the real AI action |

This **fixes the budget-rebalance gate** (it joins the pattern) and **makes
auto-categorize discoverable** (CTA instead of silent hide). It also **removes
the bespoke `/api/v1/subscriptions` probe** the transactions page does today —
visibility now comes from `useAuth().ai`.

### Backend — full enforcement (precondition 412)

Each AI route already calls `require_feature(...)` (→403 if not entitled). Add:
on `get_routing_for_feature(...) is None`, return **`412` with code
`ai_provider_not_configured`** — categorize already does this; convert
forecast's and budget's "no routing → graceful fallback" branch to the 412.
Every *other* fallback branch (cap, capability, structured-exhausted, invalid
schema) stays as a 200 baseline/empty result. The forecast service's broad
fallback contract is otherwise untouched.

### Discoverability — help + docs (reuse existing system)

- A **`HelpTooltip`** (info icon) beside each of the 3 AI actions:
  `ai.categorize` / `ai.forecast` / `ai.budget` entries in
  `frontend/lib/help/tooltips.ts`, each stating what the feature does and that
  it uses the org's configured AI provider (tokens cost money), with
  `learnMoreSection: "ai-features"` deep-linking to docs.
- A new **"AI features"** section in `frontend/app/docs/page.tsx` (add to the
  `sections` array + a `<section id="ai-features">` block): setup prerequisite
  (configure a provider), then one subsection per feature.
- Rename the vague **"Apply AI refinement"** to something clearer (e.g. "Refine
  forecast with AI") alongside its tooltip.

### Onboarding — provider doc links

In the Settings → AI Providers add-credential form, render a per-provider
**"Get an API key →"** link based on the selected provider:
`anthropic` → console.anthropic.com keys, `openai` → platform.openai.com keys,
`ollama` → ollama.ai download, `openai_compatible` → a generic
OpenAI-compatible-server doc. (URLs centralized in one map with a "may drift"
note.)

## PR breakdown

**PR 1 — functional core** (`feat(ai): provider-aware gating + Set up AI CTA`)
- `ai_feature_map.py` + drift-guard test.
- `ai` block on `/auth/status` (+ Pydantic schema) and its helper.
- `AuthProvider` exposes `ai` + `role`.
- 3-state rendering on all 3 surfaces (categorize, forecast toggle, budget
  rebalance); delete the transactions `/subscriptions` probe.
- Backend: forecast + budget routes return `412 ai_provider_not_configured` on
  missing routing.

**PR 2 — discoverability & onboarding** (`feat(ai): help tooltips, docs, provider links`)
- `HelpTooltip` entries on the 3 surfaces + the label rename.
- `/docs` "AI features" section.
- Per-provider "Get an API key" links in the AI Providers form.

## Testing (minimal — operator is compute-capped)

- Backend: `/auth/status` returns the correct `ai` block for
  entitled/configured permutations (mock `get_features` +
  `get_routing_for_feature`); the drift-guard test; one route returns `412
  ai_provider_not_configured` when routing is missing.
- Frontend: one surface renders all three states from a mocked `useAuth().ai`
  (hidden / CTA / live), and the member-role CTA shows the "ask admin" variant.
- No new frontend tests beyond the one 3-state spec; reuse existing surface
  tests where possible.

## No migrations

Pure read/derive + UI. No new tables, no schema changes.
