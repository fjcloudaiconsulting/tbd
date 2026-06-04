# AI assistant: natural-language chat over an internal MCP tool layer (backlog)

- Status: Backlog — its own project, needs a dedicated brainstorm → spec → plan
  cycle when prioritized. NOT part of the 2026-06-04 "make AI real" wave.
- Filed: 2026-06-04 (operator idea during the forecast-refine fix)

## Idea

Make the app dramatically easier to use by letting users drive it with natural
language. The operator's framing: build an **internal MCP** exposing tools for
"anything we can do manually in the system", and an AI chat that translates a
user's prompt into those tool calls. **Every real change must be confirmed by
the user before it is applied** (no silent mutations).

Examples of intent: "create a recurring rent payment of 1200 on the 1st",
"move my groceries budget up 50 this month", "categorize last week's
transactions", "what did I spend on dining in Q1?".

## Why this is its own project (not a wave item)

- It is an agentic architecture, not a feature: a tool registry, a chat loop /
  planner, a confirmation-gating layer for mutations, per-tool permission +
  org-scope enforcement, and audit of every executed action.
- It must reuse the same authz/org-scoping every existing endpoint enforces, so
  the tools should wrap the service layer, not bypass it.
- Read vs write tools need different trust treatment (reads can auto-run; writes
  require an explicit, previewed confirmation showing exactly what will change).
- Cost/latency: this rides the same per-org AI dispatch/cap/ledger chokepoint as
  the other AI features; the `chat` routing key is already reserved in the
  feature catalog.

## Rough shape to explore later

- An internal tool registry whose tools map 1:1 onto existing service-layer
  operations (transactions, recurring, budgets, categories, reports queries).
- A planner that proposes a sequence of tool calls; writes are batched into a
  preview the user confirms before execution.
- Mutations route through the existing audit trail; reads are rate-limited and
  capped like other AI features.
- Gated behind the same provider-configured requirement as the rest of AI
  (see the AI-readiness gating spec).

## Dependencies / sequencing

- Sits on top of the AI dispatch chokepoint and the AI-readiness gating work.
- Should follow the operator's standard flow: dev+architect brainstorm → spec →
  subagent-driven implementation, when picked up.
