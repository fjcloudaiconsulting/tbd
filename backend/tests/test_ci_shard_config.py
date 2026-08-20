"""Fence: the backend shard count must be consistent across test.yml.

## The hazard

The shard count was, until TBD-421, written out literally in three places in
`.github/workflows/test.yml`: the job `name:`, `strategy.matrix.group`, and the
`--splits` argument to pytest.

Those three can disagree, and the two directions are NOT symmetric:

  * matrix LARGER than `--splits`  -> loud. `pytest_split.plugin` raises
    `UsageError` ("group N is larger than splits M") and the shard fails.
  * matrix SMALLER than `--splits` -> **SILENT TOTAL LOSS.** With
    `--splits 6` and `group: [1, 2, 3, 4]`, groups 5 and 6 are never run.
    One third of the backend suite silently does not execute, every shard
    passes, `Backend Checks` is green, and the PR merges.

The second is this repo's half-fix-leaves-a-door shape, and it is installed by
exactly the edit TBD-421 asked for (raising 4 -> 6). Hence this fence.

The workflow now derives the count from `${{ strategy.job-total }}` in both the
job name and `--splits`, so `matrix.group` is the single source of truth and the
inconsistency is structurally unreachable. This fence exists for the next person
who reverts to literals -- which is the likelier failure, because literals read
more obviously.
"""
import re
from pathlib import Path

import yaml


def _find_repo_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / ".github" / "workflows" / "test.yml").exists():
            return candidate
    raise RuntimeError("Could not locate repo root containing .github/workflows/test.yml")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
WORKFLOW = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "test.yml").read_text())
SHARD_JOB = WORKFLOW["jobs"]["backend-shard"]

DURATIONS_WORKFLOW = yaml.safe_load(
    (REPO_ROOT / ".github" / "workflows" / "test-durations.yml").read_text()
)
HARVEST_JOB = DURATIONS_WORKFLOW["jobs"]["harvest"]

DERIVED = "${{ strategy.job-total }}"
# A GitHub expression contains spaces, so `\S+` would capture only "${{".
EXPR_OR_INT = r"(\$\{\{[^}]*\}\}|\d+)"


def _norm(value: str) -> str:
    """Collapse whitespace so `${{strategy.job-total}}` and
    `${{ strategy.job-total }}` compare equal."""
    return " ".join(value.split())


def _pytest_step() -> dict:
    for step in SHARD_JOB["steps"]:
        if "pytest --splits" in str(step.get("run", "")):
            return step
    raise AssertionError(
        "No step in `backend-shard` runs `pytest --splits`. If the shard "
        "command was renamed, this fence is now blind -- fix it here."
    )


def test_matrix_has_no_axis_other_than_group():
    """⚠ `strategy.job-total` is the PRODUCT of every matrix axis, not
    `len(matrix.group)`.

    Adding a second axis is a normal, likely edit -- `Migration Checks` in this
    same workflow carries a `mysql` matrix of its own. Do it here:

        matrix:
          group: [1, 2, 3, 4, 5, 6]
          python: ["3.12", "3.13"]

    ...and `job-total` becomes 12 while `--group` still only ever takes 1..6.
    CI then runs `--splits 12 --group 1..6`: groups 7-12 never execute, HALF the
    backend suite silently does not run, and every check is green.

    The derived form removed one door and opened this one. If a second axis is
    ever genuinely wanted, stop deriving from `job-total` -- compute splits from
    the group axis explicitly instead.
    """
    matrix = SHARD_JOB["strategy"]["matrix"]
    extra = sorted(set(matrix) - {"group"})
    assert not extra, (
        f"backend-shard's matrix gained the axis/axes {extra}. "
        "`strategy.job-total` is the product over ALL axes, so `--splits` "
        f"would become {len(matrix.get('group', []))} x (the rest) while "
        "`--group` still only takes 1..N. The surplus groups never run and "
        "that fraction of the suite silently does not execute, all green."
    )


