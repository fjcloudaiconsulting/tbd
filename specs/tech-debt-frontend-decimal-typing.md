---
name: Frontend Decimal Typing Debt (deferred)
description: Backend serializes Decimal fields as JSON strings; frontend types them as `number` and coerces at usage sites. Working but not type-honest. Flagged 2026-05-03 during PR-D/PR-E review; explicitly not blocking, deferred to a dedicated cleanup PR.
type: project
---
**Status:** Deferred technical debt. Not blocking any current work. Flagged 2026-05-03 by reviewer during PR-D / PR-E re-review of transfers-between-accounts: "the frontend still follows the existing project convention of typing Decimal-backed amount fields as `number`, even though backend serialization encodes Decimal as strings. Current PR-D/PR-E code coerces safely, so I would not block these PRs on that broader type debt."

## What's wrong

Pydantic v2 with `Decimal` columns serializes to JSON as **strings** (e.g., `"10.50"`, not `10.5`). The pfv frontend types `Transaction.amount`, `TransferCandidate.amount`, `Account.balance`, `Category` budget fields, etc. as `number` for ergonomic arithmetic and rendering.

This works because:
- `formatAmount(n: number | string): string` accepts both shapes and coerces.
- `equalsAmount(a: string, b: string): boolean` is string-based and reads from the JSON shape directly.
- React components coerce via `Number(...)` or `parseFloat` where needed.

But it's not type-honest: at the network boundary, the runtime values are strings, and the static types say number.

## Why it hasn't been fixed

- Solo dev, pre-launch — no incident has surfaced from the mismatch.
- Touching it requires a sweep across every API-boundary type and every consumer (transactions, accounts, budgets, forecast, dashboard, reports, smart rules — all touch Decimal-backed fields).
- The `equalsAmount` / `formatAmount` helpers already exist and gracefully handle both shapes, so consumers haven't been forced to converge.

## When to fix

Recommend revisiting after the launch-path is feature-complete (post all L4.x). At that point:
1. Decide canonical: either match backend (string at the boundary) or convert at the API client (parseFloat in a typed deserializer in `lib/api.ts`).
2. Sweep all `amount: number` / `balance: number` references and align.
3. Verify no consumer relies on float arithmetic that would break under string semantics (or update them to use string-decimal helpers consistently).

This is post-launch cleanup, not a launch-blocker.
