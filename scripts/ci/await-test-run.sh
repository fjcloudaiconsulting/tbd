#!/usr/bin/env bash
# Block until the `Test` workflow run for a commit completes. Exit 0 only on
# success; fail CLOSED on every other outcome.
#
# WHY THIS EXISTS (TBD-391)
#
# `test.yml` and `release.yml` both trigger on `push: branches: [main]` and had
# no dependency between them, so they raced and release won. Measured on PR
# #654 (SHA 1af0b388), both runs created at 18:30:38:
#
#   18:31:07  release job done -- git tag + GitHub Release PUBLISHED
#   18:31:11  deploy job STARTED -- .do/app.yaml pushed to DO App Platform
#   18:35:20  deploy done (DO ran the PRE_DEPLOY alembic migration)
#   18:38:48  Test run completed          <- 7m41s after the tag was cut
#
# The post-merge `Test` run is NOT a redundant re-run. It is the deliberate
# substitute for branch protection's `strict: true` (see the comment block at
# the top of test.yml): two PRs can each be green in isolation and conflict
# semantically once both land, and no PR check can see that. Measured
# 2026-08-12: 19 of the last 30 PRs had `main` move underneath them while they
# were open, so that is not a rare shape.
#
# So this script is the interlock. Without it, the guard reports after the
# thing it exists to prevent has already shipped.
#
# ⚠ IT GATES `release`, NOT `deploy`. semantic-release cuts an immutable git
# tag and publishes a GitHub Release before `deploy` ever starts. Gating only
# the deploy would still leave a published release for a commit whose suite
# then goes red, permanently desynchronising the version line from production.
#
# ⚠ THIS DEPENDS ON `test.yml` HAVING NO `paths:` FILTER. That ban (TBD-347) is
# what guarantees a Test run always exists for every push to `main`, which is
# what makes this wait terminate. Reintroducing a filter there would no longer
# just break PRs -- it would silently stop production deploys, one 25-minute
# timeout at a time.
set -uo pipefail

SHA="${1:?usage: await-test-run.sh <full-40-char-sha>}"
WORKFLOW="${AWAIT_TEST_WORKFLOW:-test.yml}"
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
  if payload=$(gh api \
      "repos/${GH_REPO}/actions/workflows/${WORKFLOW}/runs?head_sha=${SHA}&per_page=100" \
      2>&1); then
    # ⚠ DELIBERATELY NOT filtered on `event`. The same commit's Test run is a
    # `push` run on main and a `pull_request` run on a branch. Keeping this
    # event-agnostic is what lets the script be rehearsed against a genuinely
    # RED historical run BEFORE it ships -- `test.yml` never fires a `push`
    # event on a feature branch, so an `event=push` filter would make the
    # pre-merge proof impossible and leave the central claim untested.
    #
    # Newest run wins: a re-run of a red suite should be able to unblock a
    # deploy without a force-push.
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
if not runs:
    print("absent -")
else:
    newest = sorted(runs, key=lambda r: r.get("run_started_at") or "")[-1]
    print((newest.get("status") or "absent") + " " + (newest.get("conclusion") or "-"))
' 2>/dev/null)
    if [ -z "$parsed" ]; then
      status="parse-error"
      concl="-"
    else
      read -r status concl <<<"$parsed"
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
        echo "await-test-run: Test run for ${SHA} succeeded."
        exit 0
      fi
      # failure / cancelled / timed_out / action_required / neutral / skipped
      # all land here. Every one of them means "the suite did not pass", and a
      # deploy must not proceed on any of them. `cancelled` in particular is
      # reachable: a pending post-merge run is cancelled when a newer one
      # supersedes it in the concurrency group.
      echo "await-test-run: Test run for ${SHA} concluded '${concl}'." >&2
      echo "Refusing to release or deploy. To ship anyway after investigating:" >&2
      echo "  gh workflow run deploy.yml --ref main" >&2
      exit 1
      ;;
    absent)
      echo "await-test-run: no Test run for ${SHA} yet; waiting"
      ;;
    *)
      echo "await-test-run: Test run for ${SHA} is '${status}'; waiting"
      ;;
  esac

  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "await-test-run: timed out after ${TIMEOUT}s waiting for ${SHA}" >&2
    echo "Last observed status: '${status}'. Failing closed." >&2
    echo "To ship anyway after investigating: gh workflow run deploy.yml --ref main" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
