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

`POST /auth/logout` revokes the entire **session family** for the
calling browser/device — every `jti` ever issued under the same
`sid`, including any outstanding rotation-grace ticket. It clears
the refresh cookie. It does NOT write `sessions_invalidated_at`.
Other devices and other browser profiles remain authenticated.

**Same-browser tab semantics (architect feedback P1.2).** The refresh
cookie lives in the browser's cookie jar, which is shared across all
tabs in the same browser profile. Logout clears the cookie for the
entire profile; the session-family revoke in Redis (Section 4.2)
makes sure no still-in-flight refresh from a sibling tab in that
same profile can re-authenticate. So "per-session logout" means
**this refresh-cookie session / device**, NOT "this one tab" — that
was the wording error in the earlier draft. Other tabs in the same
browser will continue to render using their in-memory access token
until that token expires (15 min TTL); after expiry their next API
call hits 401 + the refresh attempt also 401s + the user is sent to
`/login`. If we want sibling tabs to react immediately, that is a
frontend `BroadcastChannel("auth")` follow-up — it does not keep
them signed in, it just speeds up their redirect-to-`/login`. Out of
scope for this spec.

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
  "jti": "<token_urlsafe_16>",           // NEW, mandatory, rotates each refresh
  "sid": "<uuid4_hex>"                   // NEW, mandatory, STABLE across rotations
}
```

Two distinct identifiers (architect feedback on PR #301):

- **`jti`** rotates on every `/refresh`. It identifies one specific
  refresh token in time. Used as the Redis primary key
  (`auth:session:{jti}`), as the grace-key suffix
  (`auth:session:grace:{jti}`), and as a member of the family set.
- **`sid`** is a UUID4 minted at first login and **preserved verbatim
  on every rotation**. It identifies the session FAMILY (the chain
  of refresh tokens that descend from a single login). Used as the
  family-set key (`auth:session:by_sid:{sid}`) and as the conditional
  guard for rotation (Section 4.2 Lua) and the defence-in-depth check
  on grace acceptance (Section 5.1 step 4).

Earlier drafts said the `jti` alone could double as the session
identifier and that a separate `sid` was deferred until a future
/admin/sessions UI. That direction is reversed by the family-revoke
fix for P1.1: without a stable `sid` the logout path cannot atomically
revoke every refresh token descended from one login, and the
rotation path cannot check "this session has not been logged out"
without inferring it from `jti`-level scans. `sid` is therefore
required by this design, not optional.

### 3.2 Access JWT

Unchanged. No `jti` on access tokens (15-minute TTL, no revocation
target).

## 4. Redis schema

### 4.1 Keys

| Key | Type | TTL | Value | Set by | Deleted by |
|-----|------|-----|-------|--------|-----------|
| `auth:session:{jti}` | string | `refresh_idle_ttl_days` | JSON `{"user_id": int, "sid": "<session_id>"}` | every login (new `sid`) + every successful `/refresh` rotation (same `sid`) | per-session `/logout` (deletes EVERY key for this `sid`), expiry |
| `auth:session:grace:{jti}` | string | 30s (rotation grace) | JSON `{"user_id": int, "sid": "<session_id>", "successor_jti": "<new_jti>"}` | on `/refresh` rotation BEFORE the new key is written | per-session `/logout` (deletes EVERY key for this `sid` via the index below), expiry |
| `auth:session:by_sid:{sid}` | set | `refresh_idle_ttl_days` (refreshed on every rotation) | every `jti` issued for this `sid` | every login + every rotation | per-session `/logout`, expiry |

**Session-family `sid` (architect feedback on PR #301, P1.1.)** Each
login mints a fresh opaque `session_id` (UUID4, written into the JWT
as the `sid` claim — see Section 3.1) that is **stable across the
rotation chain**: every `/refresh` issues a NEW `jti` but carries the
SAME `sid`. The per-`sid` Redis set indexes every `jti` ever issued
under that session so logout can revoke the entire family (current
primary + any outstanding grace ticket from the just-rotated
predecessor + any in-flight rotation that has not yet landed).
Without this family link, the original spec's logout could leave a
30-second window where a pre-rotation cookie still authenticates.

The grace key's value carries the `successor_jti` so that, once
logout has run and the primary key is gone, the resolver can
distinguish "grace key for a session that was just rotated normally"
(should accept, briefly) from "grace key whose entire family was
just logged out" (should reject). The logout path deletes ALL keys
for the `sid` atomically (Section 5.3); the resolver also performs a
defence-in-depth check against the `by_sid` set on every grace-path
acceptance (Section 5.1 step 4). We deliberately do NOT issue a
fresh cookie on the grace path (no rotation oracle) — see
Section 5.1.

### 4.2 Operation patterns

All multi-key mutations use **atomic Redis primitives** — Lua for
the rotation path (Section 4.2 step 5; conditional logic with three
guards), `MULTI/EXEC` for login and logout (unconditional batches).
The original sequential three-step rotation was not safe — architect
feedback P1.3 — and was replaced first by `MULTI/EXEC` and then by
Lua once the third-pass review showed that even an atomic
five-write block cannot prevent two concurrent `/refresh` callers
from both passing `SISMEMBER` and both rotating.

- **Login:** mint fresh `sid` (UUID4). In one `MULTI/EXEC`: `SET auth:session:{new_jti}` (with `{user_id, sid}` JSON value), `SADD auth:session:by_sid:{sid} {new_jti}`, `EXPIRE auth:session:by_sid:{sid} {idle_ttl}`. All atomic; on EXEC failure the client receives 503 and retries (Section 7.1).

- **Refresh:**
    1. Resolve `jti` and `sid` from the JWT.
    2. `GET auth:session:{jti}` first. If hit, normal path: rotate (step 5).
    3. If miss, `GET auth:session:grace:{jti}`. If hit, grace path: step 4.
    4. **Grace-path defence-in-depth (architect feedback P1.1).** Before accepting the grace ticket, also `EXISTS auth:session:by_sid:{sid}`. If the family set is gone (logout ran since the rotation), reject with 401 even though the grace key itself has not yet expired. Otherwise: issue an access token only. Do NOT rotate (no new refresh cookie — no rotation oracle).
    5. **Rotate (normal path), one atomic Lua script** (architect feedback PR #301 third-pass — see Section 4.3 for the races this closes). The Lua script is the authority; the earlier app-side `GET` (step 2) is purely an optimisation hint to choose which branch to enter:
       ```lua
       -- KEYS[1] = auth:session:{old_jti}
       -- KEYS[2] = auth:session:grace:{old_jti}
       -- KEYS[3] = auth:session:{new_jti}
       -- KEYS[4] = auth:session:by_sid:{sid}
       -- ARGV[1] = grace TTL seconds (30)
       -- ARGV[2] = idle TTL seconds (refresh_idle_ttl_days * 86400)
       -- ARGV[3] = grace JSON value
       -- ARGV[4] = primary JSON value
       -- ARGV[5] = old_jti
       -- ARGV[6] = new_jti

       -- (1) Family revoked? Concurrent /logout ran.
       if redis.call("SISMEMBER", KEYS[4], ARGV[5]) == 0 then
           return {err = "session_revoked"}
       end
       -- (2) Already rotated? Concurrent /refresh won the race.
       --     The earlier app-side GET cannot prevent two requests
       --     reaching this point with the same old_jti; this check
       --     inside Lua is the authority.
       if redis.call("EXISTS", KEYS[1]) == 0 then
           return {err = "already_rotated"}
       end
       -- (3) Defensive NX on new primary. 128-bit jti collisions are
       --     astronomically unlikely but overwriting a live session
       --     key is the wrong failure mode. On collision the router
       --     regenerates the jti and retries (at most once).
       --     IMPORTANT: ``SET ... NX`` returns ``nil`` (NOT ``false``)
       --     on a miss in Lua. Comparing against ``false`` makes the
       --     guard a no-op. Use ``not set_ok`` instead.
       local set_ok = redis.call("SET", KEYS[3], ARGV[4], "EX", ARGV[2], "NX")
       if not set_ok then
           return {err = "jti_collision"}
       end
       -- (4) Write grace, register the new jti in the family, delete the old primary.
       redis.call("SET", KEYS[2], ARGV[3], "EX", ARGV[1])
       redis.call("SADD", KEYS[4], ARGV[6])
       redis.call("EXPIRE", KEYS[4], ARGV[2])
       redis.call("DEL", KEYS[1])
       return "ok"
       ```

       Three guards inside the script, each load-bearing:

       - **`SISMEMBER` (family revoked check).** A concurrent `/logout` has deleted the family set. Router returns 401 `"Session has been invalidated"` (same string as cutoff; the frontend's terminal-vs-transient classifier needs no change). This closes the logout-vs-rotation race documented in Section 4.3.
       - **`EXISTS old primary` (already-rotated check).** Two concurrent `/refresh` requests with the same `old_jti` could both pass the earlier app-side `GET auth:session:{old_jti}` HIT and both enter this script. Without this check, both would pass `SISMEMBER` (the family still contains `old_jti` until the winner's `DEL`), and both would mint different successor tokens — the test at Section 9 "Concurrent rotation (no double-issue)" would fail. With the check, the loser sees the winner's `DEL old primary` and returns `already_rotated`. The router maps `already_rotated` to the **grace branch** (Section 5.1 step 4): look up `auth:session:grace:{old_jti}` — which the winner just wrote — verify the family set still exists, issue access-only, no Set-Cookie. The loser's user gets a fresh access token without disturbing the winner's new refresh cookie.
       - **`SET ... NX` (jti collision guard).** 128 bits of urlsafe entropy makes collisions cosmically improbable but not impossible; overwriting a live session is the wrong failure mode. On collision the router regenerates `jti` once and re-runs the script. If it collides twice in a row, return 503 — the operator has bigger problems (RNG broken).

       Lua executes atomically server-side; partial application is not possible. If the script errors before returning (Redis disconnect mid-script — extremely rare), the pre-rotation state is fully preserved; client gets 503 and retries.
    6. If primary AND grace both miss, 401 `"Session has been invalidated"`.

- **Logout (per-session, architect feedback P1.1 — atomic family revoke):**
    1. Read every refresh cookie value via `_extract_refresh_cookies`. Decode each, extract the `sid`. Typical case: one `sid` (single browser, single cookie).
    2. For each distinct `sid`:
       ```
       -- Step 1: read the family + delete the set in one transaction
       MULTI
         SMEMBERS auth:session:by_sid:{sid}
         DEL auth:session:by_sid:{sid}
       EXEC
       -- Step 2: for each jti returned, delete primary + grace keys
       MULTI
         DEL auth:session:{jti_1}
         DEL auth:session:grace:{jti_1}
         DEL auth:session:{jti_2}
         DEL auth:session:grace:{jti_2}
         ...
       EXEC
       ```
       After step 1 the family set is gone, so step 4 of the refresh path (`EXISTS by_sid:{sid}`) returns 0 and rejects any in-flight grace ticket — even if its 30s TTL has not yet expired and step 2 has not yet run. That closes the architect's reported logout/grace bug.
    3. Clear the cookie at `Path=/` and the legacy path via the existing `_clear_legacy_refresh_cookie` helper.
    4. DO NOT touch `sessions_invalidated_at` (that's the global-invalidation path; see Section 6).
    5. Emit audit event `auth.session.terminated`. Outcome=success even when 0 jtis were found (anonymous logout is still a clean cookie clear).

- **Global invalidation:** unchanged. Writes to `sessions_invalidated_at` and the resolver's `iat < token_cutoff` check kill every JWT issued before that moment regardless of Redis state. We do NOT bulk-delete Redis keys on global invalidation: the DB cutoff is authoritative, and the orphan keys age out via their TTL.

### 4.3 Why Lua for rotation, MULTI/EXEC for login + logout

The architect's first review of this spec (PR #301 P1.3) caught that a
sequential SET-new / SET-grace / DEL-old shape is not safe under
partial failure: if `DEL old_primary` fails after `SET new_primary`
the old `jti` accepts as primary for the full idle TTL, not just
30 seconds. The architect's **second** review (PR #301 follow-up)
caught a stronger race that MULTI/EXEC alone cannot close:

```
T0  Logout reads cookie, decodes sid=S
T1  /refresh handler (different request, same sid) runs step 2:
      GET auth:session:{old_jti}  -> HIT
