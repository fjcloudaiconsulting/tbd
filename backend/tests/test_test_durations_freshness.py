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

Run `gh workflow run test-durations.yml --ref <your-branch>` (a bare
dispatch targets the DEFAULT branch and would harvest main's suite, not
yours), download the `test-durations`
artifact, and commit it. Do NOT regenerate locally: `.test_durations` is
root-owned inside the dev container while pytest runs as uid 1001, so
`--store-durations` dies with PermissionError at `pytest_sessionfinish` after
running the entire suite, and local per-test times are measurably NOT a
uniform rescaling of runner times.
"""
import json
import os
import sys
import warnings
from pathlib import Path

import pytest
import yaml

from tests._durations_registry import COLLECTED_NODEIDS, COLLECTED_ORDER

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
# Share of entries that must be distinct. A file whose values were flattened
# (every test at the mean) has exactly ONE. The real file is >99% distinct, so
# this bar has enormous headroom and only fires on genuine degeneracy.
MIN_DISTINCT_VALUE_RATIO = 0.50
# Worst tolerated max/min across simulated shards. Today's file is ~1.01x.
MAX_SHARD_IMBALANCE = 1.30


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root containing .github/workflows/test.yml")


# Read from the workflow rather than hardcoded, so the simulation always models
# the shard count CI actually uses. test_ci_shard_config.py fences the workflow
# side of this pair.
SHARD_COUNT = len(
    yaml.safe_load(
        (_find_repo_root(Path(__file__).resolve()) / ".github" / "workflows" / "test.yml").read_text()
    )["jobs"]["backend-shard"]["strategy"]["matrix"]["group"]
)


@pytest.fixture(scope="module")
def durations() -> dict:
    assert DURATIONS_PATH.exists(), (
        f"{DURATIONS_PATH} is missing. pytest-split cannot balance the CI "
        "shards without it -- and it degrades SILENTLY, weighting every test "
        "at the mean with no warning.\n\n"
        "⚠ In the dev container this usually means the single-file bind mount "
        "was severed: `git pull`/`git checkout` REPLACE the file's inode, and "
        "a running container keeps the old one. Fix that first:\n"
        "    docker compose up -d --force-recreate backend\n\n"
        "If the file genuinely does not exist, regenerate it:\n"
        "    gh workflow run test-durations.yml --ref <branch>"
    )
    return json.loads(DURATIONS_PATH.read_text())


def _skip_if_partial_run(collected: set[str]) -> None:
    """Skip on a deliberately partial local run -- but LOUDLY.

    ⚠ These fences measure the whole collected suite, so `pytest
    tests/test_test_durations_freshness.py` cannot evaluate them and skips. That
    is the single most likely command someone runs to check this file, and a
    silent skip reads as a pass: with the conftest capture hook broken, a bare
    local run reports `1 passed, 3 skipped, exit 0`.

    They can never skip in real CI -- GitHub Actions always sets `CI=true` -- so
    the fence is live where it counts. This just stops a local check from being
    mistaken for verification.
    """
    if os.environ.get("CI"):
        return
    if len(collected) >= MIN_COLLECTED:
        return
    message = (
        f"DURATIONS FENCE DID NOT RUN: only {len(collected)} node ids collected "
        f"(need >= {MIN_COLLECTED}). This is a partial run, so nothing here was "
        "verified. Run the full suite, or force it with CI=1."
    )
    print(f"\n!!! {message}", file=sys.stderr)
    warnings.warn(message, stacklevel=2)
    pytest.skip(message)


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
    _skip_if_partial_run(collected)
    assert len(collected) >= MIN_COLLECTED, (
        f"Only {len(collected)} node ids were captured, expected "
        f">= {MIN_COLLECTED}. Either the suite shrank drastically, or "
        "conftest.pytest_collection_modifyitems is no longer running BEFORE "
        "pytest-split deselects -- in which case every coverage number in "
        "this file is being computed against one shard and is meaningless."
    )


def test_durations_file_covers_the_collected_suite(durations):
    collected = _collected()
    _skip_if_partial_run(collected)

    recorded = set(durations)
    covered = collected & recorded
    assert collected, "no node ids collected; see test_collection_capture_sees_the_whole_suite"
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
    _skip_if_partial_run(collected)

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
    """⚠ Pinned deliberately, the way test.yml's wiring guard pins its ALLOWLIST.

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


