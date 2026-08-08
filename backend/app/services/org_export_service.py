"""Streaming NDJSON export of one org's data (TBD-222).

Format — NDJSON with a TERMINAL trailer
---------------------------------------
::

    line 1      {"record":"header","schema_version":1,...}
    lines 2..N  {"record":"row","table":"transactions","data":{...}}
    last line   {"record":"trailer","tables":{...},"total_rows":N,
                 "sha256_body":"<hex>","complete":true}

**The trailer is the contract.** A file is complete iff (a) its last line
parses as ``record == "trailer"``, (b) the per-table counts match the rows
actually observed, and (c) ``sha256_body`` matches the hash of every
preceding byte.

⚠ A manifest-FIRST design was rejected. A truncated file still parses and
still *looks* complete when its completeness record is at the top. The
completeness record must be the thing truncation destroys. That is the whole
reason the trailer is last.

Why streaming at all
--------------------
Production is a single ``basic-xxs`` instance (~512 MB, one uvicorn process,
no second replica to absorb an OOM). Buffering a full-org serialization
risks taking the box down. So the hash is computed **incrementally**
(``hashlib.update()`` per line as it ships) — integrity without ever holding
the document in memory. Nothing here builds a list of all rows, and nothing
calls ``json.dumps`` on the document.

Bounding
--------
``max_rows`` is a pre-flight ``COUNT(*)`` over included tables. ⚠ A row count
does not bound bytes — ``transactions.description`` is free text and several
columns are unbounded JSON. So ``max_bytes`` is ALSO carried and checked
incrementally against cumulative encoded output, aborting the moment it is
crossed. Bytes is what kills the box, so bytes is what to count.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import enum
import hashlib
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable

from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app._time import utcnow_naive
from app.models.base import Base
from app.services.export_registry import (
    EXPORT_DISPOSITION,
    REGISTRY_VERSION,
    Include,
    OrgColumn,
    Via,
    excluded_reasons,
    included_tables,
)


SCHEMA_VERSION = 1

# Rows fetched per round trip. Bounds the driver's buffer, not the export.
_YIELD_PER = 1000


class ExportError(Exception):
    """Base for export refusals."""


class ExportTooLarge(ExportError):
    """The org exceeds a configured bound; use the email channel instead."""


# ── Encoding ───────────────────────────────────────────────────────────────


def _json_default(value: Any) -> Any:
    """Coerce DB-native types the stdlib encoder cannot represent.

    Decimals become strings, never floats: an export whose amounts have been
    through binary floating point is not a faithful copy of the ledger.
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"un-encodable value of type {type(value).__name__}")


