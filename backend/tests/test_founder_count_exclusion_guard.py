"""TBD-371 — the founder-count exclusion list must not be published in source,
and an empty list in production must be observable.

⚠⚠ THIS FILE DELIBERATELY DOES NOT CONTAIN THE SMOKE ACCOUNT USERNAME.

The obvious fence -- "grep the tree and assert the literal is absent" -- would
have to embed the literal in order to search for it, and a test file is source.
It would republish the exact string this ticket exists to unpublish, in a file
whose name advertises what it is. So the fences below assert *structural* and
*behavioural* properties, and name nothing.

⚠ WHY THERE IS NO "REFUSES TO BOOT PRODUCTION" FENCE HERE.
An earlier draft of this change added a ``model_validator`` that raised in
production when the list was empty, copying ``_validate_api_token_hmac_key``.
The full backend suite rejected it: 7 tests in ``test_api_token_config.py``
construct a production ``Settings`` and would each have had to start supplying
an unrelated founder-count value, a tax on every future production-Settings
test. The design was also disproportionate on its own terms -- an unset value
costs one wrong integer on a marketing counter, while a boot refusal costs the
whole application. ``api_token_hmac_key`` earns its refusal because losing it
breaks PAT authentication. This does not. The guard is an ERROR log instead.

The behavioural half of this fence (production + an empty list must emit
``public.founder_count.no_exclusions``) lives in
``tests/routers/test_public_stats.py``, beside the endpoint's own session
fixture and app factory rather than duplicating them here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[1] / "app" / "config.py"
_FIELD = "founder_count_exclude_usernames"


def _field_default() -> ast.expr | None:
    """Return the AST node for the field's default, parsing rather than grepping.

    A grep for ``founder_count_exclude_usernames: str = ""`` is satisfied by a
    comment or a docstring mentioning it -- this repo has been bitten by that
    (``reference_grep_is_not_parsing``). Walk the class body instead.
    """
    tree = ast.parse(_CONFIG_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == _FIELD
            ):
                return stmt.value
    pytest.fail(f"{_FIELD} is not declared on Settings — did it get renamed?")


def test_f1_field_carries_no_published_default():
    """F1. The default must be the empty string.

    Mutant this kills: someone restores ``= "<some-username>"`` as the default,
    republishing the production smoke account in source. ANY non-empty string
    literal fails, so the fence never needs to know which username it was.
    """
    default = _field_default()

    assert default is not None, (
        f"{_FIELD} must declare an explicit empty default. A bare required "
        "field would break every dev and CI run — nothing sets this variable "
        "for the test shards."
    )
    assert isinstance(default, ast.Constant), (
        f"{_FIELD} default must be a plain literal, got {type(default).__name__}"
    )
    assert default.value == "", (
        f"{_FIELD} must default to the empty string, not {default.value!r}. "
        "This field names the production post-deploy smoke account, which "
        "docs/operations/DEPLOYMENT.md requires to have NO MFA — publishing it as a source "
        "default hands out a confirmed-valid, MFA-less target (TBD-371). "
        "Supply it from the environment instead."
    )
