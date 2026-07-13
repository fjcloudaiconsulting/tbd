# Notifications completeness — design

**Date:** 2026-07-13
**Branch:** `feat/notifications-completeness`
**Status:** approved (brainstorm)

## Problem

The notification subsystem shipped incrementally (#430 settings UI, #436 hooks,
#533 best-effort dispatch guard, plus the email fanout). Three loose ends remain:

1. **In-app (bell) toggles are not surfaced.** `NotificationPreferences` carries
   all eight fields (`email_*` + `in_app_*`) and the GET/PUT round-trips them, but
   `frontend/app/settings/notifications/page.tsx` only renders the four **email**
   toggles. The `in_app_*` fields ride along untouched on save, so a user cannot
   control which categories reach their bell — even though dispatch honours those
   columns for Account / Org-admin / Org-activity.
2. **Two bare `dispatch_notification` call sites** write an in-app row + commit
   without the #533 best-effort guard, so a flush/commit failure can 500 the
   parent operation:
   - `backend/app/routers/admin_orgs.py:963` — `account.role_changed` to the
     affected member.
   - `backend/app/services/ai_dispatch.py:701` — AI soft-cap warning fanout to
     org admins.
3. **Stale docstrings.** `notification_service.py` lines 7–32 still describe the
   email fanout and the `/settings/notifications` UI as future "PR5" work; both
   have shipped.

Additionally, while surfacing in-app toggles, a latent inconsistency surfaced:
`update_preferences` force-coerces `email_security=True` (defense-in-depth) but
**not** `in_app_security`. Since dispatch treats in-app `security` as force-on
(the category is deliberately absent from `_IN_APP_PREF_FIELD`), an
`in_app_security=false` would persist yet be silently ignored — a misleading
stored value and a toggle that would lie if rendered live.

## Goal

Finish the notification subsystem cleanly: users control both channels per
category, the security lock is honest on both channels, and every in-app write is
resilient. No new categories, no new notification events.

## Design

### Part A — Surface in-app toggles (frontend)

`frontend/app/settings/notifications/page.tsx`:

- Replace the email-only `CATEGORIES` array with a per-category descriptor that
  carries **both** channel keys and the lock flag:

  ```ts
  { id, title, description, emailKey, inAppKey, locked? }
  ```

  Categories (display order): Security (locked), Account, Organization (admin),
  Organization activity.

- Render a **two-column matrix**: one card, a header row with **Email** / **In-app**
  labels above the switch columns, then one row per category with two switches
  (grid: category-info column + two fixed switch columns). The Security row shows
  both switches disabled + on, with the existing "(always on)" affordance; the
  copy notes it applies to both channels.

- Copy: card title becomes **"Notifications"**; the intro is rewritten to cover
  both channels; the "these settings only affect email" line is removed. The
  **Organization activity** description must also be corrected — the current
  "Quiet by default, turn it on to follow along" copy contradicts the 2026-07-04
  opt-out flip (`_default_preferences` now defaults `org_activity` **ON**); rewrite
  it to reflect default-on.

- The Security row's **in-app** switch renders **hardcoded on** (disabled + on),
  NOT bound to `prefs.in_app_security`, so it can never display a stale persisted
  `false` (see Part B read-honesty). The email Security switch stays as today
  (bound but locked, already read-coerced).

- `toggle(key)` already works for any of the eight keys
  (`{ ...current, [key]: !current[key] }`); Save is unchanged (PUT sends the full
  shape). No API change.

- Accessibility: each switch gets a distinct `aria-label`
  (e.g. "Account email notifications" / "Account in-app notifications"); the
  column headers label the switch columns; all existing design tokens
  (`bg-success`, `bg-border`, focus ring, etc.) are preserved. The layout stays
  usable on narrow screens (switches are small; the two switch columns remain
  fixed-width beside the description).

### Part B — Honest in-app security lock (backend)

`backend/app/services/notification_service.py` `update_preferences`:

- Add `row.in_app_security = True` immediately beside the existing
  `row.email_security = True` backstop, so a stray `in_app_security=false` can
  never persist.
- **Also add the matching read backstop in `get_preferences`** (mirror the
  existing `email_security` read coercion): if a loaded row has
  `in_app_security=false`, set it True and flush. The old `update_preferences`
  accepted and persisted `in_app_security=false` (the route rejects only
  `email_security`), so **pre-existing production rows can already hold `false`**.
  The read backstop self-heals them lazily on next GET — no data migration
  needed — and guarantees the GET the settings page consumes never returns a
  stale `false`.
- Update the docstrings (both functions) to state that **both** security channels
  are force-on on read and write.

**Route-level 400:** deliberately **not** added. The email side rejects
`email_security=false` with `400 security_emails_required` at the route *and*
coerces at the service. For in-app we do coerce-only: the UI never sends `false`,
and dispatch already ignores the column, so a non-UI client sending `false` is
silently coerced to `True` (200 OK) rather than rejected. This is a
symmetry-vs-simplicity choice; flipping to a symmetric `400
security_in_app_required` is a one-route-check addition if strictness is
preferred. **Decision: coerce-only.**

### Part C — Guard the two call sites + email + docstring cleanup

**`admin_orgs.py` role-change (line ~963).** The member mutation is already
committed at line 905, so the dispatch block is a separate best-effort
transaction — safe to guard:

- Replace `dispatch_notification(...) + await db.commit()` with
  `dispatch_notification_best_effort(...)` (owns its own commit, rolls back only
  the notification insert on failure).
- Add a **best-effort email to the affected member** (category `account`),
  reusing the existing `_tpl_account_role_changed` title/body/link (no new
  template). The email respects the member's `email_account` preference and is an
  independent channel (sent regardless of the in-app write outcome), mirroring the
  org-fanout email pattern. Implementation may either promote
  `_send_notification_email_best_effort` to public use or add a thin single-user
  helper; either is acceptable as long as the category-preference check is
  honoured.
- **Greenlet safety (required):** `dispatch_notification_best_effort` commits,
  which with `expire_on_commit=True` expires `target`. The email call therefore
  MUST use pre-snapshotted plain strings for the recipient email + title/body/link
  — reuse `member_payload["email"]` (already snapshotted at line ~909) and locals
  captured from `_tpl_account_role_changed` **before** the dispatch. Do NOT read
  any `target.*` attribute after the dispatch returns, or the expired-attribute
  lazy load raises `MissingGreenlet` (cf. the audit-on-failure snapshot pattern).

**`ai_dispatch.py` soft-cap warning (line ~701).** A hand-rolled org-admin fanout
loop with no savepoint protection. Guard it with the established per-recipient
`db.begin_nested()` savepoint pattern (rollback-before-log), keeping the single
trailing `db.commit()`. **In-app only — no email added** (emailing admins on
soft-cap is a separate product decision, out of scope). If reviewers prefer, this
site could instead be routed through the existing
`dispatch_notification_to_org_admins` helper, but that would add admin emails and
re-query the recipient set, so it is left as a noted alternative, not the plan.
Note: `_list_org_admin_user_ids` has **no `is_active` filter** (unlike the
org-admins helper) — keeping the manual wrap preserves current behavior (inactive
admins still warned); add a one-line comment marking that divergence as
intentional. Confirmed safe: no uncommitted business/cost state is pending on
`db` at either soft-cap call site (the ledger row self-commits upstream), so the
savepoints + single commit introduce no premature-commit risk.

**Docstring cleanup.** Remove/rewrite the stale PR3/PR4/PR5 references in
`notification_service.py` (module docstring lines 7–32, and the "Preference
contract (PR3)" label ~line 254) so they describe the shipped state (email fanout
live, settings UI live) rather than future PRs.

## Testing

**Frontend** — **extend** the existing `tests/app/settings-notifications-page.test.tsx`
(do not replace; it already seeds `in_app_*` and uses `"{title} email
notifications"` aria-labels, so keep that label scheme and add `"{title} in-app
notifications"` for the new column):
- Renders all eight switches (four categories × two channels).
- Both Security switches are disabled and on; the in-app Security switch shows on
  even when the seeded prefs carry `in_app_security=false` (hardcoded-on render).
- Toggling an in-app category (e.g. Org-admin in-app) flips it and Save PUTs the
  full eight-field shape with that field changed and the others intact.
- Load/save error and success states preserved.

**Backend:**
- `update_preferences` coerces `in_app_security=True` even when the payload sends
  `false` (mirrors the existing `email_security` backstop test).
- `get_preferences` read backstop: a row persisted with `in_app_security=false`
  is returned as `True` (self-heal on read).
- Role-change site: emails the affected member (best-effort, pref-respecting) and
  swallows an in-app dispatch failure without propagating (parent op still
  returns 200; role change stays committed). Assert no `MissingGreenlet` (email
  reads snapshotted strings, not `target.*`).
- Soft-cap site: a per-recipient dispatch failure is swallowed (savepoint rolled
  back, loop continues, flow not broken).
- Note: the existing `test_dispatch_force_writes_for_security_category` seeds
  `in_app_security=False` via `update_preferences`; after Part B that column
  coerces to `True`. The test still passes (security dispatch ignores the column;
  the other three `in_app_*=False` carry the intent) — add a comment so the
  softened premise is intentional, or assert the coercion explicitly.

## Out of scope

- Any new notification category or event type.
- Emailing org admins on the AI soft-cap warning.
- Fanning out role changes to org owners/admins (affected member only).
- Route-level `in_app_security` 400 (coerce-only, per decision above).
