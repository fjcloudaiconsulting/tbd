# Backend Session Model (Spec)

**Date:** 2026-05-17
**Status:** DRAFT for architect + QA review. Implementation is a separate
PR sequence (see Section 8). No code changes in this PR.
**Author:** Team H (design)
**Implementer:** Team I (post-approval)

## 0. Why this spec exists

Today the refresh-session story is split across three concepts that share
no source of truth, plus four hardcoded `7 * 24 * 60 * 60` literals that
can never be tuned via env. Two recent incidents (2026-05-15 cookie
shadow, 2026-05-16 false logout class) both traced back to the same
root: the cookie `max_age`, the refresh JWT `exp`, and the org-level
"absolute session lifetime" knob are conflated, drift-prone, and
practically capped at 7 days. The spec separates them, adds a per-token
`jti` so we can revoke a single session without nuking every device,
and introduces a small rotation grace window so cross-tab races no
longer surface as false logouts.

## 1. Current state

All citations are against `main` at `b0bffdd` (2026-05-17 06:00 GMT+2).

### 1.1 Config (`backend/app/config.py`)

```
jwt_access_token_expire_minutes: int = 15     # line 24
jwt_refresh_token_expire_days: int = 7        # line 25
jwt_algorithm: str = "HS256"                  # line 26
session_lifetime_days: int = 30               # line 27
```

`session_lifetime_days` is documented as the absolute cap and is
honoured by `_validate_single_refresh_token`. `jwt_refresh_token_expire_days`
sets both the refresh JWT `exp` (via `create_refresh_token`) and is
the de-facto idle ceiling, but the cookie `max_age` is NOT read from
it. The 7d default for refresh masks the 30d absolute cap because the
cookie disappears from the browser first.

### 1.2 Hardcoded cookie `max_age` literals (`backend/app/routers/auth.py`)

| Site | Line | Context |
|------|------|---------|
| `login` password branch | 295 | `response.set_cookie(... max_age=7*24*60*60, path="/")` |
| `refresh` rotation | 562 | `response.set_cookie(... max_age=7*24*60*60, path="/")` |
| `_issue_tokens` helper | 833 | shared by MFA branches |
| `google/callback` | 1483 | redirect response sets the cookie |

The OAuth `oauth_state` `max_age=1800` literals on lines 1223 and 1553
are a separate concern (30 minute SSO step) and NOT in scope.

### 1.3 Refresh JWT shape (`backend/app/security.py:33-53`)

```
{
  "sub": "<user_id_str>",
  "type": "refresh",
  "session_created_at": <unix_seconds_float>,  // optional; preserved across rotations
  "iat": <unix_seconds_int>,
  "exp": <unix_seconds_int>  // iat + jwt_refresh_token_expire_days
}
```

No `jti`. No `session_id`. Every rotation issues a brand-new opaque
token whose only link to the original login is `session_created_at`.

### 1.4 Global invalidation (token cutoff)

`token_cutoff(user)` in `backend/app/security.py:160-174` returns
`max(password_changed_at, sessions_invalidated_at)` and
`_validate_single_refresh_token` rejects any token with
`iat < token_cutoff(user)` (lines 410-415 of `auth.py`).

Every call site that writes `sessions_invalidated_at` therefore kills
all sessions for that user:

| Site | Trigger |
|------|---------|
| `routers/auth.py:644` | `POST /auth/logout` (current self-logout) |
| `routers/auth.py:714` | `POST /auth/reset-password` (token flow) |
| `routers/users.py:165` | `PUT /users/me` when email changes |
| `routers/users.py:241` | `PUT /users/me/password` |
| `services/invitation_service.py:286, 403` | invitation accept / role swap |
| `services/admin_org_members_service.py:172` | admin deactivates a member |

The 2026-05-16 cookie-shadow incident confirmed that today's logout
behaves as global invalidation, not per-session. That is the bug this
spec corrects on the logout path while preserving every other site's
behaviour.

### 1.5 Validation chain (`backend/app/routers/auth.py:342-507`)

Already list-aware after PR #289:

