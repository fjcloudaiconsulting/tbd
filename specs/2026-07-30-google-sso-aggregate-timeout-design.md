# Bound the aggregate Google SSO exchange (TBD-179)

Status: design settled, 2026-07-30. Two independent architects, one concede/defend round.
Branch `TBD-179-bound-sso-exchange`. Backend only. No migration, no frontend change,
no `.do/app.yaml` change.

---

## 0. Corrections — the ticket's label is wrong, and one shared claim is false

- The ticket carries a `has-spec` label. **No spec existed.** This file is it.
- The ticket says the try body contains the two HTTP calls "and further down DB work".
  **Understated.** Both try bodies contain DB work *inside* the `async with`: the two
  non-200 branches audit there (`auth.py:2924-2928`, `:2937-2941`, `:3406`, `:3412`).
  This is what makes §2's placement load-bearing rather than cosmetic.
- **Both architects initially justified 20.0s as "provably non-narrowing" — the sum of the
  two per-phase 10s budgets. That claim is FALSE and must not appear in the PR body.**
  Per-phase bounds are `connect / write / read / pool`, each 10s and *sequential within a
  single request*, and `read` applies per socket-read. One request can legitimately consume
  ~30s while violating no per-phase bound; a drip-feeding server is unbounded. The pair's
  per-phase-permitted envelope is therefore ~60s and up. **20.0s is a deliberate narrowing
  of a loose envelope**, not a restatement of it. Stated honestly in §4.

## 1. The defect

`GOOGLE_OAUTH_TIMEOUT = httpx.Timeout(10.0)` (`auth.py:98`) is applied at both callback
sites — `:2914` (`google_callback`) and `:3394` (`sso_stepup_callback`). It is a **per-phase**
bound, not a total. Each site makes two sequential awaited HTTP calls (POST token, GET
userinfo) under one client and one `try`. Nothing bounds their sum. This matches the reported
~30s hang.

Verified in the real image (httpx 0.28.1, Python 3.12.13):

```
Timeout(10.0).as_dict()                          -> {'connect':10.0,'read':10.0,'write':10.0,'pool':10.0}
httpx.TimeoutException subclass of HTTPError     -> True
asyncio.TimeoutError  subclass of HTTPError      -> False
asyncio.TimeoutError is builtins.TimeoutError    -> True
asyncio.CancelledError derives from BaseException-> True
```

The hierarchies are **disjoint** (`TimeoutException` MRO is `TransportError → RequestError →
HTTPError`; builtin `TimeoutError` MRO is `OSError → Exception`). So a naive `asyncio.wait_for`
raises something **neither** `except httpx.HTTPError` clause catches: unhandled 500, no
`audit_events` row, no friendly `?sso_error=` redirect. The ticket's prescribed one-liner is
the single change that would bypass the working error UX.

## 2. The design

**Mechanism: `asyncio.timeout_at()` with one shared absolute deadline, wrapping only the two
awaited HTTP calls.** No coroutine extraction. Every existing line of both handlers stays
byte-identical; two `async with` lines are added per site.

```python
deadline = asyncio.get_running_loop().time() + GOOGLE_OAUTH_TOTAL_TIMEOUT_S
try:
    async with httpx.AsyncClient(timeout=GOOGLE_OAUTH_TIMEOUT) as client:
        async with asyncio.timeout_at(deadline):
            token_resp = await client.post(...)
        if token_resp.status_code != 200:        # UNCHANGED, outside the bound
            await _record_google_callback_failure(..., reason="token")
            return _google_error_redirect("token")
        tokens = token_resp.json()
        async with asyncio.timeout_at(deadline):
            userinfo_resp = await client.get(...)
        if userinfo_resp.status_code != 200:     # UNCHANGED, outside the bound
            ...
except TimeoutError:
    ...
except httpx.HTTPError:
    ...                                          # UNCHANGED
```

Two blocks sharing one **absolute** deadline is what makes the bound aggregate. Verified:
`block1 done at 0.201 / TimeoutError at 0.302` against a 0.30s deadline where neither block
alone exceeded it.

### 2.1 Why the bound excludes the audit writes — the decisive evidence

This is not a probability argument. A simulation of both placements, run in the image:

```
SHAPE A (bound encloses audit + aclose)  returned=redirect:timeout   rows=['timeout']
SHAPE B (bound = network only)           returned=redirect:token     rows=['token']
```

Shape A — the bound opened around the whole `async with` block — **misclassifies a slow
non-200 as a timeout**: the `token` audit write and/or the client's `aclose()` are awaited
inside the cancellable scope, so a non-200 arriving near the deadline is rewritten into
`reason="timeout"` with the wrong redirect. An independent run of the same shape produced
`rows=['token','timeout']` — **two audit rows for one request**. Either way the forensic
signal that §3 exists to create is corrupted precisely during the incident it is meant to
diagnose. *Slow* and *erroring* are positively correlated: a degraded provider is both in the
same window, so this is not a rare race.

Shape B also keeps `httpx.AsyncClient.__aexit__` outside both bounded blocks, so `aclose()`
is never the thing being cancelled. **Coroutine extraction does NOT achieve this** — under
extraction the client context manager sits inside the bounded coroutine, so `aclose()` is
cancellable and a `_GoogleExchangeError("token")` propagating through it can still be
rewritten into `TimeoutError`. Extraction was proposed and withdrawn for this reason.

**Outside the bound, deliberately:** every `_record_google_callback_failure` /
`_stepup_failure` call, every redirect construction, `_resolve_return_path`, all main-line
session work, and every `db.commit()`. A cancelled commit would leave the request-scoped
session unusable and 500 a path that currently succeeds.

**Note on the absolute deadline:** excluding a region from *cancellation* does not exclude it
from the *budget*. A slow audit write still consumes deadline. This is harmless here because
every audit write in both handlers is immediately followed by `return`, so no further bounded
block is ever entered — the reclassification is structurally impossible, not merely unlikely.

**Keep `GOOGLE_OAUTH_TIMEOUT` unchanged.** The two bounds compose: per-phase kills a dead
connect in 10s; the aggregate kills drip-feed and the two-call sum.

### 2.2 The exception clause

`except TimeoutError:` placed **before** `except httpx.HTTPError:` at both sites.

- Exactly one name. `asyncio.TimeoutError is builtins.TimeoutError`, so one name covers both
  spellings.
- **Not too wide:** none of `TimeoutException / ConnectTimeout / ReadTimeout / WriteTimeout /
  PoolTimeout` subclasses builtin `TimeoutError` or `OSError`, so this clause cannot steal an
  httpx per-phase timeout — those keep landing in `except httpx.HTTPError` with reason
  `token`, preserving the existing passing test.
- **Never a tuple** `(httpx.HTTPError, TimeoutError)`: that forces the timeout to share the
  `token` reason and forfeits the entire ops value.
- **Never `except Exception`:** it would swallow `CancelledError`-adjacent shutdown handling
  and, concretely, mask the live `tokens['access_token']` `KeyError` path into a bogus
  `timeout` reason. That `KeyError` → 500 is pre-existing and stays exactly as-is.
- Ordering is not load-bearing (disjoint hierarchies); narrow-before-broad is convention.

## 3. Audit reason and redirect code — they differ, by precedent

**Audit `reason="timeout"`. Redirect `token`.** No frontend change.

`_record_google_callback_failure` writes `detail={"reason": ...}` as free text — no enum, no
migration. "Google rejected the code" and "Google never answered" have different remediations;
collapsing them removes the only durable signal that would let an operator confirm this fix
works in production.

This is the established pattern **in this same file**: `missing_code` audits distinctly and
redirects as `token`, at login `:2895-2899` and step-up `:3353-3360`, each with a comment
saying so. All three frontend copy dicts (`LoginPageBody.tsx:32-46`, `settings/page.tsx:21-35`,
`settings/security/page.tsx:46-60`) already map `token` to copy that is already right for a
timeout, and each has a fallback. A new redirect code would mean three more files, three more
test updates, **and strictly worse copy**.

**Step-up trap.** `_stepup_failure(reason)` builds its redirect from the *same* string it
audits (`?sso_stepup_error={reason}`), so calling it with `"timeout"` would emit an unmapped
code. Add an optional `ui_code: str | None = None` param defaulting to `reason`. This inherits
the cookie deletion at path `/api/v1/auth/sso-stepup` automatically — building the redirect
inline instead risks omitting it, leaving a stale `oauth_state` cookie that makes the user's
**retry** fail with `state`. Do not refactor the existing `missing_code` branch.