def _encode_line(obj: dict[str, Any]) -> bytes:
    """One NDJSON line, terminated. ``sort_keys`` keeps output diffable."""
    return (
        json.dumps(obj, default=_json_default, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


# ── Scoping ────────────────────────────────────────────────────────────────


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def scope_predicate(table_name: str, org_id: int) -> ColumnElement[bool]:
    """Build the WHERE clause that confines ``table_name`` to ``org_id``.

    ⚠ There is deliberately no fallback to ``org_id`` for tables that lack
    the column. A missing disposition raises rather than silently emitting
    an unfiltered read — the failure mode this whole subsystem exists to
    prevent is a cross-tenant leak that looks like a successful export.
    """
    disposition = EXPORT_DISPOSITION.get(table_name)
    if not isinstance(disposition, Include):
        raise KeyError(f"{table_name!r} has no Include disposition")

    table = _table(table_name)
    scope = disposition.scope

    if isinstance(scope, OrgColumn):
        return table.c[scope.column] == org_id

    if isinstance(scope, Via):
        parent = _table(scope.parent_table)
        return table.c[scope.local_column].in_(
            select(parent.c["id"]).where(
                scope_predicate(scope.parent_table, org_id)
            )
        )

    raise TypeError(f"unknown scope {scope!r} on {table_name!r}")


def _order_by(table: Table) -> list[ColumnElement[Any]]:
    """Stable ordering over the FULL primary key.

    Five included tables have composite PKs (``org_ai_feature_caps``,
    ``org_ai_feature_routing``, ``transaction_tags``,
    ``user_dismissed_announcements``, plus the single-column rest), so
    ordering by "the" PK column is not well defined. Order by all of them.
    """
    pk = list(table.primary_key.columns)
    return pk or list(table.columns)


def _exported_columns(table_name: str) -> list[str]:
    """Column names emitted for a table, i.e. all of them minus redactions.

    Opt-OUT by design: a new column lands in the export automatically,
    because the drift this subsystem fears is omission.
    """
    disposition = EXPORT_DISPOSITION[table_name]
    assert isinstance(disposition, Include)
    return [c.name for c in _table(table_name).columns if c.name not in disposition.redact]


# ── Counting ───────────────────────────────────────────────────────────────


async def preflight_counts(db: AsyncSession, *, org_id: int) -> dict[str, int]:
    """``COUNT(*)`` per included table under its own scope predicate."""
    counts: dict[str, int] = {}
    for name in sorted(included_tables()):
        table = _table(name)
        counts[name] = int(
            (
                await db.execute(
                    select(func.count()).select_from(table).where(
                        scope_predicate(name, org_id)
                    )
                )
            ).scalar_one()
        )
    return counts


# ── Streaming ──────────────────────────────────────────────────────────────


async def stream_org_export(
    db: AsyncSession,
    *,
    org_id: int,
    org_name: str | None = None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield the export one encoded NDJSON line at a time.

    The consumer decides where bytes land; this function never accumulates
    them. The running SHA-256 covers every byte yielded before the trailer.
    """
    included = sorted(included_tables())

    if max_rows is not None:
        total = sum((await preflight_counts(db, org_id=org_id)).values())
        if total > max_rows:
            raise ExportTooLarge(
                f"org {org_id} has {total} exportable rows, above the "
                f"{max_rows} limit; use the privacy@ email channel"
            )

    digest = hashlib.sha256()
    written = 0

    def _emit(obj: dict[str, Any]) -> bytes:
        nonlocal written
        line = _encode_line(obj)
        digest.update(line)
        written += len(line)
        if max_bytes is not None and written > max_bytes:
            # ⚠ Abort the MOMENT the bound is crossed. The point of a byte
            # cap is to not finish producing something that kills the box.
            #
            # Note: if the crossing happens on the TRAILER line, an otherwise
            # complete body is discarded. That is correct as a refusal — the
            # artifact has no trailer, so it is not an export — and at a 1 GiB
            # default the window is a few hundred bytes wide. Left as-is
            # deliberately rather than special-cased.
            raise ExportTooLarge(
                f"org {org_id} export exceeded {max_bytes} bytes; "
                f"use the privacy@ email channel"
            )
        return line

    yield _emit(
        {
            "record": "header",
            "schema_version": SCHEMA_VERSION,
            "registry_version": REGISTRY_VERSION,
            "exported_at": utcnow_naive().isoformat() + "Z",
            "org_id": org_id,
            "org_name": org_name,
            "expected_tables": included,
            "excluded": excluded_reasons(),
        }
    )

    tables: dict[str, int] = {}
    total_rows = 0

    for name in included:
        table = _table(name)
        columns = _exported_columns(name)
        statement = (
            select(*[table.c[c] for c in columns])
            .where(scope_predicate(name, org_id))
            .order_by(*_order_by(table))
            .execution_options(yield_per=_YIELD_PER)
        )
        emitted = 0
        result = await db.stream(statement)
        async for row in result:
            emitted += 1
            total_rows += 1
            yield _emit(
                {
                    "record": "row",
                    "table": name,
                    "data": dict(zip(columns, row)),
                }
            )
        tables[name] = emitted

    # The trailer is emitted LAST and only after every table has drained.
    # ``sha256_body`` covers the header and every row line, not itself.
    yield _emit(
        {
            "record": "trailer",
            "tables": tables,
            "total_rows": total_rows,
            "sha256_body": digest.hexdigest(),
            "complete": True,
        }
    )


# ── Draining without holding the document ──────────────────────────────────


@dataclass(frozen=True)
class ExportResult:
    """What the export produced — counts and integrity, never row contents."""

    tables: dict[str, int]
    total_rows: int
    sha256: str
    byte_size: int
    excluded: dict[str, str]
    registry_version: int = REGISTRY_VERSION


async def collect_export(
    db: AsyncSession,
    *,
    org_id: int,
    org_name: str | None = None,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> ExportResult:
    """Run a full export and return only its summary.

    Used by the completeness fences and by anything that needs the shape of
    an export without its payload. Consumes the stream; holds no rows.
    """
    tables: dict[str, int] = {}
    total_rows = 0
    byte_size = 0
    sha256 = ""
    excluded: dict[str, str] = {}

    body = hashlib.sha256()
    trailer_seen = False

    async for line in stream_org_export(
        db,
        org_id=org_id,
        org_name=org_name,
        max_rows=max_rows,
        max_bytes=max_bytes,
    ):
        byte_size += len(line)
        record = json.loads(line)
        kind = record.get("record")
        if kind == "trailer":
            trailer_seen = True
            tables = record["tables"]
            total_rows = record["total_rows"]
            sha256 = record["sha256_body"]
        else:
            body.update(line)
            if kind == "header":
                excluded = record["excluded"]

    if not trailer_seen:  # pragma: no cover — generator always emits one
        raise ExportError("stream ended without a trailer")
    if sha256 != body.hexdigest():  # pragma: no cover
        raise ExportError("trailer sha256 does not match the streamed body")

    return ExportResult(
        tables=tables,
        total_rows=total_rows,
        sha256=sha256,
        byte_size=byte_size,
        excluded=excluded,
    )


# ── Verification ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str | None
    tables: dict[str, int]
    total_rows: int
    sha256: str | None


def verify_export(lines: Iterable[bytes]) -> VerifyResult:
    """Decide whether an artifact is a COMPLETE export.

    Rejects, in order: a trailer that is absent or not last, a trailer whose
    table set does not match the header's promise, per-table counts that
    disagree with the rows actually present, a ``total_rows`` that does not
    match those rows, and a ``sha256_body`` that does not match the preceding
    bytes. Reads line by line — a verifier that slurped the file would defeat
    the point of streaming it.

    ⚠ The header's ``expected_tables`` MUST be read. Without it an artifact
    that dropped a whole table certifies as complete: the per-table check
    below compares the trailer against rows *observed*, and a table absent
    from both agrees with itself. The header is the only independent record
    of what the export promised to contain, which is the entire reason it
    carries ``expected_tables``.

    ⚠ ``total_rows`` MUST be recomputed. ``sha256_body`` covers the body but
    NOT the trailer, so every number in the trailer is unauthenticated; a
    forged ``total_rows`` is invisible unless it is checked against the rows
    actually counted here.
    """
    body = hashlib.sha256()
    observed: dict[str, int] = {}
    trailer: dict[str, Any] | None = None
    header: dict[str, Any] | None = None

    for raw in lines:
        if trailer is not None:
            return VerifyResult(False, "content after the trailer", observed, 0, None)
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return VerifyResult(False, "unparseable line", observed, 0, None)

        kind = record.get("record")
        if kind == "trailer":
            trailer = record
            continue

        body.update(raw)
        if kind == "header":
            header = record
        elif kind == "row":
            observed[record["table"]] = observed.get(record["table"], 0) + 1
        else:
            return VerifyResult(False, f"unknown record {kind!r}", observed, 0, None)

    if header is None:
        return VerifyResult(False, "no header", observed, 0, None)
    if trailer is None:
        # The truncation case: a partial file is well-formed NDJSON all the
        # way down and betrays nothing except the missing trailer.
        return VerifyResult(False, "no terminal trailer (truncated)", observed, 0, None)

    # The trailer is attacker-shaped input: sha256_body does not cover it, so
    # a malformed one must produce a verdict, not a traceback.
    try:
        declared_tables = dict(trailer["tables"])
        declared_total = int(trailer["total_rows"])
    except (KeyError, TypeError, ValueError):
        return VerifyResult(False, "malformed trailer", observed, 0, None)

    # ⚠ Manifest vs trailer. A table the builder skipped entirely is absent
    # from the trailer AND from the observed rows, so the count check below
    # would happily agree with itself. Only the header catches it.
    if set(declared_tables) != set(header.get("expected_tables") or ()):
        return VerifyResult(
            False,
            "trailer tables do not match the header's expected_tables",
            observed,
            0,
            None,
        )

    declared = {t: n for t, n in declared_tables.items() if n}
    if declared != observed:
        return VerifyResult(False, "row counts disagree with trailer", observed, 0, None)
    # ⚠ Recomputed, never trusted: the trailer is outside sha256_body.
    if declared_total != sum(observed.values()):
        return VerifyResult(False, "total_rows disagrees with observed rows", observed, 0, None)
    if trailer["sha256_body"] != body.hexdigest():
        return VerifyResult(False, "sha256_body mismatch", observed, 0, None)
    if trailer.get("complete") is not True:
        return VerifyResult(False, "trailer not marked complete", observed, 0, None)

    return VerifyResult(
        True,
        None,
        dict(trailer["tables"]),
        int(trailer["total_rows"]),
        trailer["sha256_body"],
    )