- `_extract_refresh_cookies(request)` walks the raw Cookie header, returns every `refresh_token` value.
- `_validate_single_refresh_token` runs the per-token checks (decode, type, user active, iat vs cutoff, absolute lifetime).
- `_validate_refresh_cookie` accepts the newest token if all surviving successes resolve to the same user, raises `AMBIGUOUS_SESSION_DETAIL` if two distinct user_ids validate.

This shape stays. Section 2 only adds a Redis-backed `jti` step.

### 1.6 Frontend in-flight refresh (`frontend/lib/api.ts:12, 169-174`)

`refreshPromise` deduplicates concurrent refreshes WITHIN one
JavaScript context. Cross-tab races (each tab has its own promise but
both observed the same pre-rotation cookie) still produce two `/refresh`
calls carrying the same refresh JWT. Today both succeed because nothing
binds the token to a specific rotation; after Section 2 the second one
would fail unless we add a grace window.

## 2. Concept inventory

Seven orthogonal concepts. Each has a single source of truth.

### 2.1 `access_token_ttl` (bearer JWT lifetime)

Owned by `jwt_access_token_expire_minutes`. Default 15 min. Out of scope
for this spec; included only to clarify it is unaffected.

### 2.2 `refresh_idle_ttl_days` (NEW config knob)

Single source of truth for:

- the refresh JWT `exp` claim
- the cookie `max_age`
- the Redis `jti` key TTL (Section 4)

Default: **30 days**. Rationale: matches the architect's earlier
roadmap text and matches the current `session_lifetime_days` default,
so absent any org override the two TTLs co-terminate cleanly. Users
who genuinely stay away for 31 days have to re-authenticate, which is
healthy hygiene.

Bounds enforced at validator time: `1 <= refresh_idle_ttl_days <= 365`.

Env name: `REFRESH_IDLE_TTL_DAYS`. The legacy
`JWT_REFRESH_TOKEN_EXPIRE_DAYS` env is removed (pre-launch, no shim).

### 2.3 `session_lifetime_days` (absolute cap, KEEP)

Already in `config.py:27`. Default 30. Per-org override via
`OrgSetting(key="session_lifetime_days")` (already honoured at
`auth.py:418-430`). This is the "you must re-log even if you stay
active" knob.

Bounds enforced at the setting-write site: `1 <= value <= 365`. Today
no validation exists at that write site; this spec adds it.

Interaction with `refresh_idle_ttl_days`: the cookie may live longer
than `session_lifetime_days` if an org sets a tighter cap, but the
server enforces the absolute cap on every `/refresh` and `/verify`
regardless. A cookie that survives past `session_lifetime_days` from
the original login is rejected with `SESSION_EXPIRED_DETAIL` and
cleared. This already works.

### 2.4 `jti` (NEW per-session identifier)

Random 16-byte URL-safe token added to the refresh JWT payload.
Persisted in Redis under `auth:session:{jti}` (Section 4). Required for
every refresh JWT issued after PR 2 ships.

### 2.5 Rotation grace window (NEW)

After a refresh rotates the cookie, the previous `jti` remains
accepted for **30 seconds**. Implemented by a separate Redis key
`auth:session:grace:{jti}` set at rotation time with `EX 30` that
points to the rotated-successor's user_id (and optionally the
successor's `jti`, see Section 4.3).

Rationale: 30s is comfortably above the worst observed cross-tab
clock-skew window from PR #287's investigation (sub-1s in all captured
traces) but well below the access-token TTL so a stolen pre-rotation
cookie cannot be used to silently extend a session. Twice the access
token TTL would be 30 minutes which is too long; one second would be
too tight against typical network jitter. 30s is the compromise.

### 2.6 Per-session logout (NEW behaviour)

`POST /auth/logout` deletes ONLY the current session's Redis key
(`DEL auth:session:{jti}` and the grace key) and clears the cookie.
It NO LONGER writes `sessions_invalidated_at`. Other tabs and other
devices remain authenticated.

### 2.7 Global invalidation (UNCHANGED)

The five sites in Section 1.4 (excluding the logout site) keep writing
`sessions_invalidated_at = now()`. The resolver still rejects any
refresh JWT with `iat < token_cutoff(user)`. Section 6 enumerates the
full preserved trigger set.

## 3. JWT claim shape

### 3.1 Refresh JWT after this design

