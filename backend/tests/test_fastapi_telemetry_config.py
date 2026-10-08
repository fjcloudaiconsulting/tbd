"""FastAPI's native OpenTelemetry stays off until INFRA-105 (INFRA-125).

FastAPI 0.142+ defaults every signal flag to on when ``telemetry=`` is
omitted (they emit once an OpenTelemetry provider is configured), and an
omitted key keeps its default. So the app must pass every flag, each one off.
Wrong implementations this kills: the ``telemetry=`` kwarg dropped from
``FastAPI(...)``; any flag set to True; any flag left out of the dict
(``auto_configure`` then follows ``FASTAPI_OTEL_AUTO_CONFIGURE`` from the env).
"""

FLAGS = ("tracing", "metrics", "logs", "operation_spans", "auto_configure")


def test_every_native_telemetry_flag_is_passed_and_off():
    from app import main

    assert main.TELEMETRY == dict.fromkeys(FLAGS, False)
    # The effective config FastAPI built (private, but the only place the
    # merged result lives): proves the dict reached FastAPI(telemetry=...).
    # Any flag still on, including one a later FastAPI adds, fails here.
    assert [k for k, v in main.app._telemetry.items() if v is True] == []
