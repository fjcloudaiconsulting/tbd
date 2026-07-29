"""AST-level regressions on ``load_complete_roster``'s two preconditions.

TBD-234a, spec ``specs/2026-07-29-billing-period-roster-design.md`` §2.2
(the complete roster and the derived end) and §4a test 14.

Two source guards live here, for the same reason: both preconditions are
claims about the SHAPE of the loader that no runtime assertion can check.

1. **COMPLETENESS** — ``CompleteRoster`` has exactly one construction
   site. See below.
2. **``start_date`` ASC** — that site's ``SELECT`` carries
   ``ORDER BY start_date``. See :func:`test_load_complete_roster_orders_by_start_date_ascending`,
   added in the PR-review fold. ⚠ **This one CANNOT be fenced
   behaviourally**, and the review's suggested fixture fix does not work:
   see that test's docstring for the ``EXPLAIN QUERY PLAN`` evidence.

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
A ``@dataclass(frozen=True)`` has a public ``__init__``, so every shape
below succeeds at runtime with nothing else objecting.

⚠ **The scan is source-scoped to ``backend/app/``, so tests are exempt by
construction.** That is deliberate, not an oversight: 234a's own kernel
fixtures hand-build rosters, and they are legal precisely because they are
not production code.

Modelled on ``tests/test_no_raw_request_client.py`` and
``tests/auth/test_sessions_invalidated_at_allowlist.py``, the two
established backend source guards (the #552 pattern).


What this guard catches
=======================

Four construction SHAPES, all of which produce a ``CompleteRoster``:

1. **Direct** — ``CompleteRoster(...)`` or ``mod.CompleteRoster(...)``.
2. **Aliased import** — ``from ...billing_service import CompleteRoster as
   _CR`` followed by ``_CR(...)``. ``ImportFrom`` aliases are resolved per
   scanned module, so the local name is matched too.
3. **``dataclasses.replace``** — ``replace(roster, rows=windowed)``, however
   the module is spelled (``dataclasses.replace``, ``import dataclasses as
   dc`` → ``dc.replace``, or ``from dataclasses import replace``). ⚠ This
   is the shape the PR-review round proved the first version of this guard
   MISSED **while its own docstring claimed to catch it**, and it is not
   hypothetical: TBD-234b is the ticket that will window a roster for
   display, and ``dataclasses.replace`` is the natural reach.
4. **Class-object indirection** — ``roster.__class__(...)`` and
   ``type(roster)(...)``.

Shape 3 is matched conservatively (over-flagging is preferred to
under-flagging, and the failure message says exactly what to do): any
``replace`` call **with at least one positional argument** that is either
resolvably ``dataclasses.replace``, or lives in ``billing_service.py``, or
whose first positional argument is a roster-ish name. The positional
requirement is not a narrowing dodge — ``dataclasses.replace(obj, /,
**changes)`` takes its instance positionally by signature, and it is what
keeps ``datetime.date.replace(day=...)`` (already used in
``billing_service._snap_to_cycle``) from being a false positive.


What this guard does NOT catch — stated, not implied
====================================================

The list is deliberately explicit, because the previous version of this
docstring asserted completeness it did not have, and a reader trusting an
overclaim is worse off than one who knows the edge:

* **Assignment aliasing** — ``_CR = CompleteRoster`` then ``_CR(...)``.
  Only ``ImportFrom`` aliases are tracked; a general assignment-alias
  analysis needs data flow, not an AST name match.
* **Fully dynamic construction** — ``globals()["CompleteRoster"](...)``,
  ``getattr(mod, name)(...)``, ``copy.deepcopy`` plus
  ``object.__setattr__`` to mutate a frozen instance in place, or
  ``pickle`` round-trips.
* **Modules the scan cannot parse** (``OSError`` / ``SyntaxError``), which
  are skipped silently. ``python -m compileall backend/app`` in CI is what
  covers that gap.
* **Anything outside ``backend/app/``** — by design; see the source-scoping
  note above.

Every uncovered shape is deliberately obscure, and the point of the covered
four is that they are the shapes a well-meaning implementer reaches for.

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

#: The type whose construction is restricted.
TARGET = "CompleteRoster"

#: First-positional-argument names treated as "this is a roster" for the
#: ``replace`` shape. Bare over-approximation, on purpose.
_ROSTER_ISH = ("roster", "complete_roster", "rosters")

#: Modules where ANY positional ``replace`` call is suspicious, because the
#: type lives there and a windowing edit would land there first.
_REPLACE_HOT_FILES = ("services/billing_service.py",)


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
    """One ``CompleteRoster``-producing call found in the source tree.

    ``file`` is relative to ``backend/app/``. ``function`` is the name of
    the innermost enclosing ``def`` / ``async def`` (``"__module__"`` for
    a top-level call, none expected). ``shape`` names which of the four
    detected forms matched, and is what makes a failure message actionable.
    ``lineno`` and ``shape`` are informational and not used in set-equality.
    """

    file: str
    function: str
    lineno: int
    shape: str = "direct"


def _enclosing_function(parents: list[ast.AST]) -> str:
    """Return the innermost enclosing function name in ``parents``
    (deepest-first stack), or ``"__module__"`` if none. Class bodies are
    skipped so a call inside a method reports the method, not the class.
    """
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _terminal_name(node: ast.AST) -> str | None:
    """The terminal name of a ``Name`` or ``Attribute`` expression:
    ``CompleteRoster`` and ``billing_service.CompleteRoster`` both resolve
    to ``"CompleteRoster"``. Anything else resolves to ``None``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _target_aliases(tree: ast.AST) -> set[str]:
    """Local names that refer to :data:`TARGET` inside this module.

    ``from app.services.billing_service import CompleteRoster as _CR``
    binds ``_CR``, and ``_CR(org_id=1, rows=windowed)`` builds exactly the
    forbidden thing while a literal-name match sees nothing.
    """
    aliases = {TARGET}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == TARGET:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _replace_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``dataclasses.replace`` by a `from` import."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
            for alias in node.names:
                if alias.name == "replace":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _dataclasses_module_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``dataclasses`` MODULE.

    ``import dataclasses as dc`` then ``dc.replace(roster, ...)`` is the same
    construction wearing a different prefix.
    """
    names = {"dataclasses"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dataclasses":
                    names.add(alias.asname or alias.name)
    return names


def _shape_of(
    node: ast.Call,
    *,
    rel_path: str,
    target_aliases: set[str],
    replace_aliases: set[str],
    dataclasses_names: set[str],
) -> str | None:
    """Which forbidden construction shape ``node`` matches, or ``None``.

    Order matters only for the failure message; the shapes are disjoint in
    practice.
    """
    func = node.func
    name = _terminal_name(func)

    # 1 / 2 — direct, or through an ImportFrom alias.
    if name is not None and name in target_aliases:
        return "direct" if name == TARGET else f"aliased import (`{name}`)"

    # 4 — `roster.__class__(...)`, and its sibling `type(roster)(...)`.
    if isinstance(func, ast.Attribute) and func.attr == "__class__":
        return "`__class__` indirection"
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "type"
        and len(func.args) == 1
    ):
        return "`type(...)` indirection"

    # 3 — `dataclasses.replace(roster, rows=windowed)`.
    #
    # A positional first argument is REQUIRED to match, because
    # `dataclasses.replace(obj, /, **changes)` takes its instance
    # positionally by signature. That is what keeps
    # `date.replace(day=...)` — already live in `_snap_to_cycle` — out,
    # without narrowing anything that could hide a real construction.
    if name == "replace" and node.args:
        qualified = (
            isinstance(func, ast.Attribute)
            and _terminal_name(func.value) in dataclasses_names
        )
        imported = isinstance(func, ast.Name) and func.id in replace_aliases
        first_arg = _terminal_name(node.args[0])
        roster_ish = first_arg is not None and (
            first_arg in _ROSTER_ISH or first_arg in target_aliases
        )
        if qualified or imported or roster_ish or rel_path in _REPLACE_HOT_FILES:
            return "`dataclasses.replace`"

    return None


def scan_source(source: str, rel_path: str) -> list[ConstructionSite]:
    """Collect every forbidden construction shape in one module's source.

    Split out from :func:`_find_construction_sites` so the guard itself can
    be exercised against synthetic modules — see
    :func:`test_guard_catches_every_documented_construction_shape`, the
    positive control. A guard with no positive control is a guard that can
    silently stop guarding, which is exactly the failure this fold is
    repairing.

    Docstrings and comments that merely mention the type are string /
    comment tokens, not ``Call`` nodes, so prose is ignored for free —
    which matters here, because the surrounding docstrings discuss the
    type at length. Type ANNOTATIONS (``roster: CompleteRoster``) are
    ``Name`` nodes without a ``Call`` parent and are likewise ignored:
    accepting the type is the whole point, only building it is forbidden.
    """
    tree = ast.parse(source, filename=rel_path)
    target_aliases = _target_aliases(tree)
    replace_aliases = _replace_aliases(tree)
    dataclasses_names = _dataclasses_module_names(tree)

    sites: list[ConstructionSite] = []

    def visit(node: ast.AST, parents: list[ast.AST]) -> None:
        if isinstance(node, ast.Call):
            shape = _shape_of(
                node,
                rel_path=rel_path,
                target_aliases=target_aliases,
                replace_aliases=replace_aliases,
                dataclasses_names=dataclasses_names,
            )
            if shape is not None:
                sites.append(
                    ConstructionSite(
                        file=rel_path,
                        function=_enclosing_function(parents),
                        lineno=node.lineno,
                        shape=shape,
                    )
                )
        for child in ast.iter_child_nodes(node):
            visit(child, parents + [node])

    visit(tree, [])
    return sites


def _find_construction_sites() -> list[ConstructionSite]:
    """Walk every ``.py`` under ``backend/app/`` and collect the sites."""
    sites: list[ConstructionSite] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel_path = path.relative_to(BACKEND_APP).as_posix()
        try:
            sites.extend(scan_source(source, rel_path))
        except SyntaxError:
            continue
    return sites


def test_complete_roster_is_constructed_only_by_load_complete_roster():
    """Every ``CompleteRoster``-producing call in ``backend/app/`` is
    allowlisted, and every allowlisted site still contains one.

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
        "`CompleteRoster` constructed outside `load_complete_roster` — "
        "the completeness precondition the anomaly kernel quantifies over "
        "cannot be assumed for those rows (spec "
        "specs/2026-07-29-billing-period-roster-design.md §2.2). Call "
        "`load_complete_roster` instead; do NOT narrow this guard:\n"
        + "\n".join(
            f"  - {s.file}::{s.function} (line {s.lineno}, shape: {s.shape})"
            for s in sorted(
                (s for s in found_sites if (s.file, s.function) not in expected),
                key=lambda s: (s.file, s.lineno),
            )
        )
    )

    missing = expected - found
    assert not missing, (
        "Allowlisted `CompleteRoster` construction site(s) no longer "
        "present — remove the stale entry from "
        "ALLOWED_CONSTRUCTION_SITES:\n"
        + "\n".join(f"  - {file}::{fn}" for file, fn in sorted(missing))
    )


