"""TBD-592: the enforced AI consent version equals the published AI Terms version.

An org's consent counts only for the exact string in
``ai_native_current_consent_version``; ``/ai-terms`` publishes
``frontend/lib/aiTerms.ts``. Both are pinned to one fixture, this test on the
backend side and ``frontend/tests/ai-terms-page.test.tsx`` on the other.

It reads the field DEFAULT, not ``settings``: the default is what ships, and a
local ``.env`` pin must not turn this red (or green). FAILS, never regenerates.
"""
from __future__ import annotations

import json
import pathlib

from app.config import Settings


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    # Same marker walk as test_period_status_frontend_contract.py: parents[2]
    # is the repo root only on a host checkout, not in the container.
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "ci.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root containing .github/workflows/ci.yml.")


FIXTURE = (
    _find_repo_root(pathlib.Path(__file__).resolve())
    / "frontend" / "tests" / "fixtures" / "ai-terms-version.json"
)


def test_enforced_consent_version_is_the_published_ai_terms_version():
    published = json.loads(FIXTURE.read_text())["version"]
    enforced = Settings.model_fields["ai_native_current_consent_version"].default
    assert enforced == published, (
        "The consent version the backend enforces differs from the AI Terms version "
        "the frontend publishes. Change both deliberately, together with the fixture."
    )
