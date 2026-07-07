"""OFX 1.x / 2.x preview parser (L3.2 Wave 2A; hardened DoS-safe rewrite).

Wraps the ``ofxtools`` library with bounds locked by the L3.2 import
contracts (see spec
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
§1) AND a killable process-isolation boundary + per-org concurrency cap
that close a latent single-replica DoS (roadmap §0).

## Why a subprocess, not a thread

The original design parsed inside ``asyncio.wait_for(loop.run_in_executor(
None, _parse, raw))``. On timeout ``wait_for`` returns HTTP 400 but the
*thread* keeps running ``ofxtools`` on a CPU core — Python threads cannot
be killed. N concurrent large / adversarial OFX uploads pin N cores on
the single-replica prod box: an unauthenticated-cost DoS reachable by any
member with an account.

The fix parses in a **hard-killable child process** (``multiprocessing``
``spawn`` context). On timeout the parent calls ``proc.terminate()`` (then
``kill()``), so the CPU is genuinely reclaimed — not merely abandoned. The
subprocess also gives fault isolation: a segfault / memory blow-up in the
C-ish depths of an XML parse takes down the child, not the ASGI worker.

Design tradeoff — per-request spawn vs. a persistent ``ProcessPoolExecutor``:
we spawn one process per parse rather than reusing a pool. A
``ProcessPoolExecutor`` cannot hard-kill a *single* in-flight task without
tearing the whole pool down (which would also kill unrelated concurrent
parses), so it cannot satisfy "terminate this one runaway on timeout." An
owned ``multiprocessing.Process`` can. Spawn startup (~100-300 ms of fresh
interpreter + module import) is negligible against a multi-second parse,
and OFX imports are user-initiated and concurrency-capped, so the cost is
paid rarely and is bounded. We deliberately use the ``spawn`` start method
(not ``fork``): forking a running asyncio + aiomysql + redis process risks
child deadlock on inherited locks; ``spawn`` starts a clean interpreter.

Because the isolation is *compute* isolation (no shared mutable state
crosses the boundary — only bytes in, ``ParsedRow``s out) it is
horizontally-scale-safe: each replica runs its own executor + its own
child processes, with no cross-replica coordination required.

## Concurrency cap

``OFXParseExecutor`` bounds concurrency two ways so one org cannot occupy
every parse slot and starve the box:

  * a **global** ``asyncio.Semaphore`` (``OFX_PARSE_MAX_CONCURRENT``) — the
    hard ceiling on simultaneous child processes;
  * a **per-org** counter (``OFX_PARSE_MAX_PER_ORG``) — a single org may
    hold at most this many slots at once.

Per-org over-cap returns **429** immediately (non-blocking). Global
contention is smoothed by a bounded wait (``OFX_PARSE_QUEUE_WAIT_S``); if a
global slot does not free within that window the request also gets **429**
rather than queueing unbounded. The executor is created at FastAPI lifespan
startup (``init_ofx_executor``) and dropped at shutdown
(``shutdown_ofx_executor``); the child processes it owns are per-request and
reaped on completion / timeout, so there is no long-lived pool to leak.

## Spec-locked bounds

  1. Hard 5 MB upload cap, enforced *before* any process is spawned.
  2. Parse timeout (``OFX_PARSE_TIMEOUT_S``) enforced by killing the child.
  3. Row cap (``OFX_MAX_ROWS``, restored to 10 000 now that parsing is
     isolated + killable) → HTTP 413 on excess.

Output normalization: emits ``ParsedRow`` instances with the OFX extras
(``fitid``, ``bank_id``, ``account_type_ofx``) populated. ``build_preview``
consumes the same ``ParsedRow`` shape regardless of source format.

Scope note: only the OFX path runs through this killable boundary. CSV and
ABN ``.TAB`` parse pure-Python, linear, synchronously in-handler, bounded
by the 5 MB + row caps — they are not the non-killable-thread CPU-pin
vector and are not routed here. No new ``source_format`` is introduced.

Privacy: never log raw OFX content (account numbers, balances).
``ParseError`` strings carry only the structural failure summary.
"""

from __future__ import annotations

import asyncio
import io
import multiprocessing as mp
import os
import time
from contextlib import asynccontextmanager
from datetime import date as date_t
from decimal import Decimal
from multiprocessing.connection import Connection

import structlog
from fastapi import HTTPException

from app.config import settings
from app.services.import_parser import ParseError, ParsedRow

logger = structlog.get_logger()


