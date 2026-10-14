"""app.logs: one stdlib JSON setup for every process (INFRA-105 step 3). Fences F1-F8, F12, F17,
F20, F21. Records never carry an exception's message, only its class and frames."""

import io
import json
import logging
import logging.config
import os
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest
import uvicorn.config

from app import logs, tracing
from opentelemetry.trace import SpanKind

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Composed, so the literal never sits in a source line (frames carry source lines).
VALUE = "SEC" + "RET-7f3a a@b.c"
log = logging.getLogger("app.tests.logs")


def _raised(kind: type[BaseException], *args: object) -> BaseException:
    try:
        raise kind(*args)
    except BaseException as error:  # noqa: BLE001 - the point is to capture it
        return error


def _run_python(body: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "JWT_SECRET_KEY": "j" * 40, "APP_ENV": "development", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-c", body], cwd=BACKEND_ROOT, env=env, capture_output=True, text=True,
        timeout=60,
    )


# F1: one JSON object per line, the head first, extras after.
def test_f1_every_line_is_json_with_the_head_keys_first(log_lines):
    with_context = {"request_id": "r", "user_id": 7}
    logs.CONTEXT.set(with_context)
    log.error("m", extra={"k": 1}, exc_info=_raised(ValueError, "x"))
    log.info("two\nlines", extra={"j": "a\nb"})

    first, second = log_lines()

    assert list(first)[:4] == ["ts", "level", "logger", "msg"]
    assert list(first)[4:] == ["request_id", "user_id", "k", "exc"]
    assert first["user_id"] == 7  # an int stays an int
    assert second["msg"] == "two\nlines"
    assert "event" not in first and "timestamp" not in first


# F2: context and extras cannot overwrite the head fields.
def test_f2_extra_and_context_never_overwrite_the_head(log_lines):
    logs.bind(level="x", msg="y")
    log.info("m", extra={"ts": "z", "logger": "w"})

    line = log_lines()[-1]

    assert line["msg"] == "m"
    assert line["level"] == "INFO"
    assert line["logger"] == "app.tests.logs"
    assert line["ts"] != "z"


# F3: trace_id/span_id come from the current span, and only while it is current.
def test_f3_ids_come_from_the_current_span_only(log_lines):
    with tracing.span("t", SpanKind.INTERNAL, {}) as current:
        log.info("inside")
        context = current.get_span_context()
    log.info("after")

    inside, after = log_lines()

    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert len(inside["trace_id"]) == 32
    assert inside["span_id"] == format(context.span_id, "016x")
    assert "trace_id" not in after and "span_id" not in after


# F4: an error is its class and frames, never its message.
def test_f4_an_error_is_class_and_frames_never_the_message(log_lines):
    try:
        raise RuntimeError(VALUE)
    except RuntimeError:
        log.exception("a")
        log.warning("b", exc_info=True)

    for line in log_lines():
        assert line["exc"][0]["type"] == "builtins.RuntimeError"
        assert line["exc"][0]["frames"]
        assert "SECRET" not in json.dumps(line)


def test_f4_an_uncaught_error_is_one_json_line_without_the_message():
    done = _run_python(
        "from app import logs\nlogs.configure()\n"
        f"raise RuntimeError({VALUE!r})\n"
    )

    assert done.returncode != 0
    assert "SECRET" not in done.stdout + done.stderr
    line = json.loads(done.stdout.splitlines()[-1])
    assert line["exc"][0]["type"] == "builtins.RuntimeError"
    assert line["exc"][0]["frames"]


# F5: the chain is walked, class by class.
def test_f5_the_cause_chain_is_walked_without_messages(log_lines):
    try:
        try:
            raise RuntimeError(VALUE + "-cause")
        except RuntimeError as cause:
            raise ValueError(VALUE + "-top") from cause
    except ValueError:
        log.exception("m")

    line = log_lines()[-1]

    assert [link["type"] for link in line["exc"]] == ["builtins.ValueError", "builtins.RuntimeError"]
    assert "SECRET" not in json.dumps(line)


# F6: a MySQL error is its number, never the row it quotes.
def test_f6_a_mysql_error_is_its_number_never_its_row(log_lines):
    error = _raised(pymysql.err.IntegrityError, 1062, "Duplicate entry 'a@b.c' for key 'email'")

    log.error("m", exc_info=error)

    line = log_lines()[-1]
    assert line["exc"][0]["errno"] == 1062
    assert "a@b.c" not in json.dumps(line)


# F7: a non-string message is its class.
def test_f7_a_non_string_message_is_its_class(log_lines):
    log.warning(_raised(RuntimeError, VALUE))

    line = log_lines()[-1]
    assert line["msg"] == "RuntimeError"
    assert "SECRET" not in json.dumps(line)


