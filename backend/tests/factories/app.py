"""Shared FastAPI test-app factory.

Almost every router test built its own ``_make_app``/``make_app`` helper that
did the same four things:

1. ``app = FastAPI()``
2. override ``get_db`` to yield a session from a test ``session_factory``
3. override ``get_current_user`` to return a seeded ``User``
4. (sometimes) override ``get_session_factory`` to return the factory itself
5. ``app.include_router(...)`` for the router(s) under test

``make_test_app`` collapses that boilerplate into one place while preserving
the *exact* override semantics each call site relied on. It deliberately makes
no assumptions a caller didn't ask for: nothing is overridden unless a value
was supplied.

Override contracts (kept byte-for-byte equivalent to the hand-rolled helpers):

- ``get_db`` →  ``async with session_factory() as session: yield session``
- ``get_session_factory`` →  returns the factory object itself
- ``get_current_user`` →  see the ``current_user`` parameter below

Example::

    from tests.factories import make_test_app
    from app.routers.accounts import router as accounts_router

    app = make_test_app(session_factory, routers=accounts_router,
                        current_user=admin_resolver)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Union

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User


# A current-user spec is one of:
#   - a ``User`` instance (returned as-is)
#   - a zero-arg callable returning a ``User`` (sync or async)
#   - a one-arg callable taking the session_factory, returning a ``User``
#     (sync or async) — the ``current_user_resolver(session_factory)`` shape
#   - ``None`` → do not override get_current_user (anonymous app)
CurrentUserSpec = Union[
    User,
    Callable[[], Union[User, Awaitable[User]]],
    Callable[[async_sessionmaker[AsyncSession]], Union[User, Awaitable[User]]],
    None,
]


def make_test_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    routers: Union[APIRouter, Iterable[APIRouter], None] = None,
    current_user: CurrentUserSpec = None,
    override_session_factory: bool = False,
) -> FastAPI:
    """Build a configured FastAPI app for a router test.

    Parameters
    ----------
    session_factory:
        The test ``async_sessionmaker`` (typically an in-memory aiosqlite
        engine). ``get_db`` is always overridden to yield from it.
    routers:
        A single ``APIRouter`` or an iterable of them to mount on the app.
        ``None`` mounts nothing (caller mounts later if needed).
    current_user:
        Controls the ``get_current_user`` override. See ``CurrentUserSpec``:

        - ``None`` (default): leave ``get_current_user`` un-overridden, so the
          app authenticates anonymously (the auth-route pattern).
        - a ``User``: return that instance.
        - a callable: invoked to resolve the user. If it accepts an argument
          it is called with ``session_factory`` (the ``current_user_resolver``
          /``current_user_factory`` shape); otherwise it is called with no
          args. The result is awaited if it is a coroutine. This covers both
          the "resolver closure" and "resolve inline from the factory" styles.
    override_session_factory:
        When True, also override ``get_session_factory`` to return the factory
        object itself (the shape used by routers that depend on it directly).
    """
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    if override_session_factory:
        async def override_get_session_factory() -> async_sessionmaker[AsyncSession]:
            return session_factory

        app.dependency_overrides[get_session_factory] = override_get_session_factory

    if current_user is not None:
        resolver = _make_current_user_resolver(session_factory, current_user)
        app.dependency_overrides[get_current_user] = resolver

    if routers is not None:
        if isinstance(routers, APIRouter):
            routers = [routers]
        for router in routers:
            app.include_router(router)

    return app


def _make_current_user_resolver(
    session_factory: async_sessionmaker[AsyncSession],
    current_user: CurrentUserSpec,
):
    """Build the ``get_current_user`` override coroutine for ``current_user``.

    Normalises the three accepted ``current_user`` shapes (instance, zero-arg
    callable, one-arg ``(factory)`` callable — each sync or async) into a
    single async override that returns a ``User``.
    """
    import inspect

    if isinstance(current_user, User):

        async def override_current_user() -> User:
            return current_user

        return override_current_user

    # It's a callable. Decide whether it wants the session_factory argument.
    try:
        sig = inspect.signature(current_user)
        wants_factory = len(sig.parameters) >= 1
    except (TypeError, ValueError):  # builtins / un-introspectable callables
        wants_factory = False

    async def override_current_user() -> User:
        result = current_user(session_factory) if wants_factory else current_user()
        if inspect.isawaitable(result):
            result = await result
        return result

    return override_current_user
