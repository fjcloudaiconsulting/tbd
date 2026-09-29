"""TBD-398: ``./pfv seed`` against an org that already has data REPLACES it,
after confirmation. It never appends a second dataset and never refuses.

Operator ruling, 2026-09-29. "Dirty" is any account or any billing period:
an account means a re-run would duplicate the dataset, and a billing period
alone (``get_current_period`` auto-creates one on first use) is enough for a
re-run's open-period POST to land a second ``end_date IS NULL`` row, the
``duplicate_open`` shape. Replacing goes through the existing audited, locked
``POST /api/v1/orgs/data/reset``, so a re-seed always starts from zero periods.

``seed.main`` cannot be driven in-process (see
``test_seed_billing_period_contract.py``), so the decision lives in
``seed.prepare_org`` and is driven here through an ``httpx.MockTransport``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

import seed

SEED_PY = Path(__file__).resolve().parents[1] / "seed.py"
RESET = "/api/v1/orgs/data/reset"
ORG = "Casa Jorge"  # deliberately NOT seed.USER["org_name"]


def _client(*, accounts=(), periods=(), reset_status=200):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        path = req.url.path
        if path == "/api/v1/accounts":
            return httpx.Response(200, json=list(accounts))
        if path == "/api/v1/settings/billing-periods":
            return httpx.Response(200, json=list(periods))
        if path == "/api/v1/auth/me":
            return httpx.Response(200, json={"org_name": ORG})
        if path == RESET:
            return httpx.Response(reset_status, json={"deleted_rows_by_table": {}})
        return httpx.Response(404)

    c = httpx.AsyncClient(base_url="http://seed.test", transport=httpx.MockTransport(handler))
    return c, calls


def _resets(calls):
    return [r for r in calls if r.method == "POST" and r.url.path == RESET]


def _never_asked(_prompt):
    raise AssertionError("prompted on a path that must not prompt")


# fence — kills: skipping the dirty check (append) on a clean org, i.e. a
# reset or a prompt on an org that has nothing to replace.
@pytest.mark.asyncio
async def test_clean_org_seeds_without_asking_or_resetting():
    c, calls = _client()
    async with c:
        assert await seed.prepare_org(c, {}, assume_yes=False, interactive=True, ask=_never_asked)
    assert _resets(calls) == []


# fence — kills: a dirty predicate that looks at accounts only. A lone open
# period is exactly what makes a re-run create a second open row.
@pytest.mark.asyncio
@pytest.mark.parametrize("accounts,periods", [
    ([{"id": 1}], []),
    ([], [{"id": 7, "start_date": "2026-09-25", "end_date": None}]),
])
async def test_dirty_org_confirmed_is_reset_with_the_real_org_name(accounts, periods):
    c, calls = _client(accounts=accounts, periods=periods)
    prompts = []
    async with c:
        ok = await seed.prepare_org(
            c, {"Authorization": "Bearer t"}, assume_yes=False, interactive=True,
            ask=lambda p: prompts.append(p) or "y",
        )
    assert ok is True
    assert len(prompts) == 1 and ORG in prompts[0]
    (reset,) = _resets(calls)
    # kills: building the phrase from SEED_ORG instead of the live org name
    assert json.loads(reset.content) == {"confirm_phrase": f"RESET {ORG}"}
    assert reset.headers["Authorization"] == "Bearer t"


# fence — kills: treating anything but an explicit yes as consent.
@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "n", "no", "nope", "Y es"])
async def test_dirty_org_declined_changes_nothing(answer):
    c, calls = _client(accounts=[{"id": 1}])
    async with c:
        ok = await seed.prepare_org(c, {}, assume_yes=False, interactive=True, ask=lambda _p: answer)
    assert ok is False
    assert _resets(calls) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
async def test_yes_answers_are_accepted(answer):
    c, calls = _client(accounts=[{"id": 1}])
    async with c:
        assert await seed.prepare_org(c, {}, assume_yes=False, interactive=True, ask=lambda _p: answer)
    assert len(_resets(calls)) == 1


# fence — kills: --yes being ignored (prompt anyway) on a dirty org.
@pytest.mark.asyncio
async def test_assume_yes_replaces_without_asking():
    c, calls = _client(accounts=[{"id": 1}])
    async with c:
        assert await seed.prepare_org(c, {}, assume_yes=True, interactive=False, ask=_never_asked)
    assert len(_resets(calls)) == 1


# fence — kills: appending silently when nobody can be asked (no TTY, no --yes).
@pytest.mark.asyncio
async def test_non_interactive_dirty_without_yes_exits_before_writing():
    c, calls = _client(accounts=[{"id": 1}])
    async with c:
        with pytest.raises(SystemExit) as exc:
            await seed.prepare_org(c, {}, assume_yes=False, interactive=False, ask=_never_asked)
    assert "--yes" in str(exc.value.code)
    assert _resets(calls) == []


# fence — kills: seeding on top of a reset that failed (e.g. 409
# reset_already_running), which is the append this ticket removes.
@pytest.mark.asyncio
async def test_failed_reset_raises_instead_of_seeding():
    c, _calls = _client(accounts=[{"id": 1}], reset_status=409)
    async with c:
        with pytest.raises(httpx.HTTPStatusError):
            await seed.prepare_org(c, {}, assume_yes=True, interactive=False, ask=_never_asked)


# fence — kills: a dirty check that trusts an error body as "empty".
@pytest.mark.asyncio
async def test_failed_dirty_probe_raises():
    def handler(_req):
        return httpx.Response(500, json={"detail": "boom"})

    async with httpx.AsyncClient(base_url="http://seed.test", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await seed.prepare_org(c, {}, assume_yes=True, interactive=False, ask=_never_asked)


# fence — the on-switch. Kills: prepare_org defined and tested but not called,
# or called AFTER the first write, or its answer ignored.
def test_main_prepares_the_org_before_the_first_write():
    tree = ast.parse(SEED_PY.read_text(encoding="utf-8"))
    main_fn = next(
        fn for fn in tree.body if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "main"
    )
    prepare_line = None
    for node in ast.walk(main_fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
                and isinstance(node.test.op, ast.Not)
                and isinstance(node.test.operand, ast.Await)
                and isinstance(node.test.operand.value, ast.Call)
                and getattr(node.test.operand.value.func, "id", None) == "prepare_org"
                and any(isinstance(s, ast.Return) for s in node.body)):
            prepare_line = node.lineno
    assert prepare_line, "main() must `if not await prepare_org(...): return`"

    writes = [
        node.lineno for node in ast.walk(main_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"post", "put", "patch", "delete"}
        and node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))
        and "/auth/" not in ast.unparse(node.args[0])
    ]
    assert writes and prepare_line < min(writes), (
        f"prepare_org (line {prepare_line}) must run before the first data write "
        f"(line {min(writes)})"
    )
