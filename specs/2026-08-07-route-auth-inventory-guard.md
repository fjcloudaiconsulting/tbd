# TBD-334 — Automated enforcement of the closed public-endpoint set

Status: agreed, ready to build
Date: 2026-08-07

Two independent architects converged on this design. A security review of the
five undocumented routes cleared all five. §7 records where the ticket is wrong.

---

## 1. The gap

Backend route tests build their app through `make_test_app(..., current_user=...)`,
which **overrides** `get_current_user`. Every test runs as if authenticated
regardless of whether the route declares the dependency. **Delete
`Depends(get_current_user)` from any route and no test fails** — the route
silently becomes public.

CLAUDE.md and CONTRIBUTING.md both state the public set is "deliberately small
and closed". That policy has **zero** automated enforcement. Same class as
TBD-322: documented, believed, untested.

---

## 2. Mechanism: runtime enumeration. Not AST.

Enumerate `app.routes` off the real `app.main:app`.

### Importing the app is safe — established, not assumed

- `_run_migrations()` is called **only** from inside the lifespan
  (`backend/app/main.py:267-268`, inside `async def lifespan` at `:255`). Module
  import never reaches it.
- The scheduler task is created inside the lifespan (`main.py:280-289`), and
  `conftest.py:359-382` additionally forces `scheduler_enabled = False`.
- `create_async_engine` is lazy — builds a pool, does not connect.
- `conftest.py:17-24` pre-seeds `JWT_SECRET_KEY` and `APP_ENV` before any app
  import.
- **Decisive precedent:** `backend/tests/test_lifespan_migrate_logging.py:23`
  already does `from app import main as app_main` at module scope, in CI, green.

**The only rule: never enter the lifespan.** Read `app.routes`; never
`with TestClient(app)`.

### Why AST is disqualified

Auth is declared four different ways, all live:
1. handler signature `current_user: User = Depends(get_current_user)` — ~156 sites
2. router-level `APIRouter(..., dependencies=[Depends(require_superadmin)])` —
   `admin_features.py:67`
3. router-level feature gates — `reports.py:85`, `dashboard.py:131`,
   `scenarios.py:60`, `budgets.py:30`, `forecast_plans.py:29`, `ai_budget.py:54`,
   `ai_forecast.py:76`
4. decorator-level `dependencies=[Depends(require_interactive_session)]` — ~30 sites

An AST scan needs a hand-maintained roster of ~13 wrapper names, **six of which
are the same name (`require_superadmin`) defined independently in six files**
(`admin_ai_usage.py:67`, `admin_announcements.py:48`, `admin_broadcasts.py:80`,
`admin_features.py:51`, `admin_rate_limit_overrides.py:54`, `api_tokens.py:63`).
That roster **fails open**: the day someone adds a `require_x` that looks
authoritative but never calls `get_current_user`, the name list blesses it
forever.

⚠ Measured during design: an AST pass reported
`PUT /api/v1/settings/features/{feature}` as PUBLIC. It is not — it is gated by
`require_settings_admin` (`settings.py:120`), a locally-defined wrapper the name
list did not know about.

**Runtime needs zero names. It needs one object identity.**

---

## 3. What counts as authentication

**Exactly one thing:** `app.deps.get_current_user` appears as `.call` somewhere
in `route.dependant`'s transitive closure.

```python
def _reaches_get_current_user(dependant) -> bool:
    stack = [dependant]
    while stack:
        node = stack.pop()
        if node.call is get_current_user:      # identity, NOT name
            return True
        stack.extend(node.dependencies)
    return False
```

FastAPI's `APIRoute.__init__` merges router-level and decorator-level
`dependencies` into `self.dependant.dependencies`, and `get_dependant` recurses
into every `Depends()` in the signature. **All four declaration shapes land in
the same tree**, so one walk covers them all.

Identity, never name: `require_permission("orgs.view")` returns a fresh closure
per call, so name matching on wrappers is useless. Matching the leaf is what
makes the guard immune to wrappers that do not yet exist.

**`get_current_user_optional` (`deps.py:95`) is NOT authentication** — it returns
`None` on every failure path. It is a different object and correctly does not
match. Its one consumer, `GET /api/v1/auth/status`, is correctly public.

**`require_interactive_session` DOES authenticate** — `auth/pat.py:205-208`
declares `user: User = Depends(get_current_user)`, and its docstring says the
dependency is load-bearing precisely so `get_current_user` runs first. The design
never asks the question: if a refactor ever drops that `Depends`, every route
reached through it flips to public and the guard goes RED. A name-based allowlist
containing `"require_interactive_session"` would stay green through exactly that
refactor.

