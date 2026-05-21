# Notification system for sensitive operations — design

**Status:** design only, awaiting architect review. Not yet implemented.
**Date:** 2026-05-21.
**Source:** operator request 2026-05-21 (today's CAPTCHA session follow-on scope). Users should be notified when sensitive operations happen to their account or org (plan change, user deletion, MFA toggle, password change, role change, etc.) so they can react if it wasn't them. Per-category opt-out, with security events forcibly on.

## Goal

Build a generic notification substrate that any sensitive-op route can call to:

1. Persist a per-user notification record (read / unread, link to context).
2. Optionally dispatch an email through the existing Mailgun service.
3. Respect per-user, per-category email opt-out (except for security events, which are non-optional).

Bell icon + dropdown in the AppShell header lets the user see + manage their inbox. A new section in `/settings` lets them toggle email channels per category.

## Substrate audit (confirmed 2026-05-21)

* `audit_events` is the mature compliance log — 47 event types, immutable, with snapshots that survive user/org deletion. **Several sensitive ops are NOT yet audited**: password change, MFA enable/disable, email change, plan change, user reactivation. The notification spec assumes these audit writes get added in the same PR family (see "Audit gap closures").
* Email service (`backend/app/services/email_service.py`) supports `send_X_email(to, ...)` with HTML + text. Fire-and-forget via FastAPI `BackgroundTasks`, no queue, no retries. We follow this pattern; a real queue is out of scope.
* No per-user settings table exists today (`OrgSetting` is org-scoped). We add a new `user_notification_preferences` table.
* No in-app notification UI exists (no bell, no badge, no inbox). Greenfield.

## Schema

### New table: `notifications`

```sql
CREATE TABLE notifications (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL,
    category ENUM('security','account','org_admin','org_activity') NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    link_url VARCHAR(512) NULL,
    read_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_notifications_user_unread (user_id, read_at, created_at),
    KEY ix_notifications_event_type (event_type),
    CONSTRAINT fk_notifications_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
);
```

* `category` is a coarse grouping that maps 1:1 to a preference toggle.
* `event_type` mirrors the corresponding `audit_events.event_type` so an operator can correlate `notifications.id ↔ audit_events.id` via the event_type + timestamp + user_id.
* `link_url` is a relative app path (e.g. `/settings/security`) so the user can jump to the affected screen.
* `read_at` is the only mutable column; everything else is set at create-time.

### New table: `user_notification_preferences`

```sql
CREATE TABLE user_notification_preferences (
    user_id INT NOT NULL,
    email_security BOOLEAN NOT NULL DEFAULT TRUE,
    email_account BOOLEAN NOT NULL DEFAULT TRUE,
    email_org_admin BOOLEAN NOT NULL DEFAULT TRUE,
    email_org_activity BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    CONSTRAINT fk_unp_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
);
```

* One row per user; lazy-created on first access by `user_notification_preferences_service.get_or_default(user_id)`.
* `email_security` toggle is exposed to the user but **ignored at dispatch time** — security emails always send. UI shows the row as locked / read-only with explanatory tooltip. We keep the column to make a future "really, opt me out of security emails" exception possible without a migration.
* Defaults: security / account / org_admin = True, org_activity = False (the "who did what" feed is noisy by nature).

## Categories — what fires what

| Category | Event types | Hooked from | Notified user(s) |
|---|---|---|---|
| `security` | password.changed, mfa.enabled, mfa.disabled, email.changed, session.terminated | `routers/users.py:240,113`, `routers/auth.py:2065,2084` | Target user (self) |
| `account` | account.role_changed, account.deleted, account.merged | `routers/admin_roles.py`, `routers/admin_users.py:263,238` | Target user (the one whose role changed / was merged) |
| `org_admin` | org.renamed, org.data_reset, plan.changed | `routers/orgs.py:105,145`, `routers/org_data.py:141`, `routers/admin_subscriptions.py:131-139` | All org members (broadcast) |
| `org_activity` | (future / opt-in only) | — | All org members |

Note: `account.deleted` notifications are theoretically pointless — the user is gone, can't read the bell. But the **email** still fires (last-known address), which is the actual safety signal. Implementation: dispatch the email path, skip the row write when user is hard-deleted in the same transaction.

## API

### Customer-facing (authed)

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/v1/notifications?unread_only=false&limit=50&offset=0` | paginated list, newest first |
| `GET` | `/api/v1/notifications/unread-count` | `{ "count": <int> }` for the badge |
| `POST` | `/api/v1/notifications/{id}/read` | 204, idempotent |
| `POST` | `/api/v1/notifications/read-all` | 204, marks every unread for the user |
| `GET` | `/api/v1/users/me/notification-preferences` | the row (creating it lazily if absent) |
| `PUT` | `/api/v1/users/me/notification-preferences` | updates the row. `email_security` writes accepted but documented as "set but ignored at dispatch." |

No admin endpoints in scope today — operators don't manage individual users' inboxes.

## Service

`backend/app/services/notification_service.py`:

```python
async def dispatch_notification(
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    *,
    user_id: int,
    category: NotificationCategory,
    event_type: str,
    title: str,
    body: str,
    link_url: str | None = None,
    force_email: bool = False,
) -> None:
    """Persist a notification row + optionally schedule an email.

    Called from every sensitive-op route. Idempotency is the caller's
    problem (e.g. an "MFA enabled" event firing twice would write two
    notification rows — but the routes themselves shouldn't fire twice).

    Email dispatch decision:
      * security category → always send (ignoring user preferences)
      * other categories → send only if the user's preference column is True
      * `force_email=True` overrides the preference (reserved for future use)
    """
```

For broadcasts (org-wide events), a wrapper:

```python
async def dispatch_notification_to_org(
    session,
    background_tasks,
    *,
    org_id: int,
    category,
    event_type: str,
    title: str,
    body: str,
    link_url: str | None = None,
    exclude_user_id: int | None = None,  # don't notify the actor
) -> None:
    """Fan out a notification to every member of the org. Single
    transaction for all row writes; one email task per recipient.
    """
```

## Audit gap closures (must land in the same PR family)

The following routes write business state today but do NOT write audit_events. The notification spec depends on these being captured so the audit log and notifications agree. Adding the writes is in-scope as a prerequisite PR:

| Route | New event_type to write |
|---|---|
| `POST /users/me/change-password` | `user.password.changed` |
| `POST /auth/mfa/enable` | `user.mfa.enabled` |
| `POST /auth/mfa/disable` | `user.mfa.disabled` |
| `PATCH /users/me` (email field) | `user.email.changed` |
| `PUT /admin/orgs/{id}/subscription` (plan change) | `admin.org.plan.changed` |

These are the same hooks that subsequently call `dispatch_notification`. Audit + notification go together.

## Frontend

### `<NotificationBell />`

* Mount: AppShell header, beside or replacing `TrialBanner` placement. Most likely next to the user avatar dropdown (top-right).
* Data: SWR fetch `/api/v1/notifications/unread-count` on mount + every 60s + on window focus. No SSE today (out of scope).
* Render: bell icon. Badge with unread count when > 0. Click → opens a popover with the latest 10 notifications. Each row: category icon, title, time-ago, mark-read button. Footer link "View all" → `/settings/notifications`.

### `/settings/notifications` page

* Full list of notifications, pagination.
* Per-category email toggle (security row read-only).
* "Mark all as read" action.

### Settings nav

* Add a "Notifications" tab to `SettingsLayout.tsx`. Visible to all roles (not owner-gated).

## Email templates

New helper `send_notification_email(to, title, body, link_url)` in `email_service.py`. Subject: `[The Better Decision] {title}`. Body: rendered HTML matching the existing template style (light-mode, escaped strings, no inline `<style>`). When `link_url` is provided, body includes a "View in app" button → `{APP_URL}{link_url}`.

Examples:

* Password change → "[TBD] Your password was changed" / body: "Your password was changed at {time} from {ip}. If this wasn't you, reset your password immediately."
* MFA disabled → "[TBD] Two-factor authentication was disabled" / body: "Two-factor was turned off at {time}. If this wasn't you, sign in and re-enable MFA."
* Plan change → "[TBD] Your org's plan changed to {new_plan}" / body: "An admin changed {org_name}'s plan from {old_plan} to {new_plan} at {time}."

## Migration

* Single Alembic revision creates both new tables.
* No data backfill (existing users get defaults lazily).
* Downgrade: DROP both tables.

## Tests

### Backend

* `tests/services/test_notification_service.py`:
  * Dispatch creates a notification row.
  * Email scheduled when preference is True.
  * Email NOT scheduled when preference is False (except security: always scheduled).
  * Broadcast helper writes one row per org member and schedules one email each.
  * `account.deleted` path: email task scheduled even when the row write is skipped.
* `tests/routers/test_notifications.py`:
  * GET list with unread_only filter.
  * POST read is idempotent (double POST → still 204, read_at unchanged).
  * POST read-all marks all unread, leaves already-read rows alone.
  * Preference PUT accepts `email_security=False` (silent contract).
  * Cross-user isolation: user A's POST /read on user B's notification returns 404.
* `tests/routers/test_users_me_notification_preferences.py`:
  * Lazy create on first GET.
  * PUT round-trips values.
* For each new hook point (password change, MFA, etc.):
  * Action fires → audit row written + notification row written + email task scheduled.

### Frontend

* `tests/components/notifications/NotificationBell.test.tsx`:
  * Renders nothing when count is 0.
  * Badge appears when count > 0.
  * Polling refetch on focus.
  * Popover opens on click; rows render.
* `tests/app/settings-notifications-page.test.tsx`:
  * Per-category toggles round-trip via PUT.
  * Security row is rendered as disabled / locked.
  * Mark-all-read button calls the right endpoint.

## Rollout

Single feature, but big enough to split into a PR train:

1. **PR 1 — substrate.** Migrations + models + services + the GET/POST endpoints. No hooks yet. No frontend.
2. **PR 2 — audit gap closures.** Add the missing `audit_event` writes (password, MFA, email change, plan change). Does not call `dispatch_notification` yet.
3. **PR 3 — first hook batch (security category).** Wire `dispatch_notification` from password change, MFA enable/disable, email change. Bell icon + popover in AppShell.
4. **PR 4 — second hook batch (account + org_admin).** Plan change, role change, org rename, org data wipe.
5. **PR 5 — settings page.** `/settings/notifications` with per-category toggles + full list view.

Each PR self-contained and reverts cleanly. PRs 2-5 can ship over multiple days.

## Out of scope

* **Real-time push** — no SSE / WebSocket. Bell icon refetches on focus + 60s polling.
* **Notification retention / archival** — keep rows forever for now. A future PR can add a cleanup task.
* **Batching / digest** — two events in 5 seconds → two notifications + two emails. Smart batching is a future polish.
* **Email queue / retries** — follow the existing `BackgroundTasks` pattern. A real queue (RQ / Celery / arq) is a separate roadmap item.
* **SMS / push notifications** — email + in-app only.
* **Org-scoped opt-out** — preferences are per-user. No "this org doesn't email anyone" knob.
* **Audit ↔ notification correlation UI** — operator-facing tooling to see "this audit event → these notifications" is not built. The `event_type + user_id + created_at` correlation is enough for ad-hoc DB queries.
* **Account deletion notification UI** — the in-app row is skipped (no user to read it), but the email still fires from the last-known address. No special UI.

## Open questions for architect

1. **Notification row writes for `account.deleted` (target user gone) — confirm the path that skips the row but still sends the email.** Spec recommends: the auth context for the route is the *admin* doing the delete; we look up the target user's email FIRST, then delete, then `background_tasks.add_task(send_notification_email, target_email, ...)`. Architect: agree, or is there a transactional safety concern?
2. **AppShell mount: replace TrialBanner location or sit beside it?** Once spec #12 lands and `BILLING_UI_ENABLED=false`, TrialBanner doesn't render anyway in prod, so the slot frees up. Spec leans toward "beside the avatar dropdown."
3. **Per-event title/body strings — i18n posture.** Spec assumes English strings hardcoded in `notification_service.py` per event type. Roadmap P2 has i18n; consistent with the rest of the codebase.
4. **`email_security=False` semantic.** Spec says we accept the write but ignore at dispatch. Alternative: reject the write with 400. Spec recommends accept-but-ignore so the API surface is uniform; the UI shows the row as locked.
5. **Broadcast email blast on org_data_reset.** That event fires once but emails N org members. Mailgun rate / reputation impact for N>20 should be modeled. Architect: any concerns, or is this small enough to ignore?

## Naming + cross-references

* Backend: `backend/app/models/notification.py`, `backend/app/services/notification_service.py`, `backend/app/routers/notifications.py`, `backend/app/routers/users_me_preferences.py` (extends existing `users.py` if simpler).
* Frontend: `frontend/components/notifications/NotificationBell.tsx`, `frontend/components/notifications/NotificationRow.tsx`, `frontend/app/settings/notifications/page.tsx`.
* `[[project_bot_signup_captcha]]` — same write-on-event pattern (synchronous side-effect into a separate table).
* `[[reference_environment_doc]]` — no new env vars in this spec (Mailgun is already configured).
