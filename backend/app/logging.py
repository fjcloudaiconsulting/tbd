import logging
import re
import sys

import structlog

from app.config import settings

# Pattern to parse uvicorn access log: '127.0.0.1:1234 - "GET /path HTTP/1.1" 200'
_ACCESS_RE = re.compile(
    r'^(?P<remote>[^\s]+)\s+-\s+"(?P<method>\w+)\s+(?P<path>\S+)\s+HTTP/[\d.]+"'
    r"\s+(?P<status>\d+)"
)


# The query string, up to the next whitespace. uvicorn percent-quotes the
# path (a literal "?" there becomes %3F), so the first "?" in an access
# line starts the query. Query values carry secrets (OAuth code/state on
# /api/v1/auth/google/callback, invite tokens), INFRA-110.
_QUERY_RE = re.compile(r"\?\S*")

# Paths excluded from access logs (health checks flood logs in production;
# Route 53 polls /health/dependencies, INFRA-83)
_SILENT_PATHS = {"/health", "/ready", "/health/dependencies"}


class _AccessLogFilter(logging.Filter):
    """Strips the query string from every uvicorn access record and drops
    health check records.

    Applied directly to the uvicorn.access logger so the record is fixed
    (or dropped) before it reaches any handler or formatter. The record
    itself is rewritten (msg formatted, args emptied) because uvicorn
    passes the full path with query in record.args.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = _QUERY_RE.sub("", record.getMessage())
        record.msg, record.args = msg, ()
        match = _ACCESS_RE.match(msg)
        if match and match.group("path") in _SILENT_PATHS:
            return False
        return True


def _parse_uvicorn_access(logger: object, method_name: str, event_dict: dict) -> dict:
    """Parse uvicorn access log into structured fields."""
    if event_dict.get("logger") != "uvicorn.access":
        return event_dict

    msg = event_dict.get("event", "")
    match = _ACCESS_RE.match(str(msg))
    if match:
        event_dict["event"] = "request"
        event_dict["remote_addr"] = match.group("remote")
        event_dict["method"] = match.group("method")
        event_dict["path"] = match.group("path")
        event_dict["status"] = int(match.group("status"))

    return event_dict


def setup_logging() -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _parse_uvicorn_access,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper()))

    # structlog uses stdlib as backend — route through same ProcessorFormatter
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet down noisy loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # ofxtools emits per-row INFO ("Converting <STMTTRN>") during OFX
    # parse — fine in unit-fixture tests, but on a real 10 000-row import
    # this floods structlog. Drop to WARNING so structural failures still
    # surface but per-row noise stays out of production logs and test
    # wall-clock measurements.
    logging.getLogger("ofxtools").setLevel(logging.WARNING)

    # Force uvicorn loggers to use our JSON handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(handler)
        uv_logger.propagate = False

    # Strip query strings and drop health checks before any handler runs
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())
