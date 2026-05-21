---
name: Functional/E2E Tests Backlog
description: Pre-launch nice-to-have — add functional/E2E tests so UX edge cases get caught by automation, not by manual exploration
type: project
originSessionId: 1d48e329-4340-4f5d-a67c-182b3d3f7479
---
**Pre-launch backlog (good-to-have, not blocking):** Add functional/E2E tests on top of the unit suite.

**Why:** Several UX bugs only surfaced through hands-on usage by fjorge — the close_period stub-collision (PR #93), the org-billing-cycle-day input behavior (number input clobber, stale value on revisit, missing projected end date) (PR for fix/org-billing-cycle-ux). These are exactly the cases unit tests miss because they're cross-layer (frontend state + backend response + UX flow).

**How to apply when picked up:**
- Stack candidates: Playwright (already used by Claude Code MCP) or Cypress, run against the local docker-compose stack.
- Seed a clean test user/org via the existing `./pfv seed` flow or a dedicated test fixture.
- Cover the high-value flows first:
  1. Sign up → first user becomes superadmin → land on dashboard.
  2. Set billing cycle day → revisit settings → value persists.
  3. Close period → dashboard reflects new period → re-close (idempotent UI behavior).
  4. Add transaction → appears in current period → close period → transaction stays where it belongs.
  5. SSO login flow (the one that has bitten us twice).
- Wire into CI as an optional/separate job (don't gate every PR on a 5-minute browser run; gate `main` merges instead).

Captured 2026-04-26 after fjorge's manual QA caught the org-billing-cycle UX bugs that unit tests didn't.