# ── The `start_date` ASC guard ─────────────────────────────────────────────


def _load_complete_roster_ast() -> ast.AST:
    """The ``load_complete_roster`` function node, parsed from source."""
    source = (BACKEND_APP / "services" / "billing_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source, filename="services/billing_service.py")
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "load_complete_roster"
        ):
            return node
    raise AssertionError(
        "`load_complete_roster` not found in billing_service.py — if it was "
        "renamed, update this guard and ALLOWED_CONSTRUCTION_SITES together"
    )


def _order_by_arguments(func_node: ast.AST) -> list[ast.expr]:
    """Every argument passed to an ``.order_by(...)`` call inside ``func_node``."""
    args: list[ast.expr] = []
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "order_by"
        ):
            args.extend(node.args)
    return args


def test_load_complete_roster_orders_by_start_date_ascending():
    """⚠ ``load_complete_roster``'s ``SELECT`` must carry
    ``ORDER BY BillingPeriod.start_date``, ascending.

    **Why this is a SOURCE guard and not a fixture.** The PR review found
    that deleting the ``ORDER BY`` left every test green and prescribed the
    usual fix: seed one fixture's rows out of ``start_date`` order so the
    ASC assertion discriminates. **That fix does not work, and the stated
    cause was wrong.** The rows do not come back in rowid order at all:
    ``BillingPeriod`` carries ``uq_billing_period_org_start`` on
    ``(org_id, start_date)``, and SQLite plans both forms of the query
    identically through the implicit index behind it —

        SELECT id, start_date, end_date FROM billing_periods WHERE org_id = 1
        -> SEARCH billing_periods USING INDEX sqlite_autoindex_billing_periods_1 (org_id=?)

        ... ORDER BY start_date
        -> SEARCH billing_periods USING INDEX sqlite_autoindex_billing_periods_1 (org_id=?)

    — so the rows arrive in ``(org_id, start_date)`` order whether the
    clause is there or not, for any insertion order whatsoever. (Verified:
    test 14 now seeds DESCENDING and still passes with the clause deleted.)
    **No behavioural test against this schema can fence the clause**, and
    gaming the query planner into a table scan would be a fragile test of
    SQLite rather than of us.

    Production is MySQL, which guarantees **no** order without an
    ``ORDER BY``, and ``start_date`` ASC is the precondition
    ``kernel_derived_end``'s equality with ``period_effective_end`` rests
    on: on an unordered list ``rows[i + 1].start_date`` is not
    ``MIN(start_date) WHERE start_date > rows[i].start_date`` and every
    derived end, gap and overlap is garbage. A property that matters in
    production and is invisible in the test DB is exactly what a source
    guard is for — the same reasoning as the completeness guard above.
    """
    order_by_args = _order_by_arguments(_load_complete_roster_ast())

    assert len(order_by_args) == 1, (
        "`load_complete_roster` must carry exactly one `.order_by(...)`; "
        f"found {len(order_by_args)}. The `start_date` ASC precondition is "
        "invisible to every behavioural test (see this test's docstring), so "
        "this guard is the only thing standing behind it."
    )

    arg = order_by_args[0]
    assert isinstance(arg, ast.Attribute) and arg.attr == "start_date", (
        "`load_complete_roster` must order by `BillingPeriod.start_date`, "
        f"not by {ast.dump(arg)}"
    )
    assert _terminal_name(arg.value) == "BillingPeriod", (
        "`load_complete_roster`'s ORDER BY must name `BillingPeriod.start_date`"
    )
    # `.desc()` would parse as a Call, never a bare Attribute, so ASC is
    # already implied by the isinstance check above; asserted explicitly so
    # the intent survives a future refactor of this guard.
    assert not isinstance(arg, ast.Call), (
        "`load_complete_roster` must order ASCENDING — `.desc()` inverts the "
        "roster and breaks `kernel_derived_end`'s successor rule"
    )