# ── Spec-locked bounds (L3.2 §1.2) ──
# Upload byte cap stays a module constant (not a knob) — it is a security
# invariant, not an operational dial. Timeout / row cap / concurrency are
# config-driven via ``settings`` (see app.config, ENVIRONMENT.md).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


def _coerce_account_type(value: object) -> str | None:
    """Normalize ofxtools ``accttype`` to the contract enum.

    Only values declared on ``ImportPreviewRow.account_type_ofx`` are
    returned; everything else collapses to None so a future bank-specific
    value doesn't break the response schema.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in ("CHECKING", "SAVINGS", "CREDITLINE", "MONEYMRKT"):
        return s
    return None


def _amount_to_decimal(value: object) -> Decimal:
    """ofxtools yields ``Decimal`` already; this is a defensive wrapper."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_date(value: object) -> date_t:
    """ofxtools returns ``datetime`` (tz-aware). Truncate to date in the
    transaction's own timezone (default UTC per spec §1.3)."""
    if hasattr(value, "date") and callable(value.date):
        return value.date()  # type: ignore[no-any-return]
    if isinstance(value, date_t):
        return value
    raise ParseError(f"Unparseable DTPOSTED: {value!r}")


def _description_for(tx: object) -> str:
    """OFX <NAME> first, fall back to <MEMO>, then placeholder.

    Per spec §1.3 description field mapping.
    """
    name = getattr(tx, "name", None)
    if name:
        return str(name).strip()
    memo = getattr(tx, "memo", None)
    if memo:
        return str(memo).strip()
    return "(no description)"


class _RowCapExceeded(Exception):
    """Internal signal: parsed transaction count exceeds the row cap.

    Raised inside the worker process (where the count is first known) and
    marshalled across the process boundary into an HTTP 413 by the parent.
    Not part of the public API.
    """

    def __init__(self, count: int, max_rows: int):
        self.count = count
        self.max_rows = max_rows
        super().__init__(f"{count} > {max_rows}")


def _parse_ofx_tree(raw: bytes) -> object:
    """Synchronous ofxtools parse + convert. Runs inside the child process.

    Import ofxtools locally so the dependency stays soft for the rest of
    the codebase (only loaded when an OFX parse actually fires).
    """
    from ofxtools.Parser import OFXTree

    parser = OFXTree()
    parser.parse(io.BytesIO(raw))
    return parser.convert()


def _normalize(ofx: object, max_rows: int) -> tuple[list[ParsedRow], dict]:
    """Turn an ofxtools OFX aggregate into ``ParsedRow``s + a metadata dict.

    Raises ``ParseError`` on structural anomalies and ``_RowCapExceeded``
    when the transaction count is over ``max_rows``. Runs in the child.
    """
    statements = getattr(ofx, "statements", None) or []
    if not statements:
        raise ParseError("OFX parse failed: no statements found in file.")
    stmt = statements[0]
    transactions = list(getattr(stmt, "transactions", []) or [])

    if not transactions:
        # Spec §1.4: reject files where <TRANLIST> is missing or empty.
        raise ParseError("OFX parse failed: no transactions in <TRANLIST>.")

    # ── Row cap (post-parse) ──
    if len(transactions) > max_rows:
        raise _RowCapExceeded(len(transactions), max_rows)

    # ── Account-level extras (per spec §1.3) ──
    account = getattr(stmt, "account", None)
    bank_id_value = getattr(account, "bankid", None) if account is not None else None
    accttype_value = (
        getattr(account, "accttype", None) if account is not None else None
    )
    bank_id = str(bank_id_value).strip() if bank_id_value else None
    account_type_ofx = _coerce_account_type(accttype_value)
    # Credit-card statements use <CCACCTFROM> which carries neither
    # <BANKID> nor <ACCTTYPE>; the implied type is CREDITLINE per the
    # OFX 2.x spec (§11.4.4 credit card aggregate).
    if account_type_ofx is None and account is not None:
        if type(account).__name__ == "CCACCTFROM":
            account_type_ofx = "CREDITLINE"

    # ── Normalize each transaction → ParsedRow ──
    parsed: list[ParsedRow] = []
    skipped = 0
    for i, tx in enumerate(transactions, start=1):
        trnamt = getattr(tx, "trnamt", None)
        dtposted = getattr(tx, "dtposted", None)
        if trnamt is None or dtposted is None:
            skipped += 1
            continue
        try:
            amount_raw = _amount_to_decimal(trnamt)
            row_date = _to_date(dtposted)
        except (ParseError, Exception):
            skipped += 1
            continue

        # Per spec §1.3: type from TRNAMT sign; amount is |TRNAMT|.
        row_type = "income" if amount_raw > 0 else "expense"
        amount_abs = amount_raw if amount_raw > 0 else -amount_raw
        if amount_abs == 0:
            skipped += 1
            continue

        description = _description_for(tx)
        fitid_raw = getattr(tx, "fitid", None)
        fitid = str(fitid_raw).strip() if fitid_raw else None

        counterparty = None
        payee = getattr(tx, "payee", None)
        if payee is not None:
            payee_name = getattr(payee, "name", None)
            if payee_name:
                counterparty = str(payee_name).strip()

        trntype_raw = getattr(tx, "trntype", None)
        transaction_type = str(trntype_raw).strip() if trntype_raw else None

        parsed.append(
            ParsedRow(
                row_number=i,
                date=row_date,
                description=description,
                amount=amount_abs,
                type=row_type,
                counterparty=counterparty,
                transaction_type=transaction_type,
                fitid=fitid,
                bank_id=bank_id,
                account_type_ofx=account_type_ofx,
            )
        )

    if not parsed:
        raise ParseError(
            "OFX parse failed: no usable transactions after normalization."
        )

    meta = {
        "statements": len(statements),
        "transactions_in": len(transactions),
        "transactions_out": len(parsed),
        "skipped": skipped,
    }
    return parsed, meta


