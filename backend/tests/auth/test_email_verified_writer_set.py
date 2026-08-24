"""AST-level regression: pin every write of ``email_verified`` (TBD-362, F11).

Spec: ``specs/2026-08-23-tbd-362-admin-email-recovery.md``, fence F11.

WHAT THIS EXISTS FOR. The TBD-362 ticket's DoD asked for an eighth
``email_verified`` writer — an operator who *asserts* an address is good —
and framed that as the central security concern. Under TBD-361 that concern
evaporates rather than being managed: the operator writes ``pending_email``
only, and the user still proves control by clicking. **The design therefore
adds ZERO writers**, and the DoD's demand becomes an invariant to enforce
rather than code to write. DoD item 2 asked for parity with the public-route
allowlist; that allowlist is enforced by a test, not a review note, so this
is that test.

The invariant it protects is load-bearing far beyond this ticket.
``email_verified`` is a **one-way latch**: every write in ``backend/app/``
sets it ``True``, and the only ``False`` is the creation-time value inside
the ``User(...)`` constructor in ``register`` — never a transition. Combined
with the unconditional login 403 on unverified accounts, that latch is what
makes ``email_verified=False`` imply "never held a session", which is the
whole basis of the admin endpoint's ``user_already_verified`` scope guard. A
new writer — in either direction — voids that reasoning silently.

Modelled function-for-function on
``tests/auth/test_sessions_invalidated_at_allowlist.py``, with two
deliberate departures documented below.

⚠ PARSE, NEVER GREP. The design spec, this docstring and the handler's own
comments all contain the string ``email_verified``; a whole-file text search
is satisfied by any of them. This repo has a recorded incident where exactly
that happened — a grep for a missing config key PASSED because the key
appeared in the comment documenting its absence. The AST only sees real
assignment and call nodes.

⚠⚠ DEPARTURE 1 — THE MODEL TEST DOES NOT COLLECT CALL KEYWORDS, AND THIS
FENCE MUST. ``_find_write_sites`` in the model matches ``ast.Assign`` with
``ast.Attribute`` targets only. ``routers/auth.py::register``'s ONLY write is
the ``email_verified=is_first_user_setup`` keyword on ``User(...)``, so a
verbatim copy reports ``register`` as a spurious MISSING and an author would
"fix" it by deleting the most important entry in the list. The keyword
collection is an extension this fence requires and the model lacks; it is
built deliberately, here.

⚠⚠ DEPARTURE 2 — KEYWORDS ARE KEYED ON THE CALLEE NAME. ``_user_response``
passes ``email_verified=`` to ``UserResponse(...)`` in two modules; those are
response serialisations, not writes. Only ``User(...)`` constructions count.

⚠ ``ast.AnnAssign`` IS EXCLUDED. ``models/user.py``, ``schemas/auth.py`` and
``schemas/admin_orgs.py`` declare the column/field as an annotation, not a
write. Harmless while the collector is Attribute-only, and load-bearing the
moment someone broadens it to ``Name`` targets.

⚠ THE TREE WALKED IS ``backend/app/`` ONLY, and that scope is a claim, not an
accident: ``backend/seed.py`` writes this column as **raw SQL**, which no
attribute-store collector can see. Widening the walk to the backend root
would silently keep missing it while implying it did not.

⚠⚠ THE ALLOWLIST HAS **SIX** ENTRIES, NOT EIGHT. Entries are ``(file,
function)`` pairs and multiple writes inside one function count as one entry.
The eight physical write sites collapse: ``google_callback`` holds two (an
attribute assign on the existing row plus a ``User(...)`` keyword on the new
one) and ``accept_invitation`` holds two (same shape). Writing eight entries
produces two spurious MISSING failures against a correct implementation.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parents[2] / "app"

ATTRIBUTE = "email_verified"

# Only a construction of a ``users`` row counts as a write via keyword.
# ``UserResponse(...)`` / any other callee is a serialisation.
WRITING_CALLEES: frozenset[str] = frozenset({"User"})


# ── The allowlist ───────────────────────────────────────────────────────────
#
# ``(relative_path, function_name, justification)``, rooted at ``backend/app/``.
#
#   * routers/auth.py::register
#       TBD-344 bootstrap. ``email_verified=is_first_user_setup`` inside the
#       ``User(...)`` constructor — the ONLY ``False`` anywhere in the tree,
#       and a creation-time value rather than a transition, which is exactly
#       what makes the column a one-way latch.
#       ⚠ Keyed on ``is_first_user_setup`` (``user_count == 0``), NOT on
#       ``is_first_user`` (``existing_superadmin == 0``). The two predicates
#       deliberately diverge; using the latter for a bypass grants it to a
#       public self-signup on any install whose superadmins were demoted.
#
#   * routers/auth.py::_promote_pending_email
#       TBD-361 promotion. The claimed address proved itself, so identity
#       moves and the flag comes with it.
#
#   * routers/auth.py::verify_email
#       The bootstrap arm: the address the account ALREADY holds proved
#       itself. No identity change, so deliberately no session cutoff and no
#       ``pending_email`` clear.
#
#   * routers/auth.py::google_callback
#       TWO sites, one entry. Google asserted ``verified_email`` on an
#       existing row, and the new-row constructor for a first Google sign-in.
#
#   * services/invitation_service.py::accept_invitation
#       TWO sites, one entry. Reactivation of a soft-deleted same-org row,
#       and the new-user constructor. The invite was mailed to the address,
#       so accepting it proves control of that inbox.
#
#   * services/user_merge_service.py::merge_users
#       Carries the bit over from a verified source row. Only ever pushes a
#       target INTO the verified state, never out of it.
#
# ⚠ TBD-362 ADDS NOTHING HERE, and that is the point of the ticket. If a
# future PR genuinely needs a new writer, add the entry WITH a justification
# and treat it as a security change — an operator-asserted verification is
# the specific shape this fence exists to refuse.

ALLOWED_WRITE_SITES: tuple[tuple[str, str, str], ...] = (
    (
        "routers/auth.py",
        "register",
        "TBD-344 bootstrap; the only False in the tree, set at creation",
    ),
    (
        "routers/auth.py",
        "_promote_pending_email",
        "TBD-361 promotion of a proven pending_email",
    ),
    (
        "routers/auth.py",
        "verify_email",
        "bootstrap arm: the address the account already holds proved itself",
    ),
    (
        "routers/auth.py",
        "google_callback",
        "Google asserted verified_email (existing row + new-row constructor)",
    ),
    (
        "services/invitation_service.py",
        "accept_invitation",
        "invite was mailed to the address (reactivation + new-user construct)",
    ),
    (
        "services/user_merge_service.py",
        "merge_users",
        "carries the verified bit over from a verified source row",
    ),
)


@dataclass(frozen=True)
class WriteSite:
    """One write of ``email_verified`` found under ``backend/app/``.

    ``kind`` is ``"assign"`` or ``"keyword"`` — informational only, and not
    part of the set-equality comparison, but printed on failure so an author
    can see which shape they added.
    """

    file: str
    function: str
    lineno: int
    kind: str


def _enclosing_function(parents: list[ast.AST]) -> str:
    """Innermost enclosing ``def`` / ``async def``, or ``"__module__"``.

    Class definitions are skipped, so a write inside a method reports the
    method name rather than the class.
    """
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _callee_name(func: ast.expr) -> str | None:
    """Name of the thing being called: ``User(...)`` -> ``"User"``,
    ``models.User(...)`` -> ``"User"``. Anything else -> ``None``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_write_sites() -> list[WriteSite]:
    """Every write of ``email_verified`` under ``backend/app/``.

    TWO node shapes are collected:

    * ``ast.Assign`` whose target is an ``ast.Attribute`` named
      ``email_verified`` — ``user.email_verified = True`` and friends.
      Tuple / list targets are unwrapped.
    * ``ast.keyword`` named ``email_verified`` on a ``ast.Call`` whose callee
      resolves to a name in :data:`WRITING_CALLEES`. This is the extension the
      model test lacks; without it ``register`` is invisible.

    Deliberately NOT collected:

    * ``ast.AnnAssign`` — the ``mapped_column`` / Pydantic field declarations
      are annotations, not writes.
    * ``ast.AugAssign`` — never used on a bool column, and would not be a
      transition we could interpret anyway.
    * every read: comparisons, ``.is_(False)`` filters, dict serialisations.
      None of those are ``Assign`` or a matching ``keyword``.
    """
    sites: list[WriteSite] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            continue

        rel_path = str(path.relative_to(BACKEND_APP))

        def visit(node: ast.AST, parents: list[ast.AST]) -> None:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    candidates: list[ast.expr] = (
                        list(target.elts)
                        if isinstance(target, (ast.Tuple, ast.List))
                        else [target]
                    )
                    for candidate in candidates:
                        if (
                            isinstance(candidate, ast.Attribute)
                            and candidate.attr == ATTRIBUTE
                        ):
                            sites.append(
                                WriteSite(
                                    file=rel_path,
                                    function=_enclosing_function(parents),
                                    lineno=node.lineno,
                                    kind="assign",
                                )
                            )
            elif isinstance(node, ast.Call):
                callee = _callee_name(node.func)
                if callee in WRITING_CALLEES:
                    for kw in node.keywords:
                        if kw.arg == ATTRIBUTE:
                            sites.append(
                                WriteSite(
                                    file=rel_path,
                                    function=_enclosing_function(parents),
                                    lineno=node.lineno,
                                    kind="keyword",
                                )
                            )
            for child in ast.iter_child_nodes(node):
                visit(child, parents + [node])

        visit(tree, [])
    return sites