**Named fail-open, stated honestly in the test docstring:** this is a
*structural* fence. It proves `get_current_user` is wired into the graph. It does
**not** prove `get_current_user` still authenticates — gutting its body leaves
the guard green. That is out of scope and covered by the behavioural auth suites.
Say so, so no future reader over-trusts it.

---

## 4. The allowlist

**Exact `(METHOD, path)` tuples, hand-written literal, two-way set equality.**

Prefixes are disqualified, and CONTRIBUTING's own glob proves why: a
`/api/v1/auth/mfa/` prefix would allowlist **four routes that are authenticated
and interactive-session-gated** (`mfa/setup` `auth.py:2558`, `mfa/enable` `:2585`,
`mfa/disable` `:2667`, `mfa/recovery-codes` `:2744`, all in
`test_interactive_session_enumeration.py:78-81`). It would silently bless the
account-takeover surface. The literal expands the glob to its four concrete
challenge routes.

Method is part of the key: `POST /auth/resend-verification-public` is public
while `POST /auth/resend-verification` (`auth.py:2021`) is not.

**Two assertions, distinct messages:**
- `found_public - ALLOWLIST` → *"UNPROTECTED: reachable without authentication
  and not on the reviewed public allowlist"*
- `ALLOWLIST - found_public` → *"STALE: allowlisted route no longer exists or is
  now authenticated; drop the entry"*

The second direction is what stops the allowlist rotting into a permanent
superset. Both existing precedents assert both directions
(`test_no_raw_request_client.py:168-187`,
`test_sessions_invalidated_at_allowlist.py:229-256`); the ticket dropped it.

**Never derive the allowlist from app state.** No
`[r for r in app.routes if not authed(r)]` seeding, ever. It must be typed by
hand. **Never parse CONTRIBUTING.md** — a prose parser that silently matches zero
entries yields a tautologically green guard.

### The 25 entries

Verified independently twice, by both architects and again by the security
review's AST transitive-closure pass. **The builder must confirm each path string
against the live `route.path`** rather than trusting this reconstruction.

```
("GET",  "/health")                                 main.py:543
("GET",  "/ready")                                  main.py:548
("GET",  "/api/v1/auth/status")                     auth.py:193   get_current_user_OPTIONAL
("GET",  "/api/v1/auth/check-username")             auth.py:262
("POST", "/api/v1/auth/register")                   auth.py:283
("POST", "/api/v1/auth/login")                      auth.py:453
("POST", "/api/v1/auth/refresh")                    auth.py:1344
("POST", "/api/v1/auth/verify")                     auth.py:1627  ⚠ not in CONTRIBUTING
("POST", "/api/v1/auth/logout")                     auth.py:1697  ⚠ not in CONTRIBUTING
("POST", "/api/v1/auth/forgot-password")            auth.py:1857
("POST", "/api/v1/auth/reset-password")             auth.py:1876
("POST", "/api/v1/auth/verify-email")               auth.py:1982
("POST", "/api/v1/auth/resend-verification-public") auth.py:2037
("POST", "/api/v1/auth/mfa/verify")                 auth.py:2817
("POST", "/api/v1/auth/mfa/recovery")               auth.py:2846
("POST", "/api/v1/auth/mfa/email-code")             auth.py:2884
("POST", "/api/v1/auth/mfa/email-verify")           auth.py:2930
("GET",  "/api/v1/auth/google")                     auth.py:3021
("GET",  "/api/v1/auth/google/callback")            auth.py:3054
("GET",  "/api/v1/auth/sso-stepup/callback")        auth.py:3597  ⚠ not in CONTRIBUTING
("GET",  "/api/v1/orgs/invitations/preview")        org_members.py:184  ⚠ not in CONTRIBUTING
("POST", "/api/v1/orgs/invitations/accept")         org_members.py:197  ⚠ not in CONTRIBUTING
("GET",  "/api/v1/public/founder-count")            public_stats.py:34
("POST", "/api/v1/security/csp-report")             security.py:170
("POST", "/api/v1/webhooks/mailgun")                webhooks.py:135
```

Each entry carries an inline justification comment, per precedent
(`test_sessions_invalidated_at_allowlist.py:53-84`).

### Non-`APIRoute` entries

With `APP_ENV=development` (`conftest.py:22` default), FastAPI registers
`/api/docs`, `/api/openapi.json` and auto-registers
`swagger_ui_oauth2_redirect_url` at `/docs/oauth2-redirect`. All are plain
`starlette.routing.Route`.

