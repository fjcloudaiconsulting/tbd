---
name: Restart the onboarding tour
description: 2026-05-14 fjorge — allow users to re-run the onboarding flow after skipping or completing it once. Currently the L3.3 tour fires once on first-run and there's no way back in.
type: project
originSessionId: e75406f8-d01b-42d2-aa05-cdc574be2d1a
---
# Restart the onboarding tour

## Observation

The L3.3 first-run onboarding wizard (shipped in PR #238) fires once on first-run and is gated by a "completed/skipped" flag. There's no in-app affordance to re-run it. Users who:
- Skipped the tour and want to see it later
- Completed it but want to refresh on a feature
- Are showing the product to a colleague or partner

...are stuck.

## Brainstorm topics before scoping

1. **Where is the "completed tour" flag stored?** Inspect `frontend/components/tour/TourProvider.tsx` and the corresponding backend field on the User or OrgSettings model. Most likely a user-level boolean. Knowing this drives whether "restart" needs a PATCH on `/users/me` or a frontend-only state reset.
2. **Resume vs full restart?** Re-run from the beginning is simpler and clearer. Resume is fiddly because the dataset changes between sessions.
3. **Per-section replay or full sequence?** A help-menu link like "Replay the onboarding tour" probably wants full sequence. A future "Tour: this feature" could pin per-section replay.
4. **Entry point:** Settings → Preferences (a "Replay onboarding tour" button)? AppShell user-menu dropdown? Footer help link? `/docs` page (the help manual)? Probably Settings + the help drawer feels right.
5. **Org vs user scope:** if multi-user orgs exist, the tour state is per-user, not per-org. Replay shouldn't affect other org members.

## Related code

- `frontend/components/tour/TourProvider.tsx` — tour engine.
- Backend likely has a `users.onboarding_completed_at` or similar; check `backend/app/models/user.py`.
- PR #238 history has the full design and dispatch context.

## Effort

XS to S. Backend: maybe one endpoint or a PATCH-allowed field on `/users/me`. Frontend: a button + the existing tour-start call.

## Priority

P3. Post-launch UX polish. Pair with the synthetic-data reseed work (see `project_reseed_synthetic_data.md`) since they have overlapping UX (both are "I want to start fresh" affordances).
