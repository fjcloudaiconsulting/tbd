"""TBD-212 — the Python side of the feature-catalog cross-language contract.

`frontend/tests/fixtures/feature-catalog.json` is generated from
:data:`app.auth.feature_catalog.ALL_FEATURE_KEYS` by the deliberately-manual
`scripts/regen_feature_catalog_fixture.py`. Until this module existed, that
fixture was pinned **only** on the TypeScript side
(`frontend/tests/lib/feature-catalog.test.ts`), which compares the fixture
against `FEATURE_LABELS` — two frontend artifacts that agree with each other
whether or not either agrees with Python.

`test_period_status_frontend_contract.py` already named this gap in prose:

    The repo's only other cross-language sync mechanism
    (`scripts/regen_feature_catalog_fixture.py`) is explicitly manual and is
    pinned by no backend test, so a Python-side change can drift past it
    silently while the TypeScript guard stays green.

`app/auth/feature_catalog.py`'s own docstring is stronger, and was wrong:
"Adding a key is a one-line edit here + a one-line edit in
frontend/lib/feature-catalog.ts; **the drift-guard test pins parity.**" No test
pinned that parity. This module is what makes the sentence true.

⚠ **The named wrong implementation, stated precisely — because the obvious
formulation of it is wrong and was measured to be wrong.**

Adding a key to `FeatureKey` and `PlanFeatures` and stopping there is NOT
silent: three existing tests fail, all on hardcoded "update me when the
catalog changes" literals —
`tests/routers/test_admin_feature_state.py::test_feature_state_returns_all_keys`
(a row count and a literal key list, the latter sitting *alongside* a
`sorted(ALL_FEATURE_KEYS)` comparison that tracks the mutant harmlessly),
`tests/services/test_feature_service.py::test_default_false_when_no_subscription`,
and
`tests/services/test_plan_service.py::test_canonicalize_partial_merges_with_existing`.
Measured: 3 failed, 32 passed.

The mutant that matters is therefore the **completed** edit — what a developer
actually lands, because those three failures force them to. Add the key to
`FeatureKey`, to `PlanFeatures`, and to every hardcoded expectation above.
Measured on that state, before this module existed, with::

    pytest tests/routers/test_admin_feature_state.py \
           tests/auth/test_feature_deps.py \
           tests/services/test_ai_feature_map.py \
           tests/services/test_feature_service.py \
           tests/services/test_plan_service.py \
           tests/services/test_ai_service.py

  * backend catalog suites: **35 passed, 0 failed**. Every remaining
    assertion either tracks `ALL_FEATURE_KEYS` self-referentially or was
    just updated by hand.
  * frontend suite: green, and necessarily so — the mutant touches only
    Python, while `frontend/tests/lib/feature-catalog.test.ts` compares two
    untouched artifacts (`FEATURE_LABELS` and the fixture) against each
    other. Two frontend files agreeing with each other says nothing about
    whether either agrees with Python.

Deliberately no line numbers above: a docstring that mis-cites the lines it
claims to have measured is the cheapest possible signal that the measurement
was reconstructed rather than observed. Test function names do not drift.

The observable consequence is that the backend gates a feature the frontend
has no label for, with every suite green. `test_keys_match_the_python_catalog`
is the assertion that goes red — measured RED on exactly that state, and green
again on restore.

⚠ **This test FAILS; it does not regenerate.** Same rule as its sibling
contract module, for the same reason: a changed key set is a change to the
wire meaning of the catalog and must be reviewed, not silently absorbed.
Regenerate deliberately with :data:`REGEN` and review the diff.

⚠ **Path resolution is deliberately LAZY here**, unlike the sibling module,
which builds its fixture path at import time and therefore raises at module
scope — a *collection error*, not a skip — when the marker directories are
absent. TBD-337 records that exact shape as the leading hypothesis for a
backend suite baseline that differs by 5 between agent stacks, which poisons
every delta gate. Resolving inside the test turns the same condition into a
loud, attributable failure of these two tests only.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.auth.feature_catalog import ALL_FEATURE_KEYS

#: ⚠ Run this on the HOST, from the repo root. The fixture directory is
#: mounted read-only into the backend container, so an in-container run
#: cannot write it.
REGEN = "python scripts/regen_feature_catalog_fixture.py"


def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """Walk upward from `start` until a directory holding both
    `.github/workflows/deploy.yml` and `.do/app.yaml` is found.

    Same marker walk, and the same rationale, as
    `tests/test_period_status_frontend_contract.py`: `parents[2]` is correct
    only from a host checkout. Inside the backend container this file lives at
    `/app/tests/...`, so `parents[2]` resolves to `/` and the fixture path
    becomes `/frontend/tests/fixtures/...`. Both marker directories are
    mounted at `/app` (see the `.github` / `.do` read-only mounts in
    docker-compose.yml).
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "deploy.yml").exists() and (
            candidate / ".do" / "app.yaml"
        ).exists():
            return candidate
    raise RuntimeError(
        "Could not locate repo root containing .github/workflows/deploy.yml "
        "and .do/app.yaml. Run these tests from a checked-out repo."
    )


