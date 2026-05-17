"""AST-level regression: pin every write of ``sessions_invalidated_at``.

Operator decision Q6 in
``specs/2026-05-17-backend-session-model.md`` §11: after PR 4 of the
backend-session-model series, the only sites that should write
``sessions_invalidated_at = now()`` are the global-invalidation
triggers enumerated in spec §6. The 2026-05-16 false-logout incident
class was caused by ``/auth/logout`` using this global-cutoff
mechanism for what should have been a per-session revoke; PR 4
removed the logout write and replaced it with per-``sid`` family
revocation in Redis.

This test fails if a future PR ever:

  * adds a NEW write of ``sessions_invalidated_at`` in a function
    outside the allowlist below (the regression bug class), OR
  * removes one of the allowlisted writes without updating this file
    (forcing the author to make an explicit decision rather than
    silently dropping the cutoff).

Architect feedback on PR #308: the original version of this test was
**file-level** — it asserted that writes lived in the four allowed
files, but a new write added inside one of those files would have
slipped through unnoticed. This version uses an AST walk to pin each
write at the ``(file, function)`` granularity instead. A new write
added inside ``routers/auth.py::reset_password`` is allowed; a new
write inside ``routers/auth.py::logout`` (the false-logout class) is
not. Same for every other allowlisted module.

If a future PR genuinely needs to add a new global-cutoff trigger,
the fix is to update :data:`ALLOWED_WRITE_SITES` with a comment
citing the new trigger's purpose. Do NOT broaden the AST pattern to
a narrower check just to dodge this test — the breadth is
load-bearing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[2] / "app"


# ── Allowlist — spec §6 trigger set, function-level ─────────────────────────
#
# Each entry is a ``(relative_path, function_name, justification)``
# tuple. Paths are rooted at ``backend/app/``. Multiple writes inside
# the same function are allowed (the function still counts as one
# entry); each distinct ``(file, function)`` pair must appear once.
#
# Why each entry exists (see spec §6 for the canonical table):
#
#   * routers/auth.py::reset_password
#       ``POST /auth/reset-password``. Resetting the password must
#       kill every existing session — an attacker who held a refresh
#       JWT before the reset cannot survive past it.
#
#   * routers/users.py::update_profile
#       ``PUT /users/me`` email change. Email is part of the identity
#       contract; a change must invalidate every JWT issued earlier.
#
#   * routers/users.py::change_password
#       ``PUT /users/me/password`` in-app password change. Credential-
#       grade mutation; every prior JWT dies.
#
#   * services/invitation_service.py::accept_invitation
#       Joins / re-joins an org. Drops sessions tied to the previous
#       membership state.
#
#   * services/invitation_service.py::remove_member
#       Org-member removal flow inside the invitation service. The
#       removed user's outstanding sessions must die at the moment
#       of removal so they cannot keep making API calls during the
#       access-token's remaining TTL.
#
#   * services/admin_org_members_service.py::update_member
#       Admin deactivates a member. Same reasoning as the invitation-
#       service removal above; the admin path is a separate function.
#
# ``routers/auth.py::logout`` was REMOVED from this set in PR 4 —
# per-session logout via Redis family revoke replaced it. That
# removal is the load-bearing change this regression pins.

ALLOWED_WRITE_SITES: tuple[tuple[str, str, str], ...] = (
    (
        "routers/auth.py",
        "reset_password",
        "password reset via token (spec §6 trigger 1)",
    ),
    (
        "routers/users.py",
        "update_profile",
        "email change (spec §6 trigger 3)",
    ),
    (
        "routers/users.py",
        "change_password",
        "in-app password change (spec §6 trigger 2)",
    ),
    (
        "services/invitation_service.py",
        "accept_invitation",
        "invitation accept (spec §6 trigger 4a)",
    ),
    (
        "services/invitation_service.py",
        "remove_member",
        "invitation flow member removal (spec §6 trigger 4b)",
    ),
    (
        "services/admin_org_members_service.py",
        "update_member",
        "admin deactivates org member (spec §6 trigger 5)",
    ),
)


@dataclass(frozen=True)
class WriteSite:
    """One ``obj.sessions_invalidated_at = ...`` write found in the
    source tree.

    ``file`` is relative to ``backend/app/``. ``function`` is the name
    of the innermost enclosing ``def`` / ``async def``; ``__module__``
    for top-level writes (we don't expect any but the placeholder
    keeps the comparison total). ``lineno`` is the assignment's start
    line in the source file — purely informational, not used in
    set-equality.
    """

    file: str
    function: str
    lineno: int


def _enclosing_function(parents: list[ast.AST]) -> str:
    """Return the name of the innermost enclosing function in
    ``parents`` (deepest-first traversal stack), or ``"__module__"`` if
    none. Class definitions are skipped — a write inside a method of
    ``class X: def f(self): self.sessions_invalidated_at = ...``
    reports ``f``, not ``X``."""
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _find_write_sites() -> list[WriteSite]:
    """Walk every ``.py`` file under ``backend/app/`` and collect every
    ``Attribute`` assignment whose attribute name is
    ``sessions_invalidated_at``.

    Only ``ast.Assign`` is matched — ``ast.AnnAssign`` (the
    ``mapped_column`` declaration in ``models/user.py``) and
    ``ast.AugAssign`` (`+=` etc., which we never use here) are
    excluded by construction. Attribute-target match catches
    ``user.sessions_invalidated_at = ...``,
    ``target.sessions_invalidated_at = utcnow_naive()``, and the like.
    Comparisons and other read-only uses are not ``Assign`` nodes and
    are correctly ignored.
    """
    sites: list[WriteSite] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue

        # Depth-first walk with an explicit parent stack so the
        # enclosing function is unambiguous at every Assign node.
        rel_path = str(path.relative_to(BACKEND_APP))

        def visit(node: ast.AST, parents: list[ast.AST]) -> None:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    # ``a, b = ...`` lands a Tuple; unwrap.
                    candidates: list[ast.expr] = (
                        list(target.elts)
                        if isinstance(target, (ast.Tuple, ast.List))
                        else [target]
                    )
                    for candidate in candidates:
                        if (
                            isinstance(candidate, ast.Attribute)
                            and candidate.attr == "sessions_invalidated_at"
                        ):
                            sites.append(
                                WriteSite(
                                    file=rel_path,
                                    function=_enclosing_function(parents),
                                    lineno=node.lineno,
                                )
                            )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + [node])

        visit(tree, [])
    return sites


def test_sessions_invalidated_at_writes_match_spec_section_6():
    """Every write to ``sessions_invalidated_at`` is in spec §6's
    function-level allowlist, and every allowlisted function still
    contains the write.

    Two assertions intentionally separate so a future failure points
    cleanly at one direction:

      * MISSING — an expected ``(file, function)`` pair was removed.
        Likely a refactor broke the global-cutoff contract for that
        trigger. Re-add the write OR explicitly drop the entry from
        the allowlist with a justification.
      * UNEXPECTED — a new write appeared in a function outside the
        allowlist. The 2026-05-16 false-logout incident class. Either
        remove the write (the per-session revoke in
        ``redis_client.session_revoke_family`` is the right answer
        for non-credential-grade flows) OR extend the allowlist with
        a justification comment.
    """
    expected: set[tuple[str, str]] = {
        (rel_path, fn) for rel_path, fn, _ in ALLOWED_WRITE_SITES
    }
    found_sites = _find_write_sites()
    found: set[tuple[str, str]] = {(s.file, s.function) for s in found_sites}

    missing = expected - found
    assert not missing, (
        "Expected (file, function) write sites in spec §6 are missing "
        f"from the codebase: {sorted(missing)}. Either the trigger was "
        "removed (in which case drop the entry from "
        "ALLOWED_WRITE_SITES with a justification) or the function "
        "was renamed (in which case update the allowlist)."
    )

    unexpected = found - expected
    if unexpected:
        # Surface the actual file:line for each offender so the
        # operator can locate them without grepping.
        details = "\n".join(
            f"  - {s.file}:{s.lineno} inside {s.function}"
            for s in found_sites
            if (s.file, s.function) in unexpected
        )
        raise AssertionError(
            "Unexpected ``sessions_invalidated_at`` write(s) outside the "
            "spec §6 allowlist:\n"
            f"{details}\n"
            "Per spec §5.3 + §6, only the enumerated global-invalidation "
            "triggers may use this cutoff. Per-session revoke goes through "
            "``redis_client.session_revoke_family`` instead. If this "
            "addition is intentional, extend ALLOWED_WRITE_SITES with a "
            "justification comment citing the new trigger's purpose."
        )


def test_auth_logout_handler_no_longer_writes_cutoff():
    """The 2026-05-16 false-logout incident regression pin.

    PR 4 of the backend-session-model series removed the
    ``user.sessions_invalidated_at = ...`` write from the
    ``POST /auth/logout`` handler. The AST-level grep above already
    catches a regression at the function-set level (any future write
    inside ``routers/auth.py::logout`` would appear as an unexpected
    site), but this narrower assertion is kept as belt-and-braces:
    a brand-new helper function inside ``routers/auth.py`` that
    happens to be called from ``logout`` would not be caught by the
    function-name allowlist alone, and we want the logout PATH to
    stay clean regardless of which function in the file does the
    write.
    """
    auth_path = BACKEND_APP / "routers" / "auth.py"
    source = auth_path.read_text(encoding="utf-8")

    # Slice the handler body from ``async def logout(`` to the next
    # top-level ``def`` / ``async def`` / ``@router.`` decorator.
    lines = source.splitlines()
    in_body = False
    handler_body: list[str] = []
    for line in lines:
        if not in_body:
            if line.startswith("async def logout(") or line.startswith("def logout("):
                in_body = True
                handler_body.append(line)
            continue
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
