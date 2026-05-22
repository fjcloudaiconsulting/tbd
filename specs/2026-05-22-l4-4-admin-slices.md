# L4.4 admin slices — admin invite, account-recovery resets, read-only impersonation — design

**Status:** ready for implementation (2026-05-22). All architect questions resolved; see §17 for the third-pass locks on Q1-Q6. Spec covers the three remaining L4.4 slices after the cross-org user search slice already shipped (see Substrate audit). Replaces no prior spec; this is the first full L4.4 doc.
**Date:** 2026-05-22.

**Source:** roadmap L4.4 (`memory/project_roadmap.md:124`). The user-management row was reset to partial after PRs #221/#222/#223/#280 closed the org-member-management slice. Open work: cross-org user search, admin invite, password / email / MFA reset, impersonate read-only. Cross-org search is already in `main` (PR #347-class, see Substrate audit §1.4). This spec scopes the remaining three slices.

## Summary

* **Cross-org user search is already shipped** — `GET /api/v1/admin/users` + `GET /api/v1/admin/users/{user_id}` + `/admin/users` UI live in main. This spec records the contract for traceability (search-field whitelist, audit conventions) but ships no new search code.
* **Admin invite** reuses the existing `invitations` table + Mailgun `send_invitation_email` pathway. New "Invite org admin" affordance on `/admin/orgs/{id}` (superadmin acting on behalf of an org), and a one-off "Invite platform admin" path that bootstraps a new superadmin user via a separate token type. Same 7-day expiry. Audited on send and on accept.
* **Account-recovery resets** (password / email / MFA) are out-of-band: the superadmin triggers an email to the user with a short-lived token, the user clicks through to set the new credential themselves. Superadmin never sees or sets the plaintext. MFA reset is "disable + force re-enroll", not "rotate secret". Each reset writes an audit row and dispatches a `security`-category notification via #354's `dispatch_notification`.
* **Impersonate read-only** mints a separate JWT (`type=impersonation`) tied to (impersonator user_id, target user_id, expiry=15 min, jti). A new pure-ASGI middleware short-circuits ALL non-`GET`/`HEAD` requests with `403 impersonation_readonly`. The frontend AppShell shows a persistent banner with target email + remaining time + Exit button. Every request made under the token writes a structlog event with `impersonation=true`, and entry / exit / expiry write durable `admin.impersonation.*` audit rows. The impersonated user receives a `security`-category notification ("an admin viewed your account").
* **Rollout train**: 5 PRs in dependency order — audit-event taxonomy seed (PR 1), admin invite (PR 2), account-recovery resets (PR 3), impersonation token + middleware (PR 4), impersonation UI + audit + notification (PR 5). Each PR ships independently and reverts cleanly.

## Substrate audit (confirmed 2026-05-22)

