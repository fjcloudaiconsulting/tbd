"""Route-bound agent tools (TBD-664).

``route_tool`` builds a ``ToolSpec`` from the ``APIRoute`` the tool mirrors, so
the tool runs the code REST runs instead of a second adapter over a service.

* ``fields`` (required, no default) is the allowlist of body and query fields
  the agent may set. The args model is the route's path params plus exactly
  those fields. Every set value is re-validated through the route's own body
  model, so its validators run (and normalise: the preview shows the stripped
  description the service stores). ``required`` makes a field mandatory; only
  a ``nullable`` field accepts null, because elsewhere the service reads null
  as "leave alone" and a preview of the null would lie.
* ``execute`` (``run`` for a read) calls ``route.endpoint`` in-process, so the
  handler's inline guards run. Its parameters come from a CLOSED dependency
  map: ``get_current_user`` is ``ctx.user`` and ``get_db`` is ``ctx.db``; a
  product-area gate must be the tool's ``product_area``; a role gate must not
  be stricter than ``min_role`` (``register`` re-checks both at invoke time).
  Anything else fails registration: an unknown dependency,
  ``require_interactive_session`` anywhere in the tree (browser-only), a raw
  ``Request`` / ``Response`` / background-tasks parameter, header or cookie
  params.
* An ``HTTPException`` 4xx from the handler is a ``ToolError``; so is a
  service ``ConflictError``. ``NotFoundError`` and ``ValidationError`` are
  mapped by ``registry.call_mapped``. A 5xx is not mapped (opaque internal).
* Previews. Three shapes get a templated preview; user text is disclosed as
  ``{"untrusted": ...}``, ``check`` adds the tool's refusals and ``present``
  its summary, context and any derived change (a balance, a rule):

  - a PUT or PATCH on one ``entity``: the row is read FOR UPDATE in the
    caller's org and each set field is listed with its current and new value
    (unchanged ones dropped; none left is ``no_change``);
  - a create (a POST with no path id, on one ``entity``): one insert,
    ``"new:1"``, naming every allowlisted field the route's body model
    resolves to a value (its defaults included);
  - a DELETE on one ``entity``: the row is read in the caller's org, the
    ``describe`` columns are named with their current value, and the tool's
    hand-written ``cascade`` (a :class:`CascadeDisclosure`) lists what the
    delete does beyond the row. Its ``covers`` must name every foreign key
    that references the table, or registration fails, so a new FK cannot go
    undisclosed.

  Every other route (a verb, a create of several rows) needs a hand-written
  ``preview``.

The tier (``risk``) is declared here and enforced by ``registry.register``,
which the caller still calls.
"""
from __future__ import annotations

import datetime
import enum
import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute, APIRouter
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model, model_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from app.agent.registry import (
    ROLE_RANK,
    UNTRUSTED_KEYS,
    Change,
    Preview,
    Risk,
    ToolContext,
    ToolError,
    ToolRegistrationError,
    ToolSpec,
)
from app.auth.org_permissions import require_org_admin, require_org_owner
from app.auth.pat import require_interactive_session
from app.database import get_db
from app.deps import get_current_user
from app.models.user import Role
from app.services.exceptions import ConflictError, NotFoundError
from app.services.feature_gate import Feature

Check = Callable[[ToolContext, Any, Any], Awaitable[None]]
Present = Callable[[ToolContext, Any, Any, list[Change]], Awaitable[Preview]]


@dataclass(frozen=True)
class CascadeDisclosure:
    """What a delete does beyond its own row, written by hand for each delete
    tool. ``covers`` names every foreign key referencing the deleted table as
    ``"table.column"`` (checked against the schema at registration);
    ``disclose(ctx, row, args)`` returns the Changes. ``locks``: ``disclose``
    takes the row locks itself, in its own order, so the template reads the
    row without one."""

    covers: frozenset[str]
    disclose: Callable[[ToolContext, Any, Any], Awaitable[list[Change]]]
    locks: bool = False


_TEMPLATED_METHODS = frozenset({"PUT", "PATCH"})
_RAW_PARAMS = ("request", "websocket", "http_connection", "response", "background_tasks", "security_scopes")
_HTTP_CODES = {
    400: "invalid_arguments", 403: "forbidden", 404: "not_found", 409: "conflict",
    422: "invalid_arguments",
}


def _user(ctx: ToolContext) -> Any:
    return ctx.user


def _db(ctx: ToolContext) -> Any:
    return ctx.db


def _none(ctx: ToolContext) -> None:
    return None


# The closed dependency map: what the handler receives in-process, by callable.
_SUPPLIED: dict[Callable[..., Any], Callable[[ToolContext], Any]] = {
    get_current_user: _user, get_db: _db,
}
_ROLE_GATES: dict[Callable[..., Any], Role] = {
    require_org_admin: Role.ADMIN, require_org_owner: Role.OWNER,
}


