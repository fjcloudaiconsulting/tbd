---
name: ci-shard-rebalance
description: "Regenerate backend/.test_durations, raise the shard count to 6, and fence the file against silent staleness (TBD-421)"
---

# TBD-421 — rebalance the backend shards and fence the durations file

## The defect, measured

`backend/.test_durations` was last written on 2026-06-09 by PR #425, the commit that
introduced sharding. Since then `backend/tests` has taken **130 commits** and nobody
regenerated it.

```
entries: 2219   total: 537.2s   files recorded: 212
test files on disk: 351      with NO entry: 139 (39.6%)
```

`pytest-split` assigns an unrecorded test the **average** duration, so ~40% of the
suite is placed by guesswork. The consequence, measured on run 32277542427:

```
Backend Shard 1/4       485s   <- critical path
Backend Shard 2/4       471s
Frontend Checks         347s
Backend Shard 3/4       298s
Backend Shard 4/4       262s
Migration Checks (8.0)   58s
Migration Checks (8.4)   50s
Backend Checks            8s
```

A 1.85x spread between slowest and fastest shard. That is not sharding, it is luck.

## Rulings

### R1 — shard count: 6

Both architects independently ruled 6, and both noted the win **saturates at 5**:

| config | Backend Checks | run total | saved |
|---|---|---|---|
| today | 497s | 501s | — |
| fresh file, N=4 | 389s | 389s | 112s |
| fresh file, **N=5** | 319s | **347s** | **154s** |
| fresh file, N=6 | 273s | **347s** | **154s** |
| fresh file, N=8 | 215s | **347s** | **154s** |

From N=5 up the run is **frontend-bound** and further shards buy exactly zero
wall-clock. Six is chosen for headroom, not speed: `Backend Checks` crosses the
347s frontend floor when the suite reaches `(347-40)*N` seconds, so N=5 buys about
2 months of growth and N=6 about 7. One extra runner buys five months before this
ticket recurs. That is the whole justification and it should be read as such.

**Reject N=8 and above.** Zero marginal wall-clock, and a real cost: the run uses
~9 concurrent jobs at N=6, so two simultaneous PRs still fit inside the 20-job
public-repo concurrency budget. At N=8 two PRs is ~13 jobs each and the overflow
*adds* wall-clock through queueing — giving back more than the 40s gained.

### R2 — retire the `<3 minutes` target

`specs/ci-test-sharding.md:31` targets a sub-3-minute backend check. That target is
now **wrong**, not merely unmet: reaching it needs N=10, and every second below the
347s `Frontend Checks` floor is unobservable in the run's wall clock.

Successor target: **`Backend Checks` must stay below `Frontend Checks`.**

### R3 — generate durations in a SEPARATE workflow, unsharded

`.github/workflows/test-durations.yml`, `workflow_dispatch` + a monthly `schedule`,
one job, same Redis service as `test.yml`, running:

```
pytest --store-durations --clean-durations --durations-path .test_durations
```

then uploading one artifact for a human to commit via an ordinary PR. ~24 minutes
of one free runner.

**Why not harvest from the shards of ordinary runs.** This was the initially
preferred option and it was withdrawn on two findings:

1. `scripts/ci/await-test-run.sh:60-72` reads the **run-level** conclusion of
   `test.yml`, not the `Backend Checks` check-run. So an artifact upload failing in
   *any* shard turns the run conclusion to `failure`, `release.yml` fails closed,
   and **production does not deploy** — for a reason unrelated to whether the tests
   passed. Adding a network-dependent write path to `push: main` puts it on the one
   path the deploy interlock reads.
2. It saves nothing anyway. `test.yml:35-36` is `permissions: contents: read`, so an
   artifact is not a commit; the harvest terminates in the same human ritual as the
   dispatch workflow. Strictly dominated.

**Why not generate locally. Three blockers, the third measured the hard way.**

- `backend/.test_durations` is **not bind-mounted** into the dev backend container
  (`docker-compose.yml` mounts `backend/app`, `alembic`, `scripts`, `tests`, and
  `seed.py`, and nothing else at the backend root). A container run writes to the
  copy baked in at image build and the host file never changes — a silent no-op of
  the same class the `seed.py` mount comment already documents.
