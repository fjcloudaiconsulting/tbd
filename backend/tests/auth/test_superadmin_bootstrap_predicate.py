"""AST-level regression: pin every write of ``is_superadmin`` (TBD-365).

WHAT THIS EXISTS FOR. Before TBD-365 the platform-superadmin grant keyed on
``count(is_superadmin) == 0`` at two sites. That predicate is strictly WEAKER
than the bootstrap condition: an empty ``users`` table implies no superadmin,
but not the reverse. So on any install whose superadmins had all been deleted
it stayed true, and the next public self-signup — or the next Google sign-in,
which has no captcha gate and issues a session in the same redirect —
silently received the platform flag.

TBD-365 collapsed both sites onto ``_is_first_user_setup`` (``user_count ==
0``). This fence pins that collapse STRUCTURALLY, which the behavioural
fences cannot:

  * ``tests/auth/test_register_login_bootstrap.py`` and
    ``tests/routers/test_auth_google_callback_first_run.py`` drive the two
    grant sites that exist TODAY. A FOURTH grant site — the
    ``admin.platform_admin.invitation.accepted`` event already forward-declared
    at ``models/audit_event.py``, or a future ``/auth/oidc/callback`` — would
    copy-paste the retired idiom and no request-level test would notice,
    because it only bites in a state a new author would not think to build.

⚠ PARSE, NEVER GREP. The surviving comments in ``auth.py``,
``invitation_service.py``, ``CLAUDE.md`` and this very docstring all contain
the string ``is_superadmin``; a whole-file text search is satisfied by the
prose documenting the invariant. This repo has a recorded incident where a
grep for a missing config key PASSED because the key appeared in the comment
explaining its absence. The AST only sees real keyword and assignment nodes.

⚠ SCOPE IS A CLAIM, NOT AN ACCIDENT. This walks ``backend/app/`` and sees the
``User(...)`` keyword form only. Invisible to it, and NOT covered:

  * ``setattr(user, "is_superadmin", ...)``
  * ``update(User).values(is_superadmin=...)`` and any raw SQL
  * ``User(**kwargs)`` where the flag arrives inside the dict
  * an aliased import (``from app.models.user import User as U; U(...)``) —
    ``WRITING_CALLEES`` matches the callee SPELLING only
  * tuple/list assignment targets (``a.is_superadmin, b.x = ...``)

None of those exists today — verified by grep — and there is no promote or
demote path anywhere in the tree: the flag is only ever set at construction.
That is a statement about today, not coverage.

⚠⚠ WHAT THESE TESTS CAN AND CANNOT SEE — read before relying on them.
Structural fences pin SHAPE and PROVENANCE. They do NOT pin SEMANTICS. The
thing that actually proves the predicate is *correct* is the behavioural
divergent-state pair (``test_register_login_bootstrap.py`` F3 and
``test_auth_google_callback_first_run.py``'s SSO twin), which build the one
state where a right and a wrong predicate disagree and observe the stored
rows. These fences exist for the case those cannot reach: a FOURTH grant site
that no request-level test drives, because it only bites in a state a new
author would not think to construct.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

TARGET = "is_superadmin"
WRITING_CALLEES: frozenset[str] = frozenset({"User"})

# The ONE approved predicate name at each bootstrap grant site. These are the
# locals `register` and `google_callback` bind from `_is_first_user_setup`.
APPROVED_PREDICATE_NAMES: frozenset[str] = frozenset(
    {"is_first_user_setup", "sso_first_user_setup"}
)

# ``(relative_path, function_name, justification)`` rooted at ``backend/app/``.
ALLOWED_WRITE_SITES: tuple[tuple[str, str, str], ...] = (
    (
        "routers/auth.py",
        "register",
        "bootstrap grant; binds is_first_user_setup (user_count == 0)",
    ),
    (
        "routers/auth.py",
        "google_callback",
        "bootstrap grant on the SSO path; binds sso_first_user_setup",
    ),
    (
        "services/invitation_service.py",
        "accept_invitation",
        "hardcoded False — an org invitation must never write a platform flag",
    ),
)


@dataclass(frozen=True)
class WriteSite:
    """One ``is_superadmin`` write found under ``backend/app/``.

    ``value_repr`` is the structural description of what was written, and is
    what the provenance test compares — never the source text.
    """

    file: str
    function: str
    lineno: int
    value_repr: str


def _enclosing_function(parents: list[ast.AST]) -> str:
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "__module__"


def _describe(node: ast.AST) -> str:
    """Compact description of the keyword's value node.

    Deliberately structural: ``Name(is_first_user_setup)`` and
    ``Constant(False)`` are the only two shapes this fence accepts, and
    anything else prints in a form that names what was actually written.
    """
    if isinstance(node, ast.Name):
        return f"Name({node.id})"
    if isinstance(node, ast.Constant):
        return f"Constant({node.value!r})"
    return type(node).__name__


@lru_cache(maxsize=1)
def _parsed() -> tuple[tuple[str, ast.Module], ...]:
    """Every module under ``backend/app/``, parsed once per session.

    Cached because five separate assertions walk this tree; re-parsing ~283
    files per assertion is pure waste. An unreadable or unparseable file is a
    hard failure here rather than a silent skip: a fence that quietly stops
    covering a file it can no longer parse is worse than one that breaks.
    """
    out: list[tuple[str, ast.Module]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        src = path.read_text(encoding="utf-8")
        out.append((rel, ast.parse(src, filename=str(path))))
    return tuple(out)


def _reads_user_attribute(node: ast.AST) -> bool:
    """True for ``user.<attr>`` and for ``bool(user.<attr>)``.

    These are the two shapes an audit payload may use to record an OUTCOME.
    A bare ``Name`` (i.e. restating the local predicate) is deliberately NOT
    one of them — see ``test_audit_outcomes_read_the_row_not_the_local``.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "bool" and len(node.args) == 1:
            return _reads_user_attribute(node.args[0])
        return False
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
        and node.value.id == "user"