def _find_route(router: APIRouter, method: str, path: str) -> APIRoute:
    found = [
        r for r in router.routes
        if isinstance(r, APIRoute) and r.path == path and method in (r.methods or ())
    ]
    if len(found) != 1:
        raise ToolRegistrationError(f"no single {method} {path} on the given router")
    return found[0]


def _product_gate(call: Any) -> Feature | None:
    """The feature a ``feature_gate.require_feature(...)`` dependency checks."""
    if (getattr(call, "__module__", ""), getattr(call, "__qualname__", "")) != (
        "app.services.feature_gate", "require_feature.<locals>._dep"
    ):
        return None
    cells = dict(zip(call.__code__.co_freevars, (c.cell_contents for c in call.__closure__)))
    return cells["feature"]


def _all_calls(dep: Any) -> Iterable[Any]:
    for sub in dep.dependencies:
        yield sub.call
        yield from _all_calls(sub)


def _dependency_map(
    name: str, dep: Any, product_area: Feature | None, min_role: Role,
) -> dict[str, Callable[[ToolContext], Any]]:
    """Handler parameter name -> how to supply it from the tool context."""
    for raw in _RAW_PARAMS:
        if getattr(dep, f"{raw}_param_name", None):
            raise ToolRegistrationError(f"{name}: the handler takes a raw {raw} parameter")
    if dep.header_params or dep.cookie_params:
        raise ToolRegistrationError(f"{name}: the handler reads headers or cookies")
    if any(c is require_interactive_session for c in _all_calls(dep)):
        raise ToolRegistrationError(f"{name}: the route requires an interactive session")
    out: dict[str, Callable[[ToolContext], Any]] = {}
    for sub in dep.dependencies:
        call = sub.call
        feature = _product_gate(call)
        if call in _SUPPLIED:
            supply = _SUPPLIED[call]
        elif feature is not None:
            if feature != product_area:
                raise ToolRegistrationError(
                    f"{name}: the route's product area {feature.value} is not the tool's"
                )
            supply = _none
        elif call in _ROLE_GATES:
            need = _ROLE_GATES[call]
            if ROLE_RANK[min_role] < ROLE_RANK[need]:
                raise ToolRegistrationError(f"{name}: the route's role gate needs {need.value}")
            supply = _user
        else:
            raise ToolRegistrationError(
                f"{name}: unknown dependency "
                f"{getattr(call, '__module__', '?')}.{getattr(call, '__qualname__', call)}"
            )
        if sub.name:
            out[sub.name] = supply
    return out


def _without_none(annotation: Any) -> Any:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = typing.get_args(annotation)
        kept = tuple(m for m in members if m is not type(None))
        if len(kept) != len(members):
            return kept[0] if len(kept) == 1 else typing.Union[kept]
    return annotation


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


def _arg(info: Any, *, required: bool, nullable: bool) -> tuple[Any, Any]:
    annotation = info.annotation if nullable else _without_none(info.annotation)
    if info.metadata:
        annotation = Annotated[annotation, *info.metadata]
    if required:
        return annotation, Field(..., description=info.description)
    # Omitted means "leave alone". A non-nullable field does not advertise its
    # internal None default, which would invite the model to send a null.
    return annotation, Field(
        None, description=info.description, json_schema_extra=None if nullable else _no_default,
    )


def _disclose(column: Any, value: Any) -> Any:
    """A column value as a preview states it (the honesty fence's normal form)."""
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, Decimal):
        scale = getattr(column.type, "scale", None)
        return f"{value:.{scale}f}" if scale is not None else str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, str) and column.name in UNTRUSTED_KEYS:
        return {"untrusted": value}
    return value


def _validator(body_model: type[BaseModel], body_fields: Sequence[str]) -> Any:
    def _through_the_route(self: BaseModel) -> BaseModel:
        sent = {k: getattr(self, k) for k in body_fields if k in self.model_fields_set}
        try:
            body = body_model.model_validate(sent)
        except PydanticValidationError as exc:
            raise ValueError("; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'body'}: {e['msg']}" for e in exc.errors()
            )) from None
        for k in sent:  # the route's normal form (a stripped description, say)
            self.__dict__[k] = getattr(body, k)
        return self

    return model_validator(mode="after")(_through_the_route)


