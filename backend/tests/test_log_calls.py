"""Source fences for the one-logging-library rule (INFRA-105 step 3): F14, F15, F16.

AST, not grep, where a call shape matters: a grep is satisfied by a comment. A "logger call" is any
``.debug/.info/.warning/.error/.exception/.critical`` (or ``a*``) call on a receiver bound to a
logger: a name assigned from ``getLogger(...)``/``get_logger(...)`` anywhere in the file, a
parameter annotated ``...Logger``, or an inline ``getLogger(...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import logs

BACKEND = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SOURCES = sorted(
    p for p in BACKEND.rglob("*.py") if "__pycache__" not in p.parts and ".venv" not in p.parts
)
# What runs in production: tests are scanned by F14 only.
RUNTIME = [p for p in SOURCES if "tests" not in p.relative_to(BACKEND).parts]

LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal"}
LOGGER_CALLS = LEVELS | {"a" + name for name in LEVELS}
ALLOWED_KWARGS = {"exc_info", "extra", "stack_info", "stacklevel"}
# The only attributes of a caught error that may reach a log line (each is a short identifier or
# number, never free text from the failing party).
# provider_reason: only ever a token-shaped provider error type/code (test_ai_provider_reason P2).
SAFE_ATTRIBUTES = {"code", "status_code", "reason", "model", "provider_reason"}


def _is_logger_factory(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in {"getLogger", "get_logger"}


def _logger_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_logger_factory(node.value):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.arg) and node.annotation is not None:
            if "Logger" in ast.unparse(node.annotation):
                names.add(node.arg)
    return names


def logger_calls(tree: ast.AST) -> list[ast.Call]:
    names = _logger_names(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOGGER_CALLS
        and (
            (isinstance(node.func.value, ast.Name) and node.func.value.id in names)
            or _is_logger_factory(node.func.value)
        )
    ]


def _extra_dict(call: ast.Call) -> ast.Dict | None:
    for kw in call.keywords:
        if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
            return kw.value
    return None


def call_problems(source: str) -> list[str]:
    """F15: what is wrong with each logger call's shape."""
    tree = ast.parse(source)
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    problems: list[str] = []
    for call in logger_calls(tree):
        where = f"line {call.lineno}"
        if id(call) in awaited:
            problems.append(f"{where}: awaited")
        if call.func.attr.startswith("a") and call.func.attr in LOGGER_CALLS - LEVELS:
            problems.append(f"{where}: async variant {call.func.attr}")
        for kw in call.keywords:
            if kw.arg not in ALLOWED_KWARGS:
                problems.append(f"{where}: keyword {kw.arg!r} (use extra={{...}})")
        if (extra := _extra_dict(call)) is not None:
            for key in extra.keys:
                if key is None:
                    continue  # a **splat: not checkable statically
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    problems.append(f"{where}: non-literal extra key")
                elif key.value in logs.STANDARD | logs.RESERVED:
                    problems.append(f"{where}: extra key {key.value!r} is a LogRecord field")
    return problems


def _safe_use(node: ast.AST, parents: dict[int, ast.AST], bound: str) -> bool:
    """Is this Name (the caught error) used in one of the allowed shapes?"""
    parent = parents.get(id(node))
    # type(x).__name__
    if (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id == "type"
        and parent.args == [node]
    ):
        grand = parents.get(id(parent))
        return isinstance(grand, ast.Attribute) and grand.attr == "__name__"
    # error_summary(x)
    if isinstance(parent, ast.Call) and node in parent.args:
        func = parent.func
        return (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")) == (
            "error_summary"
        )
    # x.code / x.status_code / x.reason / x.model
    if isinstance(parent, ast.Attribute) and parent.value is node:
        if parent.attr in SAFE_ATTRIBUTES:
            return True
        # x.data.get(...)
        grand = parents.get(id(parent))
        great = parents.get(id(grand)) if grand is not None else None
        return (
            parent.attr == "data"
            and isinstance(grand, ast.Attribute)
            and grand.attr == "get"
            and isinstance(great, ast.Call)
            and great.func is grand
        )
    return False


def _error_names(tree: ast.AST, parents: dict[int, ast.AST]) -> list[tuple[str, ast.AST]]:
    """(name, scope) for each name that holds a caught error: ``except ... as name`` (the handler
    body) and ``name = task.exception()`` (the enclosing function)."""
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.name, ast.Module(body=node.body, type_ignores=[])))
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "exception"
        ):
            scope = node
            while scope is not None and not isinstance(
                scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
            ):
                scope = parents.get(id(scope))
            found.append((node.targets[0].id, scope))
    return found


def message_problems(source: str) -> list[str]:
    """F16: an except-bound name reaching a log line other than as a class, a summary or one of the
    closed attributes."""
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = logger_calls(tree)
    problems: list[str] = []
    for name, scope in _error_names(tree, parents):
        inside = {id(n) for n in ast.walk(scope)}
        for call in (c for c in calls if id(c) in inside):
            scanned: list[ast.AST] = list(call.args)
            for kw in call.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                    scanned += kw.value.values
                elif kw.arg not in {"exc_info", "stack_info", "stacklevel"}:
                    scanned.append(kw.value)
            for root in scanned:
                for node in ast.walk(root):
                    if isinstance(node, ast.Name) and node.id == name:
                        if not _safe_use(node, parents, name):
                            problems.append(f"line {call.lineno}: {name} in a log line")
    return problems


