"""The routes an app actually serves, for the fences that enumerate them (INFRA-125).

FastAPI 0.137+ keeps each ``include_router`` as one lazy ``_IncludedRouter``
entry, so ``app.routes`` no longer lists the included routes. Every route
fence reads through ``effective_routes`` instead: FastAPI's own
``iter_route_contexts`` flattens the inclusions, and each ``RouteContext``
answers ``path``, ``methods``, ``endpoint``, ``name``, ``dependant`` and
``dependencies`` with the effective values (prefix and include-level
dependencies applied). Test the route type on ``.route``, the declared route
object: a ``RouteContext`` itself is never an ``APIRoute``.
"""

from fastapi.routing import RouteContext, iter_route_contexts


def effective_routes(app) -> list[RouteContext]:
    return list(iter_route_contexts(app.routes))
