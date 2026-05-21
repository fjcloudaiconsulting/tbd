---
name: In-app reseed with synthetic data
description: 2026-05-14 fjorge — affordance to load demo / synthetic data from the UI, for users who skip the onboarding opt-in or who wipe their org data and want to see the system populated again.
type: project
originSessionId: e75406f8-d01b-42d2-aa05-cdc574be2d1a
---
# In-app reseed with synthetic data

## Observation

Today there are two ways an org can end up empty:
1. User registers and skips the L3.3 onboarding opt-in for demo data.
2. Owner uses the L3.1 Danger Zone reset (`POST /api/v1/orgs/data/reset`) to wipe transactions/accounts/budgets/forecasts/etc.

In both cases the user lands on a clean but empty app and has to either import data, manually create accounts/transactions, or live with the blank state. There's no way to ask the product to fill itself with synthetic data so a user can poke around.

A `./pfv seed` CLI exists for local development and is documented in `CONTRIBUTING.md`'s seeding section. The opt-in inside the onboarding wizard (PR #238) uses the same underlying seed logic. What's missing is an **in-app** trigger usable after the onboarding window has closed.

## Brainstorm topics before scoping

1. **Audience:** every user, or owners only? Probably owners only (writes data into the org). Reuses the L3.1 owner-only Danger Zone guard pattern.
2. **Append vs replace:** if the org already has data, seeding on top likely produces garbage (duplicate categories, weird date overlaps). Two sane semantics:
   - **Replace:** wipe first (reuse `wipe_org_data` from L3.1) then seed. Typed-confirm like the Reset action.
   - **Append only when empty:** check that the org has zero transactions/accounts before allowing the action. Simpler, safer.
   Pick one. "Append only when empty" is the cleaner default.
3. **What dataset?** Reuse the existing seed mechanism (`./pfv seed` + `SEED_*` env vars from CONTRIBUTING.md) or carve out a leaner "demo preset" that ships fewer, more illustrative records? Probably reuse to avoid divergence.
4. **Where it lives in the UI:**
   - Org owner: Settings → Organization → "Load demo data" affordance (sibling to "Reset org data" Danger Zone).
   - On the empty dashboard: a one-time banner "Load demo data to explore" with dismiss + load actions.
   - Maybe both — banner is more discoverable, settings is the canonical location.
5. **Audit:** like L3.1, emit a `org.data.seeded` audit event. The L4.7 audit table handles this idempotently.
6. **Backend reuse:** the seed logic lives in a script outside the FastAPI app today. To trigger from a request handler we'd either shell out (yuck), import the seed module from FastAPI (cleaner), or extract the seed body into `app/services/seed_service.py` (cleanest).

## Related code

- `pfv` CLI script — has the `./pfv seed` subcommand and the `SEED_*` env vars.
- Onboarding seed opt-in: PR #238 — find the backend route it calls; that's the most reusable starting point.
- L3.1 wipe path: `app/services/org_data_service.py` (the `wipe_org_data` function and the public `POST /api/v1/orgs/data/reset` endpoint).
- L4.7 audit hook pattern.

## Effort

S to M. The biggest piece is extracting the seed logic into a service that can be called from a request handler. Once that exists the endpoint + UI + audit event are short.

## Priority

P3. Post-launch UX. Pair with the tour-restart work (see `project_restart_tour.md`); both are "I want a fresh demo experience" affordances and could be a single small initiative.