```jsonc
{
  "sub": "<user_id_str>",
  "type": "refresh",
  "iat": <unix_seconds_int>,
  "exp": <unix_seconds_int>,           // iat + refresh_idle_ttl_days
  "session_created_at": <float>,        // unix seconds, FIRST login only sets it
  "jti": "<token_urlsafe_16>"            // NEW, mandatory
}
```

`session_id` is intentionally NOT added. The `jti` doubles as the
session identifier: each rotation issues a new `jti`, but
`session_created_at` is preserved across rotations, so the (user_id,
session_created_at) tuple identifies the session for analytics and
audit. Storing a separate stable `session_id` would require a database
table; deferred until we want a /admin/sessions UI.

### 3.2 Access JWT

Unchanged. No `jti` on access tokens (15-minute TTL, no revocation
target).

## 4. Redis schema

### 4.1 Keys

| Key | Type | TTL | Value | Set by | Deleted by |
|-----|------|-----|-------|--------|-----------|
| `auth:session:{jti}` | string | `refresh_idle_ttl_days` | `"{user_id}"` | every login + every successful `/refresh` | per-session `/logout`, expiry |
| `auth:session:grace:{jti}` | string | 30s (rotation grace) | `"{user_id}"` | on `/refresh` rotation BEFORE the new key is written | natural expiry only |

The grace key's value is just `"{user_id}"` so the resolver can
sanity-check the JWT `sub` matches. We deliberately do NOT chain the
grace key to the successor `jti`. The downstream effect: a token
within its grace window is accepted but the rotation does NOT itself
re-rotate (returning the access token only) so a paranoid attacker
who steals an in-flight pre-rotation cookie cannot use it as a
rotation oracle. See Section 5.1.

### 4.2 Operation patterns

- **Login:** `SET auth:session:{new_jti} {user_id} EX {refresh_idle_ttl_days * 86400} NX`. Always succeeds because `jti` is random.
- **Refresh:**
    1. Resolve the inbound `jti` from the JWT.
    2. `GET auth:session:{jti}` first. If hit, normal path: rotate.
    3. If miss, `GET auth:session:grace:{jti}`. If hit, grace path: issue a new access token but do NOT rotate.
    4. If both miss, 401.
    5. On rotate, the order is: (a) `SET auth:session:grace:{old_jti} {user_id} EX 30`, (b) `SET auth:session:{new_jti} {user_id} EX {refresh_idle_ttl_days * 86400}`, (c) `DEL auth:session:{old_jti}`. The grace key is written FIRST so a crash between (b) and (c) cannot leave both the old primary AND no grace, which would make a concurrent racing tab fail.
- **Logout:** `DEL auth:session:{jti}` and `DEL auth:session:grace:{jti}` in a `MULTI/EXEC` so partial failure does not leave a stale grace ticket alive.
- **Global invalidation:** unchanged. Writes to `sessions_invalidated_at` and the resolver's `iat < token_cutoff` check kill every JWT issued before that moment regardless of Redis state. We do NOT bulk-delete Redis keys on global invalidation: the DB cutoff is authoritative, and the orphan keys age out via their TTL.

### 4.3 Why no Lua

The patterns above are short, the failure modes are bounded, and the
existing `redis_client.py` already handles connection retries. A Lua
script would be needed only if we wanted atomic rotate-and-grace in a
single round trip; the three-step rotation in 4.2 above is correct
even if any individual step fails (Section 7.1 covers the failure
modes).

## 5. Endpoint behaviour

### 5.1 `POST /api/v1/auth/refresh`

1. Run `_extract_refresh_cookies(request)` (unchanged).
2. For each candidate token, run `_validate_single_refresh_token` (existing chain: decode, type, user active, iat vs cutoff, absolute lifetime).
3. NEW step: `jti = payload["jti"]`. If missing, 401 (no legacy tokens, pre-launch policy).
4. NEW step: probe Redis:
    - `GET auth:session:{jti}` hit -> normal rotation path (Section 4.2 step 5).
    - `GET auth:session:grace:{jti}` hit -> grace path: issue access token only, return without `Set-Cookie`. The frontend already accepts a bare access token from `/refresh`.
    - Both miss -> 401 `"Session has been invalidated"` (same string today's cutoff check uses, so the frontend's terminal-vs-transient classifier needs no change).