def test_email_verified_writer_set_is_closed():
    """Set equality, failing in BOTH directions.

    * UNEXPECTED — a new writer appeared. This is the TBD-362 regression
      class: an operator-asserted verification, or any other path that flips
      the flag without proof of inbox control. It also catches the far worse
      direction, a write of ``False``, which would push a data-owning account
      into the admin endpoint's accepted set.
    * MISSING — an allowlisted writer vanished. Forces an explicit decision
      rather than a silent drop; a removed verification arm changes who can
      log in.
    """
    expected: set[tuple[str, str]] = {
        (rel_path, fn) for rel_path, fn, _ in ALLOWED_WRITE_SITES
    }
    found_sites = _find_write_sites()
    found: set[tuple[str, str]] = {(s.file, s.function) for s in found_sites}

    missing = expected - found
    assert not missing, (
        f"Allowlisted ``email_verified`` writer(s) are gone: {sorted(missing)}. "
        "Either the arm was removed (drop the entry WITH a justification) or "
        "the function was renamed (update the allowlist). Do not delete an "
        "entry to make this green — the six arms are the whole verification "
        "surface."
    )

    unexpected = found - expected
    if unexpected:
        details = "\n".join(
            f"  - {s.file}:{s.lineno} inside {s.function} ({s.kind})"
            for s in found_sites
            if (s.file, s.function) in unexpected
        )
        raise AssertionError(
            "Unexpected ``email_verified`` write(s) outside the allowlist:\n"
            f"{details}\n"
            "TBD-362's ruling is that the operator writes ``pending_email`` "
            "ONLY and the user proves control by clicking, so the correct "
            "design adds ZERO writers here. ``email_verified`` is a one-way "
            "latch and the admin endpoint's scope guard depends on it. If "
            "this addition is genuinely intended, extend ALLOWED_WRITE_SITES "
            "with a justification and have it reviewed as a security change."
        )


