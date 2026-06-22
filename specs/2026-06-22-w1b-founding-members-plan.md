# W1b — Founding Members v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. TDD, frequent commits.

**Goal:** Ship the founding-members offer: founder flag (grandfathering existing users), activity tracking (track-now-enforce-later), a live public counter, and landing copy that reads stable-but-free.

**Architecture:** Backend migration adds `is_founder` + `last_active_at` to `users`. Registration flags founders. A throttled, independent-session stamp records activity. A hardened **public, count-only** endpoint feeds an apex client island. Referral discount + inactivity-revoke are deferred to the payments wave.

**Tech Stack:** FastAPI / SQLAlchemy async / Alembic / Pydantic v2; Next 16 / React 19. Redis via `app/redis_client.py`; slowapi `limiter`; public-router template `app/routers/security.py`.

## Global Constraints
- Alembic head is `8e83c1dbe51b`; new migration `066_founder_fields` chains off it. Migration revision-id convention: `revision = "066_founder_fields"`, `down_revision = "8e83c1dbe51b"`.
- `is_founder` server_default `"1"` ⇒ existing rows grandfathered as founders (operator decision).
- Soft cap: NO gating — every registration is a founder during the window.
- Counter is **count-only** + public (apex is a static export; any token would leak in the bundle). Excludes a config username list (default `pfv_smoke_l05`).
- Backend tests run in an isolated compose project: `docker compose -p team-w1b ...` (per CLAUDE.md — never the default stack).
- No em-dashes in customer copy (`feedback_no_em_dashes`); No-Off-Token for any UI color.
- Hero offer line (operator-locked): **"Join as a founding member — free for life."** with the counter appended (`· N founding members so far`). NOTE: the apostrophe/dash policy test (`no-em-dash-in-customer-copy`) — use an en/em-dash-free phrasing; the existing Hero uses `&middot;`-style separators. Verify the copy passes `tests/voice/no-em-dash-in-customer-copy.test.ts` (use a regular hyphen "-" not "—").
- Branch `feat/founding-members-v1` (already created off main).

---

### Task 1: Migration + model columns + UserResponse + registration flag

**Files:**
- Create: `backend/alembic/versions/066_founder_fields.py`
- Modify: `backend/app/models/user.py` (User: add 2 columns after `is_active`/timestamps)
- Modify: `backend/app/schemas/auth.py` (`UserResponse`: add `is_founder: bool = True`)
- Modify: `backend/app/routers/auth.py` (`_user_response` ~line 99 add `is_founder=user.is_founder`; password-register create ~line 304 add `is_founder=True`; Google-SSO create ~line 2651 add `is_founder=True`)
- Test: `backend/tests/routers/test_auth.py` (register response carries `is_founder=True`)

**Interfaces:**
- Produces: `User.is_founder: bool`, `User.last_active_at: datetime|None`; `UserResponse.is_founder: bool`.

- [ ] **Step 1: Write failing test** — extend an existing register test (or add one) asserting the register/verify response includes `is_founder == True`. Read `test_auth.py` for its register helper + client fixture first.

- [ ] **Step 2: Run (isolated stack), verify fail**

```bash
docker compose -p team-w1b up -d backend mysql redis
docker compose -p team-w1b exec -T backend pytest tests/routers/test_auth.py -k "founder or register" -q
```
Expected: FAIL (`is_founder` not present / column missing).

- [ ] **Step 3: Implement**

`models/user.py` — after `is_active` (line 70) add:
```python
    # Founding-members program (2026-06-22). True for every user created
    # during the founder window; server_default "1" grandfathers all
    # existing rows (the pre-launch testers are the most-founding members).
    # Soft cap (1000 is a marketing number) — no gating at registration.
    is_founder: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
```
after `onboarded_at` (line 102) add:
```python
    # Last authenticated activity, stamped (throttled) by get_current_user.
    # Tracked now; the "lose founder status after 30 days idle" rule ships
    # with payments. NULL until first stamped.
    last_active_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```