def test_group_argument_is_bound_to_the_matrix():
    """⚠ `--group` is half of a two-argument pair and is the likelier typo:
    it sits adjacent to `--splits` on the same line.

    `pytest --splits ${{ strategy.job-total }} --group 1` makes all six shards
    run group 1. Five sixths of the backend suite never executes, every shard
    passes, and `Backend Checks` is green -- the same silent total loss this
    module exists to prevent, through the other argument.
    """
    run = str(_pytest_step()["run"])
    match = re.search(r"--group\s+" + EXPR_OR_INT, run)
    assert match, f"could not find --group in the shard command: {run!r}"
    assert _norm(match.group(1)) == "${{ matrix.group }}", (
        f"--group is {match.group(1)!r}, not `${{{{ matrix.group }}}}`. A "
        "constant here makes every shard run the SAME group and silently drops "
        "the rest of the suite while every check stays green."
    )


def test_durations_path_matches_the_file_the_fence_guards():
    """The freshness fence hardcodes `<backend>/.test_durations`. If the
    workflow splits on a different path the two silently diverge: the fence
    certifies one file while CI balances on another, and pytest-split falls
    back to weighting every test at the mean without printing anything."""
    run = str(_pytest_step()["run"])
    match = re.search(r"--durations-path\s+(\S+)", run)
    assert match, f"could not find --durations-path in: {run!r}"
    assert match.group(1) == ".test_durations", (
        f"--durations-path is {match.group(1)!r}, but "
        "test_test_durations_freshness.py guards `.test_durations`. Point them "
        "at the same file or the fence is guarding nothing CI uses."
    )


def test_shards_do_not_fail_fast():
    """`fail-fast: true` cancels sibling shards on the first failure, so a red
    run reports one failure and cancels up to five shards' worth of unrun
    tests. The aggregate would still go red, but the run would no longer tell
    you what else is broken."""
    assert SHARD_JOB["strategy"].get("fail-fast") is False, (
        "backend-shard must set `fail-fast: false` so one failing shard does "
        "not cancel the others and hide further failures."
    )


def test_matrix_is_the_single_source_of_truth_for_the_shard_count():
    groups = SHARD_JOB["strategy"]["matrix"]["group"]

    # Positive baseline: a mis-parse yielding an empty list must not pass
    # vacuously. Same reason test.yml's own wiring guard asserts len(jobs) >= 4.
    assert len(groups) >= 2, f"parsed only {len(groups)} shard group(s) from test.yml"
    assert groups == list(range(1, len(groups) + 1)), (
        f"matrix.group must be a dense 1..N range, got {groups}. pytest-split "
        "addresses groups as 1..splits; a gap means a slice of the suite is "
        "never run."
    )

    run = str(_pytest_step()["run"])
    match = re.search(r"--splits\s+" + EXPR_OR_INT, run)
    assert match, f"could not find --splits in the shard command: {run!r}"
    splits = _norm(match.group(1))

    if splits != DERIVED:
        assert splits.isdigit() and int(splits) == len(groups), (
            f"--splits is {splits!r} but matrix.group has {len(groups)} entries. "
            "⚠ If --splits EXCEEDS the matrix length, the extra groups are never "
            "run and roughly (splits - len(groups))/splits of the backend suite "
            "silently does not execute, with every check GREEN.\n\n"
            f"Prefer `--splits {DERIVED}` so matrix.group is the only place the "
            "count is written."
        )


def test_shard_job_name_reports_the_real_shard_count():
    """A job name saying `/4` while six shards run is merely confusing -- but it
    is also the first thing anyone reads when diagnosing a shard failure, and a
    stale denominator is how the three-place drift went unnoticed before."""
    groups = SHARD_JOB["strategy"]["matrix"]["group"]
    name = str(SHARD_JOB["name"])

    match = re.search(r"/" + EXPR_OR_INT + r"\s*$", name)
    assert match, f"shard job name has no `/<count>` suffix: {name!r}"
    suffix = _norm(match.group(1))

    if suffix != DERIVED:
        assert suffix.isdigit() and int(suffix) == len(groups), (
            f"shard job name says `/{suffix}` but {len(groups)} shards run. "
            f"Prefer `/{DERIVED}`."
        )