## 4. The number: `GOOGLE_OAUTH_TOTAL_TIMEOUT_S = 20.0`, a module constant

Declared directly below `GOOGLE_OAUTH_TIMEOUT` at `auth.py:98`.

**Value.** The per-phase configuration permits ~60s and up for the pair (§0). 20.0s narrows
that to roughly 40x normal end-to-end latency — two Google calls normally total well under
500ms — while sitting below the reported ~30s hang, so the fix is observable. Reaching 20s
requires both calls at ~40x normal latency without either tripping its own 10s phase bound.

**Constant, not configuration.** Both architects converged here, one by concession and one by
the argument that settles it: the aggregate's inputs are themselves hardcoded
(`GOOGLE_OAUTH_TIMEOUT` is a module constant), so an operator who widens only the aggregate to
60s mid-incident gets **nothing** for a well-behaved slow exchange — every phase still caps at
10s. The only behaviour such a knob can actually change is how long a **drip-feeding** server
is tolerated, which is the precise attack the aggregate bound exists to stop. A knob that
cannot achieve its stated purpose and whose only reachable effect is to loosen the protection
should not exist.

Corroborating: `refresh_handler_timeout_s` — the cited precedent — is bound in **neither** the
backend nor the migrate job in `.do/app.yaml` and rides its default in prod, so exercising the
"tune it from the console" scenario on the precedent would itself require a repo change plus a
deploy. It is also config for a reason that does not transfer: its correct value depends on the
deployment's Redis/MySQL topology, whereas this bound depends on Google, which is identical
everywhere.

**The `.do/app.yaml` dual-binding trap does not apply.** It fires only for settings a prod
`Settings()` instantiation *requires*. A module constant in `auth.py` is never imported by
`scripts/migrate.py` and has no env binding at all. **Adding a partial (backend-only) binding
is itself the failure mode** that broke a deploy on 2026-07-21.

**Guard the derivation** with a unit test:
`assert GOOGLE_OAUTH_TOTAL_TIMEOUT_S >= 2 * GOOGLE_OAUTH_TIMEOUT.read`.
*Named victim:* someone later tightens the aggregate below the per-phase sum, or raises the
per-phase value without raising the aggregate — either silently converts healthy exchanges into
`?sso_error=token`.

## 5. Two parallel edits. No shared failure helper.

Share only the constant. The bounded region is two statements wrapped by one context manager;
there is no meaningful shareable body. What differs between the sites is exactly the
security-relevant part:

| | login | step-up |
|---|---|---|
| audit `event_type` | `auth.google.callback.failed` | `auth.google.sso_stepup.callback.failed` |
| redirect base | `/login` | `_resolve_return_path(state)` |
| query param | `sso_error` | `sso_stepup_error` |
| cookie path | `/api/v1/auth/google` | `/api/v1/auth/sso-stepup` |
| `actor_email` | unavailable pre-userinfo | `user.email` (loaded `:3386`) |

A unifying helper needs ~5 callbacks to preserve that, and permanently tempts a future editor
to collapse step-up onto login's audit event — which would silently empty the step-up half of
`/admin/audit` while every test stays green. Step-up guards email change and first-password-set;
that is a security-observability regression. Four lines of duplication is the cheaper side.

## 6. Observability — timeout path only, ungated

The diagnosis is complete, so "observability before fix" is satisfied as a gate. But the
existing `_log_google_callback_phase` is gated on `auth_debug_logging` (default `False`) and its
`_phase` closure is defined *after* the try/except, so it structurally cannot instrument the
exchange — a hang there produces total silence in production today.

**Add, at each site, on the timeout path only:**

1. An **ungated** `_LOGGER.warning("auth.google.callback.exchange_timeout", extra={"timeout_s":
   ..., "flow": "login"|"stepup", "last_phase": progress["phase"]})`, mirroring the
   `auth.refresh.handler_timeout` precedent at `:1291-1294`.
