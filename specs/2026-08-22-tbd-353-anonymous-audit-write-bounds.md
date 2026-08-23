# TBD-353 — Bound the anonymous audit-write surface on the public auth routes

Status: implementing. Branch `TBD-353-anon-audit-ratelimit`.

## The defect

Three public routes write an `audit_events` row on a call that carries no
credential of any kind, and none of the six unlimited public auth routes
carries a `@limiter.limit` decorator. An anonymous caller can therefore
inflate the table `/admin/audit` reads and bury real security events.

Verified at real file:line on `main` @ 0c69fdd0 (the ticket's own line
numbers are stale; `auth.py` has grown since it was written):

| Route | Line | Writes an audit row anonymously? |
|---|---|---|
| `POST /api/v1/auth/logout` | 1727 | **Yes** — unconditional, `outcome="success"`, even with no cookie, no bearer, `sid_count=0` |
| `GET /api/v1/auth/google/callback` | 3364 | **Yes** — `error` branch @3405 and `code is None` branch @3421 both audit *before* the `oauth_state` check @3432 |
| `GET /api/v1/auth/sso-stepup/callback` | 3934 | **Yes** — same shape, @4021 and @4036, before the check @4046 |
| `POST /api/v1/auth/refresh` | 1374 | No — see "Deferred" |
| `POST /api/v1/auth/reset-password` | 1906 | No — audits only after the reset JWT validates |
| `GET /api/v1/auth/google` | 3331 | No — writes no audit row at all |

`google/callback` is **not** named in the ticket. It carries the identical
defect to the step-up callback it does name, on the higher-traffic route.
Fixing only the named one is the "fence records the item, not the path"
failure, so both are in scope.

## What ships

### 1. Rate limits (DoD 1 + the `/google`, `/google/callback` half of DoD 3)

Five decorators, five `PRE_AUTH_ENDPOINT_PATTERNS` entries.

| Route | Limit | Pattern | Derivation |
|---|---|---|---|
| `POST /logout` | `120/minute` | `auth.logout` | A 429 here is a **fake logout**: `AuthProvider.tsx:388-392` swallows the failure and clears local state, but the server sent no `Set-Cookie` delete and never ran `session_revoke_family`, so the cookie and the Redis family both survive. Loose on purpose; the vacuous-row gate below, not this number, is the control. |
| `POST /reset-password` | `10/minute` | `auth.reset_password` | Matches its sibling token-redemption route `verify_email` (10/minute) and pairs with `forgot_password` (5/minute). A 429 here is a hard dead end with no client retry. |
| `GET /google` | `60/minute` | `auth.google_login` | No DB, no audit, no email — mints a state cookie and returns a URL. Same tier as the existing cheap public routes `public_stats.founder_count` and `security.csp_report` (both 60/minute). |
| `GET /google/callback` | `60/minute` | `auth.google_callback` | Deliberately **equal** to its issuer: traffic is 1:1, so the callback must never be the tighter gate. A 429 on a browser navigation also renders bare JSON, the exact UX the handler's docstring exists to prevent. |
| `GET /sso-stepup/callback` | `60/hour` | `auth.sso_stepup_callback` | 6x its issuer `sso_stepup_initiate` (10/hour), which is the binding gate anyway; the slack absorbs a back-button replay of a URL that sits in history. |

`google_login`'s signature gains `request: Request`. slowapi resolves its key
from a `Request` in the wrapped signature and the handler has none today, so
the decorator alone would fail at call time, not import time.

### 2. The `/logout` vacuous audit row (the ticket's headline defect)

A limit throttles the row; it does not remove it, and `get_client_ip` returns
the resolved address as a **raw string** with no IPv6 prefix collapsing
(`rate_limit.py:135`, `:150`, `:161`), so a client with a routed /64 has 2^64
independent buckets. Gate the write instead:

```python
if sids or actor_user_id is not None:
    await audit_service.record_audit_event(...)
else:
    logger.info("auth.session.terminated.anonymous", ...)
```

A row with `actor_user_id=None, sid_count=0, jti_count=0` records nothing an
operator can act on and is the entire attack payload. Every real logout carries
a cookie or a bearer and keeps its row.

`jtis_seen` is deliberately **not** a third term: `decode_refresh_jti_sid`
raises unless both claims are present (`security.py:194-196`), so the append at
`auth.py:1792` and the `sids` append at `:1795` happen in the same iteration.
`jtis_seen` non-empty implies `sids` non-empty; a third term would be dead code
and any fence leg claiming to exercise it would be vacuous.

