#!/usr/bin/env bash
# Block until the `Backend Checks` and `Frontend Checks` jobs of the `Test`
# workflow's push run for a commit complete (INFRA-159: not the whole run, which
# now includes release/promote/smoke). Exit 0 only on success; fail CLOSED on every other outcome.
#
# WHY THIS EXISTS (TBD-391)
#
# `ci.yml` and the (since removed) `release.yml` both triggered on `push: branches: [main]` and had
# no dependency between them, so they raced and release won. Measured on PR
# #654 (SHA 1af0b388), both runs created at 18:30:38:
#
#   18:31:07  release job done -- git tag + GitHub Release PUBLISHED
#   18:31:11  deploy job STARTED (the DigitalOcean deploy of that time)
#   18:35:20  deploy done (DO ran the PRE_DEPLOY alembic migration)
#   18:38:48  Test run completed          <- 7m41s after the tag was cut
#
# The post-merge `CI` run is NOT a redundant re-run. It is the deliberate
# substitute for branch protection's `strict: true` (see the comment block at
# the top of ci.yml): two PRs can each be green in isolation and conflict
# semantically once both land, and no PR check can see that. Measured
# 2026-08-12: 19 of the last 30 PRs had `main` move underneath them while they
# were open, so that is not a rare shape.
#
# So this script is the interlock. Without it, the guard reports after the
# thing it exists to prevent has already shipped.
#
# ⚠ IT NOW GATES ONLY apex-deploy.yml: the release runs inside ci.yml (INFRA-159) and needs the gates. The
# reasoning below is why the gate sits BEFORE the irreversible step. release-please cuts an immutable git tag and publishes
# a GitHub Release, so gating any later job would still leave a published
# release for a commit whose suite then goes red.
#
# ⚠ THIS DEPENDS ON `ci.yml` HAVING NO `paths:` FILTER. That ban (TBD-347) is
# what guarantees a Test run always exists for every push to `main`, which is
# what makes this wait terminate. Reintroducing a filter there would no longer
# just break PRs -- it would silently stop releases, one 25-minute
# timeout at a time.
set -uo pipefail

SHA="${1:?usage: await-test-run.sh <full-40-char-sha>}"
WORKFLOW="${AWAIT_TEST_WORKFLOW:-ci.yml}"
INTERVAL="${AWAIT_TEST_POLL_SECONDS:-20}"
TIMEOUT="${AWAIT_TEST_TIMEOUT_SECONDS:-1500}"
DEADLINE=$(( $(date +%s) + TIMEOUT ))

# MEASURED: the `?head_sha=` query parameter matches ONLY a full 40-character
# SHA. An abbreviated one returns `total_count: 0` silently, which would burn
# the entire timeout hunting a run that can never match, then fail closed for
# the wrong reason. `${{ github.sha }}` is always full, so the shipped path is
# safe -- this guard exists so a hand-run verification command with a short SHA
# fails loudly instead of looking like a broken gate.
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "await-test-run: not a full 40-character sha: ${SHA}" >&2
  exit 2
fi

echo "await-test-run: waiting for '${WORKFLOW}' on ${SHA} (timeout ${TIMEOUT}s)"

while :; do
  status="api-error"
  concl="-"
  run_id=""
  if payload=$(gh api \
      "repos/${GH_REPO}/actions/workflows/${WORKFLOW}/runs?head_sha=${SHA}&event=push&branch=main&per_page=100" \
      2>&1); then
    # Only the `push` run on `main` counts (INFRA-96). It is the run that
    # builds the sha-<7> images `promote` retags; a `pull_request` or
    # `workflow_dispatch` run on the same sha is ignored even when newer, so it
    # can neither mask a red push run nor block a green one. A fork can add
    # `pull_request` runs on any public sha (and name its branch `main`), so
    # the EVENT check is the one that matters. Filtered twice: in the query, so
    # 100+ such runs cannot push the real one off the page, and again in the
    # parser, which is the copy the fence (stubbed `gh`) exercises. To rehearse
    # against history, pass the sha of a red push run on main.
    #
    # Newest push run wins: a re-run of a red suite (a re-run keeps its
    # `push` event) unblocks a deploy without a force-push. Dispatching CI
    # instead does not.
    #
    # ⚠ python3, NOT jq. Both exist on `ubuntu-latest`, but `jq` is ABSENT
    # from the backend container image where this script's fence runs. With
    # jq, the parse silently produced an empty status, every case fell through
    # to the timeout, and the failure-conclusion tests passed for the WRONG
    # REASON -- they were green because the script timed out, not because it
    # read the conclusion. That is a vacuous fence, caught only because the
    # success cases went red at the same time. Keep the parser in python3 so
    # the fence and production agree.
    parsed=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("workflow_runs") or []
