---
name: Remove vs Deactivate org member — investigate semantics
description: User-reported 2026-05-14 — clicking "Remove" on org member soft-deactivates instead of removing. Decide intent and align endpoint + UI copy.
type: project
originSessionId: e75406f8-d01b-42d2-aa05-cdc574be2d1a
---
# Remove vs Deactivate org member

## Observation (2026-05-14, fjorge)

Tried to delete user `jorge.flamarion` (email `jorge.flamarion@icloud.com`) from Org id=1 via the admin UI. Result: user was deactivated, not removed. If the UI exposes both "Deactivate" and "Remove" actions, "Remove" should effectively remove the membership.

## Context

L4.4 shipped the superadmin org-member management slice in PR #221, exposing `GET/PATCH/DELETE /api/v1/admin/orgs/{org_id}/members` and a frontend Members section under `/admin/orgs/[id]` with per-row Role / deactivate-reactivate / remove (`ConfirmModal`) controls. Guard rule already in place: cannot remove self, superadmin, or last active OWNER.

## What to investigate

1. **What does `DELETE /api/v1/admin/orgs/{org_id}/members/{user_id}` actually do today** — is it soft-deletion (`is_active=False`) on the user row, or does it remove the org-membership association while keeping the user? File: `backend/app/services/admin_org_members_service.py`. The likely culprit: the "Remove" handler maps to the same code path as "Deactivate" by design (preserves user history) rather than detaching from the org.
2. **What's the correct product semantic** — three options to evaluate:
   - **(a)** Remove = detach from org (drop org_membership row), preserve user identity for cross-org history. Cleanest if we ever introduce true multi-org membership.
   - **(b)** Remove = hard-delete user row + cascade to all their org-scoped data (transactions, accounts, etc.). Aggressive; rare in SaaS products.
   - **(c)** Remove = soft-delete (the current behavior). Then the UI button should be labeled "Deactivate", and "Remove" should not exist as a separate option.
3. **Audit trail expectations** — whatever semantic we pick, `audit_events` should already capture the action via L4.7's audit hook. Confirm the event_type names match the user-facing semantic.

## Constraints + prior decisions to honor

- `audit_events` has snapshot columns (`actor_email`, `target_org_name`) so audit rows survive user deletion — that's design intent from L4.7. Doesn't dictate the remove semantic; it just means option (b) is feasible without losing audit.
- Pre-launch state: no backcompat shims (per user rule), so we can change the contract freely.
- Currently a single user belongs to a single org (no multi-org membership). So option (a) and option (b) differ only in whether the user row survives.

## Effort estimate

S to M. Pure backend service decision + frontend label tweak. No migrations unless we choose option (a) AND a multi-org future requires a separate membership table.

## Priority

P3. Not urgent, not a launch blocker. Pick up after L5.2a apex split fully cuts over.

## How to apply

When picking this up: start by reading `admin_org_members_service.py`'s DELETE path to confirm which option (a)/(b)/(c) we're currently in. Then bring product semantic back to fjorge before touching code.
