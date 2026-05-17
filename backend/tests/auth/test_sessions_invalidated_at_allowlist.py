"""Grep-style regression: pin every write of ``sessions_invalidated_at``.

Operator decision Q6 in
``specs/2026-05-17-backend-session-model.md`` §11: after PR 4 of the
backend-session-model series, the only sites that should write
``sessions_invalidated_at = now()`` are the FIVE global-invalidation
triggers enumerated in spec §6. The 2026-05-16 false-logout incident
class was caused by ``/auth/logout`` using this global-cutoff
mechanism for what should have been a per-session revoke; PR 4
removed the logout write and replaced it with per-``sid`` family
revocation in Redis.

This test fails if a future PR ever:

  * adds a NEW write of ``sessions_invalidated_at`` outside the
    allowlist below (the regression bug class), OR
  * removes one of the allowlisted write sites without updating this
    file (forcing the author to make an explicit decision rather
    than silently dropping the cutoff).

If a future PR genuinely needs to add a sixth global-cutoff trigger,
the fix is to update :data:`ALLOWED_WRITE_SITES` with a comment
citing the new trigger's purpose. Do NOT extend the regex to a
narrower pattern just to dodge this test — the breadth is load
bearing.
"""
from __future__ import annotations

import re
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[2] / "app"


# ── Allowlist — spec §6 trigger set ─────────────────────────────────────────
#
# Each entry is a ``(relative_path, justification)`` tuple. Paths are
# rooted at ``backend/app/``. Multiple writes inside the same file are
# allowed; the test only checks set-membership at the file granularity.
#
# Why each entry exists (see spec §6 for the canonical table):
#
#   * routers/auth.py
#       Site: ``POST /auth/reset-password``. Resetting the password
#       must kill every existing session — an attacker who held a
#       refresh JWT before the reset cannot survive past it. Pairs
#       with ``password_changed_at``.
#
#   * routers/users.py
#       Two sites: ``PUT /users/me`` (email change) and
#       ``PUT /users/me/password`` (in-app password change). Both
#       are credential-grade mutations that must invalidate every
#       JWT issued earlier.
#
#   * services/invitation_service.py
#       Two sites: accept-invitation (joins / re-joins an org and
#       must drop sessions tied to the previous membership state)
#       and role-swap inside an invitation flow (privilege boundary
#       changes between issuance and acceptance).
#
#   * services/admin_org_members_service.py
#       Site: admin deactivates a member. Must immediately kill the
#       deactivated user's outstanding sessions so they cannot keep
#       making API calls during the access-token's remaining TTL.
#
# routers/auth.py's ``POST /auth/logout`` was REMOVED from this set
# in PR 4 — per-session logout via Redis family revoke replaced it.
# That removal is the load-bearing change this regression pins.

ALLOWED_WRITE_SITES: tuple[tuple[str, str], ...] = (
    (
        "routers/auth.py",
        "password reset via token (spec §6: routers/auth.py reset_password)",
    ),
    (
        "routers/users.py",
        "email change + in-app password change (spec §6: routers/users.py)",
    ),
    (
        "services/invitation_service.py",
        "invitation accept / role swap (spec §6)",
    ),
    (
        "services/admin_org_members_service.py",
        "admin deactivates org member (spec §6)",
    ),
)


# ``\.sessions_invalidated_at\s*=`` catches every assignment shape we
# care about: ``user.sessions_invalidated_at = ...``,
# ``target.sessions_invalidated_at = utcnow_naive()``,
# ``existing.sessions_invalidated_at = now``. The leading ``\.`` rules
# out comment-only mentions, docstring references, the column
# declaration in ``models/user.py``, and the dict access in
# ``admin_users_search_service.py`` (which reads, never writes).
_WRITE_PATTERN = re.compile(r"\.sessions_invalidated_at\s*=")


def _python_files() -> list[Path]:
    """Walk ``backend/app/`` collecting every ``*.py`` file."""
    return sorted(BACKEND_APP.rglob("*.py"))


