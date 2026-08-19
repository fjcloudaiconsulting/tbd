"""Fence: `backend/.test_durations` must keep describing the suite it splits.

## Why this exists

`.test_durations` feeds pytest-split, which balances the four (now six) CI
shards. It was written once on 2026-06-09 by PR #425 -- the commit that
introduced sharding -- and then never regenerated. By 2026-08-19 it was **130
commits** stale:

    2219 entries / 537.2s / 212 files recorded
    351 test files on disk, 139 (39.6%) with NO entry at all
    node coverage ~= 60%

The cost was measured on run 32277542427: shard 1 took 485s and shard 4 took
262s, a 2.00x max/min spread that set the whole run's 8m21s critical path.
Nothing was red, nothing was flaky, and nobody noticed for two and a half
months. That is what this file exists to prevent.

## Why coverage rather than age

`git checkout` does not preserve mtimes and CI does a depth-1 clone, so a
"last modified" signal is unavailable in the place it matters. Age is also the
wrong question: a year-old file for a frozen suite is perfectly good.

## Why NODE-id coverage rather than file coverage

A file-level metric scores a file as covered when a single one of its tests is
recorded. Measured on the stale file, 41 of the 212 recorded files contained
MORE `def test_` than they had entries, hiding **234** test functions inside
files a file-level fence would call 100% covered --
`test_account_balance_forecast_service.py` had 50 test functions and 8 entries.
Parametrization widens the gap further (89 `@pytest.mark.parametrize`
decorators), because collected node ids exceed `def` count.

## Why the bar is 80/90 and not something tighter

⚠ The mechanism here is easy to get backwards. `pytest_split.algorithms.
_get_items_with_durations` gives an unrecorded test `avg_duration_per_test` --
**the mean, not zero**. So a missing entry costs `|actual - mean|`, not
`|actual|`. That is why ~60% coverage was survivable for months, and why a
95-98% bar would be wrong: it presumes missing entries weigh their full
runtime, so it would go red within a week or two of every regeneration. A
fence that is red almost always is a fence somebody deletes.

The surviving derivation anchors to the real failure mode -- a shard
overshooting far enough to push `Backend Checks` back above the ~347s
`Frontend Checks` floor, which is the project's stated CI target since
TBD-421. 80% is the last decile that keeps double-digit-percent margin.

No single PR can cross it: the largest test file on disk is ~1.4% of collected
items and the top three combined are ~4%. Nobody gets cornered into raising
the threshold to ship, which is the other way fences die.

## If this is RED

Run `gh workflow run test-durations.yml`, download the `test-durations`
artifact, and commit it. Do NOT regenerate locally: `.test_durations` is
root-owned inside the dev container while pytest runs as uid 1001, so
`--store-durations` dies with PermissionError at `pytest_sessionfinish` after
running the entire suite, and local per-test times are measurably NOT a
uniform rescaling of runner times.
"""
import json
import os
import warnings
from pathlib import Path

import pytest

from tests._durations_registry import COLLECTED_NODEIDS

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DURATIONS_PATH = BACKEND_ROOT / ".test_durations"

# Error below this; the shards stop being trustworthy.
MIN_NODE_COVERAGE = 0.80
# Warn below this; roughly one sprint-half of drift ahead of the error bar.
WARN_NODE_COVERAGE = 0.90
# Entries pointing at tests that no longer exist. 0 on a --clean-durations
# harvest; a non-trivial ratio means the file came from another branch.
MAX_STALE_RATIO = 0.15
# ⚠ Anti-vacuity floor. See the module docstring and test_collection_capture.
MIN_COLLECTED = 3000


@pytest.fixture(scope="module")
def durations() -> dict:
    assert DURATIONS_PATH.exists(), (
        f"{DURATIONS_PATH} is missing. pytest-split cannot balance the CI "
        "shards without it. Run `gh workflow run test-durations.yml`."
    )
    return json.loads(DURATIONS_PATH.read_text())


def _collected() -> set[str]:
    """The FULL collected node-id set, captured before pytest-split deselects."""
    return set(COLLECTED_NODEIDS)