def route_tool(
    *,
    name: str,
    risk: Risk,
    router: APIRouter,
    method: str,
    path: str,
    fields: Sequence[str],
    product_area: Feature | None,
    min_role: Role,
    description: str,
    required: Sequence[str] = (),
    nullable: Sequence[str] = (),
    args_name: str | None = None,
    entity: Any = None,
    check: Check | None = None,
    present: Present | None = None,
    no_change: str = "nothing would change",
    preview: Callable[..., Awaitable[Preview]] | None = None,
    describe: Sequence[str] = (),
    cascade: CascadeDisclosure | None = None,
) -> ToolSpec:
    """A ``ToolSpec`` for ``method path`` on ``router`` (see the module doc)."""
    route = _find_route(router, method, path)
    dep = route.dependant
    fields = tuple(fields)
    if not set(required) <= set(fields) or not set(nullable) <= set(fields):
        raise ToolRegistrationError(f"{name}: required/nullable must name allowlisted fields")
    if len(dep.body_params) > 1:
        raise ToolRegistrationError(f"{name}: the route takes more than one body")
    if not inspect.iscoroutinefunction(route.endpoint):
        raise ToolRegistrationError(f"{name}: the handler is not a coroutine function")
    supplied = _dependency_map(name, dep, product_area, min_role)

    body_param = dep.body_params[0] if dep.body_params else None
    body_model = body_param.field_info.annotation if body_param else None
    if body_model is not None and not (isinstance(body_model, type) and issubclass(body_model, BaseModel)):
        raise ToolRegistrationError(f"{name}: the route body is not a model")
    body_infos = dict(body_model.model_fields) if body_model else {}
    query = {q.alias: q for q in dep.query_params}
    for f in fields:
        if f not in body_infos and f not in query:
            raise ToolRegistrationError(f"{name}: {f!r} is not a field of {method} {path}")
    missing = [k for k, i in body_infos.items() if i.is_required() and k not in fields]
    missing += [k for k, q in query.items() if q.field_info.is_required() and k not in fields]
    if missing:
        raise ToolRegistrationError(f"{name}: the route requires {missing}, outside the allowlist")

    defs: dict[str, Any] = {
        p.name: _arg(p.field_info, required=True, nullable=True) for p in dep.path_params
    }
    for f in fields:
        info = body_infos[f] if f in body_infos else query[f].field_info
        defs[f] = _arg(info, required=f in required, nullable=f in nullable)
    body_fields = [f for f in fields if f in body_infos]
    validators = {"_through_the_route": _validator(body_model, body_fields)} if body_fields else {}
    args = create_model(
        args_name or "".join(w.title() for w in name.split("_")) + "Args",
        __config__=ConfigDict(extra="forbid"), __validators__=validators, **defs,
    )

    path_names = [p.name for p in dep.path_params]
    response = TypeAdapter(route.response_model) if route.response_model is not None else None

    async def _call_handler(ctx: ToolContext, a: Any) -> Any:
        kwargs = {k: supply(ctx) for k, supply in supplied.items()}
        kwargs.update({p: getattr(a, p) for p in path_names})
        for alias, q in query.items():
            kwargs[q.name] = getattr(a, alias) if alias in fields else q.field_info.default
        if body_param is not None:
            kwargs[body_param.name] = body_model.model_validate(
                {k: getattr(a, k) for k in body_fields if k in a.model_fields_set}
            )
        try:
            out = await route.endpoint(**kwargs)
        except HTTPException as exc:
            if 400 <= exc.status_code < 500:
                detail = exc.detail if isinstance(exc.detail, str) else ""
                raise ToolError(_HTTP_CODES.get(exc.status_code, "refused"), detail) from None
            raise
        except ConflictError as exc:
            raise ToolError("conflict", exc.detail) from None
        if response is None:
            return jsonable_encoder(out)
        return response.dump_python(response.validate_python(out, from_attributes=True), mode="json")

    if risk == "read":
        return ToolSpec(
            name=name, risk=risk, args=args, product_area=product_area, min_role=min_role,
            mirrors_route=(method, path), description=description, run=_call_handler,
            route=route,
        )

    if preview is None and method == "DELETE":
        preview = _delete_preview(name, entity, path_names, describe, cascade, check, present)
    elif preview is None and method == "POST" and not path_names and entity is not None:
        preview = _create_preview(name, entity, fields, body_model, body_fields, check, present)
    elif preview is None:
        preview = _templated_preview(
            name, method, path, entity, fields, path_names, check, present, no_change,
        )
    return ToolSpec(
        name=name, risk=risk, args=args, product_area=product_area, min_role=min_role,
        mirrors_route=(method, path), description=description, preview=preview,
        execute=_call_handler, route=route,
    )


