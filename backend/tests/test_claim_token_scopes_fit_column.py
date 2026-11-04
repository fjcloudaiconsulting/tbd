"""Every ``state_db.claim_token`` scope fits ``used_tokens.scope`` (TBD-654).

The column is ``ascii_id(32)``. MySQL strict mode refuses a longer value on
every request while SQLite stores it, so the shards cannot see the defect.
This scans ``backend/app`` for ``claim_token(...)`` calls and resolves each
scope (the first argument or ``scope=``), a string literal or a module-level
string constant.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.models.session_state import UsedToken

APP = Path(__file__).resolve().parents[1] / "app"


def _trees() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text())) for p in sorted(APP.rglob("*.py"))]


def _string_constants(trees) -> dict[str, set[str]]:
    consts: dict[str, set[str]] = {}
    for _, tree in trees:
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts.setdefault(target.id, set()).add(node.value.value)
    return consts


def _claim_scopes() -> list[tuple[str, int, str]]:
    trees = _trees()
    consts = _string_constants(trees)
    found: list[tuple[str, int, str]] = []
    for path, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name != "claim_token":
                continue
            scope_kw = [kw.value for kw in node.keywords if kw.arg == "scope"]
            assert node.args or scope_kw, f"{path.relative_to(APP)}:{node.lineno}: claim_token without a scope"
            arg = node.args[0] if node.args else scope_kw[0]
            where = f"{path.relative_to(APP)}:{node.lineno}"
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((where, node.lineno, arg.value))
                continue
            ident = arg.id if isinstance(arg, ast.Name) else getattr(arg, "attr", None)
            values = consts.get(ident or "")
            assert values, (
                f"{where}: claim_token scope {ast.dump(arg)} is not a string literal "
                "or a module-level string constant, so its length cannot be checked"
            )
            found.extend((where, node.lineno, v) for v in values)
    return found


def test_every_claim_token_scope_fits_the_column():
    width = UsedToken.__table__.c.scope.type.length
    assert width == 32
    scopes = _claim_scopes()
    too_long = [(where, s) for where, _, s in scopes if len(s) > width]
    assert not too_long, f"claim_token scopes longer than {width}: {too_long}"


def test_scan_is_not_vacuous():
    scopes = {s for _, _, s in _claim_scopes()}
    assert {"password_reset", "signup", "signup_mail_10m", "signup_mail_24h"} <= scopes



def test_keyword_scope_is_read(monkeypatch, tmp_path):
    """``claim_token(scope=...)`` is checked like a positional scope."""
    import sys

    long_scope = "s" * 33
    (tmp_path / "m.py").write_text(
        'LONG = "' + long_scope + '"\n'
        'state_db.claim_token(scope="short", token="t", ttl_seconds=1)\n'
        "state_db.claim_token(token=\"t\", scope=LONG, ttl_seconds=1)\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "APP", tmp_path)
    assert sorted(s for _, _, s in _claim_scopes()) == ["short", long_scope]
