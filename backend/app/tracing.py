"""OpenTelemetry for the API process: the telemetry standard (aws-infra docs/architecture.md, Telemetry),
ported from Ziftbook's app/tracing.py.

Manual spans only, no contrib instrumentation, so every attribute set anywhere comes from an explicit
allowlist. Exception recording is off on every span: an error is its class (``error_summary``), never
``str(error)``, which can quote an email or a MySQL "Duplicate entry" row value.

Without an OTLP endpoint in the environment nothing is exported and nothing touches the network: the
providers are built, but with no exporter.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymysql
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import Span, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import Engine, event

from app.config import settings

# Only W3C traceparent: never the global propagator, whose default also reads baggage, which is
# client-controlled text.
PROPAGATOR = TraceContextTextMapPropagator()

# No span and no HTTP metric for the probes (the native metrics' `exclude` and the SERVER-span middleware).
HEALTH_PATHS = frozenset({"/health", "/ready", "/health/dependencies"})

# FastAPI's native HTTP metrics also record url.scheme and network.protocol.version, and a later
# release could add more: the View keeps only the SERVER span's allowlist.
HTTP_METRIC_ATTRIBUTES = {
    "http.request.method",
    "http.route",
    "http.response.status_code",
    "error.type",
}

_SQL_KEYWORD = re.compile(r"^\s*(\w+)")
_configured = False


def _enabled(signal: str) -> bool:
    """An endpoint (signal-specific or shared) is set and OTEL_<signal>_EXPORTER is not ``none``."""
    endpoint = os.environ.get(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", ""
    )
    exporter = os.environ.get(f"OTEL_{signal}_EXPORTER", "")
    return bool(endpoint.strip()) and exporter.strip().lower() != "none"


def _resource(service: str) -> Resource:
    """OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES still win over these defaults."""
    os.environ.setdefault("OTEL_SERVICE_NAME", service)
    return Resource({"service.version": settings.tbd_app_version}).merge(Resource.create())


def _sampler() -> Sampler:
    """OTEL_TRACES_SAMPLER(_ARG) as the SDK reads them, except that a remote parent's not-sampled flag
    is ignored: the root sampler decides, so a client sending ``traceparent ...-00`` cannot hide its
    own request. A local parent still decides for its children (the SQL spans follow the request)."""
    name = os.environ.get("OTEL_TRACES_SAMPLER", "").strip().lower()
    if name.endswith("always_off"):
        root: Sampler = ALWAYS_OFF
    elif name.endswith("traceidratio"):
        try:
            root = TraceIdRatioBased(float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1.0")))
        except ValueError:
            root = ALWAYS_ON
    else:
        root = ALWAYS_ON
    return ParentBased(root, remote_parent_not_sampled=root)


def _provider(service: str) -> TracerProvider:
    provider = TracerProvider(resource=_resource(service), sampler=_sampler())
    # Gated: without an endpoint the exporter would fall back to localhost:4318.
    if _enabled("TRACES"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def _meter_provider(service: str) -> MeterProvider:
    """Always an SDK provider, so FastAPI's metrics have somewhere to go and tests can add a reader;
    the OTLP reader only when metrics are on. Exemplars off: they would re-attach dropped attributes."""
    readers: list[MetricReader] = []
    if _enabled("METRICS"):
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    return MeterProvider(
        metric_readers=readers,
        resource=_resource(service),
        views=[View(instrument_name="http.server.*", attribute_keys=HTTP_METRIC_ATTRIBUTES)],
        exemplar_filter=AlwaysOffExemplarFilter(),
    )


def configure(service: str) -> None:
    """Idempotent. app.main calls it before FastAPI() is built, as the standard says; FastAPI looks
    the global MeterProvider up per request, so without this call no HTTP metric is recorded."""
    global _configured
    if _configured:
        return
    _configured = True
    trace.set_tracer_provider(_provider(service))
    metrics.set_meter_provider(_meter_provider(service))


def error_summary(error: BaseException) -> str:
    """The error's class, plus the MySQL error number for a driver error (1062 duplicate, 1213
    deadlock, 2013 lost connection); never its message, which quotes row values."""
    for candidate in (error, getattr(error, "orig", None)):
        if isinstance(candidate, pymysql.err.MySQLError) and candidate.args:
            if isinstance(candidate.args[0], int):
                return f"{type(error).__name__} errno={candidate.args[0]}"
    return type(error).__name__


def mark_error(current: Span, error: BaseException) -> None:
    current.set_status(trace.Status(trace.StatusCode.ERROR, error_summary(error)))
    current.set_attribute("error.type", type(error).__name__)


@contextmanager
def span(
    name: str, kind: SpanKind, attributes: dict[str, Any], context: Context | None = None
) -> Iterator[Span]:
    """Every manual span goes through this, so the SDK's exception recording stays off."""
    with trace.get_tracer(__name__).start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as error:
            mark_error(current, error)
            raise


# Registered on the Engine class, so every engine (the app's async engine wraps a sync Engine, and
# tests build their own) is covered. SQLAlchemy runs these in a greenlet that shares the caller's
# contextvars, so the current span is the request's or the job's.
@event.listens_for(Engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:
    current = trace.get_current_span()
    if not current.get_span_context().is_valid or not current.is_recording():
        return  # outside any span (pool pre-ping, startup): no orphan root spans
    match = _SQL_KEYWORD.match(statement)
    context._tbd_span = trace.get_tracer(__name__).start_span(
        match.group(1).upper() if match else "SQL",
        kind=SpanKind.CLIENT,
        # ``parameters`` is never read: values are bound, so no value can reach db.query.text.
        attributes={"db.system.name": conn.dialect.name, "db.query.text": statement},
        record_exception=False,
        set_status_on_exception=False,
    )


@event.listens_for(Engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany) -> None:
    started = getattr(context, "_tbd_span", None)
    if started is not None:
        started.end()
        context._tbd_span = None


@event.listens_for(Engine, "handle_error")
def _handle_error(ctx) -> None:
    started = getattr(ctx.execution_context, "_tbd_span", None)
    if started is None:
        return
    mark_error(started, ctx.original_exception)
    started.end()
    ctx.execution_context._tbd_span = None