**Do not filter them out with `isinstance(route, APIRoute)` alone** — that would
also silently swallow a future `app.mount()` or `WebSocketRoute`, a hole of
exactly the shape this ticket exists to close. Partition instead: run the auth
check on `APIRoute`s, and assert the non-`APIRoute` path set is a **subset** of
`{"/api/openapi.json", "/api/docs", "/docs/oauth2-redirect"}`. Subset, not
equality, so the dev-only docs toggle does not break it while anything new still
fails closed.

---

## 5. CONTRIBUTING.md must be amended in this PR

The doc enumerates 20; the real surface is 25. **Written as the ticket
specifies, the guard is RED against unmodified `main`.**

A dedicated security review assessed all five undocumented routes. **Verdict:
none is wrongly public.** Each fails the "could it carry `get_current_user`?"
test for a real structural reason. Amend the doc with these justifications:

- **`POST /api/v1/auth/verify`** — authenticated by the httpOnly refresh cookie,
  not a bearer token; Next.js RSC renders have the cookie and no Authorization
  header by construction. Shares the entire validation chain with `/auth/refresh`
  via `_validate_refresh_cookie` so the two cannot drift, and never emits
  `Set-Cookie`.
- **`POST /api/v1/auth/logout`** — must succeed when the access token has already
  expired, or a user cannot log out of a stale session. The refresh cookie's HMAC
  signature is verified before any session family is revoked, so an anonymous
  caller cannot revoke a session they do not hold; a call with no cookie is a
  no-op cookie clear.
- **`GET /api/v1/auth/sso-stepup/callback`** — a browser redirect arriving from
  Google with no Authorization header. Identity is bound by the httpOnly
  `oauth_state` cookie issued at `/sso-stepup/initiate`, which *does* require
  authentication, and re-checked against the Google account's verified email; the
  return target is an allowlisted key, never a caller-supplied URL.
- **`GET /api/v1/orgs/invitations/preview`** — the invitee has no account yet, so
  there is no credential to present. Gated by a signed, 7-day, email-bound
  invitation JWT; every failure mode returns one uniform `410` so the response
  cannot distinguish "not yours" from "does not exist".
- **`POST /api/v1/orgs/invitations/accept`** — creates the account, so it
  necessarily runs before the caller has one. The signed invitation JWT is the
  credential: `org_id` and `role` are read from the locked DB row and never from
  the request body, the role can never be OWNER, and the token is consumed
  single-use under `SELECT ... FOR UPDATE`.

**Structure the amended section as two groups** — "open" and "credential-bearing
(no bearer token by construction)" — so the "small and closed" claim stays
meaningful at 25 entries. Only 10 of the 25 have no in-handler identity check at
all; the other 15 authenticate via a mechanism outside the dependency graph
(refresh cookie, `mfa_token`, invitation JWT, reset/verify JWT, OAuth state
cookie, Mailgun HMAC). Record that distinction or a future reader reads "25
public routes" as far scarier than it is.

⚠ **Rate-limit gaps on `/auth/logout` and `/auth/sso-stepup/callback` are filed
as TBD-353 (High), not fixed here.** Note in the PR body that the amendment
documents *reachability*, not a claim that every listed route is fully hardened,
and reference TBD-353.

---

## 6. The test

**File:** `backend/tests/auth/test_public_route_allowlist.py`, beside
`test_interactive_session_enumeration.py` and
`test_sessions_invalidated_at_allowlist.py`.

- **No DB, no `TestClient`, no `make_test_app`.** Using the factory would
  enumerate a fictional app: it mounts a hand-picked router subset and writes
  `app.dependency_overrides`.
- `from app.main import app` **inside the test function body**, not at module
  scope — `main.py:31` calls `setup_logging()` at import.
- Synchronous tests, no fixtures. Runs in the existing shards; `pytest --splits`
  assigns unknown tests automatically.

### Positive controls — mandatory, a zero-route guard passes trivially

| # | Control | Kills |
|---|---|---|
| **P1** | `len(api_routes) >= 250` (258 today) | the import silently yielding an empty route list |
| **P2** | four routes asserted **authenticated**, one per declaration shape: signature-level `("GET","/api/v1/tags")`; router-level superadmin — every `/api/v1/admin/` path, set non-empty; router-level feature gate `("GET","/api/v1/reports")`; transitive-through-wrapper `("PUT","/api/v1/users/me")` | a detector that always returns `False` |
| **P3** | `("GET","/api/v1/auth/me") in authed` | makes mutant 1 attributable — it flips with the mutant and only with it |
| **P4** ⭐ | `("GET","/api/v1/auth/status") in public` | **the sharpest.** That route declares `Depends(get_current_user_optional)` and `Depends(get_db)`. If the detector matched on *presence of dependencies*, on the `HTTPBearer` scheme, or on a name containing `current_user`, this goes RED. Nothing else catches that class. |
| **P5** | non-`APIRoute` paths ⊆ the known set | a future `app.mount()` or `WebSocketRoute` escaping |
| **P6** | `len(PUBLIC_ROUTES) == 25` and the deduped set is also 25 | a silent "regenerated from app state" edit shows up as a count change in review |
| **P7** | `not app.dependency_overrides` | proves the real app is under inspection |