2. `detail_extra={"last_phase": progress["phase"]}` on the audit row.
3. A plain local `progress` dict (`"start"` → `"token_ok"`) mutated between the bounded blocks.
   Without it a timeout tells an operator nothing about *which* endpoint is wedged. PII guard
   holds: phase names and a float only.

**Regression hazard, checked and clear.** `test_auth_google_callback_breadcrumbs.py:184` asserts
**exact list equality** of emitted phases against the eight in `EXPECTED_PHASES` (`:43-52`). The
`progress` dict never calls `_log_google_callback_phase` and adds no phase; the new signal is
`_LOGGER.warning`, a different mock method, filtered out by the test's event-name predicate at
`:150-157`; and it cannot fire in that test, which asserts the 302 success path. **No
success-path breadcrumb may be added without updating `EXPECTED_PHASES`** — same class as the
"grep all `== {exact set}` assertions" lesson.

**Out:** step-up breadcrumbs (gated, off in prod, superseded by the ungated warning plus the
step-up audit row).

## 7. Test plan

Every test below is a **fence**. Injection: extend the existing `_patch_httpx`
(`test_auth_google_callback_errors.py:183-229`) with `hang_on: str | None` and `delay_s: float`,
so the fake's `post`/`get` do `await asyncio.sleep(...)` — **`await asyncio.sleep`, never
`time.sleep`**, which would wedge the suite. Speed comes from monkeypatching the constant on
`auth_module`, not from real waiting. **Nothing about the implementation under test is stubbed**:
the real `timeout_at`, the real `except` clauses, the real audit write and the real redirect all
execute. The only fake is the HTTP client, which every test in this file already fakes.

`TestClient` defaults to `raise_server_exceptions=True`, so a regression that lets `TimeoutError`
escape **raises out of `client.get(...)`** rather than returning 500. Assert
`res.status_code == 307` and let the raise error the test — never `assert status != 500`, which
would not be reached.

### `/google/callback`

| id | setup | asserts | kills |
|---|---|---|---|
| **L1** | `hang_on="post"`, budget 0.05 | 307; `location == ".../login?sso_error=token"`; exactly 1 audit row; `detail["reason"] == "timeout"`; elapsed < 1.0 | no bound at all (hangs); bound without the `except` clause (escapes → 500); timeout audited as `"token"` |
| **L2** | `hang_on="get"` | as L1, plus `detail["last_phase"] == "token_ok"` | a bound wrapping only the token POST; a warning shipped without the `progress` marker |
| **L3** | budget **0.6**, post sleeps **0.4**, get sleeps **0.4**; seed an active user so the un-timed-out path would SUCCEED | `location` contains `sso_error=` (structural, not wall-clock); `detail["reason"] == "timeout"` | **`timeout_at` applied PER CALL instead of sharing one deadline** — under it both calls complete and the location is the success redirect with no `sso_error` at all |
| **L5** | fake `post` raises a locally-defined `ProgrammerBug` | `pytest.raises(ProgrammerBug)`; **no** audit row written | `except Exception:` instead of `except TimeoutError:` — the only test fencing the clause's upper bound |

**L3 is the one a careless implementation passes L1+L2 without.** Its 0.4/0.6 ratio is
load-bearing (50% headroom): with no per-call margin the wrong implementation also times out and
the test goes vacuous in the dangerous direction. State that in the docstring so nobody "tidies"
the sleeps. L3 and `hang_on="get"` are complementary — L3 kills per-call bounds, L2 kills
POST-only bounds, and neither kills the other's target.

**No happy-path test is added.** The existing success tests in
`test_auth_google_callback_first_run.py` and `test_auth_stepup.py:230+` already drive the real
unstubbed wrapper end-to-end; if the bound were mis-wired to always fire they go red. A fourth
copy would be decoration.

### `/sso-stepup/callback` (in `test_auth_stepup.py`, via its `_FakeAsyncClient`)