`066_founder_fields.py`:
```python
"""Founding-members: add users.is_founder + users.last_active_at.

Revision ID: 066_founder_fields
Revises: 8e83c1dbe51b
Create Date: 2026-06-22

is_founder server_default "1" grandfathers every existing user as a
founding member. last_active_at is NULL until first stamped. Spec:
specs/2026-06-22-w1-quick-wins-design.md (W1b).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "066_founder_fields"
down_revision = "8e83c1dbe51b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_founder", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "users",
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "last_active_at")
    op.drop_column("users", "is_founder")
```
`schemas/auth.py` `UserResponse` — add after `is_active` (line 65):
```python
    # Founding-members program. True for users in the founder window
    # (default True so existing tokens/responses stay valid).
    is_founder: bool = True
```
`routers/auth.py` — `_user_response` (after `is_active=user.is_active,`): `is_founder=user.is_founder,`. Both `User(...)` creates (password register + Google SSO): add `is_founder=True,` next to `is_superadmin=...`.

- [ ] **Step 4: migrate + run test on isolated stack, verify pass**
```bash
docker compose -p team-w1b exec -T backend alembic upgrade head
docker compose -p team-w1b exec -T backend pytest tests/routers/test_auth.py -k "founder or register" -q
```
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(founders): is_founder + last_active_at columns, register flag, UserResponse`

---

### Task 2: Throttled activity stamp (independent session)

**Files:**
- Create: `backend/app/services/user_activity_service.py`
- Modify: `backend/app/deps.py` (`get_current_user`: call the stamp after context bind)
- Modify: `backend/app/config.py` (`last_active_stamp_throttle_seconds: int = 3600`)
- Test: `backend/tests/services/test_user_activity_service.py`

**Interfaces:**
- Produces: `async def maybe_stamp_last_active(session_factory, user_id: int, current: datetime | None) -> None`.

- [ ] **Step 1: Write failing tests** — `maybe_stamp_last_active`: (a) `current=None` → issues an UPDATE (use a fake/independent in-memory session or assert via a real isolated DB that `last_active_at` becomes non-null); (b) `current` = now → no write; (c) `current` = 2h ago with threshold 3600 → writes; (d) a raised DB error is swallowed (no exception propagates).

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

`config.py` (near other ints): `last_active_stamp_throttle_seconds: int = 3600`.
`user_activity_service.py`:
```python
"""Throttled per-user activity stamp for the founding-members program.

Writes ``users.last_active_at`` at most once per throttle window, on an
INDEPENDENT session (mirrors record_audit_event) so the request's own
transaction is never touched and an auth request can't be broken by a
stamp failure. Tracked now; the inactivity-revoke rule ships later.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.user import User

logger = structlog.stdlib.get_logger(__name__)


async def maybe_stamp_last_active(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    current: datetime | None,
) -> None:
    now = datetime.now(timezone.utc)
    if current is not None:
        cur = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        if (now - cur).total_seconds() < settings.last_active_stamp_throttle_seconds:
            return
    try:
        async with session_factory() as s:
            await s.execute(
                update(User).where(User.id == user_id).values(last_active_at=now)
            )
            await s.commit()
    except Exception:  # noqa: BLE001 — never break auth on a stamp failure
        logger.warning("user_activity.stamp_failed", user_id=user_id)
```
`deps.py` `get_current_user` — after the `bind_contextvars(...)` block, before `return user`:
```python
    # Founding-members activity stamp (throttled, independent session so
    # the request transaction is untouched and a failure never breaks auth).
    await maybe_stamp_last_active(async_session, user.id, user.last_active_at)
```
Add `from app.services.user_activity_service import maybe_stamp_last_active` (import inside the function if a circular import appears at module load — services import models; deps already imports User, so a top-level import should be fine; fall back to local import if needed).

- [ ] **Step 4: Run, verify pass** (isolated stack).
- [ ] **Step 5: Commit** — `feat(founders): throttled last_active_at stamp in get_current_user`

---

### Task 3: Public founder-count endpoint + cache + CORS

**Files:**
- Create: `backend/app/routers/public_stats.py`
- Modify: `backend/app/redis_client.py` (add `founder_count_cache_get/set` helpers)
- Modify: `backend/app/config.py` (`founder_count_exclude_usernames: str = "pfv_smoke_l05"` + `founder_count_exclude_list` property)
- Modify: `backend/app/main.py` (import + `app.include_router(public_stats.router)`)
- Modify: `.do/app.yaml` (BACKEND_CORS_ORIGINS line 104 → add apex origins)
- Test: `backend/tests/routers/test_public_stats.py`

**Interfaces:**
- Produces: `GET /api/v1/public/founder-count` → `{"count": int}`. No auth.

- [ ] **Step 1: Write failing tests** — seed N founders (incl. one named `pfv_smoke_l05` and one inactive), GET the endpoint: `count` excludes the smoke user AND the inactive user; endpoint is reachable without auth; never 500 when Redis is absent (dev). Read `test_public_stats`-style siblings / an existing public test for the client fixture.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

`config.py`:
```python
    founder_count_exclude_usernames: str = "pfv_smoke_l05"
```
and a property:
```python
    @property
    def founder_count_exclude_list(self) -> list[str]:
        return [u.strip() for u in self.founder_count_exclude_usernames.split(",") if u.strip()]
```
`redis_client.py` (mirror the mfa nonce helpers; fail-open, 5-min TTL):
```python
_FOUNDER_COUNT_KEY = "public:founder_count"
_FOUNDER_COUNT_TTL_S = 300

@_normalize_transport_errors
async def founder_count_cache_get() -> int | None:
    client = get_client()
    if client is None:
        return None
    raw = await client.get(_FOUNDER_COUNT_KEY)
    return int(raw) if raw is not None else None

@_normalize_transport_errors
async def founder_count_cache_set(n: int) -> None:
    client = get_client()
    if client is None:
        return
    await client.set(_FOUNDER_COUNT_KEY, str(n), ex=_FOUNDER_COUNT_TTL_S)
```
`public_stats.py` (template = `security.py` — public, rate-limited, never-500):
```python
"""Public, count-only stats for the marketing/apex site.