def test_the_harvest_is_sharded_the_same_way_the_suite_is():
    """⚠ The durations harvest must use the SAME shard count as consumption.

    MEASURED (TBD-421, run 32304646118). Harvesting unsharded while consuming
    sharded biases the file by collection position: the harvest is one long
    process, consumption is N short ones, and late-position tests carry the long
    process's accumulated cost. `DurationBasedChunksAlgorithm` cuts CONTIGUOUS
    slices, so chunk N maps to position N and the error concentrates instead of
    cancelling.

        shard  tests  predicted  actual  actual/predicted
          1      624     308s     283s        0.92
          4     1023     308s     181s        0.59
          5      667     308s     153s        0.50

    1.82x real spread against a self-scored prediction of 1.01x. Note the
    freshness fence's own balance simulation CANNOT see this -- it scores the
    file with itself, so a position-biased file still self-scores at 1.01x.
    Matching the shapes is what makes that simulation mean anything.

    A drift in either direction reintroduces the bias silently, because nothing
    downstream is red -- the shards simply stop being balanced again.
    """
    consume = SHARD_JOB["strategy"]["matrix"]["group"]
    harvest = HARVEST_JOB["strategy"]["matrix"]["group"]

    assert len(harvest) >= 2, f"parsed only {len(harvest)} harvest shard(s)"
    assert harvest == consume, (
        f"test-durations.yml harvests in {len(harvest)} shards ({harvest}) but "
        f"test.yml consumes in {len(consume)} ({consume}). The harvest must "
        "measure each test in the same process shape it will run in, or the "
        "timings are biased by collection position and the shards silently "
        "stop balancing."
    )


def test_the_harvest_cleans_durations():
    """⚠ `--clean-durations` is what makes the per-shard artifacts DISJOINT.

    Without it, PytestSplitCachePlugin merges each shard's fresh values on top
    of the whole committed file, so every artifact carries all ~4000 entries and
    the merge overwrites fresh values with stale ones in arbitrary artifact
    order -- silently producing a file that looks regenerated and is not.
    """
    steps = HARVEST_JOB["steps"]
    run = next(
        (str(s.get("run", "")) for s in steps if "--store-durations" in str(s.get("run", ""))),
        None,
    )
    assert run, "no harvest step runs pytest --store-durations"
    assert "--clean-durations" in run, (
        "the harvest omits --clean-durations, so each shard's artifact would "
        "carry the entire stale file and the merge would silently reinstate "
        "stale values over fresh ones."
    )


def test_the_harvest_does_not_run_the_fence_that_reads_its_own_output():
    """⚠ Deadlock guard. MEASURED: run 32306137554.

    `test_test_durations_freshness.py` asserts against the COMMITTED
    `.test_durations`. The harvest runs the whole suite, so it runs that fence
    too -- against the very file it is being run to replace. A file bad enough
    to trip the fence therefore fails the harvest, the merge job is skipped, and
    the only sanctioned remedy for the red fence becomes unreachable. That
    happened: `Harvest shard 6/6` failed on "only 824 distinct values across
    4181 entries", and the workflow could not produce the file that would have
    fixed it.

    The fence still runs for real in test.yml. This job is measuring, not
    validating.
    """
    run = next(
        (str(s.get("run", "")) for s in HARVEST_JOB["steps"] if "--store-durations" in str(s.get("run", ""))),
        None,
    )
    assert run, "no harvest step runs pytest --store-durations"
    assert "--deselect tests/test_test_durations_freshness.py" in run, (
        "the harvest no longer deselects the freshness fence. A committed "
        "durations file bad enough to trip that fence will now also fail the "
        "harvest, making regeneration impossible -- the remedy gated on the "
        "problem it fixes."
    )
