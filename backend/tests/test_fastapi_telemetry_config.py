"""FastAPI's native OpenTelemetry: HTTP metrics only (INFRA-105, the telemetry standard in aws-infra
docs/architecture.md).

FastAPI 0.142+ defaults every signal flag to on when ``telemetry=`` is omitted, and an omitted key
keeps its default, so the app passes every flag. ``tracing`` must stay False: native tracing exports
``url.path`` and ``url.query`` (it redacts only cloud-signature parameters), and secrets travel in
query strings (invitations preview ``?token=``, Google OAuth callback ``?code=``). tbd's own
allowlisted SERVER span (app/middleware/tracing.py) replaces it. Native ``logs`` export exception
messages; ``auto_configure`` would add a second exporter to the providers app/tracing.py registers.
Wrong implementations this kills: the ``telemetry=`` kwarg dropped; tracing, logs, operation_spans or
auto_configure on; metrics off; ``exclude`` dropped or not covering a health path.
"""

from app import tracing

EXPECTED = {
    "tracing": False,
    "metrics": True,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


def test_native_telemetry_is_metrics_only():
    from app import main

    assert {k: v for k, v in main.TELEMETRY.items() if k != "exclude"} == EXPECTED
    # The effective config FastAPI built (private, but the only place the merged result lives):
    # proves the dict reached FastAPI(telemetry=...). Any flag on besides metrics, including one a
    # later FastAPI adds, fails here.
    assert [k for k, v in main.app._telemetry.items() if v is True] == ["metrics"]


def test_health_probes_are_excluded_and_api_routes_are_not():
    from app import main

    exclude = main.app._telemetry["exclude"]
    assert tracing.HEALTH_PATHS == {"/health", "/ready", "/health/dependencies"}
    for path in tracing.HEALTH_PATHS:
        assert exclude({"type": "http", "path": path}) is True
    for path in ("/api/v1/auth/login", "/health/dependencies/x", "/healthz"):
        assert exclude({"type": "http", "path": path}) is False
