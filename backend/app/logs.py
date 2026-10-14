"""One logging setup for every process: the API, the MCP server, scripts and migrations.

configure() once per process. Context (request_id, user_id, org_id, ...) rides in one ContextVar and
is copied onto each record by a filter on our handler. Records never carry an exception's message:
only its class, its frames and, for MySQL driver errors, the error number.
"""

import copy
import json
import logging
import sys
import threading
import traceback
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import pymysql
from opentelemetry import trace
from pydantic import ValidationError

from app.config import settings

# Ints too (user_id, org_id, api_token_id): audit_service reads api_token_id back as an int.
CONTEXT: ContextVar[dict[str, Any]] = ContextVar("log_context")  # no default: ruff B039
HANDLER = "tbd"
# Everything a LogRecord has on its own; any other attribute came from extra=.
STANDARD = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "color_message",
    "context",
}
RESERVED = {"ts", "level", "logger", "msg"}  # set by _head(); context/extras never overwrite them
uncaught_logger = logging.getLogger("app.uncaught")
asyncio_logger = logging.getLogger("app.asyncio")


def bind(**fields: Any) -> None:
    """Add fields to every record logged from here on in this context and what it spawns.

    A new dict each time, never a mutation of the one in the contextvar: a copied context
    (a dependency's thread, a spawned task) shares the old dict.
    """
    CONTEXT.set({**CONTEXT.get({}), **fields})


def _add_context(record: logging.LogRecord) -> bool:
    context = CONTEXT.get({})
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        # A new dict: mutating the one held in the contextvar would leave stale ids on later
        # lines logged outside this span.
        record.context = {
            **context,
            "trace_id": format(span_context.trace_id, "032x"),
            "span_id": format(span_context.span_id, "016x"),
        }
    else:
        # No current span: keep whatever ids CONTEXT carries (TracingMiddleware binds them), and
        # add neither key when it never carried any.
        record.context = dict(context)
    return True


def _causes(error: BaseException | None) -> Iterator[BaseException]:
    """Walk __cause__/__context__ (honoring __suppress_context__), each error at most once: a
    manual __cause__ cycle must still terminate."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        suppressed = error.__suppress_context__
        error = error.__cause__ or (None if suppressed else error.__context__)


def _chain(error: BaseException | None) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for cause in _causes(error):
        kind = type(cause)
        link: dict[str, Any] = {
            "type": f"{kind.__module__}.{kind.__qualname__}",
            "frames": [
                {"file": f.filename, "line": f.lineno, "function": f.name, "code": f.line}
                for f in traceback.extract_tb(cause.__traceback__)
            ],
        }
        # The MySQL error number only (1062 duplicate, 1213 deadlock): args[1] is the message,
        # which quotes row values.
        if isinstance(cause, pymysql.err.MySQLError) and cause.args:
            if isinstance(cause.args[0], int):
                link["errno"] = cause.args[0]
        links.append(link)
    return links


def _json_default(value: object) -> str:
    # An exception passed as a field is its class, never str(): that is its message.
    return type(value).__name__ if isinstance(value, BaseException) else str(value)


class _LifespanFailure(logging.Filter):
    """uvicorn logs a failed lifespan as logger.error(<the whole traceback text>), which quotes the
    exception's message; a fixed message goes out instead."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith("Traceback (most recent call last)"):
            record.msg, record.args = "uvicorn.lifespan.failed", ()
        return True


LIFESPAN_FAILURE = _LifespanFailure()


class Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.args, tuple) and any(isinstance(a, BaseException) for a in record.args):
            # logger.error("...: %s", exc) from a library: the class, never the message.
            record = copy.copy(record)
            record.args = tuple(
                type(a).__name__ if isinstance(a, BaseException) else a for a in record.args
            )
        # A non-string message (logger.warning(error)) is logged as its class, never str().
        template = record.msg if isinstance(record.msg, str) else type(record.msg).__name__
        try:
            message = record.getMessage() if isinstance(record.msg, str) else template
            fields: dict[str, Any] = {
                **self._head(record, message),
                **{k: v for k, v in getattr(record, "context", {}).items() if k not in RESERVED},
                **{
                    k: v for k, v in vars(record).items() if k not in STANDARD and k not in RESERVED
                },
            }
            if record.exc_info and record.exc_info[1] is not None:
                fields["exc"] = _chain(record.exc_info[1])
            return self._render({k: v for k, v in fields.items() if v is not None})
        except Exception:
            # Never raise, never the arguments: the template only, still JSON.
            return self._render(self._head(record, template))

    @staticmethod
    def _head(record: logging.LogRecord, message: str) -> dict[str, Any]:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        return {
            "ts": ts.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }

    @staticmethod
    def _render(fields: dict[str, Any]) -> str:
        return json.dumps(fields, ensure_ascii=False, default=_json_default)