### The four mutant legs

Back each file up to `/Users/flamarion/.claude/jobs/460e17e5/tmp/` first.
**Never `git checkout --`** — it reverts the whole file and has wiped
uncommitted work twice here.

1. **Delete auth from a real route → RED.** `backend/app/routers/auth.py:1686`,
   inside `async def me(`: change `Depends(get_current_user)` →
   `Depends(get_current_user_optional)`. Chosen deliberately: the optional
   variant is **already imported** at `auth.py:19`, so it is a one-token edit;
   the route still builds (a bare `= None` on a `User`-annotated param makes
   FastAPI raise at import, giving a RED for the wrong reason); and it faithfully
   models the defect. Expect `UNPROTECTED: ('GET', '/api/v1/auth/me')`, and P3
   also RED.
2. **New unlisted public route → RED.** Append to
   `backend/app/routers/security.py`:
   ```python
   @router.get("/debug-ping")
   async def debug_ping():
       return {"ok": True}
   ```
   Expect `UNPROTECTED: ('GET', '/api/v1/security/debug-ping')`. This is a
   **different** leg from 1: leg 1 moves a pair from authed to public, leg 2
   creates a pair that never existed. A guard built as "the authed set never
   shrinks" passes leg 2.
3. **Allowlist is load-bearing → RED.** Delete
   `("POST", "/api/v1/auth/login")` from `PUBLIC_ROUTES`; expect RED in the
   `STALE` direction. Without this leg, an allowlist accidentally derived from
   app state is indistinguishable from a real one.
4. **Restore all three → GREEN**, and confirm `git status` is clean apart from
   the new test and the CONTRIBUTING amendment. **Mandatory.** A fence RED
   against its mutant *and* RED against correct code pins nothing.

---

## 7. Where the ticket is wrong

1. **DoD 2 is factually impossible as written.** "The allowlist must match
   CONTRIBUTING.md's public set exactly" cannot hold — the doc has 20 entries,
   the app has 25. The ticket's own hedge ("verify against CONTRIBUTING.md rather
   than trusting this ticket") is what surfaced it. The resolution is to **amend
   the doc in the same PR** (§5), not to relax the guard. This changes the
   deliverable.
2. **The ticket treats CONTRIBUTING as a correct spec. It is a drifted
   document** — a 20% miss on a list whose own text says "deliberately small and
   closed. Do not add to it without a security review." That drift is a second
   instance of TBD-334's own defect class.
3. **DoD 1's "every route on every registered router" is the wrong unit.**
   Routers are not the unit; the app is. Enumerating routers would miss
   `GET /health` and `GET /ready` (declared directly on `app`, `main.py:543,548`,
   both public) and any future mount.
4. **DoD 3 presents runtime vs AST as a close weighing, and its stated reason is
   the second-best one.** Router-level declaration is real but AST *can* parse
   it. The decisive reason is the ~13-name roster that **fails silently open**.
   The ticket also floats "importing the app is unsafe or slow" as a strike — it
   is not, and `test_lifespan_migrate_logging.py:23` has been proving that in CI
   for months.
5. **DoD 5's mutant is under-specified and permits a vacuous fence.** The literal
   execution — delete the parameter line — makes FastAPI raise at *import*,
   producing a RED unrelated to the guard. Leg 1 above is the mutant that models
   the defect.
6. **The DoD has no positive and no negative control.** It never requires the
   enumeration be proven non-empty, and never requires the detector be proven not
   to accept a non-authenticating dependency. Without **P4**, a guard matching on
   "has any `Depends`" is green on `main`, green everywhere, and useless. The
   ticket's own warning in its §B has no corresponding DoD item.
7. **DoD 1 describes a one-way check.** The `STALE` direction is missing, which
   is what stops the allowlist rotting into a superset that admits anything.
8. **Recorded, out of scope:** with `docs_url="/api/docs"`, FastAPI
   auto-registers `swagger_ui_oauth2_redirect_url` at `/docs/oauth2-redirect` — a
   backend route in the `/docs` namespace that `main.py:315-317` was explicitly
   written to keep free for the public manual. Harmless today (dev-only, and
   nginx routes only `/api/*` to the backend), but the stated intent is not fully
   achieved.