5. Pick the winning token via existing `_validate_refresh_cookie` rules (same user, newest iat). Multi-user -> `AMBIGUOUS_SESSION_DETAIL`.
6. On rotation: issue access + refresh tokens. The new refresh JWT carries: same `session_created_at`, new `jti`, new `iat`, new `exp`. Write `Set-Cookie: refresh_token=...; Path=/; Max-Age={refresh_idle_ttl_days * 86400}; Secure; HttpOnly; SameSite=Lax`.
7. On grace path: issue access token only, no `Set-Cookie`.
8. Emit audit event `auth.session.rotated` (rotation path) or `auth.session.grace_accept` (grace path). Outcome=success. Detail carries `{old_jti, new_jti, path}` (grace path has no new_jti).

### 5.2 `POST /api/v1/auth/verify`

Unchanged in shape (no Set-Cookie invariant load-bearing for RSC).
Adds the `jti` Redis probe inline with `_validate_refresh_cookie`:
miss in primary AND grace -> raise the same 401 the existing chain
raises today. NO audit event (matches today's silent-success
contract).

### 5.3 `POST /api/v1/auth/logout`

NEW shape:

1. Read the refresh cookie (use `_extract_refresh_cookies`). Decode each value (no validation chain, just decode for the `jti`).
2. For every decoded `jti`, `DEL auth:session:{jti}` and `DEL auth:session:grace:{jti}` via MULTI/EXEC.
3. Best-effort: if the Authorization header carries a valid access token, also delete the `jti` it implicitly belongs to (we do not put `jti` on access tokens, so this is a no-op in practice; called out so Team I does not re-add `sessions_invalidated_at = now` thinking it is missing).
4. Clear the cookie at `Path=/` and the legacy path via the existing `_clear_legacy_refresh_cookie` helper.
5. DO NOT touch `sessions_invalidated_at`.
6. Emit audit event `auth.session.terminated`. Detail carries `{jti_count}` (number of jti values deleted). Outcome=success even when 0 (anonymous logout is still a clean cookie clear).

### 5.4 `POST /api/v1/auth/login`, `/auth/google/callback`, `_issue_tokens`, `/auth/refresh` (issue side)

Every site that calls `create_refresh_token` is updated to:

