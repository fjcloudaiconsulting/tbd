"""Shared wait / teardown primitives for the broadcast drain tests (TBD-358).

``broadcast_service._DRAIN_TASKS`` holds the ONLY strong reference to an
in-flight drain task, so the GC cannot collect it mid-flight (the "Ruling 1"
comment above the set in ``broadcast_service.py`` says exactly this). A
fixture that calls ``.clear()`` on that set while a task is still PENDING
therefore does the one thing the set exists to prevent: it drops the
reference to a live task. The symptom is ``Task was destroyed but it is
pending!`` and, once the loop closes, ``RuntimeError: Event loop is closed``,
emitted by the GC through the loop's exception handler at a nondeterministic
moment — so it is attributed to whichever test happens to be running then,
not to the one that leaked. Clearing without cancelling is strictly WORSE
than doing nothing.

Two primitives, deliberately distinct:

``await_broadcast_drains``
    Wait for every tracked drain to run to COMPLETION. For a test that
    launched a drain and wants its effects to have landed. Never cancels.

``quiesce_broadcast_drains``
    Teardown. CANCEL every still-pending drain, AWAIT it so the cancellation
    is actually delivered and the coroutine's own cleanup runs (the drain
    body is an ``async with session_factory() as db``), and only THEN clear
    both registries.

⚠ Both are loop-agnostic on purpose. Measured 2026-08-09 in this container:
``TestClient`` does NOT share the pytest-asyncio test's event loop — it runs
the app on an anyio blocking portal in its own thread, and that portal's loop
is closed when the ``with TestClient(app)`` block exits (pending bare tasks
are cancelled there, which is why the ``TestClient`` tests do not leak ACROSS
tests). ``httpx.ASGITransport``, by contrast, runs the app in-process on the
test's own loop — which is why the ASGITransport-based concurrency test is
the one that actually leaves a pending task alive into fixture teardown.
A foreign-loop task cannot be ``await``ed (``got Future attached to a
different loop``) and cannot be cancelled from this thread without
``call_soon_threadsafe``, so these helpers ``await`` the tasks they own and
poll ``Task.done()`` for the rest.
"""
from __future__ import annotations

import asyncio
import time

from app.services import broadcast_service

DEFAULT_TIMEOUT = 5.0


def tracked_drain_tasks() -> list[asyncio.Task]:
    """Snapshot ``_DRAIN_TASKS``.

    Always iterate a snapshot: the done-callback discards from the live set
    the moment a task settles, which would mutate it under us.
    """
    return list(broadcast_service._DRAIN_TASKS)


async def await_broadcast_drains(
    *, timeout: float = DEFAULT_TIMEOUT
) -> list[asyncio.Task]:
    """Wait for every tracked drain to run to completion; never cancel.

    Returns every task observed, settled. Raises ``AssertionError`` on
    timeout rather than returning quietly — a drain that never finishes is a
    defect, not something to shrug off.

    Does NOT re-raise a drain's own exception: the drain wrapper already
    flips the broadcast to ``failed`` and the done-callback logs it, and the
    tests that care assert on those. Raising here would hijack them.
    """
    seen: dict[int, asyncio.Task] = {}
    deadline = time.monotonic() + timeout
    while True:
        for task in tracked_drain_tasks():
            seen[id(task)] = task
        pending = [t for t in seen.values() if not t.done()]
        if not pending:
            return list(seen.values())
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{len(pending)} broadcast drain task(s) still pending after "
                f"{timeout}s"
            )
        await asyncio.sleep(0.01)


async def quiesce_broadcast_drains(
    *, timeout: float = DEFAULT_TIMEOUT
) -> list[asyncio.Task]:
    """Cancel and await every still-pending drain, THEN clear the registries.

    Order matters in two directions:

    * within this function — cancel + await BEFORE ``.clear()``, so the
      strong reference is only dropped once the task is terminal; and
    * at the call site — this must run while the test's database is still
      alive. See the ``session_factory`` fixtures in the two broadcast test
      modules: the quiesce sits in their finalizer, immediately before
      ``engine.dispose()``.

    Returns the tasks it acted on so a fence can assert on them.
    """
    tasks = tracked_drain_tasks()
    running = asyncio.get_running_loop()
    ours = [t for t in tasks if t.get_loop() is running]
    theirs = [t for t in tasks if t.get_loop() is not running]

    for task in ours:
        task.cancel()
    for task in theirs:
        loop = task.get_loop()
        if task.done() or loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            # The foreign loop closed between the check and the call. Its
            # tasks went down with it; nothing left to cancel.
            pass

    if ours:
        # ``return_exceptions`` so one drain's error cannot abort the
        # teardown of the others; ``wait_for`` so a drain that swallows
        # ``CancelledError`` fails loudly instead of hanging the suite.
        await asyncio.wait_for(
            asyncio.gather(*ours, return_exceptions=True), timeout
        )

    deadline = time.monotonic() + timeout
    while any(not t.done() and not t.get_loop().is_closed() for t in theirs):
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.01)

    broadcast_service._ACTIVE_DRAINS.clear()
    broadcast_service._DRAIN_TASKS.clear()
    return tasks
