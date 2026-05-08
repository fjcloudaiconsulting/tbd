"""Structured-logging wrapper around `alembic upgrade`.

Goal: an operator triaging a deploy from the logs alone can answer
"did the migrate job do anything, and if so what?" without re-running it.

Behaviour vs. raw `alembic upgrade head`:
  * Same exit code (0 on success, alembic's exit code on failure, 1 on
    pre-flight/safety errors). PRE_DEPLOY contract preserved.
  * Same stdout / stderr from alembic. We do NOT capture, summarize, or
    reorder it; alembic's lines stream through unchanged. The only
    additions are extra structured JSON events emitted by THIS wrapper
    (via the existing structlog config in app.logging) before / between /
    after each per-revision subprocess invocation.
  * Multi-head detection. If ScriptDirectory.get_heads() returns more
    than one revision we refuse to act and emit migrate.failed with
    reason="multiple_heads"; alembic's own behaviour ("ambiguous") is
    surfaced as a structured event instead of a deep stack trace.
  * Per-step events let an operator see how many revisions ran, in which
    order, and how long each took.

env.py is intentionally NOT modified; this script drives alembic from the
outside via the public Python API + subprocess invocation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from typing import IO, Optional

import structlog
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.logging import setup_logging


ALEMBIC_INI = "/app/alembic.ini"


def _safe_url_fields(database_url: Optional[str]) -> dict[str, str]:
    """Return a small dict with dialect + database name only.

    NEVER includes username, password, host, or port. If we can't parse
    the URL we return an empty dict — silent is better than leaking.
    """
    if not database_url:
        return {}
    try:
        url = make_url(database_url)
    except Exception:
        return {}
    fields: dict[str, str] = {}
    if url.get_backend_name():
        fields["dialect"] = url.get_backend_name()
    if url.database:
        fields["database"] = url.database
    return fields


def _resolve_database_url(alembic_cfg: Config) -> Optional[str]:
    """Mirror env.py's resolution: DATABASE_URL env var wins."""
    return os.getenv("DATABASE_URL") or alembic_cfg.get_main_option(
        "sqlalchemy.url"
    )


def _get_current_revision_sync(database_url: str) -> Optional[str]:
    """Read alembic_version via a short-lived async engine."""

    def _get_rev(connection) -> Optional[str]:
        ctx = MigrationContext.configure(connection)
        return ctx.get_current_revision()

    async def _run() -> Optional[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(_get_rev)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _pump_stream(src: IO[str], dst: IO[str]) -> None:
    """Forward every line from src to dst as it arrives.

    Used to keep alembic's stdout / stderr separate without buffering
    past line granularity. Flush after each line so log collectors see
    output as it happens.
    """
    try:
        for line in iter(src.readline, ""):
            dst.write(line)
            dst.flush()
    finally:
        try:
            src.close()
        except Exception:
            pass


def _run_alembic_upgrade(revision: str) -> int:
    """Run `alembic upgrade <revision>` and stream its output.

    Returns the subprocess return code. stdout -> our stdout, stderr ->
    our stderr; nothing is captured.
    """
    proc = subprocess.Popen(
        ["alembic", "-c", ALEMBIC_INI, "upgrade", revision],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    # Threaded forwarders keep stdout and stderr from interleaving on a
    # single pipe; subprocess.communicate would also work but it buffers
    # until the process exits, which would hide the migration's own
    # progress lines until completion.
    stdout_thread = threading.Thread(
        target=_pump_stream, args=(proc.stdout, sys.stdout), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump_stream, args=(proc.stderr, sys.stderr), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    rc = proc.wait()
    stdout_thread.join()
    stderr_thread.join()
    return rc


def main() -> int:
    setup_logging()
    log = structlog.get_logger("migrate")

    alembic_cfg = Config(ALEMBIC_INI)
    database_url = _resolve_database_url(alembic_cfg)
    safe_url = _safe_url_fields(database_url)

    script = ScriptDirectory.from_config(alembic_cfg)
    heads = list(script.get_heads())

    # Multi-head guard: refuse to pick. One of those branches has to be
    # merged with `alembic merge` before deploy; auto-picking is exactly
    # the "silent wrong choice" we want logs to flag.
    if len(heads) > 1:
        log.error(
            "migrate.failed",
            revision=None,
            step_index=None,
            step_count=0,
            duration_ms=0,
            returncode=1,
            reason="multiple_heads",
            heads=heads,
            **safe_url,
        )
        return 1

    if not heads:
        # No revisions at all; alembic itself would no-op. Treat as no-op.
        log.info("migrate.no_op", revision=None, **safe_url)
        return 0

    head = heads[0]

    if not database_url:
        # Without a URL we can't read alembic_version; let alembic's own
        # error handling fire by delegating once.
        log.error(
            "migrate.failed",
            revision=None,
            step_index=None,
            step_count=0,
            duration_ms=0,
            returncode=1,
            reason="missing_database_url",
        )
        return 1

    try:
        current = _get_current_revision_sync(database_url)
    except Exception as exc:
        # Deliberately log only the exception class, not str(exc): driver
        # errors routinely embed username/host/port (e.g. pymysql's
        # "Access denied for user 'foo'@'10.x.x.x'"), which would defeat
        # the redaction guarantee on the migrate.failed event.
        log.error(
            "migrate.failed",
            revision=None,
            step_index=None,
            step_count=0,
            duration_ms=0,
            returncode=1,
            reason="unexpected_exception",
            error_type=type(exc).__name__,
            **safe_url,
        )
        return 1

    if current == head:
        log.info("migrate.no_op", revision=head, **safe_url)
        return 0

    # Pending revisions = everything strictly newer than `current` up to
    # and including `head`. iterate_revisions(head, current) returns
    # newest-first and excludes the lower bound, so a reverse gives us
    # apply order.
    pending = list(script.iterate_revisions(head, current))
    pending.reverse()
    step_count = len(pending)

    log.info(
        "migrate.start",
        from_revision=current,
        to_revision=head,
        step_count=step_count,
        **safe_url,
    )

    overall_start = time.monotonic()
    applied = 0
    for index, rev in enumerate(pending, start=1):
        description = (rev.doc or "").strip() or None
        log.info(
            "migrate.step.start",
            revision=rev.revision,
            step_index=index,
            step_count=step_count,
            description=description,
        )
        step_start = time.monotonic()
        try:
            rc = _run_alembic_upgrade(rev.revision)
        except Exception as exc:
            duration_ms = int((time.monotonic() - step_start) * 1000)
            # Same redaction concern as the bootstrap-time handler: don't
            # str(exc) onto the structured event.
            log.error(
                "migrate.failed",
                revision=rev.revision,
                step_index=index,
                step_count=step_count,
                duration_ms=duration_ms,
                returncode=1,
                reason="unexpected_exception",
                error_type=type(exc).__name__,
            )
            return 1

        duration_ms = int((time.monotonic() - step_start) * 1000)
        if rc != 0:
            log.error(
                "migrate.failed",
                revision=rev.revision,
                step_index=index,
                step_count=step_count,
                duration_ms=duration_ms,
                returncode=rc,
                reason="alembic_nonzero_exit",
            )
            return rc

        log.info(
            "migrate.step.end",
            revision=rev.revision,
            step_index=index,
            step_count=step_count,
            duration_ms=duration_ms,
            returncode=0,
        )
        applied += 1

    total_ms = int((time.monotonic() - overall_start) * 1000)
    log.info(
        "migrate.complete",
        from_revision=current,
        to_revision=head,
        applied_count=applied,
        duration_ms=total_ms,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