def _find_write_sites() -> set[str]:
    """Return the set of relative paths (under ``backend/app/``) that
    contain at least one write to ``sessions_invalidated_at``.

    We deliberately compare at the file level — a single file may host
    multiple write sites (``routers/users.py`` has email-change +
    password-change; ``services/invitation_service.py`` has
    accept + role-swap), and bookkeeping per-line would make the
    allowlist brittle to ordinary refactors.
    """
    found: set[str] = set()
    for path in _python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if _WRITE_PATTERN.search(line):
                # The model file declares the column; skip it.
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "Mapped[" in line or "mapped_column" in line:
                    # Column declaration in models/user.py — read-only.
                    continue
                found.add(str(path.relative_to(BACKEND_APP)))
                break
    return found


def test_sessions_invalidated_at_write_sites_match_spec_section_6():
    """Every write site lives in the spec §6 allowlist, nothing else.

    Two assertions, intentionally separate so a future failure points
    cleanly at one direction:

      * MISSING — an expected site was removed. Likely a refactor
        broke the global-cutoff contract for that trigger. Re-add
        the write OR explicitly drop the entry from the allowlist
        with a justification.
      * UNEXPECTED — a new file added a write outside the allowlist.
        Either remove the write (the per-session revoke in Redis is
        the right answer for non-credential-grade flows) OR extend
        the allowlist with a justification comment.
    """
    allowed = {site for site, _ in ALLOWED_WRITE_SITES}
    found = _find_write_sites()

    missing = allowed - found
    assert not missing, (
        f"Expected write sites in spec §6 are missing from the codebase: "
        f"{sorted(missing)}. Either the trigger was removed (in which case "
        "drop the entry from ALLOWED_WRITE_SITES with a justification) or "
        "the file moved (in which case update the path)."
    )

    unexpected = found - allowed
    assert not unexpected, (
        "Unexpected write site(s) for ``sessions_invalidated_at`` outside "
        f"the spec §6 allowlist: {sorted(unexpected)}. "
        "Per spec §5.3 + §6, only the five global-invalidation triggers "
        "may use this cutoff. Per-session revoke goes through "
        "``redis_client.session_revoke_family`` instead. If this addition "
        "is intentional, extend ALLOWED_WRITE_SITES with a justification "
        "comment citing the new trigger's purpose."
    )


def test_auth_logout_handler_no_longer_writes_cutoff():
    """The 2026-05-16 false-logout incident regression pin.

    PR 4 of the backend-session-model series removed the
    ``user.sessions_invalidated_at = ...`` write from the
    ``POST /auth/logout`` handler. The grep above already catches a
    regression at the file-set level, but this assertion is narrower:
    the logout handler body specifically must not write this column.
    A future PR that re-adds the write to ``routers/auth.py`` would
    still pass the grep above (the file is allowlisted because of
    ``reset_password``), so we read the handler body directly.
    """
    auth_path = BACKEND_APP / "routers" / "auth.py"
    text = auth_path.read_text(encoding="utf-8")

    # Slice from ``async def logout`` to the next top-level ``def`` /
    # ``async def`` / ``@router.`` decorator. Crude but effective —
    # the handler is small and self-contained.
    lines = text.splitlines()
    in_body = False
    handler_body: list[str] = []
    for idx, line in enumerate(lines):
        if not in_body:
            if line.startswith("async def logout(") or line.startswith("def logout("):
                in_body = True
                handler_body.append(line)
            continue
        # Terminate at the next decorator or top-level def/async def.
        if line.startswith("@router.") or (
            line.startswith("def ") or line.startswith("async def ")
        ):
            break
        handler_body.append(line)

    body_text = "\n".join(handler_body)
    assert (
        ".sessions_invalidated_at" not in body_text
    ), (
        "POST /auth/logout must NOT touch ``sessions_invalidated_at`` — "
        "that is the global-cutoff mechanism reserved for spec §6 "
        "triggers. Per-session logout revokes the Redis ``sid`` family "
        "via ``redis_client.session_revoke_family`` (spec §5.3)."
    )