1. Generate `jti = secrets.token_urlsafe(16)` (or accept one from the caller for `/refresh`'s rotation site).
2. Pass it into `create_refresh_token`.
3. `SET auth:session:{jti} {user_id} EX {refresh_idle_ttl_days * 86400}` BEFORE `set_cookie` is called.
4. `set_cookie(... max_age=refresh_idle_ttl_days * 86400 ...)`. The 7d literals (Section 1.2) are replaced by a single helper: `_refresh_cookie_max_age()` returning `app_settings.refresh_idle_ttl_days * 86400`.

### 5.5 `GET /api/v1/auth/me`

Unchanged. Bearer-only, no cookie touch.

## 6. Global invalidation trigger set

After this spec, the following sites STILL write
`sessions_invalidated_at = now()`. Every refresh JWT issued before
`now` is rejected by the `iat < token_cutoff(user)` check.

| Site (file:line on main) | Trigger | Audit event |
|--------------------------|---------|-------------|
| `routers/auth.py:714` | password reset via token | `auth.password.reset` (verify) |
| `routers/users.py:241` | password change while signed in | `user.password.changed` (verify) |
| `routers/users.py:165` | email address change | `user.email.changed` (verify) |
| `services/invitation_service.py:286, 403` | invitation accept / role swap | invitation events |
| `services/admin_org_members_service.py:172` | admin deactivates a member | `admin.org_member.deactivated` |

**Removed from this set:** `routers/auth.py:644` (the current
self-logout). Replaced by the per-session DEL in Section 5.3.

Resolver detection (unchanged from today): `token_cutoff(user)` returns
`max(password_changed_at, sessions_invalidated_at)`. A refresh JWT
with `iat < token_cutoff` is rejected, the Redis `jti` is left to
expire on its own TTL, the cookie is cleared on the next `/refresh`
attempt.

## 7. Failure modes

### 7.1 Redis unavailable

**Decision: fail closed on writes, fail closed on reads.**

The `mfa_email_jti` precedent (in `auth.py:1100-1112`) already does
this: missing Redis in production raises 503. Same posture here.

- **Login when Redis is down:** return 503 `"Authentication temporarily unavailable"`. Do not issue a refresh cookie without a corresponding `jti` row, because then `/refresh` would always 401 against that user.
- **Refresh when Redis is down:** return 503. The cookie remains in the browser; the user retries.
- **Logout when Redis is down:** clear the cookie locally, return 200 with detail `{"redis_unreachable": true}`. We swallow the error because the cookie clear is the user-visible effect; the orphan `jti` rows age out on their own TTL. Failing closed here would prevent users from at least getting the cookie out of their browser.
- **Verify when Redis is down:** return 503. RSC pages will get the same loading shell they get today on any other transient `/verify` failure; the frontend's transient-vs-terminal classifier already treats 503 as transient (PR #287).

Counter-argument (fail open): "rate_limit.degraded" in PR #285 chose
fail-open on Redis-unreachable. Why fail-closed here? Because rate
limiting is a defensive bonus; auth-session integrity is the primary
trust boundary. A fail-open auth path lets an attacker who controls
the Redis network bypass the `jti` rotation guarantee entirely.

### 7.2 Redis stale (key expired before cookie did)

Cookie `max_age` and `jti` TTL are both derived from
`refresh_idle_ttl_days`, set within microseconds of each other. The
only way they desync is a Redis flush or a clock-skew clock change.
In either case the result is a 401 on `/refresh`. The frontend
classifies the response detail (`"Session has been invalidated"`) as
terminal and routes the user to `/login`. Acceptable.

### 7.3 Clock skew between app replicas

JWT `exp` is encoded in absolute unix seconds, so the only skew that
matters is between the app and Redis. Redis TTL drift is bounded by
the kernel; we have never observed > 1s drift in DO managed Redis.
The 30s grace window absorbs this and more.

### 7.4 `jti` reuse

The `jti` is 16 bytes urlsafe = 128 bits of entropy = collision
probability per (lifetime, scale) is negligible. We do not add a
collision check on `SET`; the `NX` guard (Section 4.2) catches it
defensively and the caller retries with a new `jti`.

### 7.5 Cross-tab refresh race

Two tabs, same browser, both have the pre-rotation refresh cookie.
Tab A wins `/refresh` first. Tab A's cookie is now the new `jti`. Tab
B's in-flight `/refresh` still carries the old `jti`. Without the
grace window, Tab B gets 401 and the user is logged out of Tab B.

With the 30s grace window: Tab B's request hits the grace key, gets
an access token only (no new cookie), and continues. The next time
Tab B makes a `/refresh` it will use the new cookie that the browser
synced from Tab A's response. Grace window has bought enough time for
the browser cookie store to sync across tabs (typically sub-second
in Chrome/Firefox).

### 7.6 Replay of an already-rotated refresh token outside grace window

Old `jti` is gone from Redis; the grace key has expired. `/refresh`
returns 401. The frontend classifies as terminal, clears in-memory
state, redirects to /login. Same as today's expired-cookie behaviour.

## 8. Rollout plan

Four PRs. Each independently shippable. Pre-launch policy: NO backcompat
shims, NO data migrations, NO env-var aliases.

### PR 1: Consolidate cookie `max_age` + introduce `refresh_idle_ttl_days`

Scope:

- Add `refresh_idle_ttl_days: int = 30` to `config.py` with validator `1 <= v <= 365`.
- Remove `jwt_refresh_token_expire_days` from `config.py` (hard delete, pre-launch).
- Update `create_refresh_token` in `security.py` to read `refresh_idle_ttl_days`.
- Add `_refresh_cookie_max_age()` helper in `routers/auth.py`.
- Replace all four `max_age=7*24*60*60` literals (lines 295, 562, 833, 1483 today) with `_refresh_cookie_max_age()`.
- Update `ENVIRONMENT.md` for the new env var.
- Tests: cookie `Max-Age` attribute on login / refresh / Google callback / MFA branches matches the configured value; changing the env var changes all four sites in lockstep.

Why this PR first: trivially safe, ships the visible "session length"
knob the operator has been asking for, and unblocks the rest of the
sequence by removing the bare literal that would otherwise be a merge
hazard for PR 2's `jti` plumbing.

### PR 2: Refresh `jti` + Redis primary key

Scope:

- Add `jti` claim to refresh JWT (`security.py`).
- Add Redis schema (Section 4) primary key only (no grace yet).
- Update every issue site (login, refresh-rotation, Google callback, MFA branches) to generate `jti` and write the primary Redis key.
- Update `_validate_single_refresh_token` to require `jti` and probe the primary Redis key. Miss -> 401 `"Session has been invalidated"`.
- Tests: legacy (no-jti) token is rejected; new token validates iff the Redis key is present; manual Redis DEL produces 401.

Pre-launch policy means we do NOT keep the old non-jti path alive.

### PR 3: Rotation grace window

Scope:

- Add `auth:session:grace:{jti}` key with 30s TTL on rotation.
- Update `/refresh` and `/verify` to fall back to the grace key.
- Tests: cross-tab race simulation (two concurrent refreshes with the same pre-rotation cookie) both succeed; replay 31s after rotation fails.

### PR 4: Per-session logout

Scope:

- Change `/auth/logout` per Section 5.3.
- Remove the `sessions_invalidated_at = now` write from the logout path only. All other sites in Section 6 keep theirs.
- Add audit event types `auth.session.terminated`, `auth.session.rotated`, `auth.session.grace_accept`.
- Tests: logging out of Tab A leaves Tab B's session valid; password change still nukes both; admin deactivation still nukes all.

### Migration deltas

**Zero.** Pre-launch, no users, no production session state to
preserve. `sessions_invalidated_at` column stays as-is for the global
invalidation triggers.

### Backward compatibility

**Zero.** Legacy refresh JWTs without `jti` are rejected after PR 2
ships. Any developer/QA session in flight at deploy time will get a
single 401, which the frontend handles cleanly by redirecting to
`/login`. Acceptable for pre-launch.

## 9. Test plan

Surface for Team I to implement. Pin each at unit + endpoint level.

### Cookie `max_age` from config

- GIVEN `REFRESH_IDLE_TTL_DAYS=10`
- WHEN any of `/login`, `/refresh`, `/google/callback`, MFA branches emits a `Set-Cookie: refresh_token`
- THEN the `Max-Age` attribute equals `10 * 86400`.

### Absolute lifetime rejection

- GIVEN a refresh JWT whose `session_created_at` is 31 days in the past AND org's `session_lifetime_days` setting is 30
- WHEN `/refresh` is called
- THEN response is 401 with `SESSION_EXPIRED_DETAIL`, cookie is cleared.

### Org-override absolute lifetime (existing behaviour preserved)

- GIVEN an org with `OrgSetting(session_lifetime_days=60)`
- AND a refresh JWT whose `session_created_at` is 45 days old
- THEN `/refresh` rotates successfully.

### Grace-window acceptance

- GIVEN tab A's `/refresh` rotates `jti_A` -> `jti_B` at T0
- AND tab B's `/refresh` arrives at T0+15s carrying `jti_A`
- THEN tab B receives a 200 with an access token AND no `Set-Cookie`.

### Post-grace rejection

- Same as above but tab B arrives at T0+31s
- THEN 401 `"Session has been invalidated"`.

### Per-session logout isolation

- GIVEN tab A is logged in with `jti_A`, tab B with `jti_B` (same user)
- WHEN tab A calls `/auth/logout`
- THEN tab B's next `/refresh` still rotates successfully.

### Global invalidation triggers (regression pin)

For each of the five sites in Section 6:

- GIVEN a valid refresh JWT exists in Redis
- WHEN the global-invalidation trigger fires (password change, password reset, email change, admin deactivate, invitation accept)
- THEN the next `/refresh` returns 401 with `"Session has been invalidated"`.

### Redis unavailable behaviour

For each endpoint (`/login`, `/refresh`, `/verify`):

- GIVEN `REDIS_URL` is empty AND `app_env=production`
- THEN response is 503 (not 200, not 401).

For `/auth/logout`:

- GIVEN `REDIS_URL` is empty
- THEN response is 200, cookie is cleared, body includes a soft diagnostic flag.

### Concurrent rotation (no double-issue)

- Two concurrent `/refresh` calls carrying the same `jti_A` should produce: (a) one rotation, (b) one grace acceptance, (c) zero 401s. Pin with `asyncio.gather`.

### Settings validation

- GIVEN `REFRESH_IDLE_TTL_DAYS=0` or `>365`
- THEN Settings validation fails at process boot.
- GIVEN org admin writes `OrgSetting(session_lifetime_days=0)` via the admin endpoint
- THEN the request is rejected with 400.

## 10. Acceptance criteria

### AC1: Single cookie TTL source of truth

GIVEN the operator changes `REFRESH_IDLE_TTL_DAYS` and restarts the
backend, WHEN they log in, THEN the new refresh cookie's `Max-Age`
matches the new value at every entry point. There are no remaining
hardcoded `7*24*60*60` literals in `backend/app/routers/auth.py`.

### AC2: Per-session logout

GIVEN a user is signed in across multiple browser tabs or devices,
WHEN they click "Log out" on one, THEN every other tab/device remains
signed in until its own session expires or is explicitly logged out.

### AC3: Cross-tab refresh race no longer logs the user out

GIVEN two browser tabs share a refresh cookie and refresh within a
30s window, WHEN both refresh attempts complete, THEN both tabs hold a
valid access token and the user remains signed in.

### AC4: Global invalidation preserved

GIVEN a user has active sessions on five devices, WHEN they change
their password, OR an admin deactivates them, OR they reset via email
token, OR they accept an org invitation, OR their email is changed,
THEN every refresh JWT issued before that moment is rejected on its
next refresh attempt.

### AC5: Redis unavailable fails closed for issuance, fails open for cookie clear

GIVEN Redis is unreachable, WHEN a user tries to log in, THEN they
receive a 503 and no refresh cookie is set. WHEN a signed-in user
clicks log out, THEN the cookie is still cleared from their browser.

### AC6: Absolute lifetime cap still enforced

GIVEN an org sets `session_lifetime_days=60`, WHEN a user logs in and
remains active for 61 days, THEN their next `/refresh` after the
61-day mark is rejected and they are redirected to `/login`.

## 11. Open questions for the operator

1. **`refresh_idle_ttl_days` default: 30 days vs 60 days?** The earlier memo (project_session_lifetime_60d.md) mentioned 60d as the operator's mental model. Choosing 30 keeps the cookie and the absolute cap aligned by default; choosing 60 means a user has to re-auth on the absolute-cap rule, not the idle one, which is the intended UX. The spec defaults to 30 to match the existing `session_lifetime_days` default, but the operator may prefer 60.

2. **Grace window length: 30s vs 60s vs 5min?** 30s is comfortable for cross-tab races (the dominant case) and tight against theft. 5min would also cover an offline-to-online transition (e.g. laptop wake from sleep where the cookie store has not yet synced) at the cost of a wider window. No data yet on how often offline transitions produce this failure pattern.

3. **Per-org override of `refresh_idle_ttl_days`?** Section 2.3 keeps `session_lifetime_days` org-overridable (already supported). Do we want the same for the idle TTL? Punting until an org actually asks for it. Spec records "no" by default.

4. **Should `/auth/logout` ALSO clear sibling tabs for the same browser?** Per-session logout means tabs of the same browser stay signed in. If the operator wants "logout = this browser, all tabs", we need a separate broadcast mechanism (BroadcastChannel API on the frontend). Out of scope for this spec; calling it out so the operator can decide whether to file a follow-up.

5. **Audit event names: `auth.session.*` vs `session.*` vs `auth.refresh.*`?** This spec proposes `auth.session.rotated`, `auth.session.grace_accept`, `auth.session.terminated`. Aligning with existing `user.login.success` / `auth.google.callback.failed` shape suggests `auth.*` is the right top-level. Confirm with the operator before PR 4.

6. **Should the spec ban `sessions_invalidated_at` writes outside the enumerated trigger set (e.g. add a lint rule)?** The 2026-05-16 incident's root cause was that logout incorrectly used the global broadcast mechanism. A grep-based lint that flags new writes outside the allowlist would help future PRs avoid the same trap. Defer to Team I.

7. **Migration of in-flight QA sessions on the dev stack?** Pre-launch policy says no shim. Confirm operator is OK with QA testers seeing a single 401 after PR 2 ships; they re-log and continue.