def _assignments_to(file: str, function: str, name: str) -> list[ast.AST]:
    """Every value assigned to ``name`` inside ``file::function``."""
    values: list[ast.AST] = []
    for rel, tree in _parsed():
        if rel != file:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != function:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for tgt in inner.targets:
                        if isinstance(tgt, ast.Name) and tgt.id == name:
                            values.append(inner.value)
    return values


def _is_helper_call(node: ast.AST) -> bool:
    """True for ``await _is_first_user_setup(...)``."""
    inner = node.value if isinstance(node, ast.Await) else node
    return (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "_is_first_user_setup"
    )


@lru_cache(maxsize=1)
def _collect() -> tuple[WriteSite, ...]:
    sites: list[WriteSite] = []
    for rel, tree in _parsed():
        stack: list[ast.AST] = []

        def walk(node: ast.AST) -> None:
            stack.append(node)
            # `User(..., is_superadmin=<value>)`
            if isinstance(node, ast.Call):
                callee = node.func
                name = (
                    callee.id
                    if isinstance(callee, ast.Name)
                    else getattr(callee, "attr", None)
                )
                if name in WRITING_CALLEES:
                    for kw in node.keywords:
                        if kw.arg == TARGET:
                            sites.append(
                                WriteSite(
                                    file=rel,
                                    function=_enclosing_function(stack),
                                    lineno=kw.value.lineno,
                                    value_repr=_describe(kw.value),
                                )
                            )
            # `something.is_superadmin = <value>` — no such site exists today;
            # collected so that introducing one FAILS rather than hides.
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Attribute) and tgt.attr == TARGET:
                        sites.append(
                            WriteSite(
                                file=rel,
                                function=_enclosing_function(stack),
                                lineno=node.lineno,
                                value_repr=f"assign:{_describe(node.value)}",
                            )
                        )
            for child in ast.iter_child_nodes(node):
                walk(child)
            stack.pop()

        walk(tree)
    return tuple(sites)


