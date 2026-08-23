# TBD-413 — `/ready` reports healthy while Redis is unreachable

Status: accepted, 2026-08-22. Two independent architects, then a
concede-or-defend cross round. Both ruled **C** independently.

## The defect, reproduced

`backend/app/main.py:548` — `/ready` executes `SELECT 1` and nothing else.
Redis is the auth session store and every token-issue path fails **closed**
(`routers/auth.py:696-702, 757-767, 1118-1135, 2791-2797, 3219-3225`).

Measured live on 2026-08-22 in an isolated compose project, Redis stopped:

```
/health: (200, '{"status":"ok"}')
/ready : (200, '{"status":"ready","database":"connected"}')
/login : (503, '{"detail":"Authentication temporarily unavailable"}')
```

That is the 2026-08-19 incident (TBD-360 cutover, cause TBD-412).

`/ready` has **zero** behavioural tests today; it appears only as an inventory
tuple at `tests/auth/test_public_route_allowlist.py:110`.

## Where the ticket is wrong

1. **"App Platform will pull the instance out of rotation on a non-200" is
   false as wired.** `.do/app.yaml:84` health-checks **`/health`**, not
   `/ready`. Nothing on App Platform polls `/ready`. The rotation risk is real
   only for `k8s/templates/backend.yaml:56`, which is committed but not
   deployed.
2. **A constraint the ticket omits.** `.github/workflows/test.yml`
   `Migration Checks` boots the real app with **no `REDIS_URL`** and asserts
   `/ready` == 200. It feeds the required `Backend Checks` gate. A naive Redis
   check turns a required gate red 100% of runs.
3. **Phases: 1.2, 2.1 and 4.3** use `/ready` as a gate, not just 1.2 and 4.3.
4. `scripts/smoke-test.sh:12` already *claims* `/ready` covers "DB + Redis
   reachable". That comment is currently a lie.

## Ruling — C, split the concern

`/ready` stays the narrow rotation gate, **byte-identical**. A new
`GET /health/dependencies` carries per-dependency truth and 503s when a
required dependency is unusable.

Why not A (503 on `/ready`): it buys **no** automated protection on the
platform we actually run (nothing polls it), and where it does have teeth —
the k8s readinessProbe — Redis is a single shared instance, so a Redis outage
fails **every replica's** readiness at once and evicts the whole deployment,
including the data-plane routes that still work (`backend/app/deps.py` has
zero Redis references; access tokens are valid 15 minutes). It would also turn
the required CI gate red.

Why not B (200 + degraded body): a monitor alarms on a status code. A body
nobody's alerting parses is the same silent green the ticket was filed about.

C gives the rotation gate and the truth surface different jobs, so neither has
to compromise.

**Scope rule, stated in the code:** every check on this endpoint is required by
construction. A dependency whose failure does not change the status code does
not belong here.

## Contract

`/health` — unchanged. `200 {"status":"ok"}`. Pure liveness, no dependency.

`/ready` — **response contract unchanged, byte for byte**: same body, same two
status codes. One internal change only: its `SELECT 1` is wrapped in
`asyncio.wait_for(..., 3.0)`.

⚠ That wrapper is **in scope, not gold-plating**. `backend/app/database.py:22-31`
states in its own docstring that aiomysql 0.2.0 does not accept
`read_timeout` / `write_timeout`, so `connect_timeout` bounds connection
*establishment* only. A `SELECT 1` on an established-but-wedged socket has **no
driver bound at all**, and `pool_pre_ping=True` issues its own unbounded query
on checkout. `/ready` can therefore hang today. The same docstring names the
route-local `wait_for` on `/auth/refresh` as the existing instance of this
pattern. Only latency changes; body and both codes are identical.

`GET /health/dependencies` — public. Body shape identical on 200 and 503:

```json
{"status": "ok" | "unhealthy",
 "checks": {"database": "ok" | "unreachable" | "timeout",
            "redis":    "ok" | "unreachable" | "timeout" | "auth_failed"
                        | "disabled" | "not_configured"}}
```

**200 iff** `database == "ok"` **AND** `redis in {"ok", "disabled"}`.
Otherwise 503.

`disabled` and `not_configured` are the **same observation** — `redis_url` is
empty — named differently by environment: `disabled` is a supported mode
outside production, `not_configured` is a required dependency missing in
production. One extra word buys a body that explains itself at 3am, which a
bare `not_configured` sitting next to `"status":"ok"` does not.

