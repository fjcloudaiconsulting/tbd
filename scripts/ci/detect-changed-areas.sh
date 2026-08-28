#!/usr/bin/env bash
# Classify the commit range under test into per-area booleans, and write them
# to $GITHUB_OUTPUT as `backend=`, `frontend=` and `migrations=`.
#
#   EVENT_NAME=pull_request BASE_SHA=<sha> GITHUB_OUTPUT=... detect-changed-areas.sh
#
# WHY THIS EXISTS (TBD-404)
#
# `test.yml` runs six backend shards, a real-MySQL migration smoke and the
# whole frontend suite on every PR, including PRs that change nothing but
# prose. The jobs are now gated on these outputs.
#
# ⚠ NO `paths:` FILTER MAY EVER BE ADDED TO THE WORKFLOW TRIGGER (TBD-347), and
# this script is not one. The difference is load-bearing: a trigger-level
# filter stops the jobs from EXISTING, so the required contexts never report
# and the PR is blocked forever on "Expected — waiting for status to be
# reported" — and since TBD-391 it would also silently stop production deploys,
# because `scripts/ci/await-test-run.sh` waits for a concluded `Test` run on
# the merge commit. Here every job still STARTS and then decides to do nothing,
# so both required contexts always report and the run always concludes.
#
# ⚠ FAIL SAFE MEANS FAIL *TRUE*. Any condition this script is not sure about --
# an event other than `pull_request`, a base SHA it cannot resolve, a git
# failure, an unrecognised path -- reports the area as CHANGED. The cost of a
# false `true` is wasted runner seconds on a public repo where they are free.
# The cost of a false `false` is a merged regression.
#
# ⚠ ON `push: branches: [main]` EVERYTHING IS TRUE, deliberately and without
# cleverness. That run is the substitute for branch protection's `strict: true`
# and is what the deploy interlock reads; narrowing it would make the interlock
# gate on a partial suite.
#
# Fenced by backend/tests/test_ci_change_detection.py, which drives THIS file
# against real throwaway git repositories.
set -uo pipefail

OUT="${GITHUB_OUTPUT:-/dev/stdout}"
EVENT_NAME="${EVENT_NAME:-}"
BASE_SHA="${BASE_SHA:-}"

emit_all_true() {
  local why="$1"
  echo "detect-changed-areas: ${why}; treating every area as changed."
  {
    echo "backend=true"
    echo "frontend=true"
    echo "migrations=true"
  } >>"$OUT"
}

if [ "$EVENT_NAME" != "pull_request" ]; then
  emit_all_true "event is '${EVENT_NAME:-<unset>}', not a pull request"
  exit 0
fi

if [ -z "$BASE_SHA" ]; then
  emit_all_true "no base SHA was supplied"
  exit 0
fi

if ! git cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null; then
  emit_all_true "base commit ${BASE_SHA} is not present in this checkout (is fetch-depth: 0 set?)"
  exit 0
fi

# Three-dot: diff the merge base of BASE_SHA and HEAD against HEAD, so commits
# that landed on the base branch after the PR opened are not counted as this
# PR's changes. On the `refs/pull/N/merge` ref that GitHub checks out, HEAD
# already contains the base tip, which at worst makes this OVER-inclusive --
# the safe direction.
if ! files=$(git diff --name-only "${BASE_SHA}...HEAD" 2>&1); then
  emit_all_true "git diff against ${BASE_SHA} failed: ${files}"
  exit 0
fi

if [ -z "$files" ]; then
  # An empty diff is a real answer, not an error: nothing changed, so nothing
  # needs to run. The aggregates still run and still report.
  echo "detect-changed-areas: the diff is empty."
  {
    echo "backend=false"
    echo "frontend=false"
    echo "migrations=false"
  } >>"$OUT"
  exit 0
fi

backend=false
frontend=false

while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in
    # ── Shared: `backend/tests/test_rotation_runbook_credential_bindings.py`
    # PARSES this runbook, so a prose-only edit to it IS a backend change.
    # Matched ABOVE the `*.md` case deliberately: without this the fence is
    # skipped on exactly the docs-only PR that drifts the runbook away from
    # `.do/app.yaml`, which is the drift it exists to catch.
    infra/MIGRATION.md)
      backend=true
      ;;
    # ── Inert: prose. Nothing in either test suite reads these files, and
    # they are matched FIRST so `backend/NOTES.md` counts as prose rather
    # than as a backend change.
    *.md|specs/*|.gitignore)
      ;;
    # ── Shared: `backend/tests/test_report_sources_frontend_contract.py` and
    # `test_period_status_frontend_contract.py` read these fixtures from the
    # backend suite (docker-compose mounts them in), so a fixture edit is a
    # backend change as much as a frontend one.
    frontend/tests/fixtures/*)
      backend=true
      frontend=true
      ;;
    frontend/*)
      frontend=true
      ;;
    backend/*)
      backend=true
      ;;
    # ── Everything else -- repo root, .github/, scripts/, infra/, k8s/,
    # nginx/, docker-compose*.yml, pfv -- is unclassified and therefore
    # EVERYTHING. Backend tests assert on several of these (.do/app.yaml,
    # .github/workflows/*, scripts/ci/*, pfv), and an unknown new top-level
    # path must never be silently inert.
    *)
      backend=true
      frontend=true
      ;;
  esac
done <<EOF
$files
EOF

echo "detect-changed-areas: backend=${backend} frontend=${frontend}"
{
  echo "backend=${backend}"
  echo "frontend=${frontend}"
  # `Migration Checks` boots the whole app against real MySQL and hits /ready,
  # so any backend change is in its scope, not just alembic/. Kept as its own
  # output so narrowing it later is a one-line change here rather than a
  # workflow edit.
  echo "migrations=${backend}"
} >>"$OUT"