def _parse_and_normalize(raw: bytes, max_rows: int) -> tuple[list[ParsedRow], dict]:
    """Full parse + normalize, wrapping ofxtools failures as ``ParseError``.

    Runs in the child process. Returns ``(rows, meta)`` or raises
    ``ParseError`` / ``_RowCapExceeded``.
    """
    try:
        ofx = _parse_ofx_tree(raw)
    except ParseError:
        raise
    except _RowCapExceeded:
        raise
    except Exception as exc:
        # ofxtools raises a family of OFXHeaderError / OFXSpecError types.
        # Surface only the class name + first line to avoid leaking raw
        # file content into logs / HTTP responses.
        message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise ParseError(f"OFX parse failed: {message}")
    return _normalize(ofx, max_rows)


def _maybe_test_hang() -> None:
    """Test-only seam: when ``PFV_OFX_TEST_HANG_S`` is set, busy-loop for
    that many seconds to simulate a pathological CPU-pinning parse, then
    (if it survives) touch ``PFV_OFX_TEST_SENTINEL``.

    This exists solely so the termination test can prove the child is
    *hard-killed*: after a timeout the sentinel must be absent (the runaway
    never completed). The env var is never set in production; the check is a
    single cheap ``os.environ.get`` at the top of the worker. Kept in
    product code (not a test module) because ``spawn`` re-imports this
    module in the child and cannot see a test's monkeypatches.
    """
    hang = os.environ.get("PFV_OFX_TEST_HANG_S")
    if not hang:
        return
    deadline = time.monotonic() + float(hang)
    x = 0
    while time.monotonic() < deadline:
        # Genuine CPU work so terminate() must reclaim a busy core.
        x = (x + 1) % 1_000_003
    sentinel = os.environ.get("PFV_OFX_TEST_SENTINEL")
    if sentinel:
        with open(sentinel, "w") as f:
            f.write("completed")


def _worker_main(conn: Connection, raw: bytes, max_rows: int) -> None:
    """Child-process entry point. Parses ``raw`` and sends a tagged result.

    The result is marshalled as ``(tag, data)`` so no fastapi / custom
    exception type has to cross the process boundary:

      * ``("ok", (rows, meta))``           — success
      * ``("rowcap", (count, max_rows))``  — over the row cap → 413
      * ``("parse_error", (msg, row))``    — structural failure → 400
      * ``("error", msg)``                 — unexpected failure → 400
    """
    try:
        _maybe_test_hang()
        rows, meta = _parse_and_normalize(raw, max_rows)
        conn.send(("ok", (rows, meta)))
    except _RowCapExceeded as exc:
        conn.send(("rowcap", (exc.count, exc.max_rows)))
    except ParseError as exc:
        conn.send(("parse_error", (str(exc), exc.row_number)))
    except Exception as exc:  # pragma: no cover - defensive
        message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        conn.send(("error", message))
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


class _ParseTimeout(Exception):
    """Internal: the child overran the parse timeout and was terminated."""