1. **Cross-org user search — ALREADY SHIPPED.** `backend/app/routers/admin_users.py` exposes `GET /api/v1/admin/users` (list with `q` / `org_id` / `role` / `status` filters, pagination) and `GET /api/v1/admin/users/{user_id}` (detail). Gated by `users.view`. Process-local audit throttle (60s window per `(actor, list)` and `(actor, target_user_id)`). `q` is never logged or echoed — only `query_length`. `admin.user.list.viewed` and `admin.user.viewed` audit events fire on first hit per window. Service is `app/services/admin_users_search_service.py`. Frontend at `frontend/app/admin/users/page.tsx` + `[user_id]/page.tsx`. **This spec records the contract in §3 for reference but ships no new search code.**
2. **Invitations.** `backend/app/models/invitation.py` already encodes `(org_id, email, role, expires_at, accepted_at, revoked_at, open_email)` with the unique `(org_id, open_email)` constraint. `invitation_service.create_invitation` / `accept_invitation` / `revoke_invitation` are mature. Tokens are JWT (`type=invitation`, 7-day expiry) via `security.create_invitation_token`. Mailgun fanout via `email_service.send_invitation_email`. We reuse the table for "invite org admin"; we add a **new** model + token type for "invite platform admin".
3. **Password-reset.** `POST /api/v1/auth/forgot-password` (5/min rate-limited) emits a JWT (`type=password_reset`, 1-hour expiry) and emails it via `send_password_reset_email`. `POST /api/v1/auth/reset-password` validates the token, hashes the new password, sets `password_changed_at` and `sessions_invalidated_at`. We reuse this token + flow; the admin-triggered variant differs only in entry point (admin button instead of the user's own forgot-password flow).
4. **Email change.** `users.py:200-215` handles user-initiated email change with a verification step and writes `user.email.changed` audit event (actor_email = OLD email per architect decision #24). The admin-triggered variant must follow the same audit convention. No new token type — reuse `create_email_verification_token`.
5. **MFA.** `mfa_service.py` provides `encrypt_secret` / `verify_totp` / `generate_recovery_codes`. User model: `mfa_enabled`, `totp_secret`, `recovery_codes`. There is no admin-triggered MFA disable today; the user can `POST /api/v1/users/me/mfa/disable` themselves. The admin slice adds `POST /api/v1/admin/users/{user_id}/mfa/disable` which clears the three columns under a separate audit event.
6. **Notification system.** `notification_service.dispatch_notification(db, *, user_id, category, event_type, title, body, link_url, audit_event_id)` is the one entry point (notification_service.py:67). `NotificationCategory` has `SECURITY` / `ACCOUNT` / `ORG_ADMIN` / `ORG_ACTIVITY`. Every reset and impersonation event in this spec writes a SECURITY-category notification keyed to the affected user_id.
7. **Audit log.** `audit_events` table per architect lock #24: no `target_user_id` / `target_user_email` columns. Self-target events use `actor_user_id` + `actor_email`. For admin-triggered actions where target ≠ actor, the target identity rides on `detail.target_user_id` + `detail.target_email`. `target_org_id` / `target_org_name` are populated when the action affects an org-scoped resource. `audit_service.record_audit_event` opens its own `AsyncSession` so it commits independently of the business transaction.
8. **JWT.** `security.py` exposes seven token types today: `access`, `refresh`, `password_reset`, `mfa_challenge`, `mfa_email`, `email_verify`, `invitation`. We add two: `impersonation` (15-min, carries `impersonator_sub` + `target_sub` + `jti` + `target_org_id`) and `platform_admin_invitation` (24-hour, carries `invitation_id` + `email`).
9. **Permissions.** `app/auth/permissions.py:Permission` enumerates 10 today. We add three: `users.invite`, `users.reset_credentials`, `users.impersonate`. Superadmin short-circuit covers all three for v1; `ROLE_PERMISSIONS` stays empty for non-superadmin platform roles per L4.8.
10. **Existing service for admin org-member updates.** `admin_org_members_service.py` already audits `admin.org.member.role_changed` / `deactivated` / `reactivated` and enforces the last-active-owner guard. The admin invite slice operates on the same surface; we hook into the same `orgs.manage` permission for the org-admin variant of invite.

## Architect resolutions (LOCKED 2026-05-22 — first pass)

* **Cross-org user search**: ALREADY SHIPPED, contract frozen. No new code.
* **Admin invite scope**: BOTH flows. (a) "Invite org admin" — superadmin issues an invitation into an existing org with `role=admin`. Uses the existing `invitations` table. Permission: `orgs.manage` (already held by superadmin via short-circuit). (b) "Invite platform admin" — superadmin invites a NEW platform-admin user who, on accept, gets `is_superadmin=true`. Uses a separate new `platform_admin_invitations` table. Permission: new `users.invite`.
* **Account-recovery resets — all three out-of-band, never set credentials directly.** Reset email types: existing `password_reset` JWT for password, existing `email_verify` flow for email (admin pre-confirms target address, user gets a one-click confirmation), and a MFA-disable path that clears `mfa_enabled` + `totp_secret` + `recovery_codes` server-side without a user click but with a `security`-category notification + audit row. **Rationale**: setting plaintext credentials on a user violates the principle that the user owns their secret; once an admin has typed it, the admin knows it. The MFA case is the asymmetric one because the recovery flow is "lose access entirely" — we can't email an MFA token to re-enroll someone who's locked out of their email or device; the admin disables MFA and the user re-enrolls on next login.
* **Reset rate limit**: 10 admin-triggered resets per actor per hour, slowapi-keyed on `actor.id`. Caught at the router with a clear 429 envelope. Prevents a compromised admin account from mass-resetting org members. Hard cap is process-local for v1 (Redis-backed when L4.10 ships).
* **Impersonation token shape**: SEPARATE JWT type, NOT a claim on the access token. Reasons: (1) the access-token shape is read by every authenticated route in the system; adding an `impersonation` claim adds a wide blast radius. (2) The session refresh path (`refresh-token`) would need a parallel claim everywhere it's checked. (3) Impersonation needs its own expiry (15 min, no refresh), distinct from session lifetime. Mint via new `security.create_impersonation_token(impersonator_id, target_id, target_org_id, jti)`. Stored in a Redis key for jti-based revocation (`impersonation:active:{jti}` with same 15-min TTL).
* **Impersonation READ-ONLY enforcement**: a new pure-ASGI middleware mounted in `app/main.py` (per the Starlette contextvar trap doc — `reference_starlette_middleware_contextvars.md` — `BaseHTTPMiddleware` is forbidden in this codebase). The middleware reads the bearer token, detects `type=impersonation`, and rejects any request whose method is not `GET` or `HEAD` with `403 {"code": "impersonation_readonly"}`. Mounted INSIDE the request-context middleware so the request_id is bound but BEFORE auth so the 403 short-circuit doesn't hit the DB. Defense-in-depth: `get_current_user` ALSO recognises the impersonation token, resolves the TARGET user (not the impersonator), and binds `impersonation=true` + `impersonator_user_id` to structlog contextvars.
* **Impersonation visible marker**: full-width banner pinned to the top of every page, BELOW the AppShell header, NOT dismissible. Yellow-amber background, dark-amber text, no transparency. Contains: target user email, remaining time (ticks every 5 seconds), Exit button (calls `POST /api/v1/admin/impersonation/exit` to invalidate the jti). The banner CANNOT be styled out via the org's theme — it's rendered as a sibling of `<AppShell>`, with hardcoded styles, not Tailwind tokens.
* **Impersonation auto-expiry**: 15 minutes. Hardcoded in `security.create_impersonation_token` AND in the Redis jti TTL. No refresh path. After expiry the next request resolves the impersonation token, finds the Redis key gone, and falls through to 401 — the frontend catches the 401 and bounces back to the regular admin shell with a "Impersonation session expired" toast.
* **Per-request audit on impersonation**: NO. Per-request audit rows during impersonation are too noisy; cardinality blows up audit_events for what's already covered by the request log with the bound `impersonation=true` contextvar. Instead: audit rows fire on ENTER, EXIT, and EXPIRY only. The structlog access log (already enriched per L4.9) carries `impersonator_user_id` + `target_user_id` on every request, which is enough for forensic reconstruction without flooding `audit_events`.
* **Impersonated user notification**: YES, via `dispatch_notification(category=SECURITY)`. Single notification per impersonation session, written at session END (not start) so the body can say "An admin viewed your account from HH:MM to HH:MM UTC". Sent regardless of whether the user has `email_security=true` (security category is forced-on per #354).
* **Audit conventions per #24**: all events emitted by this spec follow the locked self-target / admin-target rules. Admin-triggered events where target ≠ actor: `actor_user_id` / `actor_email` = the superadmin. Target identity in `detail.target_user_id` + `detail.target_email` + `detail.target_org_id`. `target_org_id` column populated when the action is org-scoped.

## Architect resolutions (LOCKED 2026-05-22 — second pass, threat-model)

After working the threats in §7, three additional locks:

* **T-Imp-1 hijack mitigation**: impersonation tokens are NEVER acceptable on routes that mutate platform-level state. The READ-ONLY middleware blocks all non-GET, but a defense-in-depth layer at the router for sensitive READ surfaces is also added: `/api/v1/admin/audit`, `/api/v1/admin/users`, `/api/v1/admin/orgs`, `/api/v1/admin/analytics`, `/api/v1/admin/roles`, `/api/v1/admin/subscriptions`, `/api/v1/admin/announcements`, `/api/v1/admin/ai-usage` all explicitly reject impersonation tokens. Reading admin telemetry while wearing the skin of a non-superadmin user is a clear smell that we choose to block. The middleware adds an `impersonation` flag to `request.state`; these routers check it via a tiny `forbid_impersonation` dependency.
* **T-Reset-1 poisoning mitigation**: the admin-triggered email change endpoint requires a TYPED CONFIRMATION of the NEW email address (the admin types it once, then types it again, like the dup pattern in `OrgWipeModal`). Pre-launch this is cheap and structurally rejects fat-finger and lookalike-domain accidents (e.g. `support@evi1.com` vs `support@evil.com`). The new email must also pass the existing `normalize_email` + uniqueness preflight before the verification token is issued.
* **T-Inv-1 abuse mitigation**: platform-admin invites have a STRICT cap — at most 3 pending platform-admin invites at any time, system-wide. The 4th `POST /api/v1/admin/users/invite-platform-admin` returns `409 too_many_pending_platform_admin_invites`. The intent is to make a mass-invite spray immediately visible. Org-admin invites stay at the existing per-org limit (no platform-wide cap needed; the per-org owner controls the surface).

## Design

### 1. Permissions

Three new entries in `app/auth/permissions.py:Permission`:

```python
Permission = Literal[
    # existing
    "admin.view",
    "plans.manage",
    "orgs.view",
    "orgs.manage",
    "audit.view",
    "roles.manage",
    "analytics.view",
    "users.view",
    "users.delete",
    "subscriptions.view",
    # new (L4.4)
    "users.invite",            # platform-admin invite (new platform-admin user)
    "users.reset_credentials", # password / email / MFA admin-triggered resets
    "users.impersonate",       # mint + exit an impersonation session
]
```

All three are superadmin-only via the existing short-circuit. `ROLE_PERMISSIONS` stays empty per #L4.8 lock.

`ALL_PERMISSIONS` updated to include the three new entries. L4.8 role editor automatically surfaces them in the permission catalog UI.

### 2. Cross-org user search — recorded contract (NO NEW CODE)

Recorded here so future implementation PRs touching `/admin/users` have a single source of truth for the contract.

**Endpoints (live in main):**

```
GET /api/v1/admin/users
    ?q=<query>           # max_length=120; matched LIKE 'q%' against email, username, display_name
    &org_id=<int>        # ge=1; restrict to users with a membership in this org
    &role=owner|admin|member
    &status=active|inactive|unverified|superadmin
    &limit=50            # ge=1, le=200
    &offset=0            # ge=0
→ 200 { items: UserRow[], total, limit, offset }
Permission: users.view (superadmin-only today)

GET /api/v1/admin/users/{user_id}
→ 200 UserDetail { id, email, username, display_name, is_superadmin, is_active,
                   email_verified, mfa_enabled, password_changed_at, created_at,
                   onboarded_at, orgs: [{ org_id, name, role }],
                   recent_audit_events: AuditEvent[] }
404 if not found
Permission: users.view
```

**Search-field whitelist.** The service implements LIKE matching ONLY against three columns: `users.email`, `users.username`, `users.display_name`. Searching by `password_hash`, `totp_secret`, or any non-identifying column is structurally impossible — the column allowlist is enforced in `admin_users_search_service.list_users` via a hand-rolled query (no dynamic column-name parameter). This is a key non-implementation note for future expansion: adding `q` matchers MUST stay column-allowlisted.

**Privacy invariants (recorded).**
* `q` value is NEVER logged. Only `query_length` reaches structlog.
* `admin.user.list.viewed` and `admin.user.viewed` audit events: throttled 60s per `(actor_id)` and per `(actor_id, target_user_id)` respectively. First hit per window writes; refreshes inside the window are silent. Process-local throttle (no Redis dependency — restart resets, which is acceptable for audit cardinality).
* The detail endpoint returns `recent_audit_events` for the target — same `audit.view` data the audit page exposes, but pre-filtered to the user. This is intentional; superadmins debugging a user's history don't need to switch surfaces.

### 3. Admin invite

Two flows. Both reuse the established invitation + Mailgun infrastructure.

#### 3.1 Org-admin invite (use existing `invitations` table)

**Permission**: `orgs.manage`. Surface: existing `/admin/orgs/{id}` page, new "Invite admin" affordance in the Members section.

**Endpoint**: reuse the existing `POST /api/v1/orgs/{org_id}/invitations`. The route is already implemented for org owners; superadmins inherit it via the `orgs.manage` short-circuit. The role parameter is `admin` (not `member`).

**Diff from existing flow**: at audit time, `actor_email` is the superadmin (not an org owner). Add a `detail.via_platform_admin: true` flag so we can see in `/admin/audit` that this invite came from a platform admin acting on the org, not from inside the org.

**Audit events**: existing `org.invitation.sent` fires (no schema change). On accept, existing `org.invitation.accepted` fires. We update `_org_invitation_audit_event` to include the `via_platform_admin` boolean.

**Notification**: existing invitation-accept flow already triggers the user-facing welcome.

#### 3.2 Platform-admin invite (new table + new token type)

A wholly separate flow. Distinct from org invites because the invitee doesn't belong to an org yet; they're being granted `is_superadmin=true`.

**New table** `platform_admin_invitations`:

```sql
CREATE TABLE platform_admin_invitations (
    id INT NOT NULL AUTO_INCREMENT,
    email VARCHAR(120) NOT NULL,
    open_email VARCHAR(120) NULL,                  -- nullable like invitations.open_email; UQ enforces "one pending per email"
    created_by_user_id INT NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,               -- created_at + INTERVAL 1 DAY
    accepted_at DATETIME(6) NULL,
    revoked_at DATETIME(6) NULL,
    revoked_by_user_id INT NULL,
    accepted_user_id INT NULL,                     -- the User row created on accept; FK ON DELETE SET NULL
    PRIMARY KEY (id),
    UNIQUE KEY uq_platform_admin_invitations_open (open_email),
    KEY ix_platform_admin_invitations_email (email),
    CONSTRAINT fk_paininv_created_by FOREIGN KEY (created_by_user_id)
        REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paininv_revoked_by FOREIGN KEY (revoked_by_user_id)
        REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT fk_paininv_accepted FOREIGN KEY (accepted_user_id)
        REFERENCES users (id) ON DELETE SET NULL
);
```

* `created_by` is `RESTRICT` because removing a superadmin who issued a still-pending platform admin invite is itself a precondition mismatch — fix the invite first.
* `open_email` follows the same nullable-unique-key idiom as `invitations.open_email`.
* Migration: revision `056_platform_admin_invitations.py` (revision number depends on collision-state at impl time; rebase rule from architect decision #19).

**New JWT type** `platform_admin_invitation`:

```python
def create_platform_admin_invitation_token(invitation_id: int, email: str) -> str:
    """Platform-admin invitation token. 1-day expiry (tighter than org
    invites' 7-day because the blast radius of an accepted platform
    admin is system-wide).
    """
    expire = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "sub": str(invitation_id),
        "email": email,
        "type": "platform_admin_invitation",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
```

**Endpoints**:

```
POST /api/v1/admin/users/invite-platform-admin
    body: { email: str }
→ 201 { invitation_id, email, expires_at }
→ 409 { code: "user_already_exists" } if a User row already has this email
→ 409 { code: "invite_already_pending" } if a non-expired non-accepted invite exists for this email
→ 409 { code: "too_many_pending_platform_admin_invites" } if 3 pending exist platform-wide
Permission: users.invite
Rate-limit: 5/hour per actor
Side effects:
  - Persist row, set open_email
  - Mint platform_admin_invitation token
  - Mailgun: send_platform_admin_invitation_email (new template)
  - Audit: admin.platform_admin.invitation.sent
  - No notification (recipient doesn't have a user_id yet)

GET /api/v1/admin/users/platform-admin-invitations
    ?status=pending|accepted|revoked|expired
    &limit=50&offset=0
→ 200 { items: PlatformAdminInvitationRow[], total, limit, offset }
Permission: users.invite OR audit.view (read-side surface)

POST /api/v1/admin/users/platform-admin-invitations/{id}/revoke
→ 200 { id, revoked_at }
→ 404 if not found / already revoked / already accepted
Permission: users.invite
Audit: admin.platform_admin.invitation.revoked
Side effects:
  - Set revoked_at, revoked_by_user_id, open_email=NULL

POST /api/v1/auth/accept-platform-admin-invitation
    body: { token: str, username: str, password: str }
→ 200 { user_id, login_token }     # caller logs in immediately
→ 400 invalid_or_expired_token
→ 409 user_already_exists           # someone registered under this email between invite + accept
Public route (no auth — the invitee doesn't have an account yet).
Rate-limit: 5/minute (same as register / forgot-password class)
Side effects:
  - Decode token, verify email matches row.email
  - Create User row with is_superadmin=True, email_verified=True, is_active=True, role=Role.OWNER
  - Org: NEW org created for them (single-row, named "<email> (platform admin)" — they need an org_id at least nominally for the existing schema invariants; UI tucks this away).
  - Audit: admin.platform_admin.invitation.accepted (actor = the new user themselves; target_org_id = the new org's id)
  - Notification: dispatch_notification(category=SECURITY, event_type="admin.platform_admin.granted") to the new user
  - Notification fanout: also dispatch_notification to every EXISTING superadmin ("New platform admin accepted: <email>") for transparency.
```

**Email template** (`send_platform_admin_invitation_email`):

```
Subject: You've been invited as a platform administrator on The Better Decision

<inviter_email> has invited you to join The Better Decision as a platform administrator.
Platform administrators have full read and write access to every organization on the platform.
If you weren't expecting this, you can ignore this email.

Accept: <accept_url>

This invitation expires in 24 hours.
```

`accept_url` is `https://app.thebetterdecision.com/accept-platform-admin-invitation?token=<jwt>`.

#### 3.3 Sequence diagram — platform-admin invite

```
Superadmin            POST /api/v1/admin/users/invite-platform-admin
    │                            │
    │                            ▼
    │                  admin_invite_service.send_platform_admin_invitation
    │                            │
    │                            ▼
    │                  preflight: User by email? → 409 user_already_exists
    │                            │
    │                            ▼
    │                  preflight: open pending row? → 409 invite_already_pending
    │                            │
    │                            ▼
    │                  preflight: count(pending) >= 3? → 409 too_many_pending_platform_admin_invites
    │                            │
    │                            ▼
    │                  INSERT platform_admin_invitations
    │                            │
    │                            ▼
    │                  create_platform_admin_invitation_token(id, email)
    │                            │
    │                            ▼
    │                  Mailgun send_platform_admin_invitation_email
    │                            │
    │                            ▼
    │                  audit: admin.platform_admin.invitation.sent
    │ 201 {invitation_id, email, expires_at}
    ◄────────────────────────────┤
```

### 4. Account-recovery resets

Three reset paths, all out-of-band (never set credentials directly).

Shared shape:

* **Permission**: `users.reset_credentials`.
* **Rate-limit**: 10/hour per actor (slowapi).
* **Audit**: distinct event_type per reset kind; same architect-decision-#24 self-target shape (target_user_id rides in `detail`).
* **Notification**: SECURITY category to the affected user.
* **Out-of-band**: admin does NOT see or set the new credential. The user resets via email link.

#### 4.1 Password reset

```
POST /api/v1/admin/users/{user_id}/password-reset
    body: {}
→ 200 { email_dispatched: true }
→ 404 user_not_found
→ 409 { code: "user_inactive" }
Permission: users.reset_credentials
Rate-limit: 10/hour per actor
Side effects:
  - Verify target user is_active=True
  - Mint password_reset JWT (REUSE existing security.create_password_reset_token, 1-hour expiry)
  - Mailgun send_password_reset_email (existing template, no change)
  - Audit: admin.user.password_reset.triggered
            actor: superadmin
            target_org_id: user.org_id
            detail: { target_user_id, target_email, reason: <free text up to 200, optional in v1>, kind: "password" }
  - Notification: dispatch_notification(
        user_id=target.id,
        category=SECURITY,
        event_type="admin.user.password_reset.triggered",
        title="An administrator triggered a password reset on your account",
        body="<superadmin_email> requested a password reset for your account at <timestamp UTC>. A reset email has been sent to <user_email>. If you didn't expect this, contact support immediately.",
        link_url="/settings/security",
    )
```

The notification fires REGARDLESS of whether the user opens the email — this is the key transparency move per the brief.

#### 4.2 Email reset / forced change

Admin-triggered email change is dangerous if mistyped. Two-step typed-confirmation pattern.

```
POST /api/v1/admin/users/{user_id}/email-change
    body: { new_email: str, new_email_confirm: str }
→ 200 { verification_dispatched: true, target_email: <new_email> }
→ 400 emails_do_not_match            # new_email != new_email_confirm
→ 400 invalid_email                  # normalize_email rejects it
→ 409 email_already_in_use           # another user_active row has this email
→ 404 user_not_found
Permission: users.reset_credentials
Rate-limit: 10/hour per actor
Side effects:
  - Two-key typed confirmation enforced at the endpoint (new_email == new_email_confirm)
  - normalize_email + uniqueness preflight (same as user-initiated change in users.py)
  - DOES NOT mutate users.email yet. Mints an email_verify token (REUSE existing) for the user with the NEW email baked in.
  - Mailgun: send to NEW_EMAIL with verification link. The OLD email also gets a notification (see below) so the user knows their account is being moved.
  - Audit: admin.user.email_change.triggered
            actor: superadmin
            target_org_id: user.org_id
            detail: { target_user_id, target_email_old, target_email_new, kind: "email" }
            actor_email: superadmin email (not the user's OLD email — this is the admin-triggered shape, not the self-initiated shape that uses OLD email)
  - Notification to user_id at the OLD email (because the user hasn't confirmed the new email yet):
        category=SECURITY
        event_type="admin.user.email_change.triggered"
        title="An administrator requested an email change on your account"
        body="<superadmin_email> requested that your account email be changed from <old> to <new>. A verification link has been sent to the new address. If this wasn't expected, contact support immediately and do NOT click the verification link."
        link_url="/settings/security"
```

When the user clicks the verification link, the existing `POST /api/v1/auth/verify-email` flow runs. It writes `user.email.changed` per the existing convention (actor_email = OLD email, since the user is now self-confirming the change). Two audit rows result: `admin.user.email_change.triggered` (admin's intent) + `user.email.changed` (actual swap on user confirmation). Both rows are queryable.

#### 4.3 MFA reset (disable + require re-enrollment)

Asymmetric from password/email — there's no email-link version because the user may have lost the device that holds the TOTP secret. Admin disables MFA server-side; user re-enrolls on next login.

```
POST /api/v1/admin/users/{user_id}/mfa/disable
    body: { reason: str }                  # required, max 200 chars (free text)
→ 200 { mfa_disabled: true }
→ 404 user_not_found
→ 409 { code: "mfa_not_enabled" }          # mfa already off
Permission: users.reset_credentials
Rate-limit: 10/hour per actor
Side effects:
  - Set user.mfa_enabled=False, totp_secret=NULL, recovery_codes=NULL
  - Set user.sessions_invalidated_at=now() (kicks all active sessions so the user must re-authenticate)
  - Commit
  - Audit: admin.user.mfa_disabled
            actor: superadmin
            target_org_id: user.org_id
            detail: { target_user_id, target_email, kind: "mfa", reason }
  - Notification: dispatch_notification(
        user_id=target.id,
        category=SECURITY,
        event_type="admin.user.mfa_disabled",
        title="An administrator disabled MFA on your account",
        body="<superadmin_email> disabled MFA on your account at <timestamp UTC>. You'll need to re-enroll MFA the next time you sign in. Reason given: \"<reason>\". If you didn't expect this, contact support immediately.",
        link_url="/settings/security",
    )
```

`reason` is REQUIRED — unlike password/email reset, this leaves the user with a weaker security posture, so we want a forensic note in `audit_events.detail.reason`.

#### 4.4 Frontend UI for resets

Three buttons in a new "Account recovery" card on `/admin/users/{user_id}`:

* **Trigger password reset** — confirm modal (no fields, just "Send reset email?"). On confirm, POST + toast.
* **Force email change** — modal with two email-input fields (the second labeled "Confirm new email"). Submit disabled until both match and normalize-validate. On submit, POST.
* **Disable MFA** — modal with required reason textarea (min 4 chars). Submit disabled until reason filled. On submit, POST.

All three modals reuse `ConfirmModal` / `Modal` primitives from `components/ui/`.

#### 4.5 Reset-spike alert (Q4 lock — mitigates T-Reset-2)

A peer-detection layer on top of the per-actor 10/hour rate limit. When a single admin triggers 5 or more resets within a rolling 10-minute window, every OTHER superadmin receives a SECURITY-category notification flagging the spike. The actor themselves is NEVER a recipient (a compromised admin would otherwise see their own alert and act on it).

**Counter shape**: Redis-backed rolling window keyed per actor.

```
Key:    reset_spike:{actor_user_id}:{rolling_10min_window}
        where rolling_10min_window = floor(now_unix / 600) — bucketed
Value:  monotonically-incrementing counter (INCR)
TTL:    660 seconds (11 minutes — one minute of slack past the window)
```

Each of the three reset endpoints (`/admin/users/{id}/password-reset`, `/admin/users/{id}/email-change`, `/admin/users/{id}/mfa/disable`), AFTER the audit row commits successfully, performs:

```python
# Sketch — full impl in the resets PR (PR 3 of this train).
window = int(time.time()) // 600
key = f"reset_spike:{actor.id}:{window}"
count = await redis.incr(key)
if count == 1:
    await redis.expire(key, 660)

if count >= 5:
    # Fanout SECURITY notification to every OTHER superadmin
    await dispatch_notification_to_org_admins(
        db,
        category=NotificationCategory.SECURITY,
        event_type="admin.reset_spike.detected",
        title="Admin reset-spike threshold reached",
        body=f"Admin {actor.email} triggered {count} account-recovery resets in the last 10 minutes.",
        link_url="/admin/audit",
        filter_to=Role.SUPERADMIN,
        exclude_user_ids=[actor.id],
    )
```

The fanout helper is `dispatch_notification_to_org_admins` from `notification_service`, filtered to superadmins (NOT org admins of any specific org — the audience is platform-wide superadmins). The actor is excluded via `exclude_user_ids`.

**Threshold rationale**: 5-in-10-min sits well below the 10/hour per-actor cap, giving peers a chance to see the spike at half-cap rather than waiting for it to hit the hard limit. The actor still has 5 more resets available before the rate limiter fires, but every additional reset above the 5-threshold continues to refire the fanout (the check is `count >= 5`, not `count == 5`) — this means the 6th, 7th, etc. reset in-window each fire a fresh notification. We accept the duplicate-notification cost for the stronger signal.

**Audit row**: each fanout-firing reset still writes its own per-action audit row (`admin.user.password_reset.triggered` etc.). The spike-detection event itself does NOT write a separate audit row — the per-reset rows in close temporal sequence ARE the audit record. The fanout notifications are recoverable from the notifications table.

**Threat-model coverage**: T-Reset-2 (compromised admin mass-reset DoS) — see §7. The mitigation is the rate limit at 10/hour plus peer detection at the 5-in-10-min threshold; the residual is that an attacker still gets 5 free resets before peers are alerted, which is acceptable for v1.

### 5. Impersonate read-only

#### 5.1 Token shape

New JWT type `impersonation`. Distinct from `access` token so its detection is unambiguous (no leaky claim on the normal token).

```python
def create_impersonation_token(
    impersonator_user_id: int,
    target_user_id: int,
    target_org_id: int,
    jti: str,
) -> str:
    """Mint an impersonation token. 15-minute hardcoded expiry.

    The token carries enough to resolve the TARGET user without
    additional DB lookups beyond user fetch, plus the impersonator id
    so middleware/audit/log can pin the actor identity. ``jti`` is the
    revocation handle stored in Redis (impersonation:active:{jti}).
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=15)
    payload = {
        "sub": str(target_user_id),                 # downstream resolves user.id == target
        "org_id": target_org_id,                    # frontend AppShell uses this
        "type": "impersonation",
        "impersonator_sub": str(impersonator_user_id),
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
```

Constraint: `target_user_id != impersonator_user_id`. Caught at the router with 400.

Constraint: target must be `is_active=True` and `is_superadmin=False`. Caught at the router with 409 `cannot_impersonate_superadmin` or `target_inactive`. Impersonating another superadmin is not a supported workflow — superadmins debug other superadmins by talking to them.

#### 5.2 Endpoints

```
POST /api/v1/admin/impersonation/enter
    body: { target_user_id: int, reason: str }     # reason required, max 200 chars
→ 200 { impersonation_token, expires_at, target: { id, email, org_id, org_name } }
→ 400 { code: "cannot_impersonate_self" }
→ 404 user_not_found
→ 409 { code: "cannot_impersonate_superadmin" }
→ 409 { code: "target_inactive" }
→ 409 { code: "impersonation_session_active" }     # actor already has an active jti — must exit first
Permission: users.impersonate
Rate-limit: 20/hour per actor
Side effects:
  - Pre-check no active Redis key impersonation:by_actor:{actor.id}
  - secrets.token_urlsafe(16) → jti
  - Redis SET impersonation:active:{jti} = {actor_id, target_id, expires_at_iso}, TTL=900s
  - Redis SET impersonation:by_actor:{actor_id} = jti, TTL=900s    # one active session per actor
  - Mint impersonation token
  - Audit: admin.impersonation.entered
            actor: superadmin
            target_org_id: target.org_id
            detail: { target_user_id, target_email, jti, reason, expires_at_iso }

POST /api/v1/admin/impersonation/exit
    body: {}
→ 200 { exited: true, duration_seconds: int }
→ 404 no_active_session
Permission: users.impersonate
Side effects:
  - Read Redis impersonation:by_actor:{actor.id} for jti
  - DEL impersonation:active:{jti}
  - DEL impersonation:by_actor:{actor.id}
  - Audit: admin.impersonation.exited
            actor: superadmin
            target_org_id: target.org_id
            detail: { target_user_id, target_email, jti, duration_seconds }
  - Notification (DEFERRED to end-of-session — see 5.5): dispatch_notification(
        user_id=target_user_id,
        category=SECURITY,
        event_type="admin.impersonation.session",
        title="An administrator viewed your account",
        body="<superadmin_email> viewed your account in read-only mode from <enter_time UTC> to <exit_time UTC>. If you have concerns, contact support.",
        link_url="/settings/security",
    )

GET /api/v1/admin/impersonation/status
    (no body)
→ 200 { active: bool, target?, expires_at? }       # called by AppShell to verify session is still live
Permission: users.impersonate
```

The actor's REGULAR access token (the superadmin's normal session) remains active throughout. The impersonation token is a SEPARATE credential the frontend sends in place of the access token while impersonating.

#### 5.3 Middleware (READ-ONLY enforcement)

New pure-ASGI middleware `app/middleware/impersonation_middleware.py`. Mounted in `main.py` AFTER `RequestContextMiddleware` (so `request_id` is bound) but BEFORE auth dependencies resolve.

```python
# Sketch only — full impl in PR 4.
class ImpersonationReadOnlyMiddleware:
    """Pure-ASGI middleware. Inspects the Bearer token; when the token
    is type=impersonation, rejects any non-GET / non-HEAD request with
    403 { code: impersonation_readonly }.

    Pure-ASGI per the Starlette contextvar trap doc — BaseHTTPMiddleware
    is forbidden in this codebase (see reference_starlette_middleware_contextvars.md).
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope["method"]
        if method in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization")
        if not auth or not auth.startswith(b"Bearer "):
            return await self.app(scope, receive, send)

        token = auth[7:].decode(errors="replace")
        payload = decode_token(token)
        if payload is None:
            return await self.app(scope, receive, send)

        if payload.get("type") != "impersonation":
            return await self.app(scope, receive, send)

        # Whitelist the exit endpoint
        path = scope.get("path", "")
        if path == "/api/v1/admin/impersonation/exit":
            return await self.app(scope, receive, send)

        # Reject everything else
        await _send_403_impersonation_readonly(send)
```

The 403 envelope:

```json
{ "detail": { "code": "impersonation_readonly", "message": "Mutating requests are blocked during impersonation." } }
```

Frontend catches the code and shows a toast: "You can't perform write actions while viewing as another user. Exit impersonation first."

**Defense-in-depth (T-Imp-1 lock):** sensitive READ surfaces also reject impersonation tokens explicitly. A new dependency `forbid_impersonation_session` is added to:

```
/api/v1/admin/audit
/api/v1/admin/users
/api/v1/admin/orgs
/api/v1/admin/analytics
/api/v1/admin/roles
/api/v1/admin/subscriptions
/api/v1/admin/announcements
/api/v1/admin/ai-usage
/api/v1/admin/impersonation/enter           # cannot impersonate from inside impersonation
```

These routes return `403 { code: "impersonation_blocked_on_admin_surface" }` when called with an impersonation token. The check runs alongside `require_permission(...)` so impersonation cannot grant "read all platform audit events as a different user".

#### 5.4 get_current_user resolution under impersonation

`deps.get_current_user` is extended to recognize `type=impersonation` tokens:

```python
# Sketch — full impl in PR 4.
async def get_current_user(credentials, db) -> User:
    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(401, "Invalid or expired token")

    token_type = payload.get("type")
    if token_type == "impersonation":
        # Verify jti is still active in Redis (one-shot revocation)
        jti = payload.get("jti")
        active = await redis.get(f"impersonation:active:{jti}")
        if active is None:
            raise HTTPException(401, "Impersonation session expired")
        impersonator_sub = payload.get("impersonator_sub")
        if not impersonator_sub:
            raise HTTPException(401, "Malformed impersonation token")
        target_user_id = int(payload["sub"])
        # Resolve the TARGET user as the request's user
        user = await db.get(User, target_user_id)
        if user is None or not user.is_active:
            raise HTTPException(401, "Impersonation target unavailable")
        # Bind to structlog so every log line carries impersonator + target
        structlog.contextvars.bind_contextvars(
            user_id=user.id,
            org_id=user.org_id,
            role=user.role.value,
            impersonation=True,
            impersonator_user_id=int(impersonator_sub),
        )
        return user
    elif token_type == "access":
        # existing path
        ...
    else:
        raise HTTPException(401, "Wrong token type")
```

Every structlog event under impersonation carries `impersonation=true` and `impersonator_user_id`. The L4.9 uvicorn access log includes both fields. This is the per-request forensic trail per the architect lock (no per-request audit_events row needed).

#### 5.5 Frontend banner

New component `frontend/components/AppShell/ImpersonationBanner.tsx`. Mounted in `<AppShell>` between `<Header>` and `<main>`, height 56px, full-width, NOT dismissible.

Visual spec (hardcoded — DOES NOT use theme tokens; it must look the same in every theme):

```
Background: #fbbf24  (amber-400, opaque)
Text:       #1a0f00  (darker than amber-950, max contrast for AA)
Padding:    12px 24px
Position:   relative (in flex column with header above, main below)
Z-index:    not stacked — banner is in normal flow so it pushes content
Cannot be hidden via CSS class — banner is rendered as a sibling and
its presence depends on impersonation_token in localStorage.
```

Content (left-to-right):

```
[icon: eye/info]  Viewing as <target.email> in read-only mode · expires in 14:23
                                                    [Exit impersonation]
```

The timer ticks every 5 seconds (not every 1s; CPU + reflow). At 1 minute remaining the timer text turns red. At 0 the AppShell auto-calls `POST /api/v1/admin/impersonation/exit` (best-effort) and clears the impersonation_token from localStorage.

**Storage**: the impersonation token is stored in `localStorage.impersonation_token`. The regular access token stays in its existing storage (refreshtoken cookie + memory). The `apiFetch` wrapper in `frontend/lib/api.ts` checks for `localStorage.impersonation_token` and prefers it when present. On Exit (manual or auto), `apiFetch` falls back to the regular access token.

**Why localStorage and not cookie**: the impersonation token is bearer-only (no refresh path, no CSRF surface). Putting it in localStorage keeps the cookie auth path untouched and avoids accidental impersonation leaking onto requests after exit (cookies persist longer than localStorage cleanup steps).

**Visual sketch (ASCII):**

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  The Better Decision     [Dashboard] [Transactions] ...     [Bell] [Avatar]  │
├──────────────────────────────────────────────────────────────────────────────┤
│ 👁 Viewing as alice@example.com in read-only mode · expires in 14:23  [Exit] │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  <main content rendered as alice@example.com would see it>                  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

#### 5.6 Sequence diagram — full impersonation lifecycle

```
Superadmin               Frontend                Backend                Redis            Target user
    │                       │                       │                      │                 │
    │ Click "Impersonate"   │                       │                      │                 │
    │ on /admin/users/{id}  │                       │                      │                 │
    ├──────────────────────►│                       │                      │                 │
    │                       │ POST /admin/          │                      │                 │
    │                       │   impersonation/enter │                      │                 │
    │                       │   { target_user_id,   │                      │                 │
    │                       │     reason }          │                      │                 │
    │                       ├──────────────────────►│                      │                 │
    │                       │                       │ preflight: target    │                 │
    │                       │                       │   active, not super, │                 │
    │                       │                       │   no active session  │                 │
    │                       │                       │                      │                 │
    │                       │                       │ mint jti             │                 │
    │                       │                       │ SET active+by_actor  │                 │
    │                       │                       ├─────────────────────►│                 │
    │                       │                       │                      │                 │
    │                       │                       │ mint impersonation_token              │
    │                       │                       │ audit: entered       │                 │
    │                       │ 200 { token, exp }    │                      │                 │
    │                       │◄──────────────────────┤                      │                 │
    │                       │                       │                      │                 │
    │                       │ localStorage.set(...)  │                      │                 │
    │                       │ Banner mounts          │                      │                 │
    │                       │                       │                      │                 │
    │ Navigates /dashboard  │                       │                      │                 │
    ├──────────────────────►│                       │                      │                 │
    │                       │ GET /api/v1/transactions                     │                 │
    │                       │   Authorization: Bearer <imp_token>          │                 │
    │                       ├──────────────────────►│                      │                 │
    │                       │                       │ middleware: GET → pass│                │
    │                       │                       │ get_current_user:    │                 │
    │                       │                       │   detect type=imp,   │                 │
    │                       │                       │   verify jti in redis│                 │
    │                       │                       │   resolve target user│                 │
    │                       │                       │   bind contextvars:  │                 │
    │                       │                       │     user_id=target   │                 │
    │                       │                       │     impersonator=actor                 │
    │                       │                       │ Route runs as target │                 │
    │                       │ 200 {...}             │                      │                 │
    │                       │◄──────────────────────┤                      │                 │
    │                       │                       │                      │                 │
    │ Tries to delete tx    │                       │                      │                 │
    ├──────────────────────►│                       │                      │                 │
    │                       │ DELETE /transactions/123                     │                 │
    │                       │   Authorization: Bearer <imp_token>          │                 │
    │                       ├──────────────────────►│                      │                 │
    │                       │                       │ middleware:          │                 │
    │                       │                       │   method=DELETE,     │                 │
    │                       │                       │   type=impersonation │                 │
    │                       │                       │   → 403 readonly     │                 │
    │                       │ 403 { code: imp_ro }  │                      │                 │
    │                       │◄──────────────────────┤                      │                 │
    │                       │ Toast: "Can't write"  │                      │                 │
    │                       │                       │                      │                 │
    │ Click "Exit"          │                       │                      │                 │
    ├──────────────────────►│                       │                      │                 │
    │                       │ POST /admin/          │                      │                 │
    │                       │   impersonation/exit  │                      │                 │
    │                       ├──────────────────────►│                      │                 │
    │                       │                       │ Read by_actor → jti  │                 │
    │                       │                       │ DEL active+by_actor  │                 │
    │                       │                       ├─────────────────────►│                 │
    │                       │                       │ audit: exited        │                 │
    │                       │                       │ dispatch_notification│                 │
    │                       │                       │   to target          │                 │
    │                       │                       │                      │                 │
    │                       │                       │                      │ ┌───────────────┴───────────┐
    │                       │                       │                      │ │ Target sees notification │
    │                       │                       │                      │ │ "An administrator viewed │
    │                       │                       │                      │ │ your account from ... to..." │
    │                       │                       │                      │ └───────────────────────────┘
    │                       │ 200 { exited: true }  │                      │                 │
    │                       │◄──────────────────────┤                      │                 │
    │                       │ localStorage.remove   │                      │                 │
    │                       │ Banner unmounts       │                      │                 │
```

#### 5.7 Immediate revocation on superadmin demotion (Q6 lock — mitigates T-Imp-3 "actor demoted" branch)

When superadmin A is mid-impersonation and superadmin B (or any path that clears `is_superadmin`) removes A's superadmin status, every active impersonation session A holds must end immediately. Relying on the 15-min Redis TTL would leave a window where a demoted superadmin retains cross-org read access; that window is unacceptable.

**Mechanism**: the superadmin-removal endpoint (the path that flips `is_superadmin=False`, typically owned by the L4.8 role editor or the platform-admin-management surface) runs a sweep AFTER the role-change row commits.

```python
# Sketch — full impl in the role-management PR.
# After: await db.commit()  (role change committed)

# Find every active impersonation jti where THIS user is the impersonator.
# The Redis schema stores impersonation:active:{jti} = {actor_id, target_id, ...}.
# We use the by_actor key as the index (one active session per actor invariant from §5.2).
by_actor_key = f"impersonation:by_actor:{demoted_user.id}"
active_jti = await redis.get(by_actor_key)

if active_jti is not None:
    active_key = f"impersonation:active:{active_jti}"
    # Read the session detail BEFORE deletion so the audit row can carry target info
    session_blob = await redis.get(active_key)
    await redis.delete(active_key)
    await redis.delete(by_actor_key)

    # Audit row records the forced revocation
    await record_audit_event(
        actor_user_id=demoted_user.id,        # the demoted superadmin (now ex-superadmin)
        actor_email=demoted_user.email,
        target_org_id=session_blob.get("target_org_id") if session_blob else None,
        event_type="admin.impersonation.revoked",
        detail={
            "jti": active_jti,
            "target_user_id": session_blob.get("target_id") if session_blob else None,
            "reason": "actor_superadmin_revoked",
            "revoked_by_user_id": revoker.id,
            "revoked_by_email": revoker.email,
        },
    )
```

If the by_actor scan needs to evolve to handle multiple active sessions per actor (currently one, enforced by §5.2), the Redis schema would need a SCAN-based index — flagged for the impl PR but not required by v1.

**Frontend behaviour for the demoted actor**: the next request the frontend makes with the now-revoked impersonation token resolves through `get_current_user` → Redis lookup → key missing → 401. The frontend catches the 401 and bounces back to the regular admin shell (same path as natural 15-min expiry); the difference is timing, not UX. The toast message is "Impersonation session ended" (generic — we don't disclose the demotion details on the demoted user's screen).

**Audit ordering**: the role-change audit row (`admin.role.changed` or equivalent from the role-management surface) commits FIRST; the `admin.impersonation.revoked` row commits SECOND. Both rows are queryable for forensic reconstruction of "when did the cross-org read access actually end".

**Threat-model coverage**: T-Imp-3 (persistent impersonation session — specifically the "actor demoted while session active" branch). The mitigation closes the 15-min residual window for the demotion case. The natural-expiry T-Imp-3 mitigation (15-min TTL + auto-exit) still covers the non-demotion case.

### 6. Migrations

Single Alembic revision per PR:

* **PR 1** (audit seed only) — no schema migration. Adds event_type strings to taxonomy doc + a comment in `app/models/audit_event.py`.
* **PR 2** (admin invite) — `056_platform_admin_invitations.py` creates the new table.
* **PR 3** (resets) — no schema migration; column-only operation on the existing `users` table.
* **PR 4** (impersonation backend) — no schema migration; tokens are stateless + Redis-backed.
* **PR 5** (impersonation UI) — no schema migration.

Per `CLAUDE.md`: migrations run via the lifespan + migrate wrapper.

### 7. Threat model

| # | Threat | Slice | Mitigation | Residual risk |
|---|---|---|---|---|
| T-Search-1 | Cross-org search used for spam target list (exfiltration of all platform emails) | Search | `users.view` permission (superadmin only); throttle audit row per (actor, list) means we can't HIDE searches but we can SHOW that an actor scrolled 47 pages. Email column is the only PII returned without an explicit detail click. | A compromised superadmin could exfiltrate the email column. Detection via post-incident review of `/admin/audit` `admin.user.list.viewed` rows. |
| T-Search-2 | Search by sensitive column (password_hash, totp_secret) | Search | Search-field allowlist hardcoded in service (email / username / display_name). Future columns require code change, not dynamic. | Future careless edit. Covered by a unit test asserting only the allowlisted columns appear in the generated query. |
| T-Search-3 | `q` value leaks to logs | Search | `q` is NEVER logged. Only `query_length` reaches structlog. Audit-event `detail` carries `query_length`, not the raw string. | Future code that calls `logger.info("search", q=q)` would break this — covered by a grep-based test in `tests/security/test_search_no_q_logged.py`. |
| T-Inv-1 | Mass platform-admin invite spray | Admin invite (platform) | Hard cap of 3 pending invites system-wide. 4th returns 409. Plus 5/hour per-actor rate limit. Plus every send writes an audit row with the inviter identity. | A compromised superadmin who already KNOWS where to send 3 invites can still mint 3 attackers' credentials. Detection by `admin.platform_admin.invitation.sent` audit rows + notification to every other superadmin on accept. |
| T-Inv-2 | Invitation token replay (acceptance N times) | Admin invite | `open_email` is set NULL on accept, blocking duplicate accept via the unique-key on `(open_email)`. JWT `email` claim is matched against the row. Accept endpoint sets `accepted_at` and clears `open_email` in a single transaction. | None pre-launch. |
| T-Inv-3 | Email change between invite send and accept | Admin invite | Token embeds the email. Accept endpoint refuses if a User row already has the embedded email (409 user_already_exists). | None. |
| T-Inv-4 | Org-admin invite used to plant a backdoor admin into a customer's org | Admin invite (org) | Audit row carries `detail.via_platform_admin=true` so a customer org owner querying their org's audit feed (when L3.9 ships) sees that a platform admin issued the invite. v1 doesn't surface this to the org owner; customers must trust platform. | Pre-launch: acceptable. Post-launch: L3.9 customer-facing org audit should surface platform-admin-issued invites. |
| T-Reset-1 | Reset poisoning — admin types `evi1.com` instead of `evil.com` on email change | Reset | Two-key typed confirmation (new_email + new_email_confirm must match). normalize_email + uniqueness preflight. Notification to OLD email so user catches the move. | Skilled attacker who can also intercept the OLD email won't be stopped here. Detection: `admin.user.email_change.triggered` audit row with old + new in detail. |
| T-Reset-2 | Mass-reset by a compromised admin (DoS against users) | Reset | 10/hour per actor rate limit. SECURITY-category notification to every affected user. **Plus reset-spike alert (Q4 lock, §4.5)**: at 5 resets in 10 min by the same actor, every OTHER superadmin gets a SECURITY-category notification (`admin.reset_spike.detected`). Peer detection at half-cap. | Compromised admin still gets 5 free resets before peers are alerted (5-in-10-min threshold) and 10/hour before the rate limiter fires. Acceptable bound for v1. |
| T-Reset-3 | Admin sees the new password (key knowledge) | Reset | Admin NEVER sets the new credential. Token-mediated reset means only the user can choose the new password. | None — by design. |
| T-Reset-4 | MFA disable without justification | Reset (MFA) | `reason` field is REQUIRED, persisted in `audit_events.detail.reason`. Notification to user includes the reason verbatim. | A compromised admin can put garbage in reason; the audit + notification still records who did it. |
| T-Imp-1 | Impersonation token used to mutate (hijack via stolen token) | Impersonation | Middleware blocks ALL non-GET/HEAD. Plus 15-min hardcoded expiry. Plus Redis jti revocation. Plus sensitive-admin-route allowlist explicitly rejects impersonation tokens (T-Imp-1 lock). | Stolen impersonation token can READ the target user's app surface for up to 15 minutes. Audit + notification record this. |
| T-Imp-2 | Token leak via logging / error messages | Impersonation | Tokens never appear in structlog (no `logger.info("...", token=t)` paths; same hygiene as access tokens). Errors return generic envelope; no token echo. | Future careless code. Covered by grep-based test. |
| T-Imp-3 | Persistent impersonation session (never exits) | Impersonation | Hardcoded 15-min Redis TTL. Token JWT `exp` matches. Auto-exit on expiry from the frontend. Redis key disappearance forces a fresh 401 next request. **Plus immediate revocation on superadmin demotion (Q6 lock, §5.7)**: the role-removal endpoint sweeps Redis for any `impersonation:active:{jti}` keys where actor matches the demoted user and DELs them, writing `admin.impersonation.revoked` with `detail.reason="actor_superadmin_revoked"`. Closes the "actor demoted while session active" branch. | Banner shows expiry countdown for the natural-expiry case. Demotion case: next request after the role flip resolves 401 and frontend bounces back to admin shell with "Impersonation session ended" toast. |
| T-Imp-4 | Admin impersonates self / superadmin to escalate | Impersonation | Router preflight rejects `target == actor` (400) and `target.is_superadmin=True` (409). | None. |
| T-Imp-5 | Concurrent impersonation sessions per actor (forensic confusion) | Impersonation | Redis `impersonation:by_actor:{actor_id}` enforces ONE active session per actor. 2nd Enter returns 409 `impersonation_session_active`. | Actor can rapidly Enter → Exit → Enter different targets; each cycle writes audit rows so the trail stays clean. |
| T-Imp-6 | Impersonation banner CSS'd away by attacker | Impersonation | Banner uses hardcoded inline styles, not theme tokens. Z-index normal flow (pushes content; can't be `display: none`'d via theme override). | Browser dev tools can override anything client-side. Server-side correctness (mutations rejected, audit rows written) is the real defense — the banner is for the LEGITIMATE admin's awareness, not against an attacker. |
| T-Imp-7 | Impersonated user doesn't see the notification (e.g. logged out, email off) | Impersonation | Security category — non-suppressible per #354 lock. Notification fires regardless of `email_security` preference; in-app row written regardless of read state. User who returns sees the notification in the bell. | If the user account is dormant and never returns, the notification sits unread. Acceptable — audit trail is durable independently. |
| T-Imp-8 | Race: actor exits impersonation but a slow in-flight request still resolves on the target user | Impersonation | Redis `impersonation:active:{jti}` lookup in `get_current_user` is on every request. Exit DELs the key; the next request resolves 401 immediately. In-flight request that started BEFORE the DEL still runs to completion but can't mutate (READ-ONLY enforced). | Acceptable: read-only by design. |

### 8. Audit event taxonomy

All new event_type strings:

| event_type | When | actor | target_org_id | detail keys |
|---|---|---|---|---|
| `admin.platform_admin.invitation.sent` | Platform-admin invite sent | superadmin | NULL (no org) | `target_email, invitation_id, expires_at` |
| `admin.platform_admin.invitation.revoked` | Platform-admin invite revoked | superadmin | NULL | `target_email, invitation_id` |
| `admin.platform_admin.invitation.accepted` | Platform-admin invite accepted | NEW superadmin user (self-target) | new_org_id | `target_email, invitation_id, inviter_user_id` |
| `admin.user.password_reset.triggered` | Admin triggers password reset | superadmin | target.org_id | `target_user_id, target_email, kind="password"` |
| `admin.user.email_change.triggered` | Admin triggers email change | superadmin | target.org_id | `target_user_id, target_email_old, target_email_new, kind="email"` |
| `admin.user.mfa_disabled` | Admin disables MFA | superadmin | target.org_id | `target_user_id, target_email, kind="mfa", reason` |
| `admin.impersonation.entered` | Impersonation session starts | superadmin | target.org_id | `target_user_id, target_email, jti, reason, expires_at_iso` |
| `admin.impersonation.exited` | Impersonation session ends (manual or expiry) | superadmin | target.org_id | `target_user_id, target_email, jti, duration_seconds, ended_by="manual"|"expiry"` |
| `admin.impersonation.revoked` | Impersonation session force-ended because actor lost superadmin status (Q6 lock, §5.7) | demoted user (ex-superadmin) | target.org_id (from session blob) | `target_user_id, jti, reason="actor_superadmin_revoked", revoked_by_user_id, revoked_by_email` |

(Existing `org.invitation.sent` / `org.invitation.accepted` get an additional `detail.via_platform_admin: true` flag when issued by a superadmin acting on the org.)

The reset-spike alert (Q4 lock, §4.5) does NOT write its own event_type. The per-reset rows (`admin.user.password_reset.triggered` etc.) already form the audit record; the spike-detection layer only fires notifications.

### 9. Notification dispatch

All notifications in this spec use `category=SECURITY` per #354's lock. Security category is non-suppressible (preference write rejects `email_security=false` with 400 `security_emails_required`).

| event_type | Recipient | Title |
|---|---|---|
| `admin.user.password_reset.triggered` | target_user_id | An administrator triggered a password reset on your account |
| `admin.user.email_change.triggered` | target_user_id (old email, in-app row) | An administrator requested an email change on your account |
| `admin.user.mfa_disabled` | target_user_id | An administrator disabled MFA on your account |
| `admin.impersonation.session` | target_user_id | An administrator viewed your account |
| `admin.platform_admin.granted` | new_superadmin_user_id | Welcome — you're now a platform administrator |
| `admin.platform_admin.granted` (fanout) | every existing superadmin (one row each) | A new platform administrator was added |
| `admin.reset_spike.detected` (Q4 lock, §4.5) | every OTHER superadmin (excluding the actor) | Admin reset-spike threshold reached |

Dispatch contract: notification rows are written in the SAME `AsyncSession` as the action that caused them (per the #354 persistence-first lock). Audit rows are written through `audit_service.record_audit_event` which opens its own session.

### 10. Configuration

No new env vars. The 15-minute impersonation expiry is a code constant (`IMPERSONATION_TTL_SECONDS = 900` in `app/security.py`); not env-toggleable. The 3-invite cap is a code constant (`MAX_PENDING_PLATFORM_ADMIN_INVITES = 3` in `app/services/platform_admin_invitation_service.py`). The 10/hour reset cap is a slowapi decorator value.

If operations needs to flex any of these, the PR is a 1-line constant change; we treat it as code-level policy rather than config.

### 11. Frontend changes summary

| File | Change |
|---|---|
| `frontend/components/AppShell.tsx` | Mount `<ImpersonationBanner>` between header and main |
| `frontend/components/AppShell/ImpersonationBanner.tsx` | NEW component (banner + timer + Exit button) |
| `frontend/lib/api.ts` | `apiFetch` reads `localStorage.impersonation_token` and prefers it over the access token when present |
| `frontend/app/admin/users/[user_id]/page.tsx` | Add "Account recovery" card (3 buttons) + "Impersonate" button |
| `frontend/components/admin/PasswordResetModal.tsx` | NEW (confirm-only modal) |
| `frontend/components/admin/EmailChangeModal.tsx` | NEW (two email-input fields + typed confirm) |
| `frontend/components/admin/MfaDisableModal.tsx` | NEW (reason textarea) |
| `frontend/components/admin/ImpersonateConfirmModal.tsx` | NEW (target preview + reason textarea + confirm) |
| `frontend/app/admin/users/page.tsx` | Add "Invite platform admin" button in the page header |
| `frontend/components/admin/InvitePlatformAdminModal.tsx` | NEW (email input + confirm) |
| `frontend/app/admin/orgs/[org_id]/page.tsx` | Add "Invite admin" to existing Members section (superadmin variant of the existing org-owner invite UI) |
| `frontend/app/accept-platform-admin-invitation/page.tsx` | NEW (token parse + username + password + accept → login) |

### 12. Backend changes summary

| File | Change |
|---|---|
| `backend/app/auth/permissions.py` | Add `users.invite`, `users.reset_credentials`, `users.impersonate` to `Permission` + `ALL_PERMISSIONS` |
| `backend/app/security.py` | Add `create_impersonation_token`, `create_platform_admin_invitation_token`, `IMPERSONATION_TTL_SECONDS` constant |
| `backend/app/models/platform_admin_invitation.py` | NEW model |
| `backend/alembic/versions/056_platform_admin_invitations.py` | NEW migration |
| `backend/app/services/platform_admin_invitation_service.py` | NEW service (send/list/revoke/accept) |
| `backend/app/services/admin_user_recovery_service.py` | NEW service (password / email / MFA admin-triggered reset, plus reset-spike Redis counter + fanout per Q4 lock §4.5) |
| `backend/app/services/impersonation_service.py` | NEW service (enter/exit/status, Redis key management, plus `revoke_active_sessions_for_actor` helper per Q6 lock §5.7) |
| `backend/app/middleware/impersonation_middleware.py` | NEW pure-ASGI middleware |
| `backend/app/main.py` | Mount `ImpersonationReadOnlyMiddleware` after RequestContextMiddleware |
| `backend/app/deps.py` | Extend `get_current_user` to handle `type=impersonation` tokens |
| `backend/app/routers/admin_users.py` | Add `/invite-platform-admin`, `/platform-admin-invitations*`, `/{user_id}/password-reset`, `/{user_id}/email-change`, `/{user_id}/mfa/disable` |
| `backend/app/routers/admin_impersonation.py` | NEW router (`/admin/impersonation/{enter,exit,status}`) |
| `backend/app/routers/auth.py` | Add `/accept-platform-admin-invitation` |
| `backend/app/services/email_service.py` | Add `send_platform_admin_invitation_email` |
| `backend/app/services/notification_templates.py` | Add 6 new template entries (all SECURITY category) |

### 13. Tests

**Backend** (under `backend/tests/`):

* `services/test_platform_admin_invitation_service.py` — send / cap-3 / revoke / accept; preflight collision cases; expiry.
* `services/test_admin_user_recovery_service.py` — password reset emits the same JWT as user-initiated; email change requires confirm match; MFA disable requires reason; rate-limit boundary.
* `services/test_impersonation_service.py` — enter writes Redis keys, exit DELs both, status returns active+target, double-enter returns 409, can't impersonate self/superadmin.
* `middleware/test_impersonation_middleware.py` — GET passes, POST blocked, exit endpoint passes, non-impersonation tokens pass through, invalid tokens pass through (auth layer handles them).
* `routers/test_admin_users_invite_platform_admin.py` — full POST → token → accept → new User row → notification fanout to existing superadmins.
* `routers/test_admin_impersonation.py` — full enter/list/exit cycle + audit rows + notification on exit.
* `routers/test_admin_user_recovery.py` — all three reset endpoints, rate limit, audit + notification.
* `security/test_impersonation_no_token_logged.py` — grep-based: no structlog call binds `impersonation_token=` anywhere.
* `security/test_impersonation_blocked_on_admin_surfaces.py` — every listed admin route rejects impersonation tokens.

**Frontend** (under `frontend/tests/`):

* `components/admin/PasswordResetModal.test.tsx`, `EmailChangeModal.test.tsx` (two-email confirm), `MfaDisableModal.test.tsx` (reason required).
* `components/AppShell/ImpersonationBanner.test.tsx` — banner mounts when `impersonation_token` in localStorage, timer ticks, Exit button calls endpoint.
* `lib/api.test.tsx` — `apiFetch` prefers impersonation token when present.
* `app/accept-platform-admin-invitation-page.test.tsx` — token decode, accept flow, login redirect.

### 14. Out of scope

* **Per-user rate-limit overrides** for admin-triggered resets. L4.10 (per-org / per-user rate limiting with admin override table) covers the generic case.
* **Audit row per individual request during impersonation.** The structlog access log carries the same fields and `audit_events` cardinality would blow up. Reconsidered if a compliance regime ever requires it.
* **Multi-impersonation** (admin viewing two users simultaneously). One active session per actor, by design.
* **Mobile UX for the impersonation banner.** The banner is desktop-grade for v1; mobile may need a more compact variant. Not gating launch.
* **Impersonation analytics surface.** "How many times was user X impersonated in the last 30 days?" is a query over `audit_events`; no dashboard for v1.
* **Reason as a structured taxonomy.** All reasons are free text in v1. If we find ourselves wanting to slice by reason, structure can land later.
* **Impersonate read-WRITE.** Hard no for v1. If we ever ship a write-mode impersonation it must be a distinct token type (`impersonation_write`) with full per-request audit, not a flag flip.
* **SMS / push for impersonation notifications.** In-app + email only, per #354.
* **`accept-platform-admin-invitation` running through CAPTCHA gate.** The link is in a delivered email — adding a CAPTCHA wall before accept is anti-UX. The 1-day expiry + 3-invite cap is the rate-limiting layer.

### 15. Rollout train

5 PRs in dependency order. Each ships independently and reverts cleanly.

| # | Title | Dependencies | Estimated LOC (backend / frontend) | Notes |
|---|---|---|---|---|
| 1 | **L4.4 audit taxonomy seed + permissions** | none | 80 / 0 | Adds the 3 new permissions to `app/auth/permissions.py`. Documents the 8 new event_type strings in a code comment on `audit_event.py` for searchability. No new endpoints, no migration. Low-risk first PR. |
| 2 | **L4.4 admin invite (org-admin + platform-admin)** | PR 1 | 600 / 350 | New `platform_admin_invitations` table + service + router. Extends existing org-invitation audit detail with `via_platform_admin`. New Mailgun template. Frontend: invite buttons on `/admin/users` + `/admin/orgs/{id}`, accept page. **One migration**: revision `056`. |
| 3 | **L4.4 admin-triggered account recovery (password / email / MFA)** | PR 1 | 480 / 300 | New `admin_user_recovery_service`. 3 new endpoints on `admin_users` router. Frontend: "Account recovery" card on `/admin/users/{id}` with 3 modals. Notification + audit on each. **Plus reset-spike alert (Q4, §4.5)** — Redis counter + SECURITY-fanout to peer superadmins at 5-in-10-min. No migration. |
| 4 | **L4.4 impersonation backend (token + middleware + endpoints)** | PR 1 | 540 / 0 | New `create_impersonation_token`. New pure-ASGI middleware. New `impersonation_service` (Redis-backed). New `admin_impersonation` router. Extends `get_current_user` to recognize the token type. `forbid_impersonation_session` dependency added to 8 admin routes. **Plus immediate-revocation sweep helper (Q6, §5.7)** — exported from `impersonation_service` as `revoke_active_sessions_for_actor(actor_user_id, revoker)`, called by whichever role-removal surface lands first (this PR ships the helper; the role-management surface wires the call site). NO frontend in this PR. No migration. |
| 5 | **L4.4 impersonation UI + audit completion** | PR 4 | 80 / 450 | New `ImpersonationBanner` component. `apiFetch` impersonation-token preference. "Impersonate" button on `/admin/users/{id}`. ConfirmModal + reason textarea. Exit notification dispatch wired (writes the SECURITY-category notification to the impersonated user). No migration. |

Total estimate: **~1700 backend LOC, ~1100 frontend LOC** across 5 PRs.

### 16. Decisions locked

1. **Cross-org search**: NO new code. Contract recorded for traceability.
2. **Admin invite**: BOTH flows (org-admin via existing table, platform-admin via new table).
3. **Platform-admin invite cap**: 3 pending platform-wide; 5/hour per actor.
4. **Resets**: all 3 out-of-band; admin never sees plaintext. Password = reuse existing JWT. Email = two-key typed confirm + reuse existing email_verify JWT. MFA = server-side disable + force re-enroll, REQUIRED reason.
5. **Reset rate limit**: 10/hour per actor.
6. **Impersonation token**: SEPARATE JWT type (`type=impersonation`), 15-min hardcoded TTL, Redis-backed jti for revocation, ONE active session per actor.
7. **Impersonation enforcement**: pure-ASGI middleware (per Starlette contextvar trap doc) blocks all non-GET/HEAD; sensitive admin routes additionally reject impersonation tokens.
8. **Impersonation banner**: hardcoded styles, NOT dismissible, top of every page below header.
9. **Impersonation audit**: ENTER + EXIT + EXPIRY only; per-request forensic trail via structlog (L4.9 already binds the contextvars).
10. **Impersonation notification**: written at END of session, single SECURITY-category row.
11. **Token storage frontend**: localStorage (`impersonation_token`); regular access token unaffected.
12. **MFA reset is asymmetric**: server-side disable, REQUIRED reason. No "email me an MFA reset link" path — users locked out of their device can't receive that anyway.
13. **(Q1, 2026-05-22)** Org-admin invite "issued by platform admin" badge in org-members UI: NO in v1. `via_platform_admin` lives in `audit_events.detail` only until L3.9 customer audit feed ships.
14. **(Q2, 2026-05-22)** Impersonation Exit → Enter cooldown: NO cooldown. Per-actor 20/hour cap plus the one-active-session invariant cover the abuse case.
15. **(Q3, 2026-05-22)** Platform-admin-accept fanout cap: NO cap. Every existing superadmin gets a notification per accept; opt-out lives in the existing notification preference layer.
16. **(Q4, 2026-05-22)** Reset-spike alert: YES. At 5 resets in 10 min by one actor, fanout `admin.reset_spike.detected` SECURITY notification to every OTHER superadmin. Implementation in §4.5; mitigates T-Reset-2. Lands as part of resets PR (PR 3).
17. **(Q5, 2026-05-22)** Reset-modal typeahead for common reasons: NO in v1. Free-text reason fields only.
18. **(Q6, 2026-05-22)** Immediate impersonation revocation when actor loses superadmin status: YES. Role-removal endpoint sweeps Redis for active impersonation jtis where actor matches the demoted user and DELs them, writing `admin.impersonation.revoked` with `detail.reason="actor_superadmin_revoked"`. Implementation in §5.7; mitigates the demotion branch of T-Imp-3.

### 17. Decisions locked (2026-05-22, third pass — architect answers to open questions)

All six open questions are now resolved. Spec is in ready-for-implementation state.

1. **Q1 — Org-admin invite "issued by platform admin" badge in v1 org-members UI**: **NO** in v1. The `audit_events.detail.via_platform_admin` field is the durable record; surfacing it to the customer org owner is deferred until L3.9 ships a customer-facing audit feed. Rationale: pre-launch we have no L3.9 customer audit surface yet; the v1 badge would add UI without a coherent audit feed to back it.
2. **Q2 — Forced cooldown between impersonation Exit and next Enter**: **NO** cooldown. Spec stays at "actor may immediately Enter a new target after Exit". Rationale: legitimate debug flows ("check alice then bob") outweigh the drive-by-chain risk, and every Enter still writes an audit row + per-actor 20/hour cap is already in place.
3. **Q3 — Cap on platform-admin-accept fanout notifications**: **NO** cap. Every existing superadmin receives a notification per accept, regardless of N. Rationale: the notification system's existing per-user opt-out (within the SECURITY category's forced-on constraints) is the right mechanism; introducing a fanout cap would create a transparency gap when N grows.
4. **Q4 — Mass-reset alert threshold (alert other superadmins when one admin triggers 5+ resets in 10 min)**: **YES**, implement the reset-spike alert. Rationale: mass-reset is a high-signal compromised-admin indicator; firing a SECURITY-category notification to every OTHER superadmin (excluding the actor) gives peer detection within the same audit window. Implementation sketch in §4.5; threat-model entry as mitigation for T-Reset-2. Lands as part of PR 3 (resets).
5. **Q5 — Typeahead common reasons on reset modals**: **NO** typeahead in v1. Reset modals keep free-text reason fields. Rationale: without operational data we don't know which reasons cluster; locking a v1 taxonomy risks coding the wrong shape. Revisit post-launch if a structured-classification need emerges.
6. **Q6 — Immediate impersonation revocation when superadmin status is removed**: **YES**, immediate revocation; do not rely on the 15-min TTL. Rationale: a demoted superadmin should lose ALL active capabilities immediately, including read-only impersonation; allowing up to 15 more minutes of cross-org read access after demotion is an unacceptable seam. Implementation sketch in §5.7; threat-model entry as mitigation for T-Imp-3 "actor demoted while session active" branch. Lands as part of the superadmin-role-management PR (PR 4 of this train extended, or the L4.8 role editor PR — whichever lands first owns the sweep code).

## Naming + cross-references

* Backend:
  * `backend/app/models/platform_admin_invitation.py`
  * `backend/app/services/{platform_admin_invitation_service, admin_user_recovery_service, impersonation_service}.py`
  * `backend/app/middleware/impersonation_middleware.py`
  * `backend/app/routers/admin_impersonation.py`
  * `backend/alembic/versions/056_platform_admin_invitations.py`
* Frontend:
  * `frontend/components/AppShell/ImpersonationBanner.tsx`
  * `frontend/components/admin/{PasswordResetModal, EmailChangeModal, MfaDisableModal, ImpersonateConfirmModal, InvitePlatformAdminModal}.tsx`
  * `frontend/app/accept-platform-admin-invitation/page.tsx`
* Cross-refs:
  * `specs/2026-05-21-notification-system-sensitive-ops.md` — SECURITY-category dispatch contract.
  * `specs/2026-05-22-notification-system-2nd-arch-pass.md` — persistence-first dispatch order.
  * `memory/project_architect_decisions_2026_05_22.md` #24 — audit conventions for self-target vs admin-target events.
  * `memory/reference_starlette_middleware_contextvars.md` — middleware must be pure-ASGI, not BaseHTTPMiddleware.
  * `memory/reference_agent_worktree_cd_persistence.md` and `reference_compose_bind_mount_in_parallel_agents.md` — agent isolation rules apply during impl.
  * `memory/reference_do_spec_sync.md` — no new env vars (per §10), so no `.do/app.yaml` updates.

## Security review summary (recorded inline 2026-05-22)

This spec was reviewed against the established security patterns in `backend/app/services/audit_service.py`, `backend/app/services/mfa_service.py`, and the architect-locked decisions #24, #34. Key invariants enforced:

* **Out-of-band reset principle**: admins never set credentials directly. User retains sole knowledge of their secret.
* **Read-only impersonation by middleware + token type + admin-surface allowlist**: defense-in-depth at three layers.
* **Cardinality discipline**: per-request audit rows during impersonation rejected; structlog access log is the forensic trail.
* **Audit/notification atomicity**: notification rows commit with the action; audit rows commit in an independent session per `audit_service` contract.
* **Notification transparency**: every sensitive action against a user produces a SECURITY-category notification to that user, non-suppressible.
* **Rate limits at the action layer**: 10/hour resets, 5/hour platform-admin invites, 3 pending platform-admin invites system-wide.
* **No new env vars**: all policy constants are code-level for easy review-grep.