This **inverts an existing contract** and the inversion is deliberate:
- `tests/auth/test_session_logout_revoke.py::test_anonymous_logout_succeeds_with_zero_counts`
- `tests/auth/test_session_logout_revoke.py::test_corrupt_refresh_cookie_logout_still_clears`
- the docstring at `auth.py:1745-1748` and `:1757-1761`
- `specs/2026-05-17-backend-session-model.md:457` and `:560`

all currently assert "outcome=success even when 0". Each is amended in this PR.
The 200, the response body and both delete-cookie headers are unchanged; only
the row goes.

### 3. State-conditioned callback audits (DoD 2, extended to `google_callback`)

**Condition the write; do not reorder the branches.** Reordering changes what
the user sees: a cancelled consent arriving with a nuked cookie would fall
through to the state branch and render `?sso_error=state` instead of
`?sso_error=cancelled`. The comment at `auth.py:3399-3403` is a promise about
the *redirect*, not the audit, and it stays true.

Hoist one boolean above the existing branches and make **every**
`_record_google_callback_failure` call conditional on it:

```python
def _oauth_state_matches(cookie_value: str | None, state_value: str | None) -> bool:
    if not cookie_value or not state_value:
        return False
    return secrets.compare_digest(cookie_value.encode("utf-8"), state_value.encode("utf-8"))
```

⚠ **BYTES, not `str`.** `secrets.compare_digest` raises `TypeError` on `str`
operands containing non-ASCII (verified in-session), and `state` is an
attacker-controlled query parameter — the naive `compare_digest(a, b)` would turn
a state mismatch into a **500**. One helper, both call sites, one fence leg
(non-ASCII `state` → 307, not 500).

⚠⚠ **The rule is: no `_record_google_callback_failure` may run on ANY path where
`state_ok` is false — the `state` branch included.** Conditioning only the
`error` and `missing_code` branches does not close the hole, it **moves** it: an
attacker who omits the cookie simply falls through to the state branch at
`auth.py:3431-3435` (and `_stepup_failure("state")`) and gets an identical
unbounded anonymous row. This is the repo's "a half-fix leaves a door" class and
it is the single most important finding in the review.

Nothing is lost by suppressing the `state` row: `reason="state"` is reachable
**only** when `state_ok` is false, so that row is anonymous by construction.
The audit call in that branch is therefore **replaced** by a structlog line, not
left behind a guard that can never be true — a dead term in a security guard
asserts a case exists.

Retained vs lost, precisely:
- **Valid state + `?error=access_denied`** → identical 307, identical URL, and
  the row is written **unchanged** (`reason="cancelled"`, `google_error` and
  `google_error_description` intact). RFC 6749 §4.1.2.1 makes `state` REQUIRED on
  the provider's error response, and we always send it (`auth.py:3359`, `:3929`),
  but the load-bearing retainer is **our own cookie**: `/google` sets
  `oauth_state` with `max_age=1800, path="/api/v1/auth/google"`
  (`auth.py:3341-3349`) and the callback lives under that prefix. A genuine user
  who cancels keeps their row.
- **Absent or mismatched state, on any branch** → identical 307, identical URL,
  **no row**, structlog line instead. Residual loss is the >30-minute dweller and
  the cleared-cookie case: real, small, and by construction indistinguishable
  from a forged GET.

⚠ **Neither the limit nor the conditioning bounds a determined attacker.**
`oauth_state` is an **unsigned** cookie — the comment at `auth.py:3336` calling
it "signed" is false and is corrected here — so an attacker supplies both halves
of the comparison. Both changes are cost-raisers that remove drive-by and
contentless rows. `get_client_ip` also returns raw un-collapsed addresses, so a
routed IPv6 /64 is 2^64 buckets. The durable fix (retention / partitioning
anonymous pre-auth rows out of the `/admin/audit` default view) is a separate
ticket. The PR body must not overclaim this.

### 4. DoD 4 — correct the comment, do not implement `verify_exp=False`

`auth.py:1782-1785` claims "even an expired or post-cutoff token still
identifies a session family that should be cleaned up". The **expired** half is
false: `decode_refresh_jti_sid` calls `jwt.decode` with default options
(`security.py:188-190`), so `exp` is verified and the raise is swallowed at
`auth.py:1790`. The **post-cutoff** half is true and load-bearing — no cutoff
check runs here, which is exactly why logout skips `_validate_refresh_cookie`.

