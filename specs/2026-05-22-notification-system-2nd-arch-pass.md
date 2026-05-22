# Notification system — 2nd architecture pass (delta spec)

**Status:** delta. Supplements the original; does NOT replace it. Once these resolutions are merged, the primary spec (`2026-05-21-notification-system-sensitive-ops.md`) is implementation-ready.
**Date:** 2026-05-22.
**Parent spec:** [2026-05-21-notification-system-sensitive-ops.md](2026-05-21-notification-system-sensitive-ops.md)
**Why this exists:** the architect approved the direction on 2026-05-21 but flagged five open questions (and a generic "second pass before implementation"). PR #332 merged the parent spec as-is, locking the resolutions to date. This doc closes the remaining questions, surfaces new gaps found while walking the codebase, and adjusts the rollout train where needed.

Read order: parent first, then this. Sections here OVERRIDE the parent only where explicitly stated as "Override".

---

## 1. `account.deleted` — the email-still-fires path

**Question.** With the row skipped (no user to read the bell), how does the email get a recipient address transactionally, and in what order do the three sides effects fire (delete + audit + email)?

**Decision.** Use the snapshot the audit row already carries. No extra plumbing.

**Concrete sequence** (admin hard-delete at `routers/admin_users.py:376-490`):

1. Service call `admin_users_service.delete_user(...)` returns `result["snapshot"]` (includes `email`, `id`, `org_id`, `username`) BEFORE row is gone. Already implemented.
2. `await db.commit()` — user row is gone.
3. `await audit_service.record_audit_event(event_type="admin.user.deleted", ..., detail={"target_email": snapshot["email"], ...})` — runs against a separate session via `session_factory`. The audit row persists even if step 4 explodes.
4. AFTER audit commit succeeds, `background_tasks.add_task(send_account_deleted_email, snapshot["email"], snapshot["username"], actor_email, request_ip)`.
5. Skip `dispatch_notification` for the deleted user. No row to insert (FK cascade would kill it anyway).

**Why this order matters.**
- The email is the only customer-facing signal for a deletion. It MUST follow a successful delete + audit. If we send first and delete fails, we've lied to the user. The current code already commits the delete before the audit row write — same window for the email schedule. Acceptable.
- BackgroundTasks runs AFTER response is returned, AFTER any DB error inside the request handler is observable. Failing email does not roll back the delete (correct — delete already committed). Failed email is logged via existing `email_send_failed` structlog event.

**New helper.** `email_service.send_account_deleted_email(to: str, username: str, actor_email: str, ip_address: str | None) -> bool`. Body uses the same hardcoded English template style as `send_trial_expiring_email`. Subject: `[The Better Decision] Your account has been deleted`.

**Files touched.**
- `backend/app/routers/admin_users.py:467-489` — add `background_tasks.add_task(...)` AFTER the audit record call.
- `backend/app/services/email_service.py` — new `send_account_deleted_email` function.

**GDPR posture.** Sending one final transactional email to a last-known address for a security event the user (or admin acting on their behalf) just executed is legitimate-interest, not marketing. It mirrors the password-change pattern. No retention concern: we are not storing the email beyond the audit snapshot, which already exists for compliance reasons. State this explicitly in the email helper docstring so we don't regress.

**Override of parent ambiguity.** Parent says "fire email path, skip row write". Parent says BackgroundTasks "after successful commit" but does not nail down which commit. **This delta locks the rule: "After audit commit only. If the audit commit fails, no email task is enqueued."** Reason: audit failure must be loud. We never want a user-facing notification (email) to outlive a missing audit row, because then the only forensic trail for the deletion is the email itself, and we can't correlate it back to who acted or why. The audit gap becomes the recoverable signal: the user sees the delete succeed without the email, ops sees the missing audit row in `/admin/audit`, and we can manually reissue the email after triage.

**Hard rules (no exceptions).**
- The `background_tasks.add_task(send_account_deleted_email, ...)` call is placed AFTER `await db.commit()` for the audit row. Not before, not in parallel, not in a `finally`.
- If the audit insert or its commit raises, the email task is NEVER added. The exception propagates; the user-delete commit already happened (step 2), so the delete still succeeded from the user's perspective. The audit gap is the signal we triage on.
- No `try/finally` wrapper that enqueues the email regardless of audit outcome. The enqueue lives on the success path only.
- No enqueue between the user-delete commit and the audit commit. If we crash between steps 2 and 3 we ship neither audit nor email — that's strictly better than email-without-audit.