except Exception:
    print("parse-error -")
    raise SystemExit(0)
runs = [r for r in runs if r.get("event") == "push" and r.get("head_branch") == "main"]
if not runs:
    print("absent -")
else:
    newest = sorted(runs, key=lambda r: r.get("run_started_at") or "")[-1]
    print("found " + str(newest.get("id")))
' 2>/dev/null)
    if [ -z "$parsed" ]; then
      status="parse-error"
    else
      read -r status run_id <<<"$parsed"
    fi
    # INFRA-159: the release, promote and smoke jobs now live in this same
    # run, so the RUN's conclusion also reflects them. apex-deploy must depend
    # on the tests only, i.e. the two gate jobs (the required contexts), read
    # from the jobs of that push run. Reading them from the run, not from
    # `commits/<sha>/check-runs` by name, is what proves they belong to the
    # ci.yml push run on main: a check-run of the same name from another
    # workflow or event cannot be mistaken for them. `/jobs` lists the latest
    # attempt, so re-running a failed gate unblocks. A repeated name takes
    # the highest job id.
    if [ "$status" = "found" ]; then
      status="absent"
      if jobs=$(gh api "repos/${GH_REPO}/actions/runs/${run_id}/jobs?per_page=100" 2>&1); then
        parsed=$(printf '%s' "$jobs" | python3 -c '
import json, sys
GATES = ("Backend Checks", "Frontend Checks")
try:
    jobs = json.load(sys.stdin).get("jobs") or []
except Exception:
    print("parse-error -")
    raise SystemExit(0)
state = []
for name in GATES:
    mine = [j for j in jobs if j.get("name") == name]
    state.append(max(mine, key=lambda j: j.get("id") or 0) if mine else None)
if any(j is None for j in state):
    print("absent -")
elif any(j.get("status") == "completed" and j.get("conclusion") != "success" for j in state):
    bad = [j for j in state if j.get("status") == "completed" and j.get("conclusion") != "success"][0]
    print("completed " + (bad.get("conclusion") or "unknown"))
elif all(j.get("status") == "completed" for j in state):
    print("completed success")
else:
    print("in_progress -")
' 2>/dev/null)
        if [ -z "$parsed" ]; then status="parse-error"; else read -r status concl <<<"$parsed"; fi
      else
        echo "await-test-run: gh api call failed, will retry: ${jobs}" >&2
      fi
    fi
  else
    echo "await-test-run: gh api call failed, will retry: ${payload}" >&2
  fi

  # A parser that cannot run is not a reason to wait 25 minutes and then fail
  # for the wrong reason. Exit distinctly and immediately.
  if [ "$status" = "parse-error" ]; then
    echo "await-test-run: could not parse the API response (is python3 present?)" >&2
    exit 2
  fi

  case "$status" in
    completed)
      if [ "$concl" = "success" ]; then
        echo "await-test-run: gate checks for ${SHA} succeeded."
        exit 0
      fi
      # failure / cancelled / timed_out / action_required / neutral / skipped
      # all land here. Every one of them means "the suite did not pass", and a
      # deploy must not proceed on any of them. `cancelled` in particular is
      # reachable: a pending post-merge run is cancelled when a newer one
      # supersedes it in the concurrency group.
      echo "await-test-run: a gate check for ${SHA} concluded '${concl}'." >&2
      echo "Refusing to release. After investigating, re-run the Test run (or land a fix)," >&2
      echo "then re-run this workflow." >&2
      exit 1
      ;;
    absent)
      echo "await-test-run: no Test run or gate checks for ${SHA} yet; waiting"
      ;;
    *)
      echo "await-test-run: Test run for ${SHA} is '${status}'; waiting"
      ;;
  esac

  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "await-test-run: timed out after ${TIMEOUT}s waiting for ${SHA}" >&2
    echo "Last observed status: '${status}'. Failing closed." >&2
    echo "After investigating, re-run this workflow once the Test run is green." >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