def _simulate_shards(durations: dict, splits: int) -> list[float]:
    """Partition the real collection order with pytest-split's own algorithm.

    Uses `DurationBasedChunksAlgorithm` directly rather than reimplementing it,
    so this cannot drift from what CI actually does. The algorithm only needs
    `.nodeid` off each item.
    """
    from pytest_split.algorithms import DurationBasedChunksAlgorithm

    class _Item:
        __slots__ = ("nodeid",)

        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    items = [_Item(n) for n in COLLECTED_ORDER]
    groups = DurationBasedChunksAlgorithm()(splits, items, dict(durations))
    return [g.duration for g in groups]


def test_recorded_durations_are_not_degenerate(durations):
    """⚠ Every other assertion in this file looks only at the KEYS.

    A file with perfect key coverage and zero orphans, whose VALUES have been
    flattened to a constant, passes all of them -- and produces a 2.41x shard
    spread, worse than the 2.00x this ticket was written to fix. That is not a
    hypothetical: it is exactly the shape of a hand-committed local harvest,
    which the generator workflow's header warns against.

    ⚠ NOTE ON A FIX THAT DOES NOT WORK. The obvious check -- partition using
    the file, then score the partition using the file -- is CIRCULAR and this
    mutant survives it: measured, a flattened file self-scores at 1.004x
    because equal weights produce equal-count chunks that sum equally. Scoring
    against ground truth would catch it, but a test running in CI has no
    ground truth other than this file. So the check has to be on the value
    DISTRIBUTION instead.

    ⚠ WHAT THIS STILL CANNOT SEE: values that are plausibly distributed but
    assigned to the wrong tests (a rank-inverted or shuffled file). Nothing
    computable from this file alone can detect that; only comparing against a
    fresh runner harvest can. That is a reason to regenerate from CI rather
    than by hand, not a reason to weaken this test.
    """
    values = list(durations.values())
    assert values, ".test_durations is empty"

    assert all(v >= 0 for v in values), (
        "negative durations in .test_durations; the file is corrupt."
    )
    assert any(v > 0 for v in values), (
        "every recorded duration is zero. pytest-split raises IndexError on an "
        "all-zero file, and the shards carry no timing information at all."
    )

    distinct_ratio = len(set(values)) / len(values)
    assert distinct_ratio >= MIN_DISTINCT_VALUE_RATIO, (
        f"only {len(set(values))} distinct values across {len(values)} entries "
        f"({distinct_ratio:.1%}, floor {MIN_DISTINCT_VALUE_RATIO:.0%}). A real "
        "harvest is >99% distinct. A near-constant file means the timings were "
        "synthesised rather than measured -- the keys will pass every other "
        "check here while the shards balance no better than a plain count "
        "split. Regenerate: `gh workflow run test-durations.yml --ref <branch>`."
    )


def test_recorded_durations_partition_into_balanced_shards(durations):
    """The invariant the ticket is actually about: the file must produce
    balanced shards under the real algorithm.

    ⚠ Self-scored, so it is deliberately NOT the primary defence against bad
    VALUES -- see test_recorded_durations_are_not_degenerate for why that is
    circular. What it does catch is a file that cannot be balanced at all: one
    indivisible long-pole test, or a distribution so skewed that no contiguous
    cut is even, which no shard count can fix and which the coverage metric is
    blind to.
    """
    collected = _collected()
    _skip_if_partial_run(collected)

    assert len(COLLECTED_ORDER) == len(collected), (
        f"COLLECTED_ORDER has {len(COLLECTED_ORDER)} entries but "
        f"{len(collected)} were collected; the ordered capture in conftest is "
        "out of step with the set and this simulation is meaningless."
    )

    shards = _simulate_shards(durations, SHARD_COUNT)
    assert min(shards) > 0, f"a simulated shard got zero work: {shards}"
    imbalance = max(shards) / min(shards)

    assert imbalance <= MAX_SHARD_IMBALANCE, (
        f"the recorded durations partition into {SHARD_COUNT} shards with a "
        f"{imbalance:.2f}x max/min spread, above the "
        f"{MAX_SHARD_IMBALANCE:.2f}x bar.\n"
        f"  simulated shard seconds: {[round(s, 1) for s in shards]}\n"
        "Either the file is stale, or one test file is now large enough that "
        "no contiguous split can balance it -- check the longest entries."
    )
