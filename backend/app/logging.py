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


# Paths excluded from access logs (health checks flood logs in production)
_SILENT_PATHS = {"/health", "/ready"}


def _parse_uvicorn_access(logger: object, method_name: str, event_dict: dict) -> dict:
    """Parse uvicorn access log into structured fields.

    Suppresses health check requests to keep logs readable in production
    where DO pings /health every 15 seconds.
    """
    if event_dict.get("logger") != "uvicorn.access":
        return event_dict

    msg = event_dict.get("event", "")
    match = _ACCESS_RE.match(str(msg))
    if match:
        path = match.group("path")
        if path in _SILENT_PATHS:
            raise structlog.DropEvent

        event_dict["event"] = "request"
        event_dict["remote_addr"] = match.group("remote")
        event_dict["method"] = match.group("method")
        event_dict["path"] = path
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

    # Force uvicorn loggers to use our JSON handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.addHandler(handler)
        uv_logger.propagate = False
