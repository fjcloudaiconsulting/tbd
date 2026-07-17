"""AST-level regression: forbid raw ``request.client`` outside the one
sanctioned resolver.

The client IP for audit logging and rate limiting must be resolved via
``app.rate_limit.get_client_ip(request)`` — never read straight off
``request.client``. The raw peer is our own ingress (nginx in dev, DO App
Platform in prod), so a raw read stamps audit rows with the proxy's
address instead of the real client. See
``reference_audit_client_ip_single_helper`` and PR #550, where exactly
this drift shipped on the ``ai_enhanced`` scenario-simulate path: one
argument fed four audit writes, on a path with no test coverage, and it
went unnoticed until an audit-IP sweep found it.

The follow-up PR #551 made ``run_ai_simulation``'s audit context
keyword-required, but that only catches an *omitted* argument — it cannot
catch a caller passing the *wrong* value, which is what #550 actually
was. This guard closes that gap at the source level: a new
``request.client`` read anywhere but ``get_client_ip`` fails the suite.

Why an AST walk rather than a ``grep`` in CI: ``rate_limit.py``'s own
docstring and ``middleware/request_context.py``'s comment both mention
``request.client`` in prose. A text scan would flag those (or need
fragile per-line excludes); the AST only sees real attribute-access
nodes, so prose is ignored for free. It also pins each read at
``(file, function)`` granularity and runs in the existing pytest shards
— no separate CI job, and developers hit it locally before pushing.

Modelled on ``tests/auth/test_sessions_invalidated_at_allowlist.py``,
the established backend pattern for this class of source guard.

If a future change genuinely needs a new raw read (it almost certainly
should call ``get_client_ip`` instead), add a ``(file, function)`` entry
to :data:`ALLOWED_CLIENT_READS` with a justification. Do NOT narrow the
detection pattern to dodge the check — the breadth is load-bearing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


# Request-like identifiers whose ``.client`` read is what we forbid.
# FastAPI/Starlette hand handlers a ``Request`` as ``request`` (or the
# terse ``req``) and a websocket as ``websocket`` / ``ws``. A ``.client``
# read off any of them is the raw-peer access get_client_ip exists to
# replace.
REQUEST_IDENTIFIERS: frozenset[str] = frozenset(
    {"request", "req", "websocket", "ws"}
)


# ── Allowlist — the ONE sanctioned raw read ─────────────────────────────
#
# Each entry is ``(relative_path, function_name, justification)`` rooted
# at ``backend/app/``.
#
#   * rate_limit.py::get_client_ip
#       The resolver itself. It reads ``request.client`` once to get the
#       direct TCP peer, then applies the trust-boundary logic (DO
#       runtime header, right-to-left XFF walk over trusted proxies).
#       This is the single place the raw peer is allowed to be touched;
#       every other site must call this function.
ALLOWED_CLIENT_READS: tuple[tuple[str, str, str], ...] = (
    (
        "rate_limit.py",
        "get_client_ip",
        "the sanctioned resolver; reads the raw peer once, then applies "
        "the trust-boundary logic every other caller depends on",
    ),
)


@dataclass(frozen=True)
class ClientRead:
    """One ``<request-like>.client`` read found in the source tree.

    ``file`` is relative to ``backend/app/``. ``function`` is the name of
    the innermost enclosing ``def`` / ``async def`` (``"__module__"`` for
    a top-level read, none expected). ``lineno`` is informational and not
    used in set-equality.
    """

    file: str
    function: str
    lineno: int


def _enclosing_function(parents: list[ast.AST]) -> str:
    """Return the innermost enclosing function name in ``parents``
    (deepest-first stack), or ``"__module__"`` if none. Class bodies are
    skipped so a read inside a method reports the method, not the class.
    """
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _find_client_reads() -> list[ClientRead]:
    """Walk every ``.py`` under ``backend/app/`` and collect each
    ``Attribute`` read of ``.client`` on a request-like ``Name``.

    Matches ``request.client``, ``request.client.host`` (the ``.client``
    node is what we catch; ``.host`` hangs off it), and ``req.client``.
    Docstrings and comments that merely mention ``request.client`` are
    string/comment tokens, not ``Attribute`` nodes, so they never match.
    """
    reads: list[ClientRead] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue

        rel_path = str(path.relative_to(BACKEND_APP))

        def visit(node: ast.AST, parents: list[ast.AST]) -> None:
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "client"
                and isinstance(node.value, ast.Name)
                and node.value.id in REQUEST_IDENTIFIERS
            ):
                reads.append(
                    ClientRead(
                        file=rel_path,
                        function=_enclosing_function(parents),
                        lineno=node.lineno,
                    )
                )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + [node])

        visit(tree, [])
    return reads


def test_no_raw_request_client_outside_get_client_ip():
    """Every raw ``request.client`` read lives in the allowlist, and every
    allowlisted function still contains one.

    Two assertions, kept separate so a failure points cleanly at one
    direction:

      * UNEXPECTED — a new raw read appeared outside the allowlist. This
        is the #550 audit-IP drift class. Call
        ``app.rate_limit.get_client_ip(request)`` instead. If you truly
        need a new raw read, extend :data:`ALLOWED_CLIENT_READS` with a
        justification.
      * MISSING — an allowlisted read was removed (e.g. ``get_client_ip``
        was refactored). Drop the stale entry so the allowlist keeps
        matching reality.
    """
    expected: set[tuple[str, str]] = {
        (rel_path, fn) for rel_path, fn, _ in ALLOWED_CLIENT_READS
    }
    found_sites = _find_client_reads()
    found: set[tuple[str, str]] = {(s.file, s.function) for s in found_sites}

    unexpected = found - expected
    assert not unexpected, (
        "Raw `request.client` read(s) found outside the allowlist — resolve "
        "the client IP via app.rate_limit.get_client_ip(request) instead "
        "(see PR #550 / reference_audit_client_ip_single_helper):\n"
        + "\n".join(
            f"  - {s.file}::{s.function} (line {s.lineno})"
            for s in sorted(
                (s for s in found_sites if (s.file, s.function) not in expected),
                key=lambda s: (s.file, s.lineno),
            )
        )
    )

    missing = expected - found
    assert not missing, (
        "Allowlisted `request.client` read(s) no longer present — remove the "
        "stale entry from ALLOWED_CLIENT_READS:\n"
        + "\n".join(f"  - {file}::{fn}" for file, fn in sorted(missing))
    )