def _fixture_path() -> pathlib.Path:
    return (
        _find_repo_root(pathlib.Path(__file__).resolve())
        / "frontend"
        / "tests"
        / "fixtures"
        / "feature-catalog.json"
    )


def _load() -> dict:
    fixture = _fixture_path()
    if not fixture.exists():  # pragma: no cover - guards a moved fixture
        # ⚠ This FAILS; it never skips. A guard that skips is absent exactly
        # where a human runs it, and it fails open.
        pytest.fail(
            f"feature-catalog fixture missing at {fixture}. If it is missing "
            "inside the backend container, the frontend fixture mount is "
            "absent from docker-compose.yml; recreate the service."
        )
    return json.loads(fixture.read_text())


def test_keys_match_the_python_catalog() -> None:
    """FENCE. The fixture's key set equals `ALL_FEATURE_KEYS` exactly.

    Kills the mutant named in the module docstring: a key added to
    `FeatureKey` (and `PlanFeatures`) without regenerating the fixture. Both
    directions matter and both are asserted by the equality — an ADDED Python
    key leaves the fixture short, and a REMOVED Python key leaves the fixture
    carrying a key the backend no longer honours.
    """
    payload = _load()
    assert set(payload["keys"]) == set(ALL_FEATURE_KEYS), (
        "feature-catalog.json is out of sync with ALL_FEATURE_KEYS. This is a "
        "real drift, not a stale artifact to paper over: the backend and the "
        f"frontend disagree about which features exist. Regenerate with "
        f"`{REGEN}` and REVIEW the diff."
    )


def test_fixture_is_reproducible_from_the_generator() -> None:
    """FENCE (artifact reproducibility, not catalog drift).

    Byte-compares the fixture against exactly what
    `scripts/regen_feature_catalog_fixture.py` writes. Set-equality above
    cannot see any of: an appended rather than re-sorted key, a duplicated
    key, changed indentation, a missing trailing newline, or an extra
    top-level JSON key. Each of those makes the file stop being reproducible
    from its generator, so the next regen emits a spurious diff — and the one
    after it hides a real one inside the noise.

    The sibling module fences the same property for its own fixture and
    records that the fixture once shipped wrong while every value in it was
    correct.

    ⚠ The generator cannot be imported to build the expected value: it writes
    the fixture at module scope, so importing it would mutate the repo, and
    inside the backend container that path is a read-only mount and the import
    would fail. The literal below is therefore kept byte-identical to the
    generator's `json.dumps(..., indent=2) + "\\n"` by hand.
    """
    expected = json.dumps({"keys": sorted(ALL_FEATURE_KEYS)}, indent=2) + "\n"
    assert _fixture_path().read_text() == expected, (
        "feature-catalog.json is not byte-identical to its generator's "
        f"output. Regenerate with `{REGEN}` rather than hand-editing, and "
        "review the diff."
    )
