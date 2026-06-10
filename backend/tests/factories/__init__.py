"""Shared test-infra factories.

See :mod:`tests.factories.app` for ``make_test_app`` — the single place that
builds a configured FastAPI test app with the ``get_db`` /
``get_current_user`` / ``get_session_factory`` dependency overrides every
router test was previously hand-rolling.
"""

from tests.factories.app import make_test_app


__all__ = ["make_test_app"]
