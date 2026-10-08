"""Self-test of tests.app_routes.effective_routes on a synthetic app (INFRA-125)."""

from fastapi import APIRouter, Depends, FastAPI
from starlette.routing import Mount, Route

from tests.app_routes import effective_routes


def dep_outer():
    pass


def dep_inc():
    pass


def dep_own():
    pass


async def _ws(websocket):
    pass


async def _plain(request):
    pass


def test_effective_routes_apply_prefix_and_dependencies():
    inner = APIRouter()
    inner.add_api_route("/x", lambda: None, methods=["GET"], dependencies=[Depends(dep_own)])
    inner.add_api_websocket_route("/ws", _ws)
    inner.routes.append(Route("/plain", _plain))
    inner.routes.append(Mount("/m", routes=[]))
    outer = APIRouter()
    outer.include_router(inner, prefix="/in", dependencies=[Depends(dep_inc)])
    app = FastAPI()
    app.include_router(outer, prefix="/p", dependencies=[Depends(dep_outer)])

    by_path = {r.path: r for r in effective_routes(app)}

    api = by_path["/p/in/x"]
    calls = {d.call for d in api.dependant.dependencies}
    assert {dep_outer, dep_inc, dep_own} <= calls
    for path in ("/p/in/ws", "/p/in/plain", "/p/in/m"):
        assert path in by_path, (path, sorted(by_path))
