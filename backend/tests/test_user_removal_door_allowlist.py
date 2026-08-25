"""TBD-364 — source-level allowlists over the paths that can remove a user.

This repo's most predictive defect class is "a half-fix leaves a door": one
path is guarded while an equivalent path stays open, and CI is green the whole
time because the code is internally consistent. TBD-364 closed the only
org-admin-reachable door (``invitation_service.remove_member``). These fences
exist so the SIXTH door fails CI instead of shipping.

## What is deliberately NOT here, and why

An earlier draft also walked for ``<x>.is_active = False`` to pin the
soft-delete doors. It was cut, not fixed, for three measured reasons:

1. **Python's AST carries no type information.** ``plan.is_active = False``
   (``routers/plans.py``) and ``r.is_active = False``
   (``services/recurring_service.py``) are syntactically identical to the
   ``User`` write. They would have to be allowlisted as false positives, which
   makes the allowlist assert something other than what it is named after.
2. **It was blind to a door this ticket itself enumerated.**
   ``admin_org_members_service.update_member`` writes
   ``target.is_active = is_active`` — a variable, not a literal — so a
   literal-``False`` walk never sees it. Widening to any ``.is_active =``
   assignment then goes RED against correct code every time an unrelated
   Account/Scenario/Plan activation toggle is added.
3. **It duplicated shipped infrastructure.**
   ``tests/auth/test_sessions_invalidated_at_allowlist.py`` already walks a
   *User-unique* column at ``(file, function)`` granularity, bidirectionally,
   and its allowlist already contains exactly the two User soft-delete doors.
   Extend that file, not this one.

Residual gap, stated rather than papered over: a future door that soft-deletes
a User **without** stamping ``sessions_invalidated_at`` is caught by neither
file. Closing it needs a type-aware check (mypy/AST+symbol table), not this.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _iter_modules():
    for path in sorted(APP_ROOT.rglob("*.py")):
        yield path, ast.parse(path.read_text(), filename=str(path))


def _enclosing_functions(tree):
    """Map every node to its INNERMOST enclosing function name.

    ⚠ Innermost, deliberately. ``ast.walk`` is breadth-first, so an outer
    ``FunctionDef`` is visited before a nested one; using ``setdefault`` here
    would lock in the OUTERMOST name instead. That direction is the unsafe
    one: a new ``delete(User)`` added inside a nested helper within an
    already-allowlisted function would be attributed to the outer name and
    pass silently. Plain assignment lets the deeper (later-visited) function
    win, so a nested helper reports its own name, misses the allowlist, and
    fails loudly — which is the entire point of this file.
    """
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner[child] = node.name
    return owner


def _is_delete_call(node, *, arg_name: str) -> bool:
    """A ``delete(<arg_name>)`` call, whether imported bare or via a module.

    Matches ``delete(User)`` and ``sa.delete(User)``. Does NOT and cannot
    match ORM-style ``await db.delete(obj)`` — that takes an *instance*, and
    the AST carries no type information to tell a User instance from any
    other. See this test's module docstring.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (
        func.id if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute)
        else None
    )
    return (
        name == "delete"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == arg_name
    )


# ── F-8a — hard-delete sites ────────────────────────────────────────────────
#
# Each entry records the site's guard status, so adding one is a conscious act.
DELETE_USER_ALLOWLIST = {
    # Guarded: refuses a superadmin with code=CODE_USER_IS_SUPERADMIN.
    ("services/admin_users_service.py", "delete_user"),
    # Guarded (partial): refuses superadmin -> non-superadmin. Superadmin ->
    # superadmin only decrements the count, so it cannot reach zero.
    ("services/user_merge_service.py", "merge_users"),
    # GUARDED as of TBD-342: delete_org_cascade now REFUSES when the org holds
    # any superadmin (active or not), raising ConflictError with
    # code=CODE_ORG_HOLDS_SUPERADMIN. Previously unguarded — and that hazard
    # was masked, because the function raised on a RESTRICT foreign key before
    # it could destroy anything; repairing deletion is what made it reachable.
    # The own-org invariant still lives in the ROUTER, which is why F-8b pins
    # the caller set. (Formerly annotated "DELIBERATELY UNGUARDED — see
    # TBD-373"; TBD-373 was folded into TBD-342.)
    ("services/admin_orgs_service.py", "delete_org_cascade"),
}


def test_hard_delete_user_sites_match_the_allowlist():
    """A new ``delete(User)`` statement must be added here consciously.

    Kills: a fourth *statement-style* hard-delete path shipping with no
    superadmin guard.

    ⚠ Scope, stated honestly rather than overclaimed: this walks
    ``delete(User)`` / ``sa.delete(User)``. It CANNOT see ORM-style
    ``await db.delete(user_obj)`` — that takes an instance and the AST has no
    types — even though that idiom is used at 20+ sites elsewhere in ``app/``.
    Every current User hard-delete uses the statement form (all three import
    ``delete`` from sqlalchemy), so the allowlist is complete today; a future
    door written in the ORM form would evade this fence.
    """
    found = set()
    for path, tree in _iter_modules():
        owner = _enclosing_functions(tree)
        rel = path.relative_to(APP_ROOT).as_posix()
        for node in ast.walk(tree):
            if _is_delete_call(node, arg_name="User"):
                found.add((rel, owner.get(node, "<module>")))

    assert found == DELETE_USER_ALLOWLIST, (
        "delete(User) sites changed.\n"
        f"  added:   {sorted(found - DELETE_USER_ALLOWLIST)}\n"
        f"  removed: {sorted(DELETE_USER_ALLOWLIST - found)}\n"
        "A NEW site needs a superadmin guard (or a recorded reason it does "
        "not) before it is allowlisted here."
    )