def test_the_collector_sees_both_node_shapes():
    """Anti-vacuity control for the collector itself.

    If ``_find_write_sites`` silently stopped matching one of the two shapes
    the set-equality test above could still go green — MISSING would fire
    only for a function whose ONLY writes were of the lost shape. ``register``
    is exactly that function for keywords, so pin both counts directly.

    ⚠ This is the assertion that would have caught a verbatim copy of the
    model test, which collects assigns only.
    """
    sites = _find_write_sites()
    kinds = {s.kind for s in sites}
    assert kinds == {"assign", "keyword"}, (
        f"the collector only produced {sorted(kinds)} — one of the two node "
        "shapes stopped matching, which makes the allowlist comparison "
        "partially blind"
    )
    register_sites = [
        s for s in sites if s.file == "routers/auth.py" and s.function == "register"
    ]
    assert register_sites and all(s.kind == "keyword" for s in register_sites), (
        "``register``'s only write is a ``User(...)`` keyword; it must be "
        "collected, or the one site that can write False is invisible"
    )


def test_serialisation_keywords_are_not_counted_as_writes():
    """``UserResponse(email_verified=...)`` is a read, not a write.

    ``_user_response`` in BOTH ``routers/auth.py`` and ``routers/users.py``
    passes the keyword. Keying on the callee name is what keeps them out; a
    collector keyed on the keyword name alone would report two extra
    functions and an author would "fix" it by widening the allowlist, which
    is precisely the wrong direction.
    """
    found = {(s.file, s.function) for s in _find_write_sites()}
    assert ("routers/users.py", "_user_response") not in found
    assert ("routers/auth.py", "_user_response") not in found


def test_annotations_are_not_counted_as_writes():
    """The column and schema declarations are ``AnnAssign`` nodes."""
    found = {(s.file, s.function) for s in _find_write_sites()}
    assert ("models/user.py", "__module__") not in found
    assert ("schemas/auth.py", "__module__") not in found
    assert ("schemas/admin_orgs.py", "__module__") not in found


def test_the_scope_claim_is_app_only():
    """The walked tree is ``backend/app/`` and nothing above it.

    ``backend/seed.py`` writes this column as raw SQL. No attribute-store
    collector can see that, so widening the walk would add coverage this
    fence cannot actually provide while implying that it had.
    """
    assert BACKEND_APP.name == "app"
    assert (BACKEND_APP.parent / "seed.py").exists(), (
        "the out-of-scope raw-SQL writer this docstring names has moved; "
        "re-check the scope claim"
    )