def test_superadmin_writer_set_is_exactly_the_allowlist() -> None:
    """FENCE — both directions. A fourth grant site fails CI instead of shipping.

    Wrong implementation killed: a new ``User(..., is_superadmin=...)``
    anywhere in ``backend/app/`` — a platform-admin invitation acceptor, a
    second SSO provider callback — added without anyone weighing whether its
    predicate is the empty-table one. Invisible to every request-level test.

    Also red if an entry DISAPPEARS, which is how a "cleanup" that deletes the
    cold-install bootstrap gets caught.
    """
    sites = _collect()
    found = {(s.file, s.function) for s in sites}
    allowed = {(f, fn) for f, fn, _ in ALLOWED_WRITE_SITES}

    unexpected = sorted(found - allowed)
    missing = sorted(allowed - found)
    if not unexpected and not missing:
        return
    detail = "\n".join(
        f"  {s.file}::{s.function}:{s.lineno} {s.value_repr}"
        for s in sorted(sites, key=lambda s: (s.file, s.lineno))
    )
    assert not unexpected, (
        "NEW is_superadmin write site(s) — a new platform-flag grant is a "
        f"security change and needs an allowlist entry with a justification: "
        f"{unexpected}\nall sites found:\n{detail}"
    )
    assert not missing, (
        f"is_superadmin write site(s) VANISHED: {missing}. If a bootstrap "
        f"grant was deleted the install can no longer mint an operator.\n"
        f"all sites found:\n{detail}"
    )


def test_grant_sites_bind_the_single_bootstrap_predicate() -> None:
    """FENCE — the VALUE, not just the location. This is the load-bearing one.

    Wrong implementations killed:
      * ``is_superadmin=(existing_superadmin == 0)`` reintroduced INSIDE
        ``register`` or ``google_callback`` — the retired predicate, and the
        exact escalation TBD-365 removed. The writer-set test above stays
        GREEN against this, because the ``(file, function)`` pair is unchanged.
      * ``is_superadmin=True`` hardcoded at either grant site.
      * ``is_superadmin=<anything>`` other than ``False`` in
        ``accept_invitation`` — an org-scoped invitation writing the platform
        flag, on a route that is in the PUBLIC allowlist.

    A ``Compare`` node, a ``Call``, or a ``BoolOp`` at a grant site all fail
    here by construction: only a bare ``Name`` drawn from the approved set is
    accepted, which forces the predicate to be computed once, by name, from
    ``_is_first_user_setup``.
    """
    for site in _collect():
        if site.file == "services/invitation_service.py":
            assert site.value_repr == "Constant(False)", (
                f"{site.file}::{site.function}:{site.lineno} writes "
                f"is_superadmin={site.value_repr}. An org invitation must "
                "never write the platform flag — this route is public."
            )
            continue
        assert site.value_repr.startswith("Name("), (
            f"{site.file}::{site.function}:{site.lineno} binds "
            f"is_superadmin={site.value_repr}. The bootstrap grant must bind a "
            "single named predicate computed by _is_first_user_setup, never an "
            "inline expression — an inline count is how the retired flag-count "
            "predicate comes back."
        )
        bound = site.value_repr[len("Name(") : -1]
        assert bound in APPROVED_PREDICATE_NAMES, (
            f"{site.file}::{site.function}:{site.lineno} binds "
            f"is_superadmin={bound!r}, which is not one of the approved "
            f"bootstrap predicates {sorted(APPROVED_PREDICATE_NAMES)}. "
            "TBD-365 collapsed the two first-ness predicates into one; a new "
            "name here means a second one came back."
        )

        # PROVENANCE — the name alone is not enough. Pinning only the
        # identifier leaves `is_first_user_setup = <a flag count>` completely
        # green, because the keyword's value node never changes. Require the
        # name to be bound FROM the helper, and permit nothing else except the
        # literal `False` hoist that keeps the SSO local defined on every path.
        values = _assignments_to(site.file, site.function, bound)
        assert values, (
            f"{site.file}::{site.function} binds is_superadmin={bound!r} but "
            f"nothing in that function assigns {bound!r}; the predicate is "
            "coming from somewhere this fence cannot see."
        )
        assert any(_is_helper_call(v) for v in values), (
            f"{site.file}::{site.function} binds is_superadmin={bound!r}, but "
            f"{bound!r} is never assigned from _is_first_user_setup(). The "
            "bootstrap predicate must be computed by that helper and nothing "
            "else — an inline count reintroduces the retired flag-count "
            "predicate while leaving this keyword unchanged."
        )
        for v in values:
            ok = _is_helper_call(v) or (
                isinstance(v, ast.Constant) and v.value is False
            )
            assert ok, (
                f"{site.file}::{site.function} assigns {bound!r} from an "
                f"unapproved expression ({type(v).__name__}) at line "
                f"{getattr(v, 'lineno', '?')}. Only _is_first_user_setup() and "
                "the literal False hoist are permitted."
            )