**Code sketch.**

```python
# delete user (commit)
await db.delete(user); await db.commit()
# audit (commit). If this raises, no email is scheduled.
audit_row = AuditEvent(...); db.add(audit_row); await db.commit()
# only after audit commit succeeds, enqueue email task
background_tasks.add_task(send_account_deleted_email, snapshot_email, ...)
```

---

## 2. AppShell mount — bell beside avatar dropdown

**Question.** Replace TrialBanner slot (post-#330) vs separate slot near avatar?

**Decision (confirms parent).** Bell goes beside the avatar dropdown in `AppShell.tsx:460-473`, in the same `<div className="flex items-center gap-3">` that already holds TrialBanner / docs link / theme toggle.

**Reasoning — why NOT the TrialBanner slot.**
- TrialBanner is conditional content (trial countdown / upgrade CTA). When `BILLING_UI_ENABLED=false` (#330), it renders `null`. Putting the bell in that slot ties two unrelated features together and creates a stale-spec hazard: anyone touching billing's render gate could accidentally hide the bell.
- Trial copy is promotional / billing-specific. Bell is operational / security-facing. Different cognitive bucket.
- TrialBanner is a *banner*, not an *icon*. Bell is icon-shaped. Different visual primitive.

**Concrete placement.** Bell renders second in the row, right after TrialBanner. Order in the flex container: `[TrialBanner] [AddTransactionCta?] [NotificationBell] [DocsLink] [ThemeToggle] [AvatarMenu — TBD]`.

Note: AppShell currently does NOT render an explicit AvatarMenu inside this header. The "avatar dropdown" language in the parent spec is forward-looking. Practical anchor for v1: position the bell where the avatar dropdown WILL live, which is the rightmost icon position. The bell + popover ships ahead of any avatar menu refactor without depending on it.

**Cross-impact with #331 (AnnouncementBar).** AnnouncementBar mounts BETWEEN header and main, not IN the header. Per `2026-05-21-announcement-banner-system.md:124`. No conflict. The bell stays in the header row; announcements stack below the header above main content.

**Files touched.**
- `frontend/components/AppShell.tsx:461` — insert `<NotificationBell />` in the flex row.
- `frontend/components/notifications/NotificationBell.tsx` — new component.
- `frontend/components/notifications/NotificationPopover.tsx` — new component.

---

## 3. i18n posture — hardcoded English, but with an extraction seam

**Question.** Hardcoded English title/body for v1 vs key-driven, i18n-ready?

**Decision.** Hardcoded English (confirms parent). BUT: do NOT scatter the strings across `dispatch_notification(...)` call sites. Instead, centralize all titles + bodies in a single Python module so a future migration to a `key + variables` model is a mechanical refactor.

**Concrete shape.**

```python
# backend/app/services/notification_templates.py
def render_password_changed(time: datetime, ip: str | None) -> tuple[str, str]:
    """Return (title, body). v1 = English. Future: take a locale arg."""
    title = "Your password was changed"
    body = (
        f"Your password was changed at {time:%Y-%m-%d %H:%M UTC} "
        f"from {ip or 'an unknown location'}. "
        "If this wasn't you, reset your password immediately."
    )
    return title, body
```

Every sensitive-op hook calls one of these renderers, never builds the string inline. The `notification_service.dispatch_notification(...)` signature stays string-typed (`title: str, body: str`) — the template module is the only thing that knows English vs not. Migration to i18n later: change `render_password_changed(time, ip)` to `render_password_changed(time, ip, locale="en")` and back the body with a key lookup. Call sites don't move.

**Email subject + body** use the same module — single source of strings per event. Email-only flourishes (e.g. the "View in app" button HTML) live in `email_service.send_notification_email`.

**Locale resolution (future, out of scope for v1).** `users` table has no `locale` column today; adding one is the natural pairing for any i18n PR. Document this as the migration trigger: when the `User.locale` field exists, the template module accepts it as an arg.

**Override of parent.** Parent spec gives example strings inline in the "Email templates" and "Categories" sections. This delta locks: those examples become docstrings on the template-module functions, NOT inline in router code or in `dispatch_notification` invocations.

**Files touched.**
- `backend/app/services/notification_templates.py` — new module, one function per event_type.

---

## 4. `email_security=false` — consistency audit

**Question.** Already locked to reject-with-400. Spec self-consistent?

**Decision.** Yes, with one stale reference to clean up — but per task rules I'm flagging it here, not editing the parent.

**Audit of the parent spec.**
- Architect resolutions block (top of file) — says "REJECT with 400 (or coerce silently to True)". The "(or coerce ...)" parenthetical is a stale ambiguity. Locked behavior is reject-with-400 only.
- `user_notification_preferences` schema comment — locks to reject-with-400 cleanly. Good.
- API table — `PUT` row locks to reject-with-400. Good.
- Backend test bullet — locks to reject-with-400. Good.
- Open questions section — `email_security=False semantic` — "Reject with 400 ... (or coerce to True). NOT accept-but-ignore." Same stale parenthetical as the top.

**Revisit before implementation.** The parent spec has a parenthetical "(or coerce silently to True)" in two places that disagrees with the locked behavior elsewhere. Implementation should treat the locked behavior as canonical: 400 `{"code": "security_emails_required"}`. The coerce branch should NOT be implemented. (Surfacing per task rules — not editing the parent.)

**Suggested PUT request shape.** Pydantic schema rejects `email_security=False` at validation time, NOT in the route body. This keeps the 400 deterministic and the response shape uniform with other 400 envelopes already in the codebase. Concretely:

```python
class NotificationPreferencesUpdate(BaseModel):
    email_security: Literal[True] = True
    email_account: bool
    email_org_admin: bool
    email_org_activity: bool
```

**Why `Literal[True]` alone is not enough.** Pydantic v2's default `literal_error` body looks like `{"type": "literal_error", "loc": [...], "msg": "Input should be True", "input": false, "ctx": {"expected": "True"}}`. That body does NOT include the stable `security_emails_required` code the frontend keys off. The code constant is a contract; the raw Pydantic shape is not.

**Validator mechanism — locked.** Use a Pydantic v2 `field_validator(mode="before")` on `email_security` (or a `model_validator(mode="before")` on the model — either works; field-level is the smaller surface). When `email_security` is present in the input and its value is anything other than `True`, the validator raises `ValueError("security_emails_required")`. The error envelope is shaped to match the existing 400 contract used by `routers/auth.py:241-247` (the `captcha_failed` 400) — `HTTPException(status_code=400, detail={"code": "security_emails_required", "message": "Security emails are required and cannot be disabled."})`. The implementer copies that exact shape from auth.py:241-247.

The route-level handler catches the `ValueError` from validation and re-raises as the 400 above. (Alternative: let the validator raise `HTTPException` directly — Pydantic supports this in v2 since validators run inside the request lifecycle. Implementer picks whichever is cleaner against existing patterns at the time.)

**Validator sketch.**

```python
@field_validator("email_security", mode="before")
@classmethod
def _security_required(cls, v):
    if v is not True:
        raise ValueError("security_emails_required")
    return v
```

**Reference for the 400 envelope shape.** `backend/app/routers/auth.py:241-247` — the captcha_failed 400 raised by `/api/v1/auth/register`. Copy the `HTTPException(status_code=400, detail={"code": ..., "message": ...})` structure verbatim. Other call sites in the codebase using the same envelope: `backend/app/routers/admin_users.py:434`, `backend/app/routers/org_data.py:73`, `backend/app/routers/import_router.py:48`, `backend/app/routers/org_members.py:63`.

---

## 5. Mailgun fanout for org-wide events

**Question.** Org-wide events (plan change broadcast to N users). Rate limiting, sender reputation, BCC vs N individual sends, opt-out semantics.

**Decision matrix.**

| Concern | Decision | Reasoning |
|---|---|---|
| BCC vs N individual sends | **N individual sends** (one Mailgun POST per recipient) | Per-user opt-out is the whole point; BCC ignores preferences. Per-user unsubscribe headers (future) need individual messages. Mailgun's per-message metadata (tags, custom-variables) becomes per-user. Operationally simpler. |
| Rate limiting (app-side) | **None today.** Org cardinality is small (typical N < 20). | Adding rate limiting before we have a rate problem is premature. Re-evaluate at N > 100 / org. Recipient count IS logged per dispatch (parent spec, architect resolution). |
| Mailgun rate limit (Mailgun-side) | **Trust it.** Mailgun's free tier is 100/hr, paid is much higher. | If we hit it we'll see it in `email_send_failed` log. Containment: the broadcast call returns even if some sends fail; failures are logged, not raised. Per-user retry is out of scope. |
| Sender reputation | **No new risk.** Same `from` address, same DKIM. | Burst from 1 to N emails on a plan change is not unusual transactional traffic. |
| Opt-out semantics for org-wide | **Per-user preference applies even to org_admin / org_activity categories.** | A user who muted org_admin emails does not receive the broadcast. Recipient COUNT in the log reflects "scheduled", not "would have been". |
| Failure semantics | **Best-effort per recipient.** | One failed Mailgun call does not abort the rest. Each failure logs `email_send_failed` with the recipient address. |

**Concrete dispatch shape.**

```python
async def dispatch_notification_to_org(
    session, background_tasks, *,
    org_id, category, event_type, title, body, link_url=None,
    exclude_user_id=None,
) -> int:
    """Returns the count of notification rows written + email tasks scheduled."""
    # 1. Single SELECT for all active org members (id + email + preference row, JOIN-loaded).
    members = await user_service.list_active_org_members_with_preferences(session, org_id)
    if exclude_user_id is not None:
        members = [m for m in members if m.user.id != exclude_user_id]

    # 2. Bulk insert notification rows in a single statement.
    session.add_all([
        Notification(user_id=m.user.id, category=category, event_type=event_type,
                     title=title, body=body, link_url=link_url)
        for m in members
    ])
    await session.flush()  # caller commits at end of request

    # 3. Schedule one email task per recipient WHERE preference allows.
    scheduled = 0
    for m in members:
        if _should_email(category, m.preferences):
            background_tasks.add_task(send_notification_email, m.user.email, title, body, link_url)
            scheduled += 1

    await logger.ainfo("notification.broadcast",
        org_id=org_id, event_type=event_type,
        member_count=len(members), email_scheduled=scheduled)
    return scheduled
```

**Override of parent.** Parent says "Single transaction for all row writes; one email task per recipient." This delta adds: the SELECT must JOIN preferences in ONE statement (not N+1), and the broadcast helper RETURNS the scheduled count so the caller can echo it back to the operator (useful for the future audit-correlation UI).

**Files touched.**
- `backend/app/services/notification_service.py` — `dispatch_notification_to_org`.
- `backend/app/services/user_service.py` — `list_active_org_members_with_preferences` helper.

---

## New gaps surfaced

Walking the codebase against the parent spec turned up these issues. Each gets a decision, not a deferral.

### G1. Read vs seen — bell-open clears badge, click clears row

**Gap.** Parent spec uses one column (`read_at`) for both "the badge cleared" and "this specific notification was acknowledged". UX-wise these are two events: opening the bell clears the badge even if I never click a row; clicking a row signals "I dealt with this one".

**Decision.** Two columns: `seen_at` (cleared on bell-open) and `read_at` (cleared on row-click or read-all).

**Schema override.** Add `seen_at DATETIME NULL` to the `notifications` table. Index becomes `(user_id, seen_at, created_at)` for fast unseen-count, plus `(user_id, read_at, created_at)` for the inbox feed.

**API additions.**
- `POST /api/v1/notifications/mark-seen` — sets `seen_at = NOW()` for all the current user's unseen rows. Called by `<NotificationBell />` on popover open. Idempotent.
- Existing `/unread-count` becomes `/unseen-count` for the badge. The inbox view uses `read_at` for the visual unread / read distinction within the list.

**Why bother.** Without this, the badge re-fires after every poll until the user clicks every individual row. That's hostile UX.

### G2. Audit-events backfill

**Gap.** Should historical sensitive ops (audit rows already on disk) generate notifications retroactively?

**Decision. NO.** Notifications are real-time security signals; replaying them 30 days late would confuse users. Parent spec already says "No data backfill (existing users get defaults lazily)" — extend this to also state explicitly: no notification rows are generated for audit events that pre-date the notification system rollout. Document this as a one-line entry in the parent spec's "Out of scope" section.

### G3. Pagination + cardinality

**Gap.** A user with 500 unread notifications. What's the API + UI shape?

**Decision.**
- Default page size: 25. Max 100. Beyond that, force pagination.
- Popover always shows latest 10, never more. Footer link "View all" → inbox.
- Inbox view: paginated, cursor-based on `created_at, id`. **NOT** offset-based — offset pagination on a frequently-mutating set (read_at flipping) is inconsistent and slow at high offsets.
- `unseen-count` endpoint caps at 99 in the badge UI ("99+").

**Schema impact.** The existing `KEY ix_notifications_user_unread (user_id, read_at, created_at)` covers ORDER BY created_at DESC LIMIT/cursor queries. Good.

### G4. Email template store

**Gap.** Parent spec says "rendered HTML matching the existing template style" but doesn't say where the template lives.

**Decision.** Single shared template `notification_email.html` (Jinja2-style or Python f-string), variables: `{title, body, link_url, app_url}`. Co-located with `email_service.py` rendering helpers. No Mailgun-side template store (we already render HTML in-process for password reset / verification / MFA, same pattern). The template lives in the repo, not in Mailgun's template dashboard, so it's reviewable in PRs and reverts atomically with code changes.

**Files touched.**
- `backend/app/services/email_templates/notification_email.html` (or equivalent), referenced from `email_service.send_notification_email`.

### G5. Notification ↔ audit row correlation

**Gap.** Parent spec deflects with "the `event_type + user_id + created_at` correlation is enough for ad-hoc DB queries". On closer inspection, this is fine for v1, but it costs us when debugging "this notification fired but no audit row exists, or vice versa".

**Decision.** Add nullable `audit_event_id BIGINT NULL` column to `notifications`, with `FOREIGN KEY ... ON DELETE SET NULL`. The notification service accepts an `audit_event_id` arg and persists it when the caller has one. This is opt-in — call sites that don't have an audit row (none today, but possible later) can pass `None`.

**Schema override.** Add `audit_event_id BIGINT NULL` to `notifications`, with FK to `audit_events(id) ON DELETE SET NULL`. No index — we don't query notifications BY audit_event_id; the column is purely for forensic lookups.

**Caller impact.** `record_audit_event` / `add_audit_event_to_session` already return the AuditEvent row. Caller pattern becomes:
```python
audit_row = await audit_service.record_audit_event(...)
await notification_service.dispatch_notification(..., audit_event_id=audit_row.id)
```

### G6. Account-deleted email source-of-truth

**Gap.** Already covered in section 1 above. GDPR posture stated. No additional risk.

### G7. Future-queue readiness

**Gap.** Architect flagged: "smallest change today that doesn't paint us into a corner when we introduce a real queue later."

**Decision.** Three guardrails, all cheap:
- `dispatch_notification(...)` and `dispatch_notification_to_org(...)` accept `background_tasks: BackgroundTasks` as an explicit param (not pulled from a context var). When we move to RQ / arq, this param becomes a `QueueProtocol` and the only callers we have to retrofit are the service methods themselves. Today's call sites stay identical.
- Email helpers (`send_notification_email`, `send_account_deleted_email`) are pure functions: `(to, ...) -> bool`. No DB access, no FastAPI imports. Safe to lift into a worker process verbatim.
- Notification ROW writes go through the request's `AsyncSession`, NOT through background tasks. This means a notification row is committed (or rolled back) atomically with the action that caused it. If the email fails later, the row is still there — the user sees the bell, can react. Correct prioritization: persistence-first, dispatch-after.

### G8. Idempotency on the actor side

**Gap.** Parent spec says "Idempotency is the caller's problem". Plausibly fine, but the failure mode is silent duplication.

**Decision.** Soft mitigation only: log a warning when a notification row is written for a `(user_id, event_type)` pair within 5 seconds of the previous one. No DB-level dedup (would cost a SELECT before every INSERT, not worth it). The log is sufficient to spot bad call sites in review. Document the contract: "a single sensitive-op route should call `dispatch_notification` at most once per request."

### G9. Cross-impact with #330 (billing hide)

**Gap.** Plan-change notifications are `org_admin` category. With `BILLING_UI_ENABLED=false`, customer-facing plan change UI is hidden, but the BACKEND still rolls a 14-day trial on register. Plan changes during the hidden period:
- Operator changing a customer's plan via admin → still happens; notification email goes out, notification row appears in the bell.
- The bell popover links to `/settings/billing` for the plan-change notification. With billing UI hidden, that link 404s (or redirects).

**Decision.** When `BILLING_UI_ENABLED=false`, the plan-change notification's `link_url` is `null`. Render the notification row WITHOUT a "Go" link in that mode. This is a small conditional in the renderer, not a schema change.

**Files touched.**
- Whichever router triggers `admin.org.plan.changed` → wrap the `link_url=` with a config check.

### G10. Cross-impact with #331 (announcement banner)

**Gap.** Both features need to be aware of each other for the AppShell layout, plus there's a conceptual blur ("notification" vs "announcement").

**Decision.**
- Layout: no conflict. AnnouncementBar mounts BELOW header (parent #331 line 124). NotificationBell mounts IN header (this spec). Clean separation.
- Conceptual: announcements are global push from platform → all users. Notifications are per-user reactions to events on the user's account / org. Two separate substrates, no shared schema. Document the distinction in the parent spec's "Naming + cross-references" section.

---

## Revisit-before-implementation flags on the parent spec

I am NOT editing the parent. Surfacing these here per task rules.

1. **The `(or coerce silently to True)` parenthetical** appears twice (top resolutions block, open-questions block). Disagrees with the locked reject-with-400 behavior. Implementation should treat the lock as canonical and ignore the coerce branch. (See section 4 above.)
2. **The `notifications` schema as designed lacks `seen_at` and `audit_event_id`.** Both are added by this delta (G1, G5). The migration in PR 2 must include them — not as a follow-up PR.
3. **The "rejection with `{"code": "security_emails_required"}` body shape" requires a custom Pydantic validator** — vanilla `Literal[True]` gives a different body. Pin the implementation approach. (See section 4 above.)
4. **`dispatch_notification` signature in parent does not include `audit_event_id`.** Add it per G5.
5. **Background task ordering for `account.deleted`**: parent says "after successful commit" without specifying which commit (delete vs audit). This delta locks it to AFTER the audit commit (section 1). The parent should be read with this constraint applied.

---

## Rollout train — adjusted

The parent's 5-PR train still holds, with these adjustments:

| PR | Parent (2026-05-21) | Delta (this spec) |
|---|---|---|
| **PR 1** | Audit gap closures | **Unchanged.** Independent. Lowest risk. |
| **PR 2** | Substrate (migrations + models + services + GET/POST endpoints) | **+** Migration includes `seen_at` + `audit_event_id` columns (G1, G5). **+** New `notification_templates.py` module (section 3). **+** Email template HTML file (G4). **+** `Literal[True]` + custom validator on the preferences PUT (section 4). |
| **PR 3** | First hook batch (security) + bell icon + popover | **+** `/notifications/mark-seen` endpoint wired to popover-open (G1). **+** Bell mounts in AppShell header row, NOT in TrialBanner slot (section 2). |
| **PR 4** | Second hook batch (account + org_admin) | **+** `account.deleted` follows the section 1 sequence: snapshot → delete commit → audit commit → email task. **+** Plan-change `link_url` gated by `BILLING_UI_ENABLED` (G9). **+** Broadcast helper uses single SELECT with JOIN + returns scheduled count (section 5). |
| **PR 5** | Settings page | **Unchanged.** Per-category toggles + full list view. |

**No PR reordering.** The dependencies didn't change — PR 1 still depends on nothing, PR 2 depends on PR 1, PRs 3 and 4 each depend on PR 2 and can ship in either order, PR 5 depends on PR 2 only.

**Net new test obligations** (additions to parent's "Tests" section):
- `tests/services/test_notification_service.py`:
  - `dispatch_notification_to_org` returns scheduled count and writes one row per active member.
  - Broadcast respects per-user opt-out for non-security categories.
  - `audit_event_id` is persisted when passed.
- `tests/routers/test_notifications.py`:
  - `POST /notifications/mark-seen` sets `seen_at` for all unseen rows, leaves `read_at` alone.
  - Preferences PUT with `email_security=false` returns 400 with `{"code": "security_emails_required"}` body shape (custom validator path, not raw Pydantic).
- `tests/routers/test_admin_users_delete_email.py`:
  - Hard-deleting a user triggers `send_account_deleted_email` with the snapshot email AFTER the audit row write.
  - If audit write raises, email is NOT scheduled.

---

## Naming + cross-references (delta)

- `backend/app/services/notification_templates.py` — NEW (section 3, G4).
- `backend/app/services/email_templates/notification_email.html` — NEW (G4).
- `backend/app/services/notification_service.py` — gains `audit_event_id` arg + scheduled-count return on broadcast (G5, section 5).
- `backend/app/schemas/notification_preferences.py` — `Literal[True]` + custom validator (section 4).
- `frontend/components/notifications/NotificationBell.tsx` — mounts in AppShell header `<div className="flex items-center gap-3">` row at position 3 (after TrialBanner + AddTransactionCta) (section 2).

Parent spec sections still authoritative for everything not overridden here.
