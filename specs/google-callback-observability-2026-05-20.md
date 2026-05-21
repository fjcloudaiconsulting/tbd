---
name: project-google-callback-observability-2026-05-20
description: "Tomorrow-me's PR plan for the Google SSO callback hang surfaced 2026-05-19. Architect-locked constraints, framing, and scope fence."
---

# Google callback observability PR — 2026-05-20

## Why this PR exists

2026-05-19 18:51-18:54 production: user signs in via Google, hits Google's "Choose an account" page, picks account, ends up stuck. Backend logs show `POST oauth2/token` and `GET userinfo` both 200 OK — but **no uvicorn.access entry for the callback request's response**. The handler hung between userinfo and the redirect. Second attempt 2 min later worked (classic stale-pool retirement signature — see [[reference_first_attempt_stuck_pattern]]).

We can't tell which `await` is hanging because none of the steps between userinfo and the redirect emit a log line. Uvicorn's access log only fires at response time.

## Architect-locked constraints (do not negotiate without operator approval)

1. **Breadcrumb logs at each Google callback phase, with durations + request_id.** Specific event names the architect listed verbatim:
   - `userinfo_ok`
   - `db_user_lookup_ok`
   - `user_prepare_ok`
   - `ttl_resolved`
   - `session_issue_ok`
   - `db_commit_ok`
   - `redirect_built`
   - `audit_ok`

   **PII guard:** no raw Google tokens, no raw email unless hashed or already accepted audit style. The email field passes through `audit_events` for the new-user branch already — use that style.

2. **Add a timeout around `_retire_poisoned_client.aclose()`.** Current implementation in `backend/app/redis_client.py` sets `_client = None` first (good), then `await poisoned.aclose()` **unbounded**. If aclose itself hangs on a dead socket, the retirement path becomes its own hang. Bound it with `asyncio.wait_for(..., timeout=2.0)` or similar; swallow the timeout exception (best-effort cleanup).

3. **Be careful with request-level timeout middleware.** Architect: "broad cancellation can leave DB transactions and audit writes in awkward states if not tested well." Direction:
   - **For tomorrow's PR:** route-local containment for the Google callback / post-userinfo section ONLY. `asyncio.wait_for` around the post-userinfo block, or per-step inside it.
   - **NOT in this PR:** global middleware. Add later, with explicit cancellation tests (DB transaction rollback, audit fire-and-forget semantics, slowapi cleanup).

4. **Do not present this as "the fix."** Framing is **containment + observability**. The durable orphan-cookie / catch-up mapping is still the real remaining session-design fix (see [[project_auth_stability_residuals]] Class 1). Do not let the PR title or body imply otherwise.

## Scope fence

**In scope:**
- Breadcrumb logs at the 8 named phases.
- `_retire_poisoned_client.aclose()` bounded timeout.
- Route-local timeout on `/api/v1/auth/google/callback` post-userinfo section.
- Tests: assert breadcrumbs fire; assert aclose timeout doesn't surface as RedisError; assert callback returns 503 on timeout (not hang).

**Out of scope:**
- Global request-timeout middleware.
- Orphan-cookie durable catch-up (Class 1).
- slowapi async storage (Class 2).
- Any other auth refactor.

## Approach hint

The `_record_google_callback_failure` audit-write helper is already in `routers/auth.py` and used on each error branch. Add a parallel `_log_callback_breadcrumb(phase, duration_ms, request_id=...)` helper using structlog with a stable event name like `auth.google.callback.phase`. That keeps the operator-visible event family naming consistent with `auth.refresh.rejected`.

**Gate the breadcrumbs behind `AUTH_DEBUG_LOGGING`** (the env var #316 introduced — see [[reference_auth_debug_logging]])? Architect didn't specify. Default position: **yes, gate them**, so production stays quiet under normal operation. Flip the flag during incident triage to capture phase durations.

## Caveat for next-session-me

The user was tired and frustrated when this was scoped. Honor the "small, focused" framing. Don't bundle anything else into this PR. Don't promise the orphan-cookie class will be fixed by this. When the user asks "will this fix it" — tell them: it makes the callback either succeed or surface a clear 503 with breadcrumbs that pin which await hangs next time. Not a fix for the class itself.