| db | redis | env | code | status |
|---|---|---|---|---|
| ok | ok | any | 200 | ok |
| ok | unreachable / timeout / auth_failed | **any** | 503 | unhealthy |
| ok | **disabled** | non-production | 200 | ok |
| ok | **not_configured** | production | 503 | unhealthy |
| unreachable / timeout | ok | any | 503 | unhealthy |
| unreachable / timeout | any failing | any | 503 | unhealthy, **both reported** |

⚠ **Configured-but-unreachable is 503 in EVERY environment.** Only the
empty-`redis_url` row is environment-sensitive. Architect 1's prose formula
("200 iff db ok AND (redis ok OR app_env != production)") contradicted its own
table and would have returned **200 in dev with Redis down** — the exact state
reproduced above. Resolved in favour of both architects' tables.

Coarse strings only. **Never** exception text, host, port, or driver message —
this endpoint is unauthenticated.

Database vocabulary deliberately has no `auth_failed`: MySQL's 1045 is only
reachable through `exc.orig.args[0]`, too fragile to claim. Redis's
`AuthenticationError` is a real exception class, and credential failure is what
the 2026-08-19 incident actually was.

## Mechanics

In `backend/app/main.py`, beside `ready()`. `asyncio`, `JSONResponse`,
`engine`, `text` and `redis_client` are all already imported.

- `_probe_database()` — `asyncio.wait_for(<SELECT 1 via engine.connect()>, 3.0)`
  → `ok` / `timeout` / `unreachable`. ⚠ This wrapper is the **only** bound on
  the query, not belt-and-braces: per `database.py:22-31` aiomysql 0.2.0 takes
  no `read_timeout`, `connect_timeout` covers establishment only,
  `pool_pre_ping=True` adds another unbounded query on checkout, and
  SQLAlchemy's `pool_timeout` defaults to 30s. 3.0s over A2's 2.0s because a
  cold pool-grow connect legitimately takes seconds and 2.0s would false-alarm
  a healthy app into a failed deploy gate.
- `_probe_redis()` — `redis_client.get_client()`. `None` → `disabled`
  (non-production) or `not_configured` (production), with **zero I/O**. Else `asyncio.wait_for(client.ping(), 5.0)`.
  **The bound is deliberately ABOVE the auth path's own 3.2s honest worst
  case** (`redis_client.get_client()` docstring: `1.0 + 1*(1.0+1.0+0.2)`). A
  bound below it makes the probe report failures that real logins absorb — a
  flapping false alarm on the idle-dropped-socket class this infrastructure is
  documented to produce (`redis_client.py:44-54`).
  ⚠ **Revised 3.5 → 5.0 during review.** 3.2s bounds the *library's* own
  waits, not wall clock: the coroutine shares an event loop with the
  concurrent database probe and with real traffic, so scheduling delay lands
  on top. 0.3s of headroom made the single commonest real event — an
  idle-dropped socket — report `timeout` + 503 under load, and
  `scripts/smoke-test.sh` turns that 503 into a failed deploy. 5.0 still fits
  under the 6.0s backstop because the probes run **concurrently**: the floor
  the backstop must clear is `max(3.0, 5.0)`, not the sum. Fenced by F17b.
- Both under one `asyncio.gather(..., return_exceptions=True)`, that gather
  under `asyncio.wait_for(..., 6.0)` as a pure backstop. If the backstop fires,
  report **both** as `timeout` and 503 — never an empty body.
- **Except order is load-bearing:** `AuthenticationError` **before**
  `ConnectionError`. Verified in-session: `AuthenticationError.__mro__` is
  `(AuthenticationError, ConnectionError, RedisError, Exception, ...)`, and
  `TimeoutError` is **not** a `ConnectionError` subclass. Then
  `TimeoutError`/`asyncio.TimeoutError`, then `ConnectionError`/`RedisError`/
  `OSError`, then a broad `Exception` so uvloop's bare `RuntimeError` cannot
  turn a monitoring endpoint into a 500.
- Raw `client.ping()` on the **shared singleton**. Never a
  `@_normalize_transport_errors`-wrapped helper (it calls
  `_retire_poisoned_client`, so a monitor scrape could tear down the live auth
  pool). Never `Redis.from_url()` per request. Never `require_client()`. Never
  `close_client()`.
- `logger.error("readiness.dependencies.unhealthy", database=..., redis=...)`
  on every 503, matching the neighbouring `/ready` handler's level.