Option (a) is rejected because it would revoke nothing. The JWT `exp`, the
cookie `Max-Age`, the Redis primary TTL and the family-set TTL are all the same
`ttl_seconds`, set together (`security.py:162-164`, `auth.py:543`,
`redis_client.py:471-473`, rotation Lua `EXPIRE KEYS[4]` at `:616`). Both cookie
paths are walked by `_extract_refresh_cookies`, so any jar whose family is still
live also carries the unexpired head, which decodes fine and revokes the same
`sid`. A path that fires only under clock skew, to delete a key that has already
expired, would be a security-shaped no-op a future reader mistakes for a control.

Ships: the corrected comment, plus `except Exception` in place of the tautology
`except (ValueError, Exception)` at `auth.py:1790` and a `logger.debug` carrying
`type(exc).__name__`, so the swallow the comment now describes is observable.
Correcting a comment about an invisible behaviour while leaving it invisible is
a half-fix. `decode_refresh_jti_sid` is **not** touched: one production call site,
~60 lines of test driving it as an oracle across five files.

### 5. Catalogue truthfulness

`rate_limit_endpoint_catalogue.py` claims to be the single source of truth and
is rendered in an admin dropdown, but there are **31** `@limiter.limit`
decorators under `app/routers/` and **25** patterns. Six have no entry:

- `PRE_AUTH`: `public_stats.founder_count`, `security.csp_report`, `webhooks.mailgun`
- `OVERRIDABLE` (post-auth, so an operator literally cannot store an override for
  them today): `reports.sankey_query`, `users.cancel_pending_email`,
  `org_members.remove_member`

Backfilled here, plus the five new pre-auth patterns, and fenced (§F5).

## Deferred, deliberately: DoD 3's `/refresh` item

**Not done in this PR, and the operator can overrule this at merge.**

Two reasons.

1. **`/refresh` is not this ticket's defect.** Every audit write on the route
   (`auth.py:1505`, `:1535`, `:1624`, `:1645`, and `_record_session_reuse_detected`
   at `:2808`) is past `user` resolution. `RefreshBothMissError` is only raised
   after the user row is loaded — it carries `user.id`, `user.email`,
   `user.org_id` (`auth.py:1152-1158`). An anonymous caller gets a failed JWT
   decode and a plain 401: no Redis Lua, no MySQL, no audit row. It was
   pattern-matched into DoD 3 alongside two routes that genuinely do write rows
   anonymously.

2. **There is no defensible limit value, because the client latches.** A 429 on
   `/refresh` at page load is neither transient nor terminal:
   `isTransientAuthError` admits only `status === 0 || status >= 500`
   (`AuthProvider.tsx:38-48`), `isTerminalAuthError` only 401/403 (`:50-52`),
   `withAuthRetry` rethrows after exhausting `[0, 250, 500]` (`:54-71`), and the
   mount catch takes the `else` branch that holds `loading = true` **by design**
   (`:296-320`). The tab spins forever and the only offered recovery — reload —
   fires another `/refresh`. A wedged tenant therefore *sustains* load above
   whatever ceiling tripped it: crossing the line holds you across it. Adding 429
   to the classifier does not fix this; it buys two retries and lands in the same
   branch, tripling the calls.

Filed as two backlog tickets: the mount-path recovery defect (live **today** for
5xx, carries a visual gate) and, blocked on it, the `/refresh` limit itself.

## Fences

Every fence file carries the established autouse `limiter.reset()` fixture
(before **and** after). Under `TestClient` the peer is the literal `"testclient"`
(verified: `get_client_ip` returns it unchanged, since it fails `_is_trusted_proxy`),
so every call in a module shares one deterministic key. Storage is `MemoryStorage`
in CI (`REDIS_URL` unset) and Redis in the dev container; both count identically
and `reset()` works on both (verified in-session against `FailOpenRedisStorage`).

**F1 — the five limits.** For each route: calls 1..N assert the route's **exact
normal status**, call N+1 asserts **429**.
- Wrong implementation killed: (i) decorator deleted; (ii) number loosened
  (`60/minute` → `600/minute`) — which is why the boundary is exact rather than
  "a 429 eventually".
- ⚠ Downstream-guard trap: `_validate_google_config()` raises 501 when the client
  id is unset (`auth.py:3325-3329`), and slowapi increments *before* the handler,
  so a 429 would still fire while every earlier call was a 501 and the fence
  proved nothing. Every google leg uses the `google_config` fixture and asserts
  the pre-limit status is 200/307. A 501 cannot produce a 307.