def _invalid_env(error: BaseException) -> list[str]:
    """Field names of a Settings ValidationError in the chain, never the rejected input. A
    ValidationError from anything else (an ad hoc TypeAdapter) names nothing, and so does a
    model-level error with no field location."""
    names: set[str] = set()
    for cause in _causes(error):
        if isinstance(cause, ValidationError) and cause.title == "Settings":
            names |= {str(e["loc"][0]) for e in cause.errors() if e["loc"]}
    return sorted(names)


def _excepthook(kind: type[BaseException], error: BaseException, tb: TracebackType | None) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, error, tb)  # a deliberate interrupt, not a crash
        return
    try:
        extra = {"invalid_env": names} if (names := _invalid_env(error)) else {}
    except Exception:
        # Never let building extras fall through to Python's own excepthook, which prints the
        # message (and, for a ValidationError, the rejected input).
        extra = {}
    uncaught_logger.critical("uncaught error", exc_info=(kind, error, tb), extra=extra)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
        info = (args.exc_type, args.exc_value, args.exc_traceback)
        uncaught_logger.critical("uncaught error", exc_info=info)


def _unraisable_hook(args: "sys.UnraisableHookArgs") -> None:
    # Not CRITICAL: a finalizer/__del__ error doesn't take the process down. Never args.err_msg
    # or repr(args.object): either can carry a value (e.g. a row's __repr__).
    if args.exc_value is None:
        return
    info = (args.exc_type, args.exc_value, args.exc_traceback)
    uncaught_logger.error("unraisable error", exc_info=info)


def asyncio_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """The loop's handler for an exception nobody awaited. The default one prints
    context["message"] and the future's repr, either of which can carry a value; this logs the
    exception's class and frames only."""
    error = context.get("exception")
    asyncio_logger.error(
        "asyncio.unhandled", exc_info=error if isinstance(error, BaseException) else None
    )


def configure() -> None:
    """Idempotent. Emits nothing: `python -m app.main` could print to stdout."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper())
    if level is None:
        raise SystemExit("invalid log_level: DEBUG, INFO, WARNING or ERROR")
    logging.raiseExceptions = False  # a broken stream never takes a request down
    # Crashes outside any handler (a Settings ValidationError, which quotes its input; a
    # migration error) come out as one JSON line, class and frames only. Plain assignment, so a
    # second configure() doesn't wrap twice.
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    sys.unraisablehook = _unraisable_hook

    root = logging.getLogger()
    for old in [h for h in root.handlers if h.name == HANDLER]:  # ours only: pytest's stay
        root.removeHandler(old)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(Formatter())
    handler.addFilter(_add_context)
    handler.name = HANDLER
    root.addHandler(handler)
    # Third-party DEBUG never shows; their INFO (alembic's "Running upgrade") does.
    root.setLevel(max(level, logging.INFO))

    for name, value in {
        "app": level,
        "uvicorn": level,
        "uvicorn.error": logging.NOTSET,  # follows "uvicorn"; never below DEBUG: no TRACE (5)
        # sqlalchemy.engine logs result rows at DEBUG.
        "sqlalchemy.engine": logging.WARNING,
        # Per-row INFO ("Converting <STMTTRN>") during OFX parse floods a real import.
        "ofxtools": logging.WARNING,
        # Their INFO line has the full URL with query.
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        # Its WARNING quotes a malformed OTEL_EXPORTER_OTLP_HEADERS entry verbatim, token included.
        "opentelemetry.util.re": logging.ERROR,
        # Its WARNING "ratelimit ... exceeded" carries the limit key, which here is the client IP.
        "slowapi": logging.ERROR,
    }.items():
        logging.getLogger(name).setLevel(value)
    # uvicorn configured these before importing the app: drop its handlers and format.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.error").addFilter(LIFESPAN_FAILURE)
    # Its access line has the client IP and the raw path with query: RequestContextMiddleware
    # writes the access line instead.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