class _CapExceeded(Exception):
    """Internal: a concurrency cap rejected the parse. ``reason`` is one of
    ``per_org`` / ``global_busy``."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _reap(proc: mp.Process) -> None:
    """Ensure a child process is dead and reaped (no zombie).

    Escalates SIGTERM → join → SIGKILL → join. Called from a thread via
    ``run_in_executor`` because ``join`` blocks.
    """
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
    if proc.is_alive():  # pragma: no cover - kill fallback
        proc.kill()
        proc.join(timeout=5)
    else:
        proc.join()


class OFXParseExecutor:
    """Bounded, killable OFX parse executor.

    Owns the global + per-org concurrency caps and the spawn/terminate
    lifecycle of the per-request child processes. Instantiate once at
    lifespan; the asyncio primitives (semaphore / lock) are created lazily
    and rebound if the running event loop changes (so a fresh per-test loop
    gets fresh primitives rather than tripping "bound to a different loop").
    """

    def __init__(self, *, max_concurrent: int, max_per_org: int, queue_wait_s: float):
        self.max_concurrent = max_concurrent
        self.max_per_org = max_per_org
        self.queue_wait_s = queue_wait_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None
        self._org_counts: dict[int, int] = {}
        # Explicit spawn context: a clean interpreter, never a fork of the
        # async parent (see module docstring).
        self._ctx = mp.get_context("spawn")

    def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._sem = asyncio.Semaphore(max(1, self.max_concurrent))
            self._lock = asyncio.Lock()
            self._org_counts = {}

    @asynccontextmanager
    async def slot(self, org_id: int):
        """Acquire a parse slot for ``org_id`` or raise ``_CapExceeded``.

        Per-org cap is enforced first and rejects immediately (non-blocking).
        The global slot is then acquired with a bounded wait; on timeout the
        per-org reservation is rolled back and ``_CapExceeded("global_busy")``
        is raised.
        """
        self._ensure()
        assert self._sem is not None and self._lock is not None
        # 1. Reserve the per-org slot (immediate 429 when at cap).
        async with self._lock:
            current = self._org_counts.get(org_id, 0)
            if self.max_per_org > 0 and current >= self.max_per_org:
                raise _CapExceeded("per_org")
            self._org_counts[org_id] = current + 1

        acquired = False
        try:
            # 2. Acquire a global slot. With a positive queue-wait we allow a
            #    bounded wait for a slot to free; with queue_wait_s <= 0 we
            #    take a slot only if one is free right now (a bare
            #    ``wait_for(..., 0)`` would time out even on a free slot).
            if self.queue_wait_s > 0:
                try:
                    await asyncio.wait_for(
                        self._sem.acquire(), timeout=self.queue_wait_s
                    )
                    acquired = True
                except asyncio.TimeoutError:
                    raise _CapExceeded("global_busy")
            else:
                if self._sem.locked():
                    raise _CapExceeded("global_busy")
                # value > 0 here → acquire returns without suspending.
                await self._sem.acquire()
                acquired = True
            yield
        finally:
            if acquired:
                self._sem.release()
            async with self._lock:
                remaining = self._org_counts.get(org_id, 0) - 1
                if remaining <= 0:
                    self._org_counts.pop(org_id, None)
                else:
                    self._org_counts[org_id] = remaining

    async def run(
        self, raw: bytes, *, timeout_s: float, max_rows: int
    ) -> tuple[list[ParsedRow], dict]:
        """Parse ``raw`` in a hard-killable child process.

        Returns ``(rows, meta)``. Raises ``_ParseTimeout`` when the child
        overruns ``timeout_s`` (and terminates it), ``HTTPException(413)``
        on row-cap, or ``ParseError`` on structural failure.
        """
        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=_worker_main,
            args=(child_conn, raw, max_rows),
            daemon=True,
        )
        proc.start()
        # The parent never sends; close its copy of the send end so that if
        # the child dies without sending, ``recv`` raises EOFError promptly.
        child_conn.close()

        loop = asyncio.get_running_loop()
        try:
            # ``poll`` blocks the worker thread for at most ``timeout_s`` —
            # a bounded call, so no thread is leaked (unlike a bare ``recv``).
            ready = await loop.run_in_executor(None, parent_conn.poll, timeout_s)
            if not ready:
                raise _ParseTimeout()
            try:
                payload = parent_conn.recv()
            except EOFError:
                raise ParseError(
                    "OFX parse failed: parser process exited before producing a result."
                )
        finally:
            try:
                parent_conn.close()
            except Exception:  # pragma: no cover
                pass
            # Reap in a thread (terminate + join), guaranteeing no zombie and
            # that a timed-out runaway is actually killed.
            await loop.run_in_executor(None, _reap, proc)

        return self._decode(payload)

    @staticmethod
    def _decode(payload: tuple) -> tuple[list[ParsedRow], dict]:
        tag, data = payload
        if tag == "ok":
            rows, meta = data
            return rows, meta
        if tag == "rowcap":
            count, max_rows = data
            raise HTTPException(
                status_code=413,
                detail=(
                    f"OFX file contains {count} transactions; "
                    f"max {max_rows}. Split by date range."
                ),
            )
        if tag == "parse_error":
            message, row_number = data
            raise ParseError(message, row_number=row_number)
        # tag == "error"
        raise ParseError(f"OFX parse failed: {data}")


# ── Module singleton (created at lifespan; lazily defaulted for tests) ──
_executor: OFXParseExecutor | None = None


def init_ofx_executor(settings_obj=settings) -> OFXParseExecutor:
    """Create the process-local OFX parse executor. Call once at lifespan
    startup. Idempotent-ish: replaces any previous instance."""
    global _executor
    _executor = OFXParseExecutor(
        max_concurrent=settings_obj.ofx_parse_max_concurrent,
        max_per_org=settings_obj.ofx_parse_max_per_org,
        queue_wait_s=settings_obj.ofx_parse_queue_wait_s,
    )
    return _executor


def get_ofx_executor() -> OFXParseExecutor:
    """Return the current executor, lazily creating a default one.

    Lazy creation keeps direct ``parse_ofx`` calls (service-layer tests,
    scripts) working without a lifespan having run."""
    global _executor
    if _executor is None:
        _executor = init_ofx_executor()
    return _executor


def shutdown_ofx_executor() -> None:
    """Drop the executor at lifespan exit. The child processes it owns are
    per-request and already reaped, so there is no pool to close — this just
    releases the singleton."""
    global _executor
    _executor = None


async def parse_ofx(
    file_bytes: bytes,
    *,
    org_id: int | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
    timeout_s: float | None = None,
    max_rows: int | None = None,
) -> list[ParsedRow]:
    """Parse an OFX 1.x SGML or 2.x XML file and return ``ParsedRow``s.

    Args:
        file_bytes: Raw upload payload (bytes).
        org_id: Owning org, used for the per-org concurrency cap. ``None``
            maps to org 0 (single-tenant / direct service-layer callers).
        max_bytes: Hard upload cap; HTTP 413 above this.
        timeout_s: Parse timeout; defaults to ``settings.ofx_parse_timeout_s``.
            On overrun the parser process is terminated and HTTP 400 raised.
        max_rows: Post-parse row cap; defaults to ``settings.ofx_max_rows``.
            HTTP 413 on excess.

    Returns:
        ``list[ParsedRow]`` in the file's natural order, OFX extras populated.

    Raises:
        HTTPException(413): Upload exceeds ``max_bytes`` or row count exceeds
            ``max_rows``.
        HTTPException(429): A concurrency cap (global or per-org) rejected it.
        HTTPException(400): Parse exceeded ``timeout_s`` (child terminated).
        ParseError: Structural failure. The router maps this to HTTP 400.
    """
    if timeout_s is None:
        timeout_s = settings.ofx_parse_timeout_s
    if max_rows is None:
        max_rows = settings.ofx_max_rows

    # ── (1) Size cap — before spawning anything ──
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"OFX file too large ({len(file_bytes)} bytes; "
                f"max {max_bytes // 1024 // 1024} MB). "
                "Split by date range and re-upload."
            ),
        )

    executor = get_ofx_executor()
    org_key = org_id if org_id is not None else 0

    # ── (2) Concurrency cap + killable subprocess parse ──
    try:
        async with executor.slot(org_key):
            try:
                rows, meta = await executor.run(
                    file_bytes, timeout_s=timeout_s, max_rows=max_rows
                )
            except _ParseTimeout:
                await logger.ainfo(
                    "import.ofx.parse.timeout",
                    timeout_s=timeout_s,
                    bytes=len(file_bytes),
                    org_id=org_id,
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"OFX file too complex to parse within {int(timeout_s)} seconds; "
                        "split into smaller exports."
                    ),
                )
    except _CapExceeded as exc:
        await logger.ainfo(
            "import.ofx.parse.rejected",
            reason=exc.reason,
            org_id=org_id,
            max_concurrent=executor.max_concurrent,
            max_per_org=executor.max_per_org,
        )
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many statement imports are being processed right now. "
                "Please wait a moment and try again."
            ),
        )

    await logger.ainfo("import.ofx.parsed", bytes=len(file_bytes), **meta)
    return rows