### Cancellation safety — established from installed source, not assumed

`redis==5.2.1`. A `ping()` cancelled by `asyncio.wait_for` does **not** poison
the shared pool:

- `redis/asyncio/connection.py :: AbstractConnection.send_packed_command` —
  `except BaseException: await self.disconnect(nowait=True); raise`
- `redis/asyncio/connection.py :: AbstractConnection.read_response` —
  `except BaseException:` ... `await self.disconnect(nowait=True); raise`
  (comment cites redis-py #1128)
- `asyncio.CancelledError` is a `BaseException`, so the socket closes before
  the exception propagates. `Redis.execute_command`'s
  `finally: await pool.release(conn)` then returns an already-disconnected
  connection, which is safe.
- Defense in depth: `ConnectionPool.ensure_connection` calls
  `can_read_destructive()` and rebuilds any connection holding stale data
  before handing it to auth traffic.

Residual, named honestly: the next auth request borrowing that connection pays
one disconnect+reconnect bounded by `socket_connect_timeout` (1.0s). Bounded
and self-healing. Putting the probe's bound *above* redis-py's own budget makes
this rare rather than routine — which is why the 5.0s number matters.

A per-probe single-connection client was proposed and **withdrawn by its own
author**: it costs a TCP connect per scrape, abandons the singleton, and
measures a path production never uses — so it could report green while the
pool real traffic uses is poisoned.

## Tests — `backend/tests/test_readiness_dependencies.py`

Prerequisite: `_SharedFakeRedis` (`tests/conftest.py:169+`) has **no `ping`**
and is a plain class, not a mock, so `fake.ping()` raises `AttributeError`.
Under a probe's broad `except Exception` that surfaces as `unreachable` — a
silent false red that invites "fixing" the except clause, which would mask real
errors forever. Add `async def ping(self): return True`.

⚠ **Corrected during review:** this does *not* affect the fences below. Every
test in `test_readiness_dependencies.py` substitutes `get_client` itself, so
the ambient fake is never that probe's client. The real effect is latent: a
future test driving `admin_dashboard_service`'s Redis probe through the ambient
fake now gets `{"ok": True}` rather than an `AttributeError`-shaped failure.

Every test monkeypatches `get_client` and `settings.app_env` explicitly.
⚠ Never rely on ambient `settings.redis_url`: it is `redis://redis:6379/0` in
the dev container and `""` on the CI shards, so an ambient-dependent test
passes in one and fails in the other.

| # | Kind | Asserts | Wrong implementation it kills |
|---|---|---|---|
| F1 | fence | db ok + redis ok → 200, both `ok` | — baseline |
| F2 | fence | db ok + redis unreachable → **503** | the shipped bug: reporting healthy while Redis is down |
| F3 | fence | F2 holds in **development** too | the env-gated formula that returns 200 in dev with Redis down |
| F4 | fence | redis `AuthenticationError` → `auth_failed`, 503 | catching `ConnectionError` first, which swallows auth failure into `unreachable` |
| F4b | guard | `AuthenticationError` really does subclass `ConnectionError` | a redis-py change that silently evaporates F4's ordering rationale |
| F4c | fence | redis `NoPermissionError` (ACL `NOPERM`) → `auth_failed`, 503 | omitting it from the `auth_failed` clause. It is a `ResponseError` → `RedisError`, **not** a `ConnectionError`, so unlike `AuthenticationError` it does not ride F4's subclass ordering — it falls into the `RedisError` tuple and reports `unreachable`, the same misdiagnosis F4 exists to prevent, one rung over |
| F4d | guard | `NoPermissionError` is not a `ConnectionError` but is a `RedisError` | the premise F4c rests on |
| F5 | fence | redis absent + non-prod → 200 `disabled` | making unconfigured Redis fail, which reds the required CI gate |
| F6 | fence | redis absent + production → 503 `not_configured` | treating prod-without-Redis as fine; also kills collapsing the two names into one |
| F7 | fence | db down + redis ok → 503, **redis still reported `ok`** | short-circuiting on the first failure |
| F8 | fence | db down + redis down → 503, **both** reported | same, other order |
| F9 | fence | redis `ping` hangs → `timeout`, bounded well under the backstop, **database answer preserved** | no per-probe bound. ⚠ Asserting only `redis == "timeout"` is vacuous: the outer backstop catches the hang and reports both as `timeout`. The discriminators are the surviving `database: "ok"` and wall clock |
| F9b | fence | db connect hangs → `timeout`, redis still `ok` | no per-probe bound on the database side, which has no driver bound at all |
| F9c | fence | redis `TimeoutError` (redis-py's own `socket_timeout`) → `timeout`, not `unreachable` | deleting the `except RedisTimeoutError` clause, which collapses the state into `unreachable` via the `RedisError` tuple with every other test green |
| F10 | fence | `/ready` still 200 `{"status":"ready","database":"connected"}` with Redis down | someone "helpfully" adding the Redis check to `/ready`, breaking CI + k8s |
| F11 | fence | `/health` still `{"status":"ok"}` with both down | making liveness depend on anything external |
| F12 | fence | the substituted singleton was pinged **exactly once** | `Redis.from_url()` per request, `require_client()`, or any wrapped helper. ⚠ Revised during review: the original monkeypatched `_build_auth_redis_client` to raise, which is **structurally unreachable** because the test also patches `get_client`, its only caller — leaving `status_code == 200` as the sole assertion, which the healthy fixture produces regardless. `ping_calls` is the real discriminator |
| F13 | fence | singleton identity unchanged after a failing probe | routing through a `_normalize_transport_errors` helper, which retires the live pool. ⚠ Revised during review: raising `RedisConnectionError` made this **vacuous** — the decorator hits `except RedisError: raise` first and never reaches `_retire_poisoned_client`, so it passed against correct code, the mutant, and unmodified `main`. `ConnectionResetError` is the discriminating input: an `OSError` subclass, so it takes the decorator's retiring leg while still landing in the probe's own except tuple (and it is that tuple's only `OSError` coverage) |
| F14 | guard | `inspect.signature(AbstractConnection.read_response).parameters["disconnect_on_error"].default is True` | a redis-py upgrade silently dropping the cancellation-safety contract the design rests on. Parsed, not grepped; both file:line citations live in the docstring as the next-upgrade checklist |
| F15 | guard | no exception text / host / port in any response body | leaking internals on an unauthenticated endpoint |
| F16 | fence | the SET of states every branch produces equals `_DB_STATES` / `_REDIS_STATES` / `_STATUS_VALUES`, declared in `app/main.py` | ⚠ Revised during review: the original drove one scenario and asserted membership in a superset containing the one value that scenario can produce, while F1 already pinned that whole body by strict equality. It killed nothing. Now fenced both ways — a state produced but not declared, and a declaration nothing produces |
| F16b | fence | every string literal either probe can `return`, by AST | a new state on a path no scenario reaches. ⚠ Only value positions count: `ast.walk` over the whole `return` subtree reports the constant in `app_env == "production"` as a returnable state |
| F17 | fence | both probes hanging → 503 with both `timeout`, in ~0.05s with the backstop patched low and the per-probe bounds pinned at 3.0s | deleting the total `asyncio.wait_for`. ⚠ The body alone does not discriminate: with it deleted the per-probe bounds produce the identical body, seconds later. Wall clock is the kill |
| F17b | guard | `max(db, redis) < backstop` | a per-probe bound raised above the backstop, which makes that probe's own timeout unreachable and silently loses the other probe's real answer |
| F17c | fence | a probe that RAISES still yields the normal body with `redis: "unreachable"` | dropping the `isinstance(raw[i], str)` fallback, which puts an exception instance in the body and 500s a monitoring endpoint |
| F18 | fence | `/ready` 503s with the byte-identical `{"status":"not_ready","database":"connection error"}` | this diff changed `/ready` and nothing drove its failure branch at all |
| F19 | fence | `/ready` against a hanging connect 503s in ~0.05s **and** logs a discriminating `TimeoutError` | removing the `wait_for` this diff added to `/ready`; and logging `error=str(e)` alone, which is the **empty string** for the bare `TimeoutError()` `wait_for` raises — on precisely the wedged-socket mode the bound was added to catch |

Structural fences (repo-root discovery + CI guard, the
`test_deploy_drift_probe.py` pattern — `.github/`, `infra/`, `nginx/` are not
mounted into the backend container):

| # | Kind | Asserts |
|---|---|---|
| S1 | fence | `Migration Checks` asserts the endpoint, parsing JSON — **not** grepping the body |
| S2 | fence | that job still sets no `REDIS_URL` and has no Redis service |
| S3 | fence | `nginx/default.conf` has an exact `location = /health/dependencies` |
| S4 | fence | `k8s/templates/ingress.yaml` has a `pathType: Exact` rule for it |
| S5 | fence | the chart's readinessProbe stays on `/ready` and carries an explicit `timeoutSeconds >= 3` (the k8s default is **1s**, under the app's own 3.0s database bound) |
| S6 | fence | `smoke-test.sh` checks it, and its header no longer claims `/ready` covers Redis |
| S7 | fence | `.do/app.yaml`'s ingress routes `/health/dependencies` to the `backend` component, under both longest-prefix and document-order semantics |

⚠ S1–S7 **parse structure**; none is a whole-file grep. A whole-file grep for a
key has been satisfied by the comment documenting that key's absence three
times in this repo.

⚠ **S7 was added during review, and it covers the only environment that is
actually in production.** S3 fences dev nginx, S4/S5 the k8s chart (deployed
nowhere today), S1/S2 CI, S6 the smoke test — production is DO App Platform and
`.do/app.yaml` had no fence at all. The route there is **incidental**: App
Platform rules are prefixes, so `/health/dependencies` reaches the backend only
as a side effect of the `prefix: /health` rule, and nothing in the file names
this endpoint. Narrow or delete that rule and the request falls through to the
`prefix: /` catch-all, which points at the **frontend** — the monitor and the
deploy gate then measure the wrong component. That is why the spec's "`.do/app.yaml`
— no change" line above is correct about the *file* and wrong about the
*coverage*.

## Other forced edits

- `tests/auth/test_public_route_allowlist.py` — add
  `("GET", "/health/dependencies")`; counts 25 → 26.
- `CONTRIBUTING.md:298` public-endpoints table, and `CLAUDE.md:266` —
  25 → 26 pairs, 10 → 11 truly open.
- `nginx/default.conf` — new `location = /health/dependencies`. The blocks at
  `:59` and `:68` are **exact**; without this it falls through to Next.js.
- `k8s/templates/ingress.yaml` — new `pathType: Exact` rule (`/health` and
  `/ready` at `:27-40` are both Exact).
- `k8s/templates/backend.yaml:54-59` — add `timeoutSeconds: 3` and
  `failureThreshold: 3` to the `/ready` readinessProbe (the k8s default is
  **1s**, under the client's own budget), plus a comment: `/ready` is DB-only
  on purpose, never repoint this probe at the dependency endpoint.
- `.do/app.yaml` — **no change.** Its rule is `prefix: /health`, which already
  covers the sub-path, and the file overwrites all 16 live secrets on every
  deploy (TBD-425). Shipping this never opens it.
- `backend/app/logging.py` — **no change.** See Rejected.
- `scripts/smoke-test.sh` — add the check; fix the `:12` claim.
- Docs repointed: `infra/MYSQL-84-EXECUTE.md` 1.2/2.1/4.3 (rewrite the `:387`
  caveat rather than delete it — it stays true of `/ready`),
  `infra/MYSQL-84-CUTOVER.md`, `DEPLOYMENT.md:229`, and `infra/MIGRATION.md:293`,
  which names `/api/v1/health` and `/api/v1/ready` — neither has ever existed.

## Security review — `GET /health/dependencies`

CONTRIBUTING.md's public-endpoints section states the list must not be added
to without a security review. This section is that review, recorded in tree so
the addition is not merely asserted.

**Assessed**

- **Unauthenticated by necessity.** The caller is an uptime monitor (DO uptime
  check, and `scripts/smoke-test.sh` running against a fresh deploy). Neither
  holds a bearer token, and a monitor that has to log in cannot report on the
  outage class this endpoint exists for — the 2026-08-19 state where Redis was
  enforcing a stale password and *every login returned 503*. An authenticated
  readiness endpoint would have been down at exactly the moment it was needed.
- **Closed, coarse vocabulary.** The response is two enumerated strings plus a
  status word, declared in `app/main.py` as `_DB_STATES` / `_REDIS_STATES` /
  `_STATUS_VALUES` and fenced in both directions by `test_f16_*` (every state
  produced is declared) and `test_f16b_*` (every literal either probe can
  return is declared). The vocabulary cannot widen without a deliberate edit
  in the module that owns it.
- **No exception text reaches the body.** No `str(exc)`, no hostname, port,
  driver message, URL or credential. `test_f15_response_never_leaks_exception_detail`
  drives both probes with exception messages containing a Redis URL with a
  password, a MySQL DSN with a password, private IPs and ports, and asserts
  none of it appears in the response. The detail that operators need goes to
  the structured `logger.error`, which is not public.
- **No new information disclosure beyond what already existed.** `/ready`
  already tells an anonymous caller whether the database is reachable. The
  increment is "is Redis reachable / authenticating", which is the same class
  of fact and is what the endpoint is for. No version strings, no topology, no
  component names.
- **No state is written and no user data is touched.** The database probe is
  `SELECT 1`; the Redis probe is `PING` on the existing shared singleton. The
  probe is a pure observer: `test_f12_*` pins that it pings the shared client
  exactly once and constructs nothing, and `test_f13_*` pins that a failing
  probe does not retire the pool real auth traffic is using.
- **Enumerated in the allowlist.** The pair is added to
  `backend/tests/auth/test_public_route_allowlist.py`, which enforces the set
  in both directions, so the route cannot silently stop being reviewed.

**Stated limitations — accepted, not defects to fix here**

- **`PING` is weaker than the contract.** `PING` succeeds against a Redis at
  `maxmemory` with `noeviction`, and against a read-only replica. Both states
  return `redis: "ok"` and 200 while session *writes* fail, so a green verdict
  is not proof that login works. Detecting those needs a write probe, which
  brings its own key-lifecycle and blast-radius questions on an unauthenticated
  endpoint. Out of scope: this ticket's defect is a *reachability and
  credential* failure reported as healthy, and `PING` covers that.
- **Unauthenticated and unrate-limited, and each hit checks out a pooled DB
  connection.** The engine is `pool_size=5, max_overflow=10`, so a high volume
  of concurrent anonymous scrapes can contend with real traffic for
  connections. This is **pre-existing and unchanged in kind**: `/ready` has had
  exactly this property since it was written, and the new endpoint adds a
  second path with the same cost rather than a new one. Rate-limiting a
  readiness surface has its own failure mode — a throttled monitor reports the
  app down — so it is deliberately not added here.

## Rejected, with reasons

- **Silencing the new path in `_SILENT_PATHS`.** The two architects **swapped**
  positions, so the cross produced no resolution and the repo's rule is not to
  run another reading round. `logging.py:16` states health checks are silenced
  because they "flood logs in production" — which justifies silencing paths a
  *platform* probe hits every 10-15s. Nothing platform-level polls this
  endpoint; that is the entire point of the split. Budget is ~3 DO regions x
  60s = ~4,300 lines/day, and not silencing is the reversible choice.
  **Counter, recorded:** the access line carries only `503`, not which
  dependency failed, so the structured log dominates it. True — which is why
  the structured `logger.error` on the unhealthy path stays. The access line
  gives the timeline; the structured log gives which dependency and in what
  state, and it also fires for a human `curl` and for the smoke test.
- **Fixing `_DropHealthCheck.filter`'s query-string gap** (`logging.py:28-33`:
  `_ACCESS_RE` captures the target including `?x=1`, so `/ready?x=1` is not
  silenced). Pre-existing, unrelated, out of scope.
- **A production validator on `redis_url` in `config.py`.** Both architects
  ended up declining it here. It is a **boot-refusal**, i.e. a one-way door on
  deploy behaviour, and `.do/app.yaml:400-406` carries a comment inviting
  removal of the PRE_DEPLOY `REDIS_URL` binding — once the validator exists,
  `migrate.py` imports `app.config`, `Settings` refuses to construct in
  production without `REDIS_URL`, and a good-faith cleanup acting on that
  comment breaks PRE_DEPLOY on **every** production deploy. It also adds no
  detection this ticket does not already deliver: the unconfigured-in-production
  row is already 503, `smoke-test.sh` already fails the deploy on it, and it
  would **not** have caught TBD-425 (credentials overwritten with
  stale-but-present values). Filed to the backlog, carrying the condition that
  it does not ship unless `.do/app.yaml:400-406`'s comment is rewritten in the
  same PR to mark the binding required.
- **A nested `required` field per check.** Derivable from `app_env`, and a CI
  assertion on `required: false` still would not prove the production branch
  503s — a fence that looks load-bearing and is not. Both architects dropped it.

## Definition of done

The endpoint is not an alarm until something polls it. **A DO uptime check plus
alert policy against `/health/dependencies` is a DoD line, operator-run** (the
tool classifier blocks agent `doctl apps update`), not a follow-up ticket.
Without it this ticket closes green over the open half of its own defect.