The apex landing is a static export (no server runtime) so it fetches
this cross-origin from the browser. A bearer token would be exposed in
the public bundle, so this endpoint is intentionally PUBLIC and returns
only a single non-sensitive integer — the founding-members count the
landing page advertises. Cached (Redis, 5 min) + rate-limited; excludes
the configured non-real usernames (e.g. the smoke-test account).
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter
from app import redis_client
from fastapi import Depends

logger = structlog.stdlib.get_logger(__name__)
router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.get("/founder-count")
@limiter.limit("60/minute")
async def founder_count(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    try:
        cached = await redis_client.founder_count_cache_get()
    except Exception:  # noqa: BLE001 — cache is best-effort
        cached = None
    if cached is not None:
        return {"count": cached}

    stmt = select(func.count()).select_from(User).where(
        User.is_founder.is_(True), User.is_active.is_(True)
    )
    excluded = settings.founder_count_exclude_list
    if excluded:
        stmt = stmt.where(User.username.notin_(excluded))
    count = int(await db.scalar(stmt) or 0)

    try:
        await redis_client.founder_count_cache_set(count)
    except Exception:  # noqa: BLE001
        pass
    return {"count": count}
```
`main.py`: add `public_stats` to the `from app.routers import ...` line and `app.include_router(public_stats.router)` near the other includes.
`.do/app.yaml` line 104 value → `"https://app.thebetterdecision.com,https://thebetterdecision.com,https://www.thebetterdecision.com"`.

- [ ] **Step 4: Run, verify pass** (isolated stack). Confirm the limiter decorator's `request: Request` arg is present (slowapi requires it).
- [ ] **Step 5: Commit** — `feat(founders): public founder-count endpoint + redis cache + apex CORS`

---

### Task 4: Landing copy + apex counter island

**Files:**
- Create: `frontend/components/landing/FounderCount.tsx` (client island)
- Modify: `frontend/components/landing/Hero.tsx` (line 47-49 copy → offer + `<FounderCount />`)
- Modify: `frontend/lib/comparison.ts:158` (`"Free while in beta"` → `"Free while we grow"`)
- Modify: `frontend/components/landing/VsPageLayout.tsx:133` (`"Free while in beta."` → `"Free while we grow."`)
- Modify: `frontend/tests/comparison-data.test.ts:24` (expected string)
- Test: `frontend/tests/components/landing/FounderCount.test.tsx`

- [ ] **Step 1: Write failing tests** — (a) `comparison-data.test.ts` expects `"Free while we grow"`; (b) FounderCount renders nothing initially, then renders `· {n} founding members so far` after the fetch resolves with `{count: 142}`; renders nothing on fetch error (no fake number). Mock global `fetch`.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement**

`FounderCount.tsx`:
```tsx
"use client";

import { useEffect, useState } from "react";
import { isApexBuild } from "@/lib/analytics";
import { BRAND_APP_URL } from "@/lib/links";

// Live founding-members counter. Count-only public endpoint; on the apex
// static host it fetches cross-origin from the app API, in the SSR app it
// is same-origin. Renders nothing until a real number arrives (no baked
// fallback — we never show a number we can't stand behind).
export default function FounderCount() {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    const base = isApexBuild ? BRAND_APP_URL : "";
    let alive = true;
    fetch(`${base}/api/v1/public/founder-count`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d && typeof d.count === "number") setCount(d.count);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  if (count === null || count <= 0) return null;
  return (
    <>
      {" "}
      <span aria-hidden className="text-text-muted/60">&middot;</span>{" "}
      <span>{count.toLocaleString()} founding members so far</span>
    </>
  );
}
```
`Hero.tsx` line 44-49 → replace the "Free while in beta." block with:
```tsx
          <p className="mt-3 text-sm text-text-muted">
            Join as a founding member - free for life.
            <FounderCount />
          </p>
```
and `import FounderCount from "./FounderCount";` at the top. (Regular hyphen, not an em-dash — passes the voice test.)
`comparison.ts:158` → `value: "Free while we grow"`. `VsPageLayout.tsx:133` → `"... Free while we grow."`. `comparison-data.test.ts:24` → expect `"Free while we grow"`.

- [ ] **Step 4: Run, verify pass** — the two test files + the voice test:
```bash
docker compose exec -T frontend npx vitest run tests/comparison-data.test.ts tests/components/landing/FounderCount.test.tsx tests/voice/no-em-dash-in-customer-copy.test.ts
```
- [ ] **Step 5: Commit** — `feat(founders): founding-member landing copy + live counter island`

---

### Task 5: Full verification + apex build + PR

- [ ] **Backend** (isolated stack): `docker compose -p team-w1b exec -T backend pytest -q` → all green. Tear down: `docker compose -p team-w1b down -v`.
- [ ] **Frontend**: `docker compose exec -T frontend npx vitest run` (full suite) + `npx tsc --noEmit` + `npx eslint .` → clean.
- [ ] **Apex build composition**: the apex static export is covered by `build-apex.test.ts` + CI (cannot run in the dev container — `next.config.ts` bind-mount). Confirm `FounderCount` (a client component) is included by the apex build and that the analytics deploy-paths filter / apex `paths:` covers `components/landing/**` (the #466 lesson). The new component lives under `components/landing/`, already in the apex surface.
- [ ] **Push + PR** — title: `feat(founders): founding-members v1 — flag, activity, public counter, landing copy`. Body: what/why, the grandfather + soft-cap + track-now decisions, the count-only-public rationale, CORS change, and the **operator follow-ups**: (1) confirm apex CORS reaches prod + counter loads live; (2) **review the live Google Ads copy** for consistency with the new founders messaging (wave-level final task).

## Self-Review (plan-write time)
- **Coverage:** copy (T4), is_founder + grandfather (T1), last_active track (T2), public counter + CORS + exclude-list (T3), grandfather via server_default (T1), both user-creation sites flagged (T1), apex delivery (T4), ads-copy follow-up (T5 PR body). All W1b spec points covered.
- **Placeholders:** none — code shown for every create/modify; test steps name the exact file + assertion and say "read sibling first" only where a fixture/client harness must be matched.
- **Type consistency:** `is_founder: bool`, `last_active_at: datetime|None`, `maybe_stamp_last_active(session_factory, user_id, current)`, `{count:int}` used consistently.
- **Deviations from spec:** no build-time fallback number for the counter (renders only a real count) — avoids shipping a stale/fake number; the offer line stands alone without it. last_active stamped via independent session (not the request session) — safer than the spec's "in get_current_user" literal reading, same effect.