# ── F-8b — delete_org_cascade CALLERS ───────────────────────────────────────
#
# ⚠ F-8a does NOT cover this, despite an earlier draft of the spec (and the
# first version of TBD-373) claiming it did. F-8a walks `delete(User)` CALL
# SITES; a second CALLER of delete_org_cascade adds zero such nodes and would
# sail through green. An AST fence pins the syntactic shape it walks for, not
# the semantic property it is named after.
DELETE_ORG_CASCADE_CALLERS = {
    ("routers/admin_orgs.py", "delete_org"),
}


def test_delete_org_cascade_has_exactly_one_caller():
    """TBD-373's re-rate trigger (a), mechanically enforced.

    `delete_org_cascade` hard-deletes every user in an org, superadmins
    included, with no guard of its own. It is safe ONLY because its single
    caller refuses to delete the actor's own org (`admin_orgs.py:244-248`) and
    `require_permission("orgs.manage")` means the actor is necessarily a
    superadmin — so the actor's own row is structurally outside the delete set
    and `count(is_superadmin)` can never reach zero.

    ⚠ TBD-365 RETIRED THE CONSEQUENCE, NOT THE GUARD. Driving
    `count(is_superadmin)` to zero no longer re-arms the bootstraps — they now
    count ROWS, not flags. What this guard still protects is the ability to
    administer the platform at all: there is no promotion endpoint, so an
    install that loses its last superadmin is recoverable only by direct SQL.

    ⚠ The proof above rests on `ROLE_PERMISSIONS = {}` (`permissions.py`)
    making `orgs.manage` superadmin-only. L4.8's platform-role editor is
    exactly what breaks that premise. The service-level guard added by TBD-342
    is independent of it and is what actually holds after L4.8.

    Kills: a self-serve org-closure endpoint, cleanup script, or scheduler
    sweep calling this service without the own-org guard.
    """
    found = set()
    for path, tree in _iter_modules():
        owner = _enclosing_functions(tree)
        rel = path.relative_to(APP_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Attribute)
                 and node.func.attr == "delete_org_cascade")
                or (isinstance(node.func, ast.Name)
                    and node.func.id == "delete_org_cascade")
            ):
                found.add((rel, owner.get(node, "<module>")))

    assert found == DELETE_ORG_CASCADE_CALLERS, (
        "delete_org_cascade caller set changed.\n"
        f"  added:   {sorted(found - DELETE_ORG_CASCADE_CALLERS)}\n"
        f"  removed: {sorted(DELETE_ORG_CASCADE_CALLERS - found)}\n"
        "A NEW caller must re-establish the own-org guard, or TBD-373 becomes "
        "a live privilege escalation. Re-rate it to P1 before allowlisting."
    )


# ── F-9 — rollback must precede the audit write ─────────────────────────────


def test_remove_member_rolls_back_before_writing_the_audit_row():
    """Ordering fence for `routers/org_members.py::remove_member`.

    The rollback releases this session's connection before
    ``record_audit_event`` draws a second one. Reversing the two is harmless on
    SQLite/StaticPool — both sessions share a single connection — and CI runs
    on aiosqlite, so **no behavioural test in this repo can observe the
    defect**. Measured: with the two swapped, all 56 behavioural fences stay
    green.

    ⚠ The cost of reversing them is NOT a deadlock (the service issued only
    plain SELECTs, which take no record locks under InnoDB consistent-snapshot
    reads). It is connection-pool amplification: the request holds its own
    connection while a second is drawn, doubling concurrent checkouts. Under a
    burst of refusals the pool exhausts, ``record_audit_event`` raises
    TimeoutError, and it SWALLOWS it — so the audit row disappears silently.

    This is the repo's documented MySQL-invisible-to-CI class, so it is pinned
    structurally rather than behaviourally.

    Kills: moving ``await db.rollback()`` after the ``record_audit_event`` call.
    """
    src = (APP_ROOT / "routers" / "org_members.py").read_text()
    tree = ast.parse(src)

    handler = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "remove_member"
    )
    conflict_handlers = [
        h for h in ast.walk(handler)
        if isinstance(h, ast.ExceptHandler)
        and h.type is not None
        and "ConflictError" in ast.unparse(h.type)
    ]
    assert len(conflict_handlers) == 1, "expected exactly one ConflictError handler"

    # Collect ALL line numbers for each call, then compare the strictest
    # pair: the LAST rollback must still precede the FIRST audit write.
    # ast.walk is breadth-first, not source order, so taking "whichever came
    # last in the walk" would be accidentally-correct only while there is
    # exactly one of each — a second record_audit_event added before the
    # rollback would otherwise slip through green.
    rollback_lines: list[int] = []
    audit_lines: list[int] = []
    for node in ast.walk(conflict_handlers[0]):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node.func)
        if rendered.endswith("db.rollback"):
            rollback_lines.append(node.lineno)
        elif rendered.endswith("record_audit_event"):
            audit_lines.append(node.lineno)

    assert rollback_lines, "no db.rollback() in the ConflictError handler"
    assert audit_lines, "no record_audit_event() in the ConflictError handler"
    last_rollback, first_audit = max(rollback_lines), min(audit_lines)
    assert last_rollback < first_audit, (
        f"db.rollback() (line {last_rollback}) must precede every "
        f"record_audit_event() (first at line {first_audit}) — see this test's "
        "docstring for why CI cannot catch the reversal behaviourally."
    )
