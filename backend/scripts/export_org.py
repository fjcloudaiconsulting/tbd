"""Operator CLI: export one org's data as NDJSON (TBD-222).

Usage
-----
::

    python -m scripts.export_org --org-id 42 --out /tmp/org-42.ndjson
    python -m scripts.export_org --org-id 42 --stdout > org-42.ndjson
    python -m scripts.export_org --org-id 42 --dry-run

⚠ Per ``reference_prod_db_readonly_access.md`` prod reads are
operator-authorized and an agent cannot ``doctl compute ssh``. ``--stdout``
exists so this is runnable as a DigitalOcean console job with the operator
redirecting to a file; every diagnostic goes to **stderr** so the redirect
captures only the artifact.

Partial files
-------------
File mode writes ``<out>.part`` and ``os.rename``s to ``<out>`` only after
the trailer has flushed. A crashed or killed run therefore never leaves a
file under the final name, and the ``.part`` it does leave has no terminal
trailer — so ``verify_export`` rejects it. Two independent signals of the
same truncation.

Audit
-----
Writes an ``org.data.exported`` audit row on the independent-session pattern.
⚠ ``sha256`` and ``row_counts_by_table`` are load-bearing: they are what
later proves the artifact handed to a data subject matches what was
generated. Row COUNTS, never row contents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.database import async_session, engine
from app.models.user import Organization
from app.services.audit_service import record_audit_event
from app.services.export_registry import REGISTRY_VERSION, excluded_reasons
from app.services.org_export_service import (
    ExportTooLarge,
    preflight_counts,
    stream_org_export,
)


def _log(message: str) -> None:
    """Diagnostics go to stderr so ``--stdout`` stays a clean artifact."""
    print(message, file=sys.stderr, flush=True)


async def _org_name(org_id: int) -> Optional[str]:
    async with async_session() as db:
        return (
            await db.execute(
                select(Organization.name).where(Organization.id == org_id)
            )
        ).scalar_one_or_none()


async def _run(args: argparse.Namespace) -> int:
    org_name = await _org_name(args.org_id)
    if org_name is None:
        _log(f"error: no organization with id {args.org_id}")
        return 2

    if args.dry_run:
        async with async_session() as db:
            counts = await preflight_counts(db, org_id=args.org_id)
        total = sum(counts.values())
        _log(f"org {args.org_id} ({org_name}): {total} rows across {len(counts)} tables")
        for table, count in sorted(counts.items()):
            _log(f"  {table:38s} {count}")
        return 0

    part_path = f"{args.out}.part" if args.out else None
    sink = sys.stdout.buffer if part_path is None else open(part_path, "wb")

    byte_size = 0
    # Only the LAST line is retained, and only to read its counts. The point
    # of streaming is that nothing here accumulates the document.
    last_line: bytes = b""
    try:
        async with async_session() as db:
            async for line in stream_org_export(
                db,
                org_id=args.org_id,
                org_name=org_name,
                max_rows=args.max_rows,
                max_bytes=args.max_bytes,
            ):
                sink.write(line)
                byte_size += len(line)
                last_line = line
        sink.flush()
    except ExportTooLarge as exc:
        _log(f"refused: {exc}")
        if part_path:
            sink.close()
            os.unlink(part_path)
        return 3
    finally:
        if part_path and not sink.closed:
            sink.close()

    trailer = json.loads(last_line) if last_line else {}
    if trailer.get("record") != "trailer":
        # ⚠ Do NOT rename the .part into place. An artifact whose last line
        # is not a trailer is by definition incomplete, and the whole design
        # rests on a partial file never appearing under the final name.
        _log("error: stream ended without a trailer; artifact is incomplete")
        return 4

    if part_path:
        os.rename(part_path, args.out)
        _log(f"wrote {args.out} ({byte_size} bytes)")

    _log(
        f"org {args.org_id}: {trailer['total_rows']} rows, "
        f"sha256={trailer['sha256_body']}"
    )

    await record_audit_event(
        async_session,
        event_type="org.data.exported",
        actor_user_id=None,
        actor_email=args.operator,
        target_org_id=args.org_id,
        target_org_name=org_name,
        request_id=None,
        ip_address=None,
        outcome="success",
        detail={
            "operator": args.operator,
            "org_id": args.org_id,
            "org_name": org_name,
            "row_counts_by_table": trailer["tables"],
            # Read from the registry rather than the artifact so the audit
            # row records the RULES that were applied, not the file.
            "excluded_tables": sorted(excluded_reasons()),
            "byte_size": byte_size,
            "sha256": trailer["sha256_body"],
            "registry_version": REGISTRY_VERSION,
        },
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_org", description="Export one org's data as NDJSON."
    )
    parser.add_argument("--org-id", type=int, required=True)
    parser.add_argument("--out", help="destination path; writes <out>.part first")
    parser.add_argument(
        "--stdout", action="store_true", help="stream to stdout (DO console job)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print per-table counts and exit"
    )
    parser.add_argument(
        "--operator",
        default=os.environ.get("EXPORT_OPERATOR", "operator@localhost"),
        help="operator identity recorded in the audit row",
    )
    parser.add_argument("--max-rows", type=int, default=settings.export_max_rows)
    parser.add_argument("--max-bytes", type=int, default=settings.export_max_bytes)
    args = parser.parse_args(argv)

    if not args.dry_run and not args.out and not args.stdout:
        parser.error("one of --out, --stdout or --dry-run is required")
    if args.out and args.stdout:
        parser.error("--out and --stdout are mutually exclusive")

    try:
        return asyncio.run(_run(args))
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