# ── Positive control ───────────────────────────────────────────────────────
#
# The guard above can only fail if the collector actually detects things.
# The first version of this file detected exactly one of the four shapes
# while its docstring named two, and nothing noticed, because there was no
# test of the guard itself. These are that test.


_SYNTHETIC_HITS: tuple[tuple[str, str, str], ...] = (
    (
        "dataclasses.replace",
        "services/roster_view.py",
        """
import dataclasses
from app.services.billing_service import CompleteRoster


def window(roster: CompleteRoster, months: int) -> CompleteRoster:
    windowed = roster.rows[-months:]
    return dataclasses.replace(roster, rows=windowed)
""",
    ),
    (
        "aliased dataclasses module",
        "services/roster_view.py",
        """
import dataclasses as dc


def window(r, months):
    return dc.replace(r, rows=r.rows[-months:])
""",
    ),
    (
        "bare `replace` imported from dataclasses",
        "services/roster_view.py",
        """
from dataclasses import replace


def window(roster, months):
    return replace(roster, rows=roster.rows[-months:])
""",
    ),
    (
        "aliased import",
        "services/roster_view.py",
        """
from app.services.billing_service import CompleteRoster as _CR


def window(roster, months):
    return _CR(org_id=roster.org_id, rows=roster.rows[-months:])
""",
    ),
    (
        "__class__ indirection",
        "services/roster_view.py",
        """
def window(roster, months):
    return roster.__class__(org_id=roster.org_id, rows=roster.rows[-months:])
""",
    ),
    (
        "type() indirection",
        "services/roster_view.py",
        """
def window(roster, months):
    return type(roster)(org_id=roster.org_id, rows=roster.rows[-months:])
""",
    ),
    (
        "direct call",
        "services/roster_view.py",
        """
from app.services.billing_service import CompleteRoster


def window(roster, months):
    return CompleteRoster(org_id=roster.org_id, rows=roster.rows[-months:])
""",
    ),
    (
        "positional replace inside billing_service itself",
        "services/billing_service.py",
        """
def window(r, months):
    return replace(r, rows=r.rows[-months:])
""",
    ),
)