# F8: bad %-args still produce one JSON line.
def test_f8_bad_percent_args_still_emit_one_json_line(log_lines):
    log.info("x %s %s", 1)

    assert log_lines()[-1]["msg"] == "x %s %s"


# An exception passed as a field value is its class, never str().
def test_an_exception_field_value_is_its_class(log_lines):
    log.info("m", extra={"error": _raised(RuntimeError, VALUE)})

    line = log_lines()[-1]
    assert line["error"] == "RuntimeError"
    assert "SECRET" not in json.dumps(line)


# F12: configure() twice leaves one handler of ours, and other handlers alone.
def test_f12_configure_twice_keeps_one_own_handler_and_caplog(caplog):
    logs.configure()
    logs.configure()

    ours = [h for h in logging.getLogger().handlers if h.name == logs.HANDLER]
    log.warning("kept")

    assert len(ours) == 1
    assert [r.getMessage() for r in caplog.records if r.name == "app.tests.logs"] == ["kept"]


# F17: an httpx INFO record (it carries the full URL) is dropped; a WARNING is not.
def test_f17_an_httpx_info_record_is_dropped(log_lines):
    logging.getLogger("httpx").info('HTTP Request: GET https://x/?code=SECRET "200 OK"')
    logging.getLogger("httpx").warning("kept")

    assert [line["msg"] for line in log_lines() if line["logger"] == "httpx"] == ["kept"]


# F20: uvicorn's own handlers are gone after configure(), so its error text never reaches a stream.
def test_f20_uvicorn_error_goes_through_our_formatter(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)  # before dictConfig: uvicorn's handler binds stderr
    monkeypatch.setattr(sys, "stderr", err)
    try:
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        logs.configure()
        try:
            raise RuntimeError(VALUE)
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
        monkeypatch.undo()
        seen = out.getvalue() + err.getvalue()
        assert "SECRET" not in seen
        assert json.loads(out.getvalue().splitlines()[-1])["exc"][0]["type"] == "builtins.RuntimeError"
    finally:
        monkeypatch.undo()
        logs.configure()


# F21: a slowapi WARNING (its text carries the limit key, the client IP) is dropped.
def test_f21_a_slowapi_warning_is_dropped(log_lines):
    logging.getLogger("slowapi").warning("ratelimit 5 per 1 minute (203.0.113.9) exceeded")
    logging.getLogger("slowapi").error("kept")

    lines = [line for line in log_lines() if line["logger"] == "slowapi"]
    assert [line["msg"] for line in lines] == ["kept"]


# F19: a Settings error at import exits non-zero, naming fields or validator messages, never a value.
def test_f19_a_field_error_names_the_field_and_not_the_value():
    value = "SEC" + "RET-field-value"
    done = _run_python("import app.config", {"DB_POOL_SIZE": value})

    assert done.returncode != 0
    assert "db_pool_size" in done.stdout + done.stderr
    assert value not in done.stdout + done.stderr
    assert "input_value" not in done.stdout + done.stderr


def test_f19_a_model_validator_error_shows_its_message_and_not_the_values():
    value = "SEC" + "RET-" + "k" * 40
    done = _run_python(
        "import app.config",
        {"JWT_SECRET_KEY": value, "MFA_RECOVERY_HMAC_KEY": value},
    )

    assert done.returncode != 0
    assert "must differ from JWT_SECRET_KEY" in done.stdout + done.stderr
    assert value not in done.stdout + done.stderr
    assert "input_value" not in done.stdout + done.stderr  # pydantic's repr of the input dict


# F13: alembic's env.py sets logging up through logs.configure(), not fileConfig.
def test_f13_alembic_env_keeps_our_handler_and_does_not_disable_loggers(caplog):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    early = logging.getLogger("app.tests.created_before_alembic")
    config = Config(str(BACKEND_ROOT / "alembic.ini"), output_buffer=io.StringIO())
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    first = ScriptDirectory.from_config(config).walk_revisions("base", "heads")
    base = [r.revision for r in first][-1]

    command.upgrade(config, f"{base}", sql=True)  # offline: runs env.py, touches no database

    root_handlers = [h for h in logging.getLogger().handlers if h is not caplog.handler]
    assert [h.name for h in root_handlers if h.name == logs.HANDLER] == [logs.HANDLER]
    assert not any(h.name != logs.HANDLER and type(h) is logging.StreamHandler for h in root_handlers)
    assert early.disabled is False
    early.warning("still emits")
    assert any(r.getMessage() == "still emits" for r in caplog.records)
