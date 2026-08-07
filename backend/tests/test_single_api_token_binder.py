"""AST-level regression: ``api_token_id`` is bound in exactly ONE place.

``audit_events.api_token_id`` (TBD-188) is resolved ambiently — every audit
call site reads it out of the request-scoped structlog context rather than
being handed it. That answer is only true while the context has a single
writer. A second ``bind_contextvars(api_token_id=...)`` anywhere under
``backend/app/`` silently converts an audited fact into a race between two
binders, and the losing one is invisible: the row still gets *a* token id, so
nothing goes red and the value is simply wrong.

The specific failure this exists to catch is the one a reviewer would ask for
by name: "just bind it in ``mint_token`` too, the row is about a token." That
would stamp the SUBJECT token of an interactive-session action into the ACTOR
column, permanently merging two different facts (see
``app/models/audit_event.py`` design note 3).

Why an AST walk and not a grep: ``pat.py``'s bind site carries a long comment
explaining that the bind's *position* is load-bearing, and that comment names
``api_token_id`` and ``bind_contextvars`` in prose several times. A text scan
would count those. The AST only sees real ``Call`` nodes, so prose is ignored
for free.

⚠ **These two tests pass against pre-TBD-188 code.** Verified by AST against
``origin/main``: it already had exactly one ``bind_contextvars(api_token_id=
...)``, inside ``authenticate_pat`` — the composite bind at the end of the
function that TBD-188 split out and moved up. So a green here is NOT evidence
that the feature works; it is an invariant guard on the *number* of binders,
and it says nothing about the surviving one's position or value. The
behavioural evidence lives entirely in
``tests/auth/test_audit_api_token_attribution.py``.

Modelled on ``tests/test_no_raw_request_client.py``, the established backend
source-guard pattern. If a future change genuinely needs a second binder (it
almost certainly wants to move the existing one instead), add a
``(file, function)`` entry to :data:`ALLOWED_BINDERS` with a justification —
and re-read ``pat.py``'s bind-site comment first, because a second binder is
also what makes the first one's position stop mattering.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


# ── Allowlist — the ONE sanctioned binder ───────────────────────────────
#
# Each entry is ``(relative_path, function_name, justification)`` rooted at
# ``backend/app/``.
ALLOWED_BINDERS: tuple[tuple[str, str, str], ...] = (
    (
        "auth/pat.py",
        "authenticate_pat",
        "the sole writer; bound immediately after the token row resolves so "
        "the api_token.auth_rejected branches below it are attributed too "
        "(TBD-188 §3 — the bind's line position IS the fix)",
    ),
)


@dataclass(frozen=True)
class Bind:
    """One ``bind_contextvars(..., api_token_id=..., ...)`` call site."""

    file: str
    function: str
    lineno: int


def _enclosing_function(parents: list[ast.AST]) -> str:
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _is_bind_contextvars(func: ast.expr) -> bool:
    """Match ``bind_contextvars(...)`` however it was imported.

    Covers the fully-qualified ``structlog.contextvars.bind_contextvars``
    used in this tree today AND a bare ``bind_contextvars`` from a
    ``from structlog.contextvars import bind_contextvars``, so dodging the
    guard by changing the import style does not work.
    """
    if isinstance(func, ast.Attribute):
        return func.attr == "bind_contextvars"
    return isinstance(func, ast.Name) and func.id == "bind_contextvars"


def _find_binds() -> list[Bind]:
    binds: list[Bind] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue

        rel_path = path.relative_to(BACKEND_APP).as_posix()

        def visit(node: ast.AST, parents: list[ast.AST]) -> None:
            if (
                isinstance(node, ast.Call)
                and _is_bind_contextvars(node.func)
                and any(kw.arg == "api_token_id" for kw in node.keywords)
            ):
                binds.append(
                    Bind(
                        file=rel_path,
                        function=_enclosing_function(parents),
                        lineno=node.lineno,
                    )
                )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + [node])

        visit(tree, [])
    return binds


def test_api_token_id_has_exactly_one_binder():
    """Every ``api_token_id`` context bind is allowlisted, and each
    allowlisted binder still exists.

    Two assertions, kept separate so a failure points at one direction:

      * UNEXPECTED — a second binder appeared. The ambient read in
        ``audit_service`` now has competing writers; almost certainly you
        want to move the existing bind rather than add one.
      * MISSING — the sanctioned binder is gone, so every audit row is
        silently NULL. (``tests/auth/test_audit_api_token_attribution.py``
        catches that behaviourally; this catches it structurally.)
    """
    expected: set[tuple[str, str]] = {
        (rel_path, fn) for rel_path, fn, _ in ALLOWED_BINDERS
    }
    found_sites = _find_binds()
    found: set[tuple[str, str]] = {(b.file, b.function) for b in found_sites}

    unexpected = found - expected
    assert not unexpected, (
        "A second `bind_contextvars(api_token_id=...)` appeared. "
        "audit_events.api_token_id is resolved ambiently and must have "
        "exactly one writer (TBD-188 §3):\n"
        + "\n".join(
            f"  - {b.file}::{b.function} (line {b.lineno})"
            for b in sorted(
                (b for b in found_sites if (b.file, b.function) not in expected),
                key=lambda b: (b.file, b.lineno),
            )
        )
    )

    missing = expected - found
    assert not missing, (
        "The sanctioned `api_token_id` binder is gone — every audit row will "
        "record a NULL acting token:\n"
        + "\n".join(f"  - {file}::{fn}" for file, fn in sorted(missing))
    )


def test_the_single_binder_is_counted_once():
    """Exactly one call site, not merely "all sites are allowlisted".

    The set-equality check above collapses duplicates: two binds in the same
    function would compare equal to one. That is precisely the shape the
    composite-bind regression takes (``pat.py`` binding ``api_token_id``
    both before the rejection branches AND again at the end), and it is the
    shape that makes the first bind's position stop mattering.
    """
    binds = _find_binds()
    assert len(binds) == 1, (
        "expected exactly one `bind_contextvars(api_token_id=...)` call in "
        "backend/app/, found "
        + str(len(binds))
        + ":\n"
        + "\n".join(f"  - {b.file}::{b.function} (line {b.lineno})" for b in binds)
    )