- ⚠⚠ **The obvious invocation fails AFTER the full run, not before it.** Measured
  2026-08-19: `docker compose exec backend pytest --store-durations` runs the entire
  suite green to `[100%]` and then dies at `pytest_sessionfinish`:

  ```
  File "/usr/local/lib/python3.12/site-packages/pytest_split/plugin.py", line 223
      with open(self.config.option.durations_path, "w") as f:
  PermissionError: [Errno 13] Permission denied: '/app/.test_durations'
  ```

  The container runs as `uid=1001(backend)`; `/app/.test_durations` is `root:root
  0644` and `/app` is `root:root 0755`. **Twenty-five minutes of test execution is
  discarded at the final step.** Anyone regenerating locally must write to a
  writable path (`--durations-path /tmp/.test_durations`) and `docker cp` the
  result out. This footgun costs a full suite run per rediscovery and is a
  standalone argument for R3.
- Host contention makes the per-test error random rather than systematic. A uniform
  hardware scale factor cancels exactly (both algorithms are scale-invariant), so
  "my laptop is faster" is a non-issue; an unpredictable neighbour process is not,
  because random error cannot be corrected for or detected.

⚠ **`--clean-durations` is load-bearing for any future harvest.** Without it,
`PytestSplitCachePlugin.pytest_sessionfinish` merges the run's fresh values *on top
of the whole stale file*, so a naive union across artifacts overwrites fresh values
with stale ones — silently producing a file that looks regenerated and is not.

### R4 — keep `duration_based_chunks`; do NOT switch to `least_duration`

⚠ **Record the correct reason, because the plausible one is false.** pytest-split
**never reorders tests**. `LeastDurationAlgorithm.__call__` assigns items to groups
by a duration heap and then re-sorts each group by original collection index:

```python
# sort the items by their original index to maintain relative ordering
s = [item for item, original_index in sorted(selected[i], key=lambda tup: tup[1])]
```