| id | setup | asserts | kills |
|---|---|---|---|
| **S1** | `hang_on="post"`, real state cookie from `initiate` | 307; `location.endswith("/settings?sso_stepup_error=token")`; 1 row via `_callback_failure_rows(..., event_type="auth.google.sso_stepup.callback.failed")`; `reason=="timeout"`; **`actor_email == "alice@acme.io"`**; `user.stepup_token is None` | the bound applied to login only; **a shared helper collapsing onto login's `event_type`** (the §5 risk); a helper dropping `actor_email`; minting a step-up token on the timeout path |
| **S2** | `hang_on="get"` | as S1 plus `detail["last_phase"] == "token_ok"` | a POST-only bound at the step-up site |
| **S3** | aggregate, 0.4/0.4 vs 0.6 | `location` contains `sso_stepup_error=`, not `#stepup_token=` | a per-call bound at the step-up site |
| **S4** | S1 with `return_to: "security"` | `location.endswith("/settings/security?sso_stepup_error=token")` | a timeout branch building its redirect with the hard-coded default instead of routing through `_resolve_return_path(state)` |

### Strengthening an existing weak test

`test_httpx_error_during_token_exchange_redirects_with_token_code` (`:293-313`) asserts only the
Location; its immediate neighbour at `:288-290` already asserts the audit row, so the omission is
an oversight. Add `len(rows) == 1` and `rows[0].detail == {"reason": "token"}`.
*Named victim:* an implementation that mis-labels a genuine httpx error as `timeout`, or one that
drops the httpx branch's audit write while refactoring around it. Also turns §2.2's
disjoint-hierarchy analysis from an assumption into a fence.

### Anti-vacuity gate — mandatory, not optional

For **every** fence above: revert the `auth.py` change and confirm it goes **RED for the right
reason** — specifically that the `{"reason": "timeout"}` tests fail via `TimeoutError`
propagation, not via a fixture error or a `KeyError`. Green-against-unmodified-`main` is this
repo's most-repeated defect (17 instances).

## 8. Scope

**IN:** the constant + its guard test; `timeout_at` + `except TimeoutError` at both sites; the
`ui_code` param on `_stepup_failure`; audit `reason="timeout"` + `last_phase`; the ungated
warning at both sites; tests L1, L2, L3, L5, S1-S4; strengthening the weak test.

**OUT, each on the merits:**
- **Frontend** — precedent, fallback, and identical advice; a new code means worse copy.
- **`.do/app.yaml` / `.env.example`** — no config field exists; a partial binding is the risk.
- **Step-up breadcrumbs** — gated, off in prod, superseded.
- **Tightening `GOOGLE_OAUTH_TIMEOUT`** — still the basis of the 20.0 rationale.
- **`tokens['access_token']` KeyError → 500** — pre-existing; opportunistically fixing it would
  require the `except Exception` that §2.2 forbids.
- **`services/email_service.py:194, 301`** — and the ticket's framing of this is wrong. Each is a
  **single** `client.post` (N=1, no aggregate to bound) and each is wrapped in `except Exception`,
  which already catches builtin `TimeoutError`. It shares neither of TBD-179's two defects. It
  does share the per-read-chunk drip-feed exposure, which is genuine but lower severity —
  background send, fails closed, not a top-level navigation. Note it in the PR body; do not fix it
  here.

## 9. Biggest production risk

**The aggregate lands below the real p99 of a healthy exchange, converting working sign-ins into
`?sso_error=token`.** That is worse than the bug being fixed: today's failure is
slow-but-eventually-succeeds; the new one is fast, terminal, and total, on a public endpoint, and
it hits step-up too — locking users out of email change and first-password-set. It would surface
as an unexplained conversion drop with no 5xx.

Held by, in order of strength:

1. **20.0s is ~40x normal end-to-end latency** and is guarded against drift by the §4 relationship
   test. Reaching it requires both calls at ~40x normal without either tripping its own phase bound.
2. **The failure is loud before it is large.** Every trip writes an ungated
   `auth.google.callback.exchange_timeout` warning *and* a `reason:"timeout"` audit row visible in
   `/admin/audit` — deliberately a **distinct** reason, so a mis-set bound shows up as a
   previously-empty bucket filling rather than as noise in the pre-existing `token` bucket. That
   distinctness is what turns "conversion is down and nobody knows why" into one query.
3. **The user is never stranded.** Every timeout still lands on the existing friendly copy with a
   working password fallback, because the redirect code stays `token`.

Runner-up — the `except` swallowing a genuine bug and masking it as a friendly retry banner — is
held by the narrow single-name clause, the verified-disjoint hierarchies, and test **L5**.