T2  Logout runs Step 1: DEL auth:session:by_sid:{S}  (family gone)
T3  Logout runs Step 2: DEL primary + grace keys
T4  /refresh handler proceeds to step 5 rotation:
      SET new_primary, SET grace, SADD by_sid:S new_jti, DEL old
      -> SUCCEEDS unconditionally, re-creating the family for the
         full idle TTL.
T5  User holds the new refresh cookie. Logout never took effect.
```

MULTI/EXEC cannot prevent this because all five writes in the
transaction succeed unconditionally; there is no "abort if the
family set is gone" primitive. WATCH/MULTI works but needs an
application-side retry loop and is awkward under concurrent
rotations of the same family. **Lua is the cleanest fit:** it runs
atomically server-side and we can express the conditional check
("only rotate if `sid` is still alive AND `old_jti` is still its
member") as a single `SISMEMBER` call before any writes.

The full Lua script for rotation lives in Section 4.2 step 5. It
is **not** a post-launch follow-up — it is the production rotation
path required by PR 3. Login and logout stay on MULTI/EXEC (no
conditional check needed: login always writes a fresh family,
logout always tears down an existing one).

The grace-path defence-in-depth check (Section 5.1 step 4,
`EXISTS auth:session:by_sid:{sid}`) is kept as belt-and-braces: it
catches the same race for the grace acceptance branch where no
rotation happens and therefore the Lua script is not run.

The full production Lua script — with all three guards (family
revoked, already rotated, jti collision) and the `SET ... NX`
primary write — lives in Section 4.2 step 5. Team I should treat
that block as the only authoritative copy in this spec. Earlier
drafts of this section carried an illustrative sketch missing the
new guards; it was removed during the PR #301 third-pass review
to avoid copy-paste mistakes.

## 5. Endpoint behaviour

### 5.1 `POST /api/v1/auth/refresh`

1. Run `_extract_refresh_cookies(request)` (unchanged).
2. For each candidate token, run `_validate_single_refresh_token` (existing chain: decode, type, user active, iat vs cutoff, absolute lifetime).
3. NEW step: `jti = payload["jti"]` and `sid = payload["sid"]`. If **either** is missing, 401 (no legacy tokens, pre-launch policy). Both claims are mandatory after PR 2 ships.
4. NEW step: probe Redis:
    - `GET auth:session:{jti}` hit -> normal rotation path (Section 4.2 step 5 — the Lua script checks `sid` membership atomically before writing).
    - `GET auth:session:grace:{jti}` hit -> read the grace value and confirm its stored `sid` matches the JWT's `sid` (defence against an attacker minting a JWT with someone else's `jti` + their own `sid`). Then `EXISTS auth:session:by_sid:{sid}` — if the family set is gone, 401 (logout ran). Otherwise grace path: issue access token only, return without `Set-Cookie`. The frontend already accepts a bare access token from `/refresh`.
    - Both miss -> 401 `"Session has been invalidated"` (same string today's cutoff check uses, so the frontend's terminal-vs-transient classifier needs no change).
5. Pick the winning token via existing `_validate_refresh_cookie` rules (same user, newest iat). Multi-user -> `AMBIGUOUS_SESSION_DETAIL`.
6. On rotation: run the Lua script (Section 4.2 step 5) and dispatch on its return value:
    - **`"ok"`**: issue access + refresh tokens. The new refresh JWT carries: same `session_created_at`, **same `sid`**, new `jti`, new `iat`, new `exp`. Write `Set-Cookie: refresh_token=...; Path=/; Max-Age={refresh_idle_ttl_days * 86400}; Secure; HttpOnly; SameSite=Lax`.
    - **`{err = "session_revoked"}`** (concurrent logout): 401 with the same `"Session has been invalidated"` string. Do NOT write a Set-Cookie.
    - **`{err = "already_rotated"}`** (concurrent `/refresh` won the race): fall through to the grace branch. Re-probe `auth:session:grace:{old_jti}` (the winner just wrote it inside their Lua transaction) AND `EXISTS auth:session:by_sid:{sid}`. On both hits, issue access token only, no Set-Cookie — same shape as the original grace path. On either miss (grace already expired or family revoked since), 401.
    - **`{err = "jti_collision"}`** (defensive NX guard hit a 128-bit collision): regenerate `jti` and re-run the Lua script once. If the second attempt also collides, return 503 — the operator's RNG is broken and we want a loud failure, not a silent overwrite.
7. On grace path (entered directly from step 4 OR via `already_rotated` in step 6): issue access token only, no `Set-Cookie`. `sid` is NOT rotated either (no new refresh token is minted at all).
8. Emit audit event:
    - `auth.session.rotated` on Lua `"ok"`. Detail: `{old_jti, new_jti, sid}`.
    - `auth.session.grace_accept` on grace path (direct OR via `already_rotated`). Detail: `{old_jti, sid, via_already_rotated: bool}` so operators can distinguish "tab race" from "in-flight rotation race".
    - `auth.session.terminated` is logout-only — see Section 5.3.
9. The rotation-loser's path (Lua returns `already_rotated`) is the single most subtle case for Team I to test. Section 9 "Concurrent rotation (no double-issue)" pins it explicitly: two concurrent `/refresh` with the same `old_jti` must produce exactly one rotation (one new Set-Cookie), one grace acceptance (the loser, access-only), and zero 401s.

### 5.2 `POST /api/v1/auth/verify`

Unchanged in shape (no Set-Cookie invariant load-bearing for RSC).
Adds the `jti` + `sid` Redis probe inline with `_validate_refresh_cookie`:
`GET auth:session:{jti}` hit, OR `GET auth:session:grace:{jti}` hit
AND `EXISTS auth:session:by_sid:{sid}` = 1 -> success. Otherwise raise
the same 401 the existing chain raises today. The family-set check on
the grace branch mirrors `/refresh` step 4 — without it, `verify`
would silently accept a grace ticket that `refresh` would reject.
NO audit event (matches today's silent-success contract).

### 5.3 `POST /api/v1/auth/logout`

NEW shape (architect feedback on PR #301 — revoke by `sid` family, not by `jti`):

1. Read the refresh cookie (use `_extract_refresh_cookies`). Decode each value (no validation chain, just decode for the `sid` and `jti`).
2. Collect the distinct `sid` values across all decoded cookies (typical case: one).
3. For each distinct `sid`, run the **atomic family revoke** from Section 4.2:
    - Step A (MULTI/EXEC): `SMEMBERS auth:session:by_sid:{sid}` then `DEL auth:session:by_sid:{sid}`.
    - Step B (MULTI/EXEC): for every `jti` returned by step A, `DEL auth:session:{jti}` and `DEL auth:session:grace:{jti}`.
   Step A's atomic delete of the family set is what closes the architect's PR #301 follow-up race: any concurrent `/refresh` rotation Lua script will see `SISMEMBER` return 0 after step A lands and refuse to write a successor (Section 4.2 step 5). Step B's deletes are then strictly cleanup of orphan keys.
4. The Lua rotate script in Section 4.2 step 5 also handles the inverse race: a logout that begins AFTER an in-flight rotation has already SADD'd the new `jti` will simply find the new `jti` in the family set during step A's SMEMBERS and delete it in step B.
5. Best-effort: if the Authorization header carries a valid access token, no extra work (we do not put `jti` on access tokens). Called out so Team I does not re-add `sessions_invalidated_at = now` thinking it is missing.
6. Clear the cookie at `Path=/` and the legacy path via the existing `_clear_legacy_refresh_cookie` helper.
7. DO NOT touch `sessions_invalidated_at`.
8. Emit audit event `auth.session.terminated`. Detail carries `{sid_count, jti_count}` (number of distinct sessions revoked, total `jti` values deleted across all families). Outcome=success even when 0 (anonymous logout is still a clean cookie clear).

### 5.4 `POST /api/v1/auth/login`, `/auth/google/callback`, `_issue_tokens`, `/auth/refresh` (issue side), `POST /api/v1/orgs/invitations/accept`

All FIVE issue sites — login password, `/refresh` rotation, MFA branches via `_issue_tokens`, Google callback, AND `routers/org_members.py` invitation accept — call `create_refresh_token` and must populate Redis identically. PR #305's review found `org_members.py` had been missed in PR 1; the same trap is structurally easy to fall back into, so the grep-style guard test in §9 pins every `create_refresh_token` call site to a paired Redis write within the same source file.

Every site that calls `create_refresh_token` is updated to:

1. Generate `jti = secrets.token_urlsafe(16)`.
2. **Login / Google callback (new session):** generate `sid = uuid4().hex`. **`/refresh` rotation site:** pass the existing `sid` through (read from the decoded predecessor JWT) — DO NOT mint a new one. This is what makes the family stable across the rotation chain.
3. Pass both `jti` and `sid` into `create_refresh_token`, which writes them as claims.
4. Issuance Redis writes:
    - **Login / Google callback:** one `MULTI/EXEC` — `SET auth:session:{jti}` with JSON `{"user_id", "sid"}` value, `SADD auth:session:by_sid:{sid} {jti}`, `EXPIRE auth:session:by_sid:{sid} {refresh_idle_ttl_days*86400}`. All before `set_cookie`.
    - **`/refresh` rotation site:** the atomic Lua script from Section 4.2 step 5 handles every write.
5. `set_cookie(... max_age=refresh_idle_ttl_days * 86400 ...)`. The 7d literals (Section 1.2) are replaced by a single helper: `_refresh_cookie_max_age()` returning `app_settings.refresh_idle_ttl_days * 86400`.

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
probability per (lifetime, scale) is cosmically negligible. The
rotation Lua script (Section 4.2 step 5, check (3)) still uses
`SET ... NX` defensively because overwriting a live session key is
the wrong failure mode. On a collision the router regenerates `jti`
and re-runs the script once; a second collision in a row trips a
503 (loud failure, signals an RNG problem rather than data loss).

Login + Google-callback paths use a separate `MULTI/EXEC` (no Lua,
no `NX`) because there is no existing key to collide with — `jti`
has just been generated and nothing else has had a chance to write
the same key. If we ever observe a real-world collision at issue
time, that itself is a signal worth alerting on.

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

### PR 2: Refresh `jti` + `sid` + primary key + family set

Scope (architect-revised PR #301 third-pass — the original "primary key only, sid in PR 3" split is unsafe because PR 3 introduces the Lua rotation that depends on the family set existing for every session, and we must not have a tranche of sessions in production that pre-date the family set):

- Add `jti` claim AND `sid` claim to refresh JWT (`security.py`). Both mandatory.
- Add Redis schema (Section 4.1) — `auth:session:{jti}` (primary) AND `auth:session:by_sid:{sid}` (family set). NO grace key in this PR.
- Update every issue site (login, Google callback, MFA branches) to generate `jti`, generate `sid`, and write both keys in one `MULTI/EXEC`.
- Update the `/refresh` rotation issue site to generate `jti` and **preserve `sid`** from the predecessor JWT.
- Pre-launch: NO Lua yet. Rotation in PR 2 uses sequential `SET new primary / SADD by_sid / DEL old primary` — this is technically the unsafe shape from architect P1.3, but in PR 2 there is no grace key and no concurrent-logout-vs-rotation race surface, so the partial-failure window is narrow and tolerated for one PR cycle. PR 3 replaces it with Lua.
- Update `_validate_single_refresh_token` to require `jti` AND `sid` and probe the primary Redis key. Miss -> 401 `"Session has been invalidated"`.
- Tests: legacy (no-jti / no-sid) token is rejected; new token validates iff the Redis primary key is present; manual Redis `DEL` produces 401; `sid` is preserved across one rotation; family set membership matches the issued `jti` chain.

Pre-launch policy means we do NOT keep the old non-jti path alive.

### PR 3: Rotation grace window + Lua rotation + verify fallback

Scope:

- Add `auth:session:grace:{jti}` key with 30s TTL on rotation.
- Replace the sequential rotation shape from PR 2 with the **production Lua script** (Section 4.2 step 5). Wire the three return values (`ok`, `session_revoked`, `already_rotated`, `jti_collision`) per Section 5.1 step 6.
- Update `/refresh` to fall back to the grace key on app-side `GET old primary` miss (Section 5.1 step 4) AND to fall through to the grace branch when Lua returns `already_rotated`.
- Update `/verify` per Section 5.2 (probe grace + EXISTS family).
- Add the `auth.session.rotated` and `auth.session.grace_accept` audit event types.
- Tests: cross-tab race simulation (two concurrent refreshes with the same pre-rotation cookie) produces exactly one rotation + one grace acceptance + zero 401s; replay 31s after rotation fails; concurrent rotation Lua-race pin (the canonical "no double-issue" test); `jti` collision path under a forced-collision RNG (script returns `jti_collision` once, router retries, second attempt succeeds).

### PR 4: Per-session logout — family revoke

Scope:

- Change `/auth/logout` per Section 5.3 — revoke by `sid` family, not by `jti`.
- Add the `auth.session.terminated` audit event type.
- Remove the `sessions_invalidated_at = now` write from the logout path only. All other sites in Section 6 keep theirs.
- Add the grep-style regression test/allowlist pinning the Section 6 trigger set (operator decision Q6, Section 11).
- Tests: logout-after-rotation revokes the entire family (Section 9 P1.1 pin); concurrent logout-vs-rotation race produces `session_revoked` from the Lua script and 401 from `/refresh`; `/verify` rejects grace ticket after logout.

After PR 4, Team I posts a checkpoint summary and we open the dispatch for the follow-up `sessions_invalidated_at` allowlist + future `auth:recovery-*` events if product wants them.
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

### Per-session logout isolation (per device/profile, not per tab)

- GIVEN device A is logged in with `sid_A` / `jti_A`, and device B (or a separate browser profile) is logged in as the same user with `sid_B` / `jti_B`
- WHEN device A calls `/auth/logout`
- THEN device B's next `/refresh` still rotates successfully — `sid_B`'s family set is untouched.
- AND any sibling tab on device A that still holds an in-memory access token continues to render until the access token expires (15 min); its next `/refresh` after expiry returns 401 because `sid_A`'s family set was deleted by the logout. This is the same-browser semantic clarified in AC2.

### Global invalidation triggers (regression pin)

For each of the five sites in Section 6:

- GIVEN a valid refresh JWT exists in Redis
- WHEN the global-invalidation trigger fires (password change, password reset, email change, admin deactivate, invitation accept)
- THEN the next `/refresh` returns 401 with `"Session has been invalidated"`.

### Redis unavailable behaviour (architect feedback P2 — explicit in every runtime)

For each endpoint (`/login`, `/refresh`, `/verify`):

- GIVEN Redis is unreachable in **any** runtime (production, dev, CI) once PR 2 has shipped
- THEN response is 503. There is no fail-open path.

For `/auth/logout`:

- GIVEN Redis is unreachable
- THEN response is 200 and the cookie is still cleared (best-effort cookie cleanup is safer than refusing to log out). Response body includes `{"redis_partial_revoke": true}` so the frontend can show a soft notice.

**Dev requirement after PR 2.** The existing rate-limit fail-open
(PR #285's `FailOpenRedisStorage`) was justified because rate limits
are a defensive bonus — graceful degradation is preferable to
service interruption. **Auth sessions are different**: they are the
primary trust boundary, and a fail-open path lets stolen-credential
windows persist indefinitely. After PR 2 ships, **local dev must
run Redis**. The existing `./pfv start` already brings Redis up; no
operator change required. Tests use a fake Redis fixture
(`fakeredis` or equivalent) — call it out in the Section 9 test
plan so Team I doesn't introduce a real-Redis test dependency in
unit-test mode.

Code-wise, this means Team I should use `redis_client.require_client()`
(or equivalent) at every callsite that touches `auth:session:*`,
never an optional / try/except-around-None pattern.

### Concurrent rotation (no double-issue)

Two concurrent `/refresh` calls carrying the same `jti_A` must produce exactly:

- One winner with HTTP 200 + Set-Cookie carrying a new `jti_B` (Lua returned `"ok"`).
- One loser with HTTP 200 + NO Set-Cookie, access token only (Lua returned `{err = "already_rotated"}` → router fell into grace branch → grace key for `jti_A` was found → access-only).
- Zero 401s.

Pin with `asyncio.gather` and assert the response shapes. Without the Lua `EXISTS old primary` guard (Section 4.2 check (2)), this test fails: both requests pass the earlier app-side `GET` and both `SISMEMBER` checks, both run the full rotation, and the user receives two different new refresh cookies, one of which immediately becomes orphan when the other lands in the browser cookie jar.

### `jti` collision path (defensive NX guard)

- GIVEN a forced-collision RNG that returns the SAME `jti` for two successive `secrets.token_urlsafe(16)` calls on the same process
- WHEN `/refresh` runs the Lua rotation script
- THEN the first attempt fails with `{err = "jti_collision"}` (the `SET ... NX` returned false), the router regenerates `jti`, and the second attempt succeeds. Audit event `auth.session.rotated` is emitted once.
- AND if the RNG is monkey-patched to ALWAYS collide, the second attempt also fails and the router returns 503 with a structlog event flagging the RNG. (We do not want silent overwrite of a live session key; the architect's PR #301 P2 finding.)

### Logout-after-rotation revokes the entire family (architect P1.1)

- GIVEN a user logs in (sid=S1, primary jti=A), then rotates (primary jti=B, grace jti=A still valid for 30s)
- AND a sibling tab still holds the pre-rotation refresh cookie carrying `jti=A`
- WHEN the user logs out using the current cookie (jti=B)
- THEN `/refresh` with the pre-rotation `jti=A` returns 401, even within the 30s grace window. The family-set delete in logout step 1 makes the resolver's step-4 `EXISTS auth:session:by_sid:{sid}` check fail.
- Without the family-set guard, the old test would have shown jti=A accepted via grace and a fresh access token issued AFTER logout — that is the architect's reported bug.

### Atomic rotation under partial Redis failure (architect P1.3)

- GIVEN Redis connection drops mid-rotation
- THEN no client observes "old primary still works AND new primary also works" (the Lua script runs atomically server-side; both pre-rotation and post-rotation states are reachable, but never both simultaneously).
- Pin by mocking the Redis client to simulate disconnect between SET new_primary and DEL old_primary; assert the next refresh attempt with old_jti either succeeds (transaction aborted) OR fails with 401 (transaction committed), but never that old_jti is accepted as primary AFTER another /refresh successfully used new_jti.

### Concurrent logout-vs-rotation race (architect PR #301 follow-up P1)

This is the canonical race the family-set + Lua-guard design closes. Without the `SISMEMBER` check in Section 4.2 step 5, an in-flight `/refresh` can recreate a family after `/logout` has deleted it.

- GIVEN a user has refresh cookie `(jti_A, sid=S)` and a single browser tab is mid-`/refresh` — specifically: the handler has already passed step 2 (`GET auth:session:{jti_A}` returned HIT) but has NOT yet entered the Lua rotate script.
- AND a second request from the same browser calls `/auth/logout` and completes step A (`DEL auth:session:by_sid:{S}`) before the rotate Lua script runs.
- WHEN the in-flight `/refresh` then runs the Lua rotate script
- THEN the script's `SISMEMBER auth:session:by_sid:{S} {jti_A}` returns 0 and the script returns `session_revoked` without writing any keys. The router maps this to 401 `"Session has been invalidated"`.
- AND a subsequent `/refresh` with either `jti_A` or with the (never-issued) successor jti also returns 401 — the family is gone.
- Pin by orchestrating the two coroutines with explicit `asyncio.Event`s in the test: gate the rotate script entry until after the logout has run, then release.

### Verify endpoint must reject grace ticket after logout

- GIVEN the same setup as above but using `/auth/verify` instead of `/auth/refresh`, and a grace ticket exists for `jti_A` at the moment logout deletes the family set
- WHEN `/verify` is called with the pre-rotation refresh cookie
- THEN it returns 401 — `EXISTS auth:session:by_sid:{S}` returns 0, so the grace branch in Section 5.2 rejects the call. (Without the family-set check, `/verify` would accept the grace ticket and `/refresh` would reject it, which is the inconsistency the architect called out.)

### `sid` rotation invariant

- GIVEN a session with `sid_0` is rotated 5 times in succession (each rotate within the idle TTL but outside the 30s grace of the previous step)
- THEN every refresh JWT issued during those 5 rotations decodes to the same `sid_0` value; only `jti` changes.
- AND the family set `auth:session:by_sid:{sid_0}` accumulates every `jti` ever issued under that session and **retains them all** until logout or until the family set's idle-TTL expiry sweeps it. Grace keys (`auth:session:grace:{jti}`) and primary keys (`auth:session:{jti}`) each expire on their own clocks (30s and `refresh_idle_ttl_days * 86400` respectively), but those expirations do NOT remove the `jti` from the family set — only logout or the family set's own TTL does. This is intentional: it keeps `SISMEMBER` cheap and guarantees the family set is the single source of truth for "is this `jti` still owned by an unrevoked session".

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

GIVEN a user is signed in on two distinct devices or two distinct
browser profiles (each holding its own refresh cookie and therefore
its own `sid`), WHEN they click "Log out" on one device, THEN the
other device or profile remains authenticated until its own refresh
cookie expires or its user explicitly logs it out.

Same-browser sibling-tab semantics (architect feedback on PR #301):
sibling tabs in the same browser profile share one refresh cookie
and one `sid`. Logout revokes that whole family in Redis and clears
the cookie. The sibling tabs continue to render using their
in-memory access token until it expires (15 min TTL); at that point
their next API call hits 401, their `/refresh` attempt also returns
401 (the family set is gone), and the frontend redirects them to
`/login`. A future frontend `BroadcastChannel("auth")` would make
that redirect immediate; it is out of scope here and would only
speed up the redirect, not keep the sibling tab authenticated.

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

## 11. Operator decisions (resolved during PR #301 architect review)

Each open question now has the operator's answer. Team I builds against these.

1. **`refresh_idle_ttl_days` default: ANSWERED — 30 days.** Keep `session_lifetime_days` default at 30 too. Idle TTL and absolute lifetime are different controls and stay distinct. The operator's test org can use the existing per-org `session_lifetime_days` override to set 60 for their own testing without changing global idle behavior.

2. **Grace window length: ANSWERED — 30 seconds.** Cross-tab race coverage is the dominant concern; theft window stays tight.

3. **Per-org override of `refresh_idle_ttl_days`: ANSWERED — no.** Don't add it. Punt until an actual org asks. Spec records "no" by default.

4. **Sibling-tab logout semantics: ANSWERED via the P1.2 patch above.** Per-session logout means the refresh-cookie session (this browser profile / device). It does NOT mean "this one tab". Sibling tabs lose access after their current access token expires (15 min). A future frontend `BroadcastChannel("auth")` would speed up their redirect-to-`/login` but does not keep them signed in — out of scope here.

5. **Audit event names: ANSWERED — `auth.session.rotated`, `auth.session.grace_accept`, `auth.session.terminated`.** Locked.

6. **Lint to ban `sessions_invalidated_at` writes outside the allowlist: ANSWERED — yes, add a regression test/allowlist.** Team I adds a grep-style test that fails if any new write site appears outside the enumerated Section 6 trigger list. This is the structural defense against the 2026-05-16 incident class.

7. **In-flight QA reauth wave after PR 2: ANSWERED — acceptable.** Pre-launch QA testers will see a single 401 after PR 2 ships; they re-log and continue. No backcompat shim required.