- Leg 1 is DoD 5's required **control**: a legitimate call inside the budget
  still succeeds with its normal side effect.

**F2 — the `/logout` gate.** The predicate is `if sids or actor_user_id is not None`
— **two** guards, so:
1. No cookie, no bearer → 200, both delete-cookie headers, **zero** rows.
   (Asserting the 200 and the headers matters: a mutant that 401s anonymous
   logout also yields zero rows and would pass a rows-only assertion.)
2. Garbage `refresh_token=not-a-jwt`, no bearer → **zero** rows. Proves the
   decode-failure path writes nothing. It exercises no distinct term and is not
   labelled as if it did.
3. Valid bearer, no cookie → **one** row, `sid_count == 0`. Kills `if sids:`.
4. Signature-valid cookie, empty Redis → **one** row, `sid_count == 1`,
   `jti_count == 0`. Kills `if actor_user_id is not None:` alone.
- Wrong implementation killed: the unconditional `record_audit_event`. Legs 1-2
  go red. Also run against the two **rewritten** existing tests — with the gate
  removed, `audit == []` must go red on both, or the rewrite is decoration.
- Rows are read back through the same `session_factory`, never a mock:
  `record_audit_event` opens its own session.

**F3 — callback state conditioning**, for each of the two callbacks:
1. `?error=access_denied`, no cookie → **zero** rows.
2. `?error=access_denied`, matching cookie → **one** row, `reason="cancelled"`,
   `google_error` detail intact. Kills the "just delete the audit call" pseudo-fix.
3. `?error=server_error`, matching cookie → **one** row, `reason="provider_error"`.
4. No `code`, no `error`, no cookie → **zero** rows.
5. No `code`, no `error`, matching cookie → **one** row, `reason="missing_code"`.
6. `?error=access_denied` with a cookie that **mismatches** `state` → **zero**
   rows, and the redirect is still `cancelled`, **not** `state`. This is the leg
   that discriminates *condition* from *move*: a literal reordering produces
   `state` here and goes red.
7. `code` present, cookie mismatched → **zero** rows, redirect still `state`.
   This is the half-fix-door leg: an implementation that conditions only the
   `error` and `missing_code` branches leaves the `state` branch writing and goes
   red here.
8. Non-ASCII `state` (e.g. `state=\u00e9x`) with a mismatching cookie → **307**,
   not 500. Kills the `str`-operand form of `compare_digest`.
- Every leg asserts the `Location` header **byte-for-byte** against today's value.
- Wrong implementations killed: dropping the `state_ok` condition (legs 1/4/7);
  reordering the branches (leg 6); conditioning only the `error` and
  `missing_code` branches (leg 7); `compare_digest` on `str` (leg 8).
- ⚠ Same 501 trap as F1: without `google_config` the zero-row legs pass vacuously.
  The 307 assertion is the guard.

**F4 — DoD 4, a characterization test, labelled as one.** An expired but
signature-valid refresh cookie at `/logout` → 200, cookie cleared,
`session_revoke_family` **not** called. Green against `main` **by design**: it
pins behaviour we are deliberately keeping so a future `verify_exp: False` cannot
land as a silent no-op. Mutant: switch `decode_refresh_jti_sid` to
`options={"verify_exp": False}` → red. The docstring says all of this; an
unlabelled green-against-main test is indistinguishable from the vacuous pattern.

**F5 — catalogue drift.** An `ast` walk over `app/routers/*.py` collecting
`(module, function)` for every `@limiter.limit` decorator, compared as a **set**
against a checked-in `(module, function) -> pattern` map, plus both-direction
assertions against `ALL_KNOWN_ENDPOINT_PATTERNS`.
- Not a count. A count nets to zero on the commonest real edit — add a route,
  copy a nearby catalogue line — and goes green while the new route has no knob.
  That is the six-times failure, unfixed.
- `ast`, not grep: a whole-file grep for `@limiter.limit` is satisfied by the five
  occurrences in the catalogue's own docstring.
- **Born red against `main`** (31 decorators, 25 patterns) and green only after
  the backfill. That is its vacuity proof, taken before the backfill lands.
- Wrong implementations killed: a new decorator with no map entry; a catalogue
  pattern deleted while a live decorator maps to it; a decorated function renamed
  without touching the map.
- Stated end state, filed to the backlog: a `@rate_limited(pattern, limit)`
  wrapper that makes the pattern derivable by construction, deletes both lists,
  and gives `rate_limit_overrides.dynamic_limit` its first call sites. It touches
  31 decorator sites and does not belong inside a security fix.