def test_no_promote_or_demote_path_exists() -> None:
    """FENCE — the flag is set at construction ONLY.

    Wrong implementation killed: ``user.is_superadmin = <anything>`` anywhere
    in ``backend/app/``. Today no such statement exists, and several guards
    reason from that: ``user_merge_service`` refuses to merge a superadmin
    source into a NON-superadmin target (superadmin-to-superadmin IS permitted
    and is the only path that can decrement the count at all; it floors at 1
    because the target keeps the flag), and
    ``invitation_service``/``admin_users_service``/``admin_orgs_service`` all
    refuse to delete one. A demote path would make ``count(is_superadmin) ==
    0`` reachable on a live install for the first time.

    ⚠ This does NOT make the collapse safe — the collapse is what makes
    reaching zero harmless. It fences the REASONING the surrounding guards
    depend on, so that adding a demote endpoint is a deliberate act that
    fails CI first.
    """
    assigns = [s for s in _collect() if s.value_repr.startswith("assign:")]
    assert not assigns, (
        "a promote/demote path for is_superadmin was introduced: "
        + ", ".join(f"{s.file}::{s.function}:{s.lineno}" for s in assigns)
    )


def test_audit_outcomes_read_the_row_not_the_local() -> None:
    """FENCE — the audit payloads bind OUTCOMES to the stored row.

    ⚠ THIS IS THE FENCE THAT WAS MISSING. ``auth.py`` documents the audit row
    as a detection layer INDEPENDENT of this file: the AST fences above cannot
    see a second grant condition added inside an already-allowed function, but
    a row reporting what the account actually received can. That claim was
    made in three docstrings and a production comment with nothing holding it.

    Wrong implementation killed::

        "granted_superadmin": is_first_user_setup,   # restate the local

    ⚠ NO BEHAVIOURAL TEST CAN KILL THAT, and the reason is worth understanding.
    After TBD-365 there is, by construction, no state in which the local
    predicate and the stored flag differ — unless the constructor is ALSO
    mutated, in which case the count assertions in the divergent-state fences
    fire first and the audit assertions never discriminate. So
    ``detail["granted_superadmin"] == body["is_superadmin"]`` is green against
    the right AND the wrong implementation. It is a structural property or it
    is nothing.

    Accepted shapes are ``user.<attr>`` and ``bool(user.<attr>)``. A bare
    ``Name`` is refused precisely because that is what restating the local
    predicate looks like.
    """
    tracked = {"granted_superadmin", "email_verified_on_create"}
    checked: list[str] = []

    # ⚠ PASS-THROUGH HELPERS ARE EXEMPT FROM THE DICT CHECK, and this exemption
    # is load-bearing rather than a convenience. `_record_google_callback_created_user`
    # builds its payload from its own PARAMETERS, so its dict values are bare
    # Names by design; the row-read it must perform happens at the CALL SITE
    # and is pinned by the keyword branch below. Checking the dict there would
    # make this fence red against a correct implementation — the inverse
    # defect, and just as bad as a vacuous one.
    #
    # The exemption is safe ONLY because the helper takes those values as
    # parameters. If one ever acquires a default, or starts computing a value
    # itself, it stops being a pass-through and must come off this list.
    PASS_THROUGH = {"_record_google_callback_created_user"}

    for rel, tree in _parsed():
        if rel != "routers/auth.py":
            continue
        stack: list[str] = []

        def enclosing() -> str:
            return stack[-1] if stack else "__module__"

        def walk(node: ast.AST) -> None:
            pushed = False
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append(node.name)
                pushed = True
            _check(node, enclosing())
            for child in ast.iter_child_nodes(node):
                walk(child)
            if pushed:
                stack.pop()

        def _check(node: ast.AST, fn: str) -> None:
            if isinstance(node, ast.Dict) and fn not in PASS_THROUGH:
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value in tracked:
                        checked.append(f"dict:{k.value}@{getattr(v, 'lineno', '?')}")
                        assert _reads_user_attribute(v), (
                            f"routers/auth.py line {getattr(v, 'lineno', '?')}: "
                            f'audit key "{k.value}" is bound to a '
                            f"{type(v).__name__}, not to an attribute read off "
                            "`user`. An outcome slot must record what the row "
                            "ACTUALLY holds — restating the local predicate "
                            "lets the row certify an outcome the constructor "
                            "may not have produced, and deletes the only "
                            "detection layer independent of this file."
                        )
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in tracked:
                        checked.append(
                            f"kwarg:{kw.arg}@{getattr(kw.value, 'lineno', '?')}"
                        )
                        assert _reads_user_attribute(kw.value), (
                            "routers/auth.py line "
                            f"{getattr(kw.value, 'lineno', '?')}: audit argument "
                            f"`{kw.arg}` is bound to a "
                            f"{type(kw.value).__name__}, not to an attribute "
                            "read off `user`."
                        )

        walk(tree)

    # CONTROL — if the payloads are renamed or moved out of this module the
    # loop above silently checks nothing and passes green. Two outcome slots
    # per audit site, two sites.
    assert len(checked) >= 4, (
        "expected at least four outcome bindings (two per audit site) but "
        f"found {len(checked)}: {checked}. The payloads moved or were renamed "
        "and this fence stopped covering them."
    )


