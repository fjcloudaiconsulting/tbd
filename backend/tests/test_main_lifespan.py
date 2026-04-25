from pathlib import Path

import app.main as main_module


MAIN_SOURCE = Path(main_module.__file__).read_text()


def test_main_does_not_invoke_subprocess_for_migrations():
    """Migrations are owned by backend/entrypoint.sh, NOT the FastAPI lifespan.

    Catches a regression to the previous pattern where lifespan ran
    `alembic upgrade head` and the production path was deliberately skipped
    via APP_ENV branching — the exact arrangement that let migration 025
    sit un-applied on prod for hours after PR #79 merged.

    If you're moving migration logic back into Python, re-architect both
    the entrypoint and this test together so the contract stays explicit."""
    assert not hasattr(main_module, "_run_migrations"), (
        "_run_migrations must not be re-introduced in app.main; "
        "migrations are an image-level concern (entrypoint.sh)."
    )
    assert "alembic upgrade head" not in MAIN_SOURCE, (
        "app.main must not shell out to alembic; entrypoint.sh handles it."
    )
    assert "import subprocess" not in MAIN_SOURCE, (
        "app.main no longer needs subprocess once migrations are in entrypoint.sh."
    )