_SYNTHETIC_MISSES: tuple[tuple[str, str, str], ...] = (
    (
        "date.replace(day=...) — keyword-only, no instance argument",
        "services/billing_service.py",
        """
def _snap_to_cycle(d, cycle_day):
    return d.replace(day=cycle_day)
""",
    ),
    (
        "a type ANNOTATION, which is accepted and not a construction",
        "services/roster_view.py",
        """
from app.services.billing_service import CompleteRoster


def render(roster: CompleteRoster) -> int:
    return len(roster.rows)
""",
    ),
    (
        "`exc.__class__.__name__` — an attribute read, not a call",
        "redis_client.py",
        """
def describe(exc):
    return exc.__class__.__name__
""",
    ),
    (
        "prose mentioning CompleteRoster(...) in a docstring",
        "services/roster_view.py",
        '''
def render(rows):
    """Never call CompleteRoster(org_id=1, rows=rows) outside the loader."""
    return len(rows)
''',
    ),
)


def test_guard_catches_every_documented_construction_shape():
    """⚠ **Positive control.** The collector must actually FIRE on each of
    the shapes this file claims to catch.

    Without this, the guard's own detection can silently narrow (or never
    have been wide enough — the PR-review round proved three of these
    shapes passed the first version while its docstring asserted "Only this
    test objects"), and the module test above stays green forever because
    green is also what "detects nothing" looks like.
    """
    for label, rel_path, source in _SYNTHETIC_HITS:
        sites = scan_source(source, rel_path)
        assert sites, (
            f"the construction-site collector MISSED {label!r} — that shape "
            "builds a windowed CompleteRoster in backend/app/ and the guard "
            "would stay green"
        )
        assert all(s.function == "window" for s in sites), (
            f"{label!r} was attributed to the wrong enclosing function: "
            f"{[s.function for s in sites]}"
        )


def test_guard_does_not_fire_on_the_documented_non_constructions():
    """The negative half: over-flagging is preferred, but not to the point
    of flagging shapes that build nothing.

    ``date.replace(day=...)`` is live in ``billing_service._snap_to_cycle``
    today, so this is a real false-positive fence and not a hypothetical.
    """
    for label, rel_path, source in _SYNTHETIC_MISSES:
        sites = scan_source(source, rel_path)
        assert sites == [], (
            f"the collector FALSE-POSITIVED on {label!r}: {sites}"
        )