So `least_duration` does not conflict with `specs/ci-test-sharding.md:25` ("NO random
ordering") and does not implicate the `capture_logs()` order-dependence class. Any
future reader who rejects it on ordering grounds is rejecting it for a reason that is
not true, and will eventually discover that and reopen a settled decision.

The real reason is **co-location**, measured at N=6 against the stale file:

| algorithm | max shard | spread | files split across shards |
|---|---|---|---|
| `duration_based_chunks` | 90.1s | 1.7s | **5** / 212 |
| `least_duration` | 89.5s | 0.0s | **203** / 212 |

0.6s off the critical path, for a 40x increase in files that execute as a partial
subset no local `pytest tests/test_foo.py` ever reproduces. `duration_based_chunks`
gives each shard a contiguous slice of collection order, so exactly N-1 files
straddle a boundary. Keep the default; this is a no-change decision.

## The fence

`backend/tests/test_test_durations_freshness.py`, riding the existing shards — no new
CI job, no `jobs.backend.needs` wiring. Precedent: `test_deploy_workflow.py`,
`test_await_test_run_gate.py`.

### Capturing the full collected set

⚠ Under `--splits`, `session.items` at test time is only ~1/N of the suite, and it is
a **non-random** 1/N — pytest-split places recorded tests deterministically, so
measuring coverage over it is biased *toward looking healthy*. Capture the whole set
in `backend/tests/conftest.py`:

```python
COLLECTED_NODEIDS: set[str] = set()

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    COLLECTED_NODEIDS.update(i.nodeid for i in items)
```

`tryfirst` runs **before** `PytestSplitPlugin.pytest_collection_modifyitems`, which is
declared `@hookimpl(trylast=True)`, and pytest-split *deselects* rather than reducing
collection — so inside any shard this set is the whole suite.

### Assertions

| id | asserts | threshold |
|---|---|---|
| T1 | node-id coverage: `|durations ∩ collected| / |collected|` | **error < 0.80, warn < 0.90** |
| T2 | stale ratio: `|durations \\ collected| / |durations|` | **error > 0.15** (0.0 today) |
| T3 | positive baseline: `len(COLLECTED_NODEIDS)` | **>= 3000** |

### Why 80/90 and not a tighter bar

⚠ **The mechanism that decides this is easy to get wrong, and one architect's first
derivation did.** `algorithms._get_items_with_durations` assigns every unrecorded test
`avg_duration_per_test`, computed from the recorded set. **An unmodelled test is not
weighted zero — it is weighted the mean.** So the error a missing entry contributes is
`|actual - mean|`, not `|actual|`.

That is why the suite has tolerated ~60% coverage for two and a half months without
anyone filing anything, and it is why a 98%-style bar is wrong: it presumes missing
entries weigh their full runtime, would be red within a week or two of every fresh
harvest, and **a fence that is red almost always is a fence somebody disables** — the
exact vacuity failure mode the design is supposed to prevent.

The surviving derivation anchors to the real failure mode: a shard overshooting far
enough to push `Backend Checks` back above the 347s `Frontend Checks` floor. Empirical
misallocation constant 0.71, fitted from run 32277542427 (single-point fit, stated as
such; it is used only to rank thresholds against each other and the ranking is
monotone in it), giving `overshoot ~= 0.71 * (1-c) * T / N`:

| coverage | overshoot | shard job | margin vs 347s floor |
|---|---|---|---|
| 0.90 | 16s | ~291s | 56s (16%) |
| **0.80** | **31s** | **~306s** | **41s (13%)** |
| 0.70 | 47s | ~322s | 25s (7%) |
| 0.60 | 63s | ~338s | 9s (3%) |

80% is the last decile keeping double-digit-percent margin. At 60% one slow new test
file makes the backend critical again. The threshold and the target (R2) are therefore
derived from **one** number, not two unrelated ones.

Warn at 90%: measured churn is ~4.5 coverage points lost per week, so the warning fires
about once a sprint-half and the error bar is a month behind it. The warning cannot
block a merge, so there is no incentive to weaken it — only to act on it.

**No single PR can flip this fence red.** The largest test file on disk is 50 tests =
1.35% of items; the top three combined are 4.0%. From a fresh harvest it would take
~10 of the largest PRs ever merged to cross 80%. The PR that gets cornered into raising
the threshold to ship does not exist.

⚠ **Node-id coverage, not file coverage.** 41 of the 212 currently-recorded files
contain MORE `def test_` than they have entries — **234 test functions hiding inside
files a file-level metric scores as 100% covered.** Two files are egregious:
`test_account_balance_forecast_service.py` has 50 test functions and 8 entries;
`test_billing_service.py` has 34 and 4. There are also 89 `@pytest.mark.parametrize`
decorators, so collected node count exceeds def count and true node coverage is lower
than any figure derived from defs.

⚠ **Not age.** `git checkout` does not preserve mtimes, and `actions/checkout` at
`test.yml:90` sets no `fetch-depth`, so it is a depth-1 clone and
`git log -1 -- backend/.test_durations` may return nothing. Age is also not the
defect: a year-old file for a frozen suite is fine.

### Designed against being weakened rather than obeyed

1. **Make obeying cheaper than weakening.** The failure message prints
   `gh workflow run test-durations.yml` and nothing else. A fence whose remedy is a
   20-minute local ritual gets weakened; a fence whose remedy is one command gets run.
2. **Pin the thresholds in a second place**, as `test.yml:166` pins its `ALLOWLIST`,
   so relaxing them is a deliberate two-place edit rather than a reflex fix.
3. **Put the numbers in the docstring** — this file rotted to ~55% node coverage over
   130 commits and cost ~110s on every CI run. A reviewer can wave through a threshold
   change; they cannot wave it through next to that sentence.
4. **Name the offenders** in the failure output — count plus the first 10 missing
   files. A red fence that says *which* files are missing invites regeneration; one
   that says `0.55 < 0.95` invites editing the constant.

## ⚠ Discovered while building: conftest.py is imported TWICE, under two names

The collection-capture hook was first written as a module-level set inside
`backend/tests/conftest.py`, with the fence doing
`from tests.conftest import COLLECTED_NODEIDS`. That produces **two distinct
module objects with two distinct sets**:

```
MODULES HOLDING THE SET: {'conftest': ..., 'tests.conftest': ...}
  conftest:       4172 nodeids     <- the copy whose hook actually fires
  tests.conftest:    0 nodeids     <- the copy the fence was reading
```

pytest loads the conftest as top-level `conftest`; any `from tests.conftest
import ...` triggers a second import under the dotted name. The hook mutates one
set, the reader sees the other.

This would have failed loudly in CI rather than passing silently (the
`MIN_COLLECTED` floor fires on 0), but it would have failed for a reason with
nothing to do with staleness, and the obvious "fix" is to delete the floor.

**Resolution:** the set lives in `backend/tests/_durations_registry.py`, which
both sides import by the same dotted name. Do not move it back into conftest.

⚠ Generalises beyond this ticket: **any cross-module state shared between a
conftest hook and a test is subject to this.** `backend/tests/` has no
`__init__.py`, so both import paths resolve and neither errors.

## ⚠ Also discovered while building: the shard-count fence needed its own fix

The first version of `test_ci_shard_config.py` matched `--splits\s+(\S+)`, which
captures `${{` from a GitHub expression because the expression contains spaces.
The fence failed against a correct workflow. Fixed with an expression-aware
pattern plus whitespace normalisation. Recorded because the failure was in the
FENCE, not the subject — a fence that is red against correct code is the inverse
defect class, and it is the one that gets fences deleted.

## ⚠ The hazard this change installs if done carelessly

The shard count is encoded in **three** places:

- `test.yml:71` — `name: Backend Shard ${{ matrix.group }}/4`
- `test.yml:76` — `group: [1, 2, 3, 4]`
- `test.yml:111` — `pytest --splits 4 --group ...`

A matrix **larger** than `--splits` is loud: `pytest_split/plugin.py:99-100` raises
`UsageError`. A matrix **smaller** than `--splits` is **silent total loss** — set
`--splits 6` and leave the matrix at `[1,2,3,4]` and **one third of the suite never
runs, with every check green.**

That is this repo's half-fix-leaves-a-door shape, installed by the very edit this
ticket requests. Two mitigations, both in this PR:

1. Collapse to one source of truth using `${{ strategy.job-total }}` in both the job
   name and the `--splits` argument, so only `matrix.group` changes.
2. A fence assertion that parses `test.yml` and asserts the matrix length equals the
   `--splits` argument equals the job-name suffix.

## Verification — inject and confirm RED

| # | mutation | must |
|---|---|---|
| M1 | restore the stale file: `git show d1e66ea0:backend/.test_durations > backend/.test_durations` | T1 RED |
| M2 | drop entries for 1 file from the fresh file | GREEN — establishes resolution; the docstring must name what the fence does not catch |
| M3 | change the conftest hook to `@pytest.hookimpl(trylast=True)` or `return` early | T3 RED (the anti-vacuity mutant) |
| M4 | add `"tests/deleted_file.py::test_gone": 1.0` | T2 RED |
| M5 | set `--splits 6` with `matrix.group: [1,2,3,4]` | shard-count fence RED |

⚠ **M1 is free and needs no synthetic injection** — it is the real, pre-existing
failing case. Land the fence RED-verified against the stale file, *then* commit the
regenerated file and confirm GREEN.

⚠ **M3 is the one most likely to be skipped and the one that matters most.** Without
it the fence would report ~100% coverage on a 1/6 sample and certify the gap it exists
to close.

## One-line change that must ship with it

Add `- ./backend/.test_durations:/app/.test_durations:ro` to `docker-compose.yml`'s
backend `volumes:`. Without it an agent verifying the fence inside the dev container
evaluates the image's baked copy, so after regenerating the file the fence still reads
RED locally. That false red is exactly what gets a fence weakened. `:ro` on purpose —
regeneration comes from CI artifacts, never from a container run.

## Measured outcome

| | before | after |
|---|---|---|
| shard times | 485 / 471 / 298 / 262s | 279 / 277 / 253 / 274 / 268 / 259s |
| max/min spread | **2.00x** | **1.10x** |
| critical path | 485s | **279s** |
| `Backend Checks` | ~497s | **~290s** |
| `Frontend Checks` | ~347s | ~344s (unchanged) |
| run wall clock | 8m21s | **~5m44s** |

R2's successor target is met: `Backend Checks` (~290s) is now below
`Frontend Checks` (~344s).

### ⚠ Two predictions that were WRONG, and why

Both are recorded because the reasoning that produced them looked sound.

**1. "The runner-measured file will balance at 1.01x."** It measured **1.82x**.
The harvest ran UNSHARDED (one ~31-minute process) while consumption is sharded
into six short ones, so late-position tests carried the long process's
accumulated cost and were overweighted. `DurationBasedChunksAlgorithm` cuts
CONTIGUOUS slices, so chunk N maps to collection position N and the error
CONCENTRATED rather than cancelling:

```
shard  tests  predicted  actual  actual/predicted
  1      624     308s     283s        0.92
  4     1023     308s     181s        0.59
  5      667     308s     153s        0.50
```

This contradicted an explicit design ruling — that a shard-measured duration
equals an unsharded one, since `backend/tests/` has no session- or
module-scoped fixtures. That argument is about **fixture attribution** and
misses **process-level accumulation** entirely. Two architects, a
concede-or-defend round and three reviewers all passed over it; only running it
on real hardware surfaced it.

⚠ It also partially rehabilitates an option that was rejected: harvesting from
sharded runs is **more** accurate, not less. The other objection to it — that it
would put an artifact upload on the run whose conclusion gates production
deploys — still stands, which is why the sharding happens in this separate
generator workflow and not in `test.yml`.

**2. "The balance fence will catch a bad file."** It cannot catch this one. The
simulation scores the file with **itself**, so a position-biased file still
self-scores at 1.01x. Matching the harvest and consumption shapes is what makes
that simulation mean anything — which is why both shapes are now fenced against
drift in either direction.

## ⚠ The generator deadlocked on its own output

The harvest runs the whole suite, so it ran the freshness fence too — against
the COMMITTED `.test_durations`. A file bad enough to trip the fence therefore
failed the harvest, the merge job was skipped, and the only sanctioned remedy
for the red fence became unreachable. Measured: run 32306137554, `Harvest shard
6/6` red on "only 824 distinct values across 4181 entries", no artifact.

The fence is deselected in the harvest, and **that deselection is itself
fenced** — removing it reintroduces a deadlock that stays invisible until the
day it matters. Cost: the six freshness-fence tests carry no timing entry, so
they are weighted at the mean. They are sub-second, and coverage stays at
99.9%.

## ⚠ Rounding fought its own fence

The merge rounds the float repr for readability. At **3dp** every sub-millisecond
test collapses onto a handful of values and a genuinely-measured file came out
at **19.7% distinct**, tripping the degeneracy fence — which was correct to fire,
since its job is to notice a file whose values carry no information.

Resolved by raising precision to **6dp** (86% distinct, max error 5e-7s ≈ 2ms
across the whole suite), **not** by lowering the threshold. Lowering it would
have been the "fence gets weakened rather than obeyed" failure this spec argues
against three sections earlier. The workflow says so at the call site.

## Follow-up to file with this PR

**`Frontend Checks` is now the CI floor and its dominant step is vitest at 215s of
347s.** `specs/ci-test-sharding.md:16` recorded frontend vitest at ~11s for 1017 tests
and `:45` declared frontend sharding out of scope as "not material at current runtime".
That is refuted by **19.5x**. Once this ticket lands, backend CI investment is
unobservable and the next CI ticket is the frontend job. Cheap first cut: the two Next
builds run serially at the tail for 56s and are trivially a separate parallel job;
vitest has its own `--shard` flag for the 215s.
