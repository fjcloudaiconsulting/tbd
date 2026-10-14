"""TBD-617: 4xx reason codes in the log, responses unchanged.

The reference app below has NO custom handler: it is what FastAPI answers by
default, so "byte-identical" is measured, not assumed.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.main import app
from app.rate_limit import limiter
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.usage_service import PlanLimitReached

SENT = "ZQ-SENTINEL-617"


class Body(BaseModel):
    n: int


async def dict_detail(item: str):
    raise HTTPException(404, detail={"code": "thing_missing", "echo": SENT})


async def text_detail(item: str):
    raise HTTPException(400, detail="echo " + SENT)


async def list_detail(item: str):
    raise HTTPException(422, detail=[{"msg": SENT}])


async def no_detail(item: str):
    raise HTTPException(403)


async def unauth(item: str):
    raise HTTPException(401, detail="nope " + SENT, headers={"WWW-Authenticate": "Bearer"})


async def server_error(item: str):
    raise HTTPException(503, detail={"code": "down"})


async def e404(item: str):
    raise NotFoundError("Thing " + SENT)


async def e400(item: str):
    raise ValidationError("bad " + SENT)


async def e402(item: str):
    raise PlanLimitReached("assistant.turns", 5, "day", None)


async def e409(item: str):
    raise ConflictError("clash " + SENT, code="budget_period_conflict")


async def e409_plain(item: str):
    raise ConflictError("clash " + SENT)


@limiter.limit("1/minute")
async def e429(request: Request, item: str):
    return {}


# handler-registered exceptions that are NOT HTTPException subclasses
SERVICE = {"nf": e404, "val": e400, "plan": e402, "conf": e409, "confplain": e409_plain,
           "rate": e429}


async def validated(body: Body):
    return {}


ROUTES = {"dict": dict_detail, "text": text_detail, "list": list_detail,
          "none": no_detail, "unauth": unauth, "five": server_error}


@pytest.fixture
def probe():
    ref = FastAPI()
    for name, fn in ROUTES.items():
        app.add_api_route(f"/__t617/{name}/{{item}}", fn)
        ref.add_api_route(f"/__t617/{name}/{{item}}", fn)
    for name, fn in SERVICE.items():
        app.add_api_route(f"/__t617/{name}/{{item}}", fn)
    app.add_api_route("/__t617/validated", validated, methods=["POST"])
    ref.add_api_route("/__t617/validated", validated, methods=["POST"])
    n = len(ROUTES) + len(SERVICE) + 1
    limiter.reset()
    yield ref
    limiter.reset()
    del app.router.routes[-n:]


async def _both(ref, method, path, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as a:
        got = await a.request(method, path, **kw)
    async with AsyncClient(transport=ASGITransport(app=ref), base_url="http://t") as r:
        want = await r.request(method, path, **kw)
    return got, want


@pytest.mark.parametrize("name,status", [
    ("dict", 404), ("text", 400), ("list", 422), ("none", 403), ("unauth", 401)])
async def test_response_is_byte_identical_to_the_default(probe, name, status):
    """FENCE H1. Wrong implementation: a handler that builds its own body or
    drops ``exc.headers`` (the 401 WWW-Authenticate)."""
    got, want = await _both(probe, "GET", f"/__t617/{name}/abc")
    assert got.status_code == want.status_code == status
    assert got.content == want.content
    for k, v in want.headers.items():
        assert got.headers[k] == v, k


async def test_4xx_logs_status_route_template_and_code_only(probe, logs):
    """FENCE H2. Wrong implementations: the concrete URL or query string instead
    of the route template; logging a detail's content; ``code`` from a non-dict."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for name in ("dict", "text", "list", "none", "unauth"):
            await c.get(f"/__t617/{name}/secret-item?q={SENT}")
        await c.get("/__t617/nowhere/at/all")  # unrouted 404
    ev = {e["route"]: e for e in logs.named("http.client_error") if e.get("route")}
    base = {"status": 404, "method": "GET", "route": "/__t617/dict/{item}"}
    assert {k: ev["/__t617/dict/{item}"][k] for k in base} == base
    assert ev["/__t617/dict/{item}"]["code"] == "thing_missing"
    for name, kind in (("text", "text"), ("list", "list"), ("none", "text"), ("unauth", "text")):
        e = ev[f"/__t617/{name}/{{item}}"]
        assert e["detail_kind"] == kind and "code" not in e, name
    unrouted = [e for e in logs.named("http.client_error") if e["status"] == 404 and e["route"] is None]
    assert len(unrouted) == 1
    text = logs.text()
    assert SENT not in text and "secret-item" not in text and "nowhere" not in text


async def test_5xx_is_not_logged_here(probe, logs):
    """FENCE H3. 5xx stays out of scope."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/__t617/five/x")).status_code == 503
        await c.get("/__t617/dict/x")
    assert [e["status"] for e in logs.named("http.client_error")] == [404]


async def test_validation_error_logs_count_and_field_names_only(probe, logs):
    """FENCE H4. Wrong implementation: logging the errors (they carry ``input``)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/__t617/validated", json={"n": SENT})
    assert r.status_code == 422
    (e,) = logs.named("http.validation_error")
    assert (e["count"], e["fields"], e["route"]) == (1, ["body.n"], "/__t617/validated")
    assert SENT not in logs.text()


@pytest.mark.parametrize("name,status,extra", [
    ("nf", 404, {"detail_kind": "text"}),
    ("val", 400, {"detail_kind": "text"}),
    ("plan", 402, {"code": "plan_limit_reached"}),
    ("conf", 409, {"code": "budget_period_conflict"}),
    ("confplain", 409, {"detail_kind": "text"}),
    ("rate", 429, {"detail_kind": "text"}),
])
async def test_every_4xx_path_logs_exactly_once(probe, logs, name, status, extra):
    """FENCE H5. Handlers registered for non-HTTPException classes (and slowapi's
    429, which wins over the HTTPException handler by MRO) bypass the generic
    handler. Wrong implementation: logging only in that one."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(2 if name == "rate" else 1):
            r = await c.get(f"/__t617/{name}/abc?q={SENT}")
    assert r.status_code == status
    (e,) = logs.named("http.client_error")
    assert (e["status"], e["route"], e["method"]) == (status, f"/__t617/{name}/{{item}}", "GET")
    assert {k: e[k] for k in extra} == extra and ("code" in e) == ("code" in extra)
    assert SENT not in logs.text()
    # slowapi's own "exceeded at endpoint" line carries the concrete path (pre-existing, not ours)
    assert name == "rate" or f"/__t617/{name}/abc" not in logs.text()


async def test_service_exception_responses_are_unchanged(probe):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/__t617/nf/x")).json() == {"detail": f"Thing {SENT} not found"}
        assert (await c.get("/__t617/val/x")).json() == {"detail": f"bad {SENT}"}
        assert (await c.get("/__t617/plan/x")).json()["detail"]["code"] == "plan_limit_reached"
        assert (await c.get("/__t617/conf/x")).json() == {
            "detail": f"clash {SENT}", "code": "budget_period_conflict"}
