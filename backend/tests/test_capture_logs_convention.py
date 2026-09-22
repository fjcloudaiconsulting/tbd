"""``structlog.testing.capture_logs`` is banned in this suite (TBD-555).

``app/main.py`` calls ``setup_logging()`` at IMPORT, which bridges structlog
to stdlib logging holding the import-time stdout. Once that is in force
``capture_logs()`` intercepts nothing, so whether a test sees its own events
depends entirely on what ran before it in the same process. Serial,
alphabetical collection hid that; ``pytest -n 6 --dist loadfile`` does not,
and file-to-worker assignment shifts whenever a test file is added or
renamed -- so it is latent instability, not a parallelism problem.

Measured on this tree before the sweep, two full parallel runs of the
IDENTICAL source gave **2 failures** and then **9**. Nothing changed between
them but which worker collected which file.

⚠ The worst shape is not the red run, it is the green one. An assertion that
a list of captured events is EMPTY -- "we did NOT log this" -- passes
trivially when capture is broken. Two such tests in
``test_auth_debug_logging_gate.py`` were green for months while proving
nothing: deleting the production guard they exist to protect did not fail
them. A ceiling cannot detect its own death.

**The remedy** (used by every converted file): bind a recorder onto the
module's own logger and assert against that. It is immune to global structlog
configuration, and in every case it made the assertion strictly stronger --
exact event lists and exact field values instead of substring matching over a
flattened blob.

This fence parses with ``ast`` rather than grepping, because a grep is
satisfied by a comment and this file's own docstring would trip it. Only real
CALLS and IMPORTS count; prose mentioning ``capture_logs`` (as the converted
files do, to explain why they do not use it) is deliberately fine.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

# Nothing is allow-listed. Every call site in the suite was converted; if a
# future one genuinely cannot be, pin it HERE with its reason rather than
# weakening the fence -- an empty allow-list is the whole point.
ALLOWED: dict[str, str] = {}


def _capture_logs_uses(source: str) -> int:
    """Real calls to / imports of ``capture_logs``. Comments do not count."""
    tree = ast.parse(source)
    uses = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "capture_logs":
                uses += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "capture_logs":
                    uses += 1
    return uses


def test_no_test_uses_structlog_capture_logs() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = str(path.relative_to(TESTS_ROOT))
        uses = _capture_logs_uses(path.read_text())
        if uses and rel not in ALLOWED:
            offenders.append(f"{rel} ({uses} use{'s' if uses > 1 else ''})")

    assert not offenders, (
        "structlog.testing.capture_logs is banned in this suite -- it is "
        "order-dependent, because app/main.py bridges structlog to stdlib "
        "logging at import and capture_logs then intercepts nothing. Worse, "
        "an assertion that NO event was logged passes trivially once capture "
        "is broken, so the test goes green while proving nothing.\n\n"
        "Bind a recorder onto the module's own logger instead; see "
        "tests/routers/test_auth_debug_logging_gate.py for the pattern.\n\n"
        "Offending files:\n  " + "\n  ".join(offenders)
    )


def test_the_fence_ignores_prose_but_catches_a_real_call() -> None:
    """The fence must not be satisfiable by a comment, and must still fire on
    a real call. Without this, a matcher that greps source text would pass on
    every converted file (they all discuss ``capture_logs`` in prose) and the
    ban above would be silently unenforced.
    """
    prose_only = (
        '"""We deliberately avoid capture_logs here."""\n'
        "# capture_logs() is banned -- see TBD-555\n"
        "x = 'capture_logs'\n"
    )
    assert _capture_logs_uses(prose_only) == 0

    real_call = "from structlog.testing import capture_logs\nwith capture_logs() as logs:\n    pass\n"
    assert _capture_logs_uses(real_call) == 2  # the import and the call

    attribute_call = "import structlog\nwith structlog.testing.capture_logs() as logs:\n    pass\n"
    assert _capture_logs_uses(attribute_call) == 1