def test_collection_capture_sees_the_whole_suite():
    """⚠ The guard for the guard.

    Every other assertion in this file divides by the collected set. If the
    conftest hook ever stopped seeing the whole suite -- by being moved after
    pytest-split's `trylast` deselection, or by returning early -- coverage
    would be computed against a single shard's ~1/N slice and report inflated
    health. The two likeliest accidental breakages (a moved test tree, a
    conftest refactor) would then both read GREEN.

    This mirrors `assert len(jobs) >= 4` in test.yml's wiring guard, which
    exists for the same reason.
    """
    collected = _collected()
    if len(collected) < MIN_COLLECTED and not os.environ.get("CI"):
        pytest.skip(
            f"only {len(collected)} node ids collected; this looks like a "
            "partial local run (`pytest tests/one_file.py`). Run the full "
            "suite, or set CI=1 to force the check."
        )
    assert len(collected) >= MIN_COLLECTED, (
        f"Only {len(collected)} node ids were captured, expected "
        f">= {MIN_COLLECTED}. Either the suite shrank drastically, or "
        "conftest.pytest_collection_modifyitems is no longer running BEFORE "
        "pytest-split deselects -- in which case every coverage number in "
        "this file is being computed against one shard and is meaningless."
    )


def test_durations_file_covers_the_collected_suite(durations):
    collected = _collected()
    if len(collected) < MIN_COLLECTED and not os.environ.get("CI"):
        pytest.skip("partial local run; see test_collection_capture_sees_the_whole_suite")

    recorded = set(durations)
    covered = collected & recorded
    coverage = len(covered) / len(collected)
    missing = sorted(collected - recorded)

    if coverage < WARN_NODE_COVERAGE:
        message = (
            f".test_durations covers {coverage:.1%} of collected tests "
            f"({len(missing)} unrecorded). Regenerate it: "
            "`gh workflow run test-durations.yml`."
        )
        warnings.warn(message, stacklevel=2)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning file=backend/.test_durations::{message}")

    assert coverage >= MIN_NODE_COVERAGE, (
        f".test_durations covers only {coverage:.1%} of the {len(collected)} "
        f"collected tests ({len(missing)} unrecorded), below the "
        f"{MIN_NODE_COVERAGE:.0%} floor. pytest-split weights every "
        "unrecorded test at the mean, so the CI shards are being balanced "
        "against a suite that no longer exists.\n\n"
        "Fix: `gh workflow run test-durations.yml`, then commit the "
        "`test-durations` artifact. Do NOT regenerate locally -- see this "
        "module's docstring.\n\n"
        f"First unrecorded tests:\n  " + "\n  ".join(missing[:10])
    )


def test_durations_file_is_not_full_of_deleted_tests(durations):
    collected = _collected()
    if len(collected) < MIN_COLLECTED and not os.environ.get("CI"):
        pytest.skip("partial local run; see test_collection_capture_sees_the_whole_suite")

    recorded = set(durations)
    orphans = sorted(recorded - collected)
    ratio = len(orphans) / len(recorded) if recorded else 0.0

    assert ratio <= MAX_STALE_RATIO, (
        f"{len(orphans)} of {len(recorded)} entries ({ratio:.1%}) point at "
        f"tests that are not collected, above the {MAX_STALE_RATIO:.0%} bar. "
        "A --clean-durations harvest leaves zero. A large ratio usually means "
        "the file was generated on a different branch or worktree.\n\n"
        f"First orphans:\n  " + "\n  ".join(orphans[:10])
    )


def test_thresholds_are_not_quietly_relaxed():
    """⚠ Pinned deliberately, as test.yml:166 pins its ALLOWLIST.

    Relaxing a bar here must be a two-place edit that a reviewer sees, not the
    reflex fix for a red build. If this is in your way, the remedy is
    `gh workflow run test-durations.yml`, not a smaller number.
    """
    assert (MIN_NODE_COVERAGE, WARN_NODE_COVERAGE, MAX_STALE_RATIO, MIN_COLLECTED) == (
        0.80,
        0.90,
        0.15,
        3000,
    ), (
        "The freshness thresholds were changed. These are derived in this "
        "module's docstring from the ~347s Frontend Checks floor; changing "
        "them changes what 'the shards are balanced' means. Regenerate the "
        "durations file instead."
    )