# F14 ------------------------------------------------------------------------------------------
def test_f14_one_logging_library():
    offenders = [
        p.relative_to(BACKEND).as_posix()
        for p in SOURCES
        if p != SELF and "structlog" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"structlog is gone; still named in: {offenders}"
    for name in ("pyproject.toml", "uv.lock"):
        assert "structlog" not in (BACKEND / name).read_text(encoding="utf-8"), name


# F15 ------------------------------------------------------------------------------------------
def test_f15_every_logger_call_has_a_runtime_safe_shape():
    problems = {}
    total = 0
    for path in RUNTIME:
        source = path.read_text(encoding="utf-8")
        total += len(logger_calls(ast.parse(source)))
        if found := call_problems(source):
            problems[path.relative_to(BACKEND).as_posix()] = found
    assert total > 250, f"only {total} logger calls found: the receiver detection went blind"
    assert not problems, problems


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("import logging\nlog = logging.getLogger('x')\nasync def f():\n    await log.info('m')\n", "awaited"),
        ("import logging\nlog = logging.getLogger('x')\nlog.ainfo('m')\n", "async variant"),
        ("import logging\nlog = logging.getLogger('x')\nlog.info('m', org_id=1)\n", "keyword 'org_id'"),
        ("import logging\nlog = logging.getLogger('x')\nlog.info('m', extra={'name': 1})\n", "'name'"),
        ("import logging\nlog = logging.getLogger('x')\nlog.info('m', extra={'msg': 1})\n", "'msg'"),
        ("import logging\nlogging.getLogger('x').error('m', message='y')\n", "keyword 'message'"),
        ("import logging\ndef f(log: 'logging.Logger'):\n    log.info('m', k=1)\n", "keyword 'k'"),
    ],
)
def test_f15_the_scanner_catches_each_wrong_shape(source, fragment):
    assert any(fragment in problem for problem in call_problems(source)), call_problems(source)


def test_f15_the_scanner_passes_the_right_shape():
    source = (
        "import logging\nlog = logging.getLogger('x')\n"
        "log.info('m', extra={'org_id': 1, **d}, exc_info=True)\n"
    )
    assert call_problems(source) == []


# No client address in a log field (telemetry standard) ---------------------------------------
ADDRESS_KEYS = {"ip", "ip_address", "client_ip", "remote_addr"}


def address_problems(source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for call in logger_calls(tree):
        if (extra := _extra_dict(call)) is not None:
            for key in extra.keys:
                if isinstance(key, ast.Constant) and key.value in ADDRESS_KEYS:
                    problems.append(f"line {call.lineno}: {key.value!r}")
    return problems


def test_no_log_field_carries_a_client_address():
    problems = {}
    for path in RUNTIME:
        if found := address_problems(path.read_text(encoding="utf-8")):
            problems[path.relative_to(BACKEND).as_posix()] = found
    assert not problems, problems


@pytest.mark.parametrize("key", sorted(ADDRESS_KEYS))
def test_the_address_scanner_catches_each_key(key):
    source = f"import logging\nlog = logging.getLogger('x')\nlog.info('m', extra={{{key!r}: 1}})\n"
    assert address_problems(source)
    assert address_problems("import logging\nlog = logging.getLogger('x')\nlog.info('m', extra={'org_id': 1})\n") == []


# F16 ------------------------------------------------------------------------------------------
def test_f16_no_exception_message_reaches_a_log_line():
    problems = {}
    for path in RUNTIME:
        if found := message_problems(path.read_text(encoding="utf-8")):
            problems[path.relative_to(BACKEND).as_posix()] = found
    assert not problems, problems


HEAD = "import logging\nlog = logging.getLogger('x')\ntry:\n    pass\nexcept Exception as e:\n"


@pytest.mark.parametrize(
    "use",
    [
        "log.info('m %s', e)",
        "log.info('m', extra={'error': e})",
        "log.info('m', extra={'error': str(e)})",
        "log.info('m', extra={'error': repr(e)})",
        "log.info(f'm {e}')",
        "log.info('m', extra={'error': e.args})",
        "log.info('m', extra={'error': e.detail})",
        "log.info('m', extra={'error': e.response.text})",
        "log.info('m', error=str(e))",
    ],
)
def test_f16_the_scanner_catches_each_message_leak(use):
    assert message_problems(HEAD + "    " + use + "\n"), use


@pytest.mark.parametrize(
    "use",
    [
        "log.info('m', extra={'error': type(e).__name__})",
        "log.info('m', extra={'error': error_summary(e)})",
        "log.info('m', extra={'error': tracing.error_summary(e)})",
        "log.info('m', extra={'code': e.code, 's': e.status_code, 'r': e.reason, 'm': e.model})",
        "log.info('m', extra={'c': e.data.get('c')})",
        "log.exception('m')",
        "log.warning('m', exc_info=e)",
    ],
)
def test_f16_the_scanner_passes_the_allowed_uses(use):
    assert message_problems(HEAD + "    " + use + "\n") == [], use


def test_f16_the_scanner_catches_a_task_exception_value():
    source = (
        "import logging\nlog = logging.getLogger('x')\n"
        "def done(task):\n    exc = task.exception()\n"
        "    log.error('m', extra={'error': str(exc)})\n"
    )
    assert message_problems(source)
