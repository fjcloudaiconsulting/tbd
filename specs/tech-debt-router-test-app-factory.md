---
name: Centralized router test-app factory
description: Centralize router test app construction so security middleware and exception handlers are opt-in by default, not manually re-created per file. Small tech-debt follow-up surfaced during PR #164 review.
type: project
originSessionId: 497b4e5b-526f-4ed1-bca0-0ee0c5bbc716
---
Captured 2026-05-08. Small tech-debt follow-up, NOT blocking #164.

## Problem

Ad-hoc FastAPI test apps in `backend/tests/` can silently omit middleware and exception handlers like SlowAPI's `app.state.limiter` + `RateLimitExceeded` handler. Tests pass without ever exercising the production wiring. Affects security middleware, exception handlers, auth overrides, session factory overrides, and any other app-state the production app installs.

## Concrete trigger

PR #164 review surfaced that `test_orgs_rename.py` and `test_account_balance_adjustment.py` build their own FastAPI test apps without wiring SlowAPI. The new rate-limit decorators (added in #164) silently no-op in those tests. If someone later writes a 429 regression test in either file, the assertion will spuriously pass because the limiter isn't installed.

PR #164's own new tests wire SlowAPI correctly (mirroring `test_users_password_set.py`), so the immediate gap is covered. The tech-debt is the pattern, not the PR.

## Desired shape

Shared helper, e.g.:
```python
def make_test_app(
    routers: list[APIRouter],
    *,
    with_limiter: bool = True,
    with_exception_handlers: bool = True,
    db_override: Callable | None = None,
    auth_override: Callable | None = None,
) -> FastAPI: ...
```

- Defaults install the production middleware/handler stack.
- Opt-out flags exist for tests that intentionally want a stripped app.
- Centralizes the auth/db override boilerplate that's currently copy-pasted.
- Single source of truth for what a "router test app" looks like.

## Acceptance

- A new 429 test on a rate-limited endpoint **fails** if the test forgot to call `make_test_app(..., with_limiter=True)` (or, if `with_limiter` defaults true, the limiter wiring just works).
- Existing endpoint tests can migrate gradually; no big-bang rewrite.
- New router tests written after this lands use the factory by default; PR review catches deviations.

## Priority

Small tech-debt follow-up. Not blocking any current launch-path work. Slot in alongside the next test-touching item that already has the relevant files open.
