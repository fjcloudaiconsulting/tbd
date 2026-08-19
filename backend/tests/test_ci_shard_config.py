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