def test_expire_on_commit_is_false_on_the_production_session() -> None:
    """FENCE — the application session must not expire attributes on commit.

    Both bootstrap audit payloads read ``user.is_superadmin`` and
    ``user.email_verified`` AFTER ``db.commit()``. That is safe only while
    ``expire_on_commit=False``.

    ⚠ HARVEST SHAPE vs CONSUME SHAPE. Every test in this repo builds its OWN
    ``async_sessionmaker(..., expire_on_commit=False)``; none imports
    ``app.database.async_session``. Flipping the production setting would
    therefore raise MissingGreenlet on both bootstrap paths in production and
    pass the entire suite. This fence is the only thing that reads the real
    object, and it exists because the diff that added those reads promoted
    this setting to load-bearing.
    """
    from app.database import async_session

    kw = getattr(async_session, "kw", None)
    assert kw is not None, (
        "could not introspect the async_sessionmaker keywords. If SQLAlchemy "
        "changed its internals, REWRITE this fence rather than deleting it — "
        "the invariant it protects is load-bearing for the audit payloads."
    )
    assert kw.get("expire_on_commit") is False, (
        "app.database.async_session no longer sets expire_on_commit=False. "
        "The bootstrap audit rows read user attributes after commit and will "
        "raise MissingGreenlet in production; no other test can see this."
    )
