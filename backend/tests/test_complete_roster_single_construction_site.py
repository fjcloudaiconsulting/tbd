"""AST-level regression: ``CompleteRoster`` has ONE construction site.

TBD-234a, spec ``specs/2026-07-29-billing-period-roster-design.md`` §2.2
(the completeness precondition) and §4a test 14.

``find_period_anomalies`` is pure and quantifies over the WHOLE roster:
contiguity, non-overlap, exactly-one-open, straddling and lapsed are all
properties of a complete roster, and a windowed sample of a roster is not
a roster and does not carry them. A pure kernel can never verify
completeness itself — completeness is a claim about rows that are NOT in
the list, so purity and self-verification are mutually exclusive and no
in-kernel length check or invariant guard can work.

The precondition is therefore enforced at CONSTRUCTION:
``billing_service.load_complete_roster`` is the only place in
``backend/app/`` allowed to build a ``CompleteRoster``, and it issues the
unbounded ``SELECT`` with no LIMIT, no date predicate and no branches. A
windowed ``list[BillingPeriod]`` cannot reach the kernel without someone
first building a ``CompleteRoster`` out of it, and this test is what makes
that visible.

⚠ **This guard is the precondition's ONLY mechanism.** Round 4's finding
F3 struck the "type error" framing revision 4 relied on: this repository
has no type checker and never has (the backend CI job runs ``pytest
--splits 4`` plus ``python -m compileall backend/app``; there is no mypy,
pyright, ``pyproject.toml``, ``mypy.ini`` or pre-commit config anywhere).
A ``@dataclass(frozen=True)`` has a public ``__init__``, so both
``CompleteRoster(org_id=1, rows=tuple(windowed))`` and
``dataclasses.replace(roster, rows=windowed)`` succeed at runtime with
nothing objecting. Only this test objects.

⚠ **The scan is source-scoped to ``backend/app/``, so tests are exempt by
construction.** That is deliberate, not an oversight: 234a's own kernel
fixtures hand-build rosters, and they are legal precisely because they are
not production code.

Modelled on ``tests/test_no_raw_request_client.py`` and
``tests/auth/test_sessions_invalidated_at_allowlist.py``, the two
established backend source guards (the #552 pattern).

If a future change genuinely needs a second construction site, add a
``(file, function)`` entry to :data:`ALLOWED_CONSTRUCTION_SITES` with a
justification that explains how the new site guarantees completeness. Do
NOT narrow the detection pattern to dodge the check — the breadth is
load-bearing.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[1] / "app"


# ── Allowlist — the ONE sanctioned construction site ────────────────────
#
# Each entry is ``(relative_path, function_name, justification)`` rooted
# at ``backend/app/``.
#
#   * services/billing_service.py::load_complete_roster
#       The only constructor. Issues `SELECT id, start_date, end_date
#       WHERE org_id = ? ORDER BY start_date` with no LIMIT, no date
#       predicate and no branches, so the rows it wraps are, by
#       construction, every row the org has.
ALLOWED_CONSTRUCTION_SITES: tuple[tuple[str, str, str], ...] = (
    (
        "services/billing_service.py",
        "load_complete_roster",
        "the only constructor; its unbounded, unbranched SELECT is what "
        "makes the completeness precondition true",
    ),
)


@dataclass(frozen=True)
class ConstructionSite:
    """One ``CompleteRoster(...)`` call found in the source tree.

    ``file`` is relative to ``backend/app/``. ``function`` is the name of
    the innermost enclosing ``def`` / ``async def`` (``"__module__"`` for
    a top-level call, none expected). ``lineno`` is informational and not
    used in set-equality.
    """

    file: str
    function: str
    lineno: int


def _enclosing_function(parents: list[ast.AST]) -> str:
    """Return the innermost enclosing function name in ``parents``
    (deepest-first stack), or ``"__module__"`` if none. Class bodies are
    skipped so a call inside a method reports the method, not the class.
    """
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _called_name(node: ast.Call) -> str | None:
    """The callee's terminal name: ``CompleteRoster(...)`` and
    ``billing_service.CompleteRoster(...)`` both resolve to
    ``"CompleteRoster"``. Anything else resolves to ``None``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_construction_sites() -> list[ConstructionSite]:
    """Walk every ``.py`` under ``backend/app/`` and collect each call whose
    callee is named ``CompleteRoster``.

    Docstrings and comments that merely mention the type are string /
    comment tokens, not ``Call`` nodes, so prose is ignored for free —
    which matters here, because the surrounding docstrings discuss the
    type at length. Type ANNOTATIONS (``roster: CompleteRoster``) are
    ``Name`` nodes without a ``Call`` parent and are likewise ignored:
    accepting the type is the whole point, only building it is forbidden.
    """
    sites: list[ConstructionSite] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue

        rel_path = path.relative_to(BACKEND_APP).as_posix()

        def visit(node: ast.AST, parents: list[ast.AST]) -> None:
            if isinstance(node, ast.Call) and _called_name(node) == "CompleteRoster":
                sites.append(
                    ConstructionSite(
                        file=rel_path,
                        function=_enclosing_function(parents),
                        lineno=node.lineno,
                    )
                )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + [node])

        visit(tree, [])
    return sites


def test_complete_roster_is_constructed_only_by_load_complete_roster():
    """Every ``CompleteRoster(...)`` in ``backend/app/`` is allowlisted, and
    every allowlisted site still contains one.

    Two assertions, kept separate so a failure points cleanly at one
    direction:

      * UNEXPECTED — a new construction site appeared. Whatever rows it
        wraps are NOT known to be the org's complete roster, so every
        kernel property (contiguity, non-overlap, exactly-one-open,
        straddling, lapsed) silently degrades to a claim about a sample.
        Call ``load_complete_roster`` instead.
      * MISSING — the sanctioned site no longer constructs one (e.g.
        ``load_complete_roster`` was refactored). Drop the stale entry so
        the allowlist keeps matching reality.
    """
    expected: set[tuple[str, str]] = {
        (rel_path, fn) for rel_path, fn, _ in ALLOWED_CONSTRUCTION_SITES
    }
    found_sites = _find_construction_sites()
    found: set[tuple[str, str]] = {(s.file, s.function) for s in found_sites}

    unexpected = found - expected
    assert not unexpected, (
        "`CompleteRoster(...)` constructed outside `load_complete_roster` — "
        "the completeness precondition the anomaly kernel quantifies over "
        "cannot be assumed for those rows (spec "
        "specs/2026-07-29-billing-period-roster-design.md §2.2):\n"
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
        "Allowlisted `CompleteRoster(...)` construction site(s) no longer "
        "present — remove the stale entry from "
        "ALLOWED_CONSTRUCTION_SITES:\n"
        + "\n".join(f"  - {file}::{fn}" for file, fn in sorted(missing))
    )
