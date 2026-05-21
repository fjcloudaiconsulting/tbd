# Announcement banner system — design

**Status:** design only, awaiting architect review. Not yet implemented.
**Date:** 2026-05-21.
**Source:** operator request 2026-05-21 (today's CAPTCHA session, follow-on scope). Operator wants a way to post promo / maintenance / general info notices visible across the app, with per-user dismissal that survives device changes.

## Goal

Build a substrate for operator-authored announcements that:

* Stack above the main content on every authed page.
* Carry one of three severities — `info` (neutral), `promo` (highlight), `maintenance` (force-shown, not dismissible).
* Support optional `start_at` / `end_at` scheduling plus a hard `is_active` kill switch.
* Allow per-user dismissal for `info` / `promo`; persist dismissals in DB so a user who dismisses on desktop doesn't see the banner on mobile.
* Coexist when multiple announcements are active — render as a vertical stack, severity-ordered (maintenance on top, then promo, then info, then by `created_at`).
* Provide a `/system/announcements` admin CRUD for operator authoring.

## Substrate audit (what does NOT exist today, confirmed 2026-05-21)

* No generic banner stack. `TrialBanner.tsx` and `SsoStepupErrorBanner.tsx` exist but are single-purpose.
* No dismiss-with-persistence pattern in production. Closest precedent is `User.onboarded_at` (one-time lifecycle flag), not a per-item dismiss.
* No operator content-authoring UI (no markdown editor, no rich-text component integrated). All admin screens today are structured forms / KPIs.
* `OrgFeatureOverride` is the wrong fit for content (designed for booleans). Need a dedicated table.
* `OrgSetting` is org-level only. Per-user dismissal needs a new join table.

So this is mostly greenfield, riding only the AppShell layout slot.

## Schema

### New table: `announcements`

```sql
CREATE TABLE announcements (
    id INT NOT NULL AUTO_INCREMENT,
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    severity ENUM('info', 'promo', 'maintenance') NOT NULL DEFAULT 'info',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    start_at DATETIME NULL,
    end_at DATETIME NULL,
    created_by_user_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_announcements_active_window (is_active, start_at, end_at),
    CONSTRAINT fk_announcements_created_by FOREIGN KEY (created_by_user_id)
        REFERENCES users (id) ON DELETE SET NULL
);
```

Open: enum vs short string. Existing models use SQLAlchemy Enum with `values_callable=lambda x: [e.value for e in x]` (see CLAUDE.md). Pick the same pattern.

### New table: `user_dismissed_announcements`

```sql
CREATE TABLE user_dismissed_announcements (
    user_id INT NOT NULL,
    announcement_id INT NOT NULL,
    dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, announcement_id),
    CONSTRAINT fk_uda_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_uda_announcement FOREIGN KEY (announcement_id)
        REFERENCES announcements (id) ON DELETE CASCADE
);
```

The composite PK gives us idempotent dismiss writes (a double-dismiss POST is a no-op).

## API

### Customer-facing

| Method | Path | Auth | Returns |
|---|---|---|---|
| `GET` | `/api/v1/announcements` | authed user | active + visible-to-user announcements, ordered. Empty array when none. |
| `POST` | `/api/v1/announcements/{id}/dismiss` | authed user | `204 No Content`. Idempotent. Maintenance severity → `400` with `code=announcement_not_dismissible`. |

`GET /announcements` filter logic:

```python
# Active window:
is_active = TRUE
AND (start_at IS NULL OR start_at <= NOW())
AND (end_at IS NULL OR end_at > NOW())

# Visibility: maintenance is always shown; info/promo hidden if user dismissed them.
AND (severity = 'maintenance' OR id NOT IN (
    SELECT announcement_id FROM user_dismissed_announcements WHERE user_id = ?
))

# Ordering:
ORDER BY
    FIELD(severity, 'maintenance', 'promo', 'info'),
    created_at DESC
```

### Admin / operator

Under `/api/v1/admin/announcements`:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/admin/announcements` | superadmin | list ALL announcements regardless of active state, scheduled, expired. |
| `POST` | `/admin/announcements` | superadmin | create. Body: `{ title, body, severity, is_active, start_at?, end_at? }`. |
| `PATCH` | `/admin/announcements/{id}` | superadmin | edit any field. |
| `DELETE` | `/admin/announcements/{id}` | superadmin | hard delete. Cascades to `user_dismissed_announcements`. |

Open: superadmin-only or any admin role? Spec leans superadmin since this is global content, not org-scoped (see "Open questions for architect").

## Frontend

### New component: `<AnnouncementBar />`

* Mount: a new section in `frontend/components/AppShell.tsx`, BETWEEN the header (`AppShell.tsx:461`) and the main content area. Full-width, stacks vertically.
* Data: SWR fetch from `/api/v1/announcements`. Cache key `/announcements`. Revalidate on focus + route change. No polling (next page load is good enough).
* Render: one row per announcement, severity-styled. Dismiss button on `info` / `promo`; absent on `maintenance`.
* Dismiss handler: optimistic local removal, `POST /announcements/{id}/dismiss`, on failure revert and toast.

### New admin page: `/system/announcements/page.tsx`

* List of all announcements with status badges (active / scheduled / expired / inactive).
* "New announcement" button → modal with: title, body (textarea — see Markdown question), severity dropdown, optional start_at / end_at datetime pickers, is_active toggle.
* Edit + Delete actions per row.
* Audit event written for each create / edit / delete (event types `system.announcement.created` / `.updated` / `.deleted`).

### Styling

* `info`: neutral border + subtle background, dismiss "x".
* `promo`: accent-color border + slightly louder background, dismiss "x".
* `maintenance`: amber / caution palette, no dismiss button. Icon (e.g. wrench) to telegraph "this is operational, not advertising."

## Migration

* Single Alembic revision creates both tables. Reserves the next migration slot.
* No data backfill — both tables start empty.
* Downgrade is `DROP TABLE` both. Safe because no other model FKs into them yet.

## Tests

### Backend

* Model unit: severity enum values match the DB schema, `created_by_user_id` cascade-set-null behavior on user delete.
* Router (`tests/routers/test_announcements.py`):
  * `GET /announcements` filters correctly (active, schedule window, dismiss-exclusion-for-non-maintenance, severity ordering).
  * `POST /dismiss` is idempotent (double POST → still 204, single row in table).
  * Maintenance dismiss POST → 400 with `code=announcement_not_dismissible`.
  * Admin CRUD endpoints require superadmin (any other role → 403).
  * Audit events written for create / update / delete.

### Frontend

* `AnnouncementBar.test.tsx`:
  * Renders nothing when API returns empty array.
  * Stacks multiple announcements in severity-then-created_at order.
  * Dismiss button absent on maintenance severity.
  * Clicking dismiss removes the row immediately + POSTs to backend.
  * Network failure on dismiss → row re-appears + toast.
* `system-announcements-page.test.tsx`:
  * Create form posts the right payload.
  * Edit modal pre-fills existing values.
  * Delete confirms before firing.

## Rollout

1. Land the spec PR (this file). Architect review.
2. Build backend: migration + model + service + router + tests. Single PR.
3. Build admin CRUD frontend: `/system/announcements`. Could split from #4.
4. Build customer-facing `<AnnouncementBar />`. Could pair with #3.
5. Once shipped, the operator posts the first real announcement (probably the "we are pre-launch, beta feedback welcome" promo or similar).

No env-var kill switch needed — `is_active=FALSE` on every announcement is the operational off-switch. The whole substrate is inert when the table is empty.

## Out of scope

* **Org-scoped announcements** — "show this only to plan=Pro orgs" / "show this only to org_id=X". Today everything is global. Easy to extend later (add `target_plan_slug` and `target_org_ids` columns), but no current use case.
* **Role-scoped announcements** — "only show this to owners." Same reasoning; extend later if asked.
* **i18n / translations** — single text body, no localization. Roadmap P2.
* **Markdown body parsing** — see Open questions. Spec recommends plain text + safe-href URLs (auto-linkify) for now.
* **Real-time push** — no SSE, no WebSocket. Next page load picks up the new announcement. Acceptable for the use cases listed (planned maintenance is scheduled hours in advance; promos aren't time-critical).
* **Engagement metrics** — dismiss counts, view counts. The data is in the DB if we ever want it but no UI exposes it today.

## Open questions for architect

1. **Markdown vs plain text in `body`.** Plain text is safest (no XSS surface, no parsing dependency). Markdown is friendlier for operators who want emphasis / links. Spec recommends: plain text + auto-linkify URLs via a tiny client-side helper (no external markdown library). Architect: agree, or push toward `react-markdown` with a strict allowlist?
2. **Superadmin-only vs any admin role for `/admin/announcements`?** Today's superadmin gate (`is_superadmin=True`) is the right ceiling for global content. Architect: any reason to lower it?
3. **`announcement_id` as integer PK vs UUID/slug.** Integer follows the rest of the schema. UUID would be cleaner for external URL sharing but no such surface today. Spec defaults to integer.
4. **Title — required or optional?** Spec marks required for UX clarity (so the list page has a readable handle). Architect: agree, or allow body-only announcements?
5. **`maintenance` severity always-show behavior** — should it bypass the dismiss UI entirely (no "x" button rendered), or render the button but reject the POST? Spec recommends "no button rendered" for unambiguous UX.

## Naming + cross-references

* Backend module: `backend/app/models/announcement.py`, service `backend/app/services/announcement_service.py`, router `backend/app/routers/announcements.py` + `backend/app/routers/admin_announcements.py`.
* Frontend component: `frontend/components/announcements/AnnouncementBar.tsx` + `AnnouncementRow.tsx`.
* Frontend admin page: `frontend/app/system/announcements/page.tsx`.
* `[[project_bot_signup_captcha]]` — same admin-control-plane pattern (operator-driven kill switch, surfaces via auth-touching endpoint).
* `[[reference_do_spec_sync.md]]` — no new env vars here, so no `.do/app.yaml` change.