def _templated_preview(
    name: str, method: str, path: str, entity: Any, fields: Sequence[str],
    path_names: Sequence[str], check: Check | None, present: Present | None, no_change: str,
) -> Callable[[ToolContext, Any], Awaitable[Preview]]:
    if method not in _TEMPLATED_METHODS:
        raise ToolRegistrationError(f"{name}: a {method} route needs a hand-written preview")
    if entity is None or len(path_names) != 1:
        raise ToolRegistrationError(
            f"{name}: a templated preview needs an entity and exactly one id in the path"
        )
    table = entity.__table__
    if "org_id" not in table.c:
        raise ToolRegistrationError(f"{name}: {table.name} is not org-scoped")
    unknown = [f for f in fields if f not in table.c]
    if unknown:
        raise ToolRegistrationError(f"{name}: {unknown} is not a column of {table.name}")
    (id_name,) = path_names
    label = entity.__name__

    async def _preview(ctx: ToolContext, args: Any) -> Preview:
        row = await ctx.db.scalar(
            select(entity)
            .where(entity.id == getattr(args, id_name), entity.org_id == ctx.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError(label)
        if check is not None:
            await check(ctx, row, args)
        changes = []
        for f in fields:
            if f not in args.model_fields_set:
                continue
            before, after = _disclose(table.c[f], getattr(row, f)), _disclose(table.c[f], getattr(args, f))
            if before != after:
                changes.append(Change(table.name, row.id, f, before, after))
        if not changes:
            raise ToolError("no_change", no_change)
        if present is not None:
            return await present(ctx, row, args, changes)
        return Preview(
            summary=f"Change {', '.join(c.field for c in changes)} of {label.lower()} {row.id}",
            changes=changes,
        )

    return _preview


def _referencing(table: Any) -> frozenset[str]:
    """Every foreign key in the schema that references ``table``."""
    import app.models  # noqa: F401  (every table on the metadata)
    from app.models.base import Base

    return frozenset(
        f"{t.name}.{fk.parent.name}"
        for t in Base.metadata.sorted_tables for fk in t.foreign_keys
        if fk.column.table is table
    )


def _create_preview(
    name: str, entity: Any, fields: Sequence[str], body_model: Any, body_fields: Sequence[str],
    check: Check | None, present: Present | None,
) -> Callable[[ToolContext, Any], Awaitable[Preview]]:
    table = entity.__table__
    unknown = [f for f in fields if f not in table.c or f not in body_fields]
    if body_model is None or unknown:
        raise ToolRegistrationError(f"{name}: {unknown} is not a body column of {table.name}")
    if "org_id" not in table.c:
        raise ToolRegistrationError(f"{name}: {table.name} is not org-scoped")
    label = entity.__name__

    async def _preview(ctx: ToolContext, args: Any) -> Preview:
        if check is not None:
            await check(ctx, None, args)
        # What the route will insert: the set fields through its body model,
        # whose defaults fill the rest.
        body = body_model.model_validate(
            {k: getattr(args, k) for k in body_fields if k in args.model_fields_set}
        )
        changes = []
        for f in fields:
            value = _disclose(table.c[f], getattr(body, f))
            if value is not None:
                changes.append(Change(table.name, "new:1", f, None, value))
        if present is not None:
            return await present(ctx, None, args, changes)
        return Preview(summary=f"Create a {label.lower()}", changes=changes)

    return _preview


def _delete_preview(
    name: str, entity: Any, path_names: Sequence[str], describe: Sequence[str],
    cascade: CascadeDisclosure | None, check: Check | None, present: Present | None,
) -> Callable[[ToolContext, Any], Awaitable[Preview]]:
    if entity is None or len(path_names) != 1:
        raise ToolRegistrationError(
            f"{name}: a templated delete needs an entity and exactly one id in the path"
        )
    table = entity.__table__
    if "org_id" not in table.c:
        raise ToolRegistrationError(f"{name}: {table.name} is not org-scoped")
    if not describe or any(c not in table.c for c in describe):
        raise ToolRegistrationError(f"{name}: describe must name columns of {table.name}")
    if cascade is None:
        raise ToolRegistrationError(f"{name}: a delete carries a hand-written FK-cascade disclosure")
    if set(cascade.covers) != _referencing(table):
        raise ToolRegistrationError(
            f"{name}: the cascade disclosure covers {sorted(cascade.covers)}, "
            f"but the foreign keys referencing {table.name} are {sorted(_referencing(table))}"
        )
    (id_name,) = path_names
    label = entity.__name__

    async def _preview(ctx: ToolContext, args: Any) -> Preview:
        query = (
            select(entity)
            .where(entity.id == getattr(args, id_name), entity.org_id == ctx.org_id)
            .execution_options(populate_existing=True)
        )
        row = await ctx.db.scalar(query if cascade.locks else query.with_for_update())
        if row is None:
            raise NotFoundError(label)
        if check is not None:
            await check(ctx, row, args)
        beyond = await cascade.disclose(ctx, row, args)
        # Named after the disclosure, which may have re-read the row locked.
        changes = [
            Change(table.name, row.id, c, _disclose(table.c[c], getattr(row, c)), None)
            for c in describe
        ] + beyond
        if present is not None:
            return await present(ctx, row, args, changes)
        return Preview(
            summary=f"Delete {label.lower()} {row.id}", changes=changes,
            warnings=["A delete cannot be undone."],
        )

    return _preview
