#!/usr/bin/env bash
#
# Is there a releasable commit on main that no release covers, and has that
# been true for longer than the pipeline's own latency? TBD-448.
#
# ⚠⚠ WHY THIS EXISTS, AND WHY NEITHER EXISTING ALARM CAN COVER IT.
#
# The invariant "every releasable merge gets released" has had zero monitoring,
# and both existing monitors are STRUCTURALLY incapable of providing one:
#
#   * scripts/notify-undeployed-release.sh is gated on
#     `needs.release.outputs.new_release_published == 'true'`. It can never
#     fire when nothing was published. That is definitional, not a tuning knob.
#
#   * scripts/ci/check-deploy-drift.sh compares production against the latest
#     TAG, and its own header documents that comparing against main HEAD "is
#     WRONG and would false-alarm continuously". TBD-434 declined this question
#     deliberately and for good reasons.
#
# The gap is real and has bitten. Measured across every releasable commit on
# main since 2026-06-01: two `fix(apex):` commits merged 2026-06-19/20 sat
# 22h and 16h UNRELEASED, shipping only when an unrelated `feat(apex):` swept
# them into v0.177.0 the next evening. Every signal was green throughout. The
# next largest benign lag was 17 minutes.
#
# ⚠⚠ THE DEFECT THIS SCRIPT EXISTS TO NOT HAVE: THE DRY RUN INHERITS THE
# FAILURE IT MONITORS.
#
# semantic-release's "local branch main is behind the remote one" bail and its
# "there are no relevant changes" outcome are BOTH exit 0 and both leave
# new_release_published false. Measured directly on 2026-08-26:
#
#   behind remote : "The local branch main is behind the remote one,
#                    therefore a new version won't be published."   exit 0
#                    (analyzeCommits NEVER RUNS)
#   genuine no-op : "Found git tag v0.259.2 ... Found 0 commits since last
#                    release ... no relevant changes"               exit 0
#
# So a probe that reads only the published flag reports a FALSE ALL-CLEAR by
# precisely the mechanism it was built to detect. That is why this script
# asserts HEAD == origin/main around the analysis and treats any non-verdict
# as exit 2. Do not "simplify" either away.
#
# ⚠ READ-ONLY. This script must never tag, push, dispatch a workflow, or
# deploy. It is the FIRST invocation of semantic-release outside release.yml,
# and one boolean (`dry_run`) separates a monitor from a tag cut with no
# await-test-run.sh interlock behind it (TBD-391). The workflow pins
# `dry_run: true` AND `permissions: contents: read` so a mis-set flag still
# cannot push a tag. Both are fenced.
#
# Exit contract, mirroring check-deploy-drift.sh:
#   0 = no alarm (not due, or still inside the grace window)
#   1 = ALARM; the report is exported as LAG_REPORT for the notifier
#   2 = could not run; the caller must fail LOUDLY rather than read it as OK
#
# Required env:
#   DRY_RUN_PUBLISHED   "true" | "false"  (from semantic-release --dry-run)
#   DRY_RUN_VERSION     optional; the version it would cut
# Optional env:
#   LAG_GRACE_MINUTES   default 60

set -euo pipefail

fail() { echo "check-release-lag: $*" >&2; exit 2; }

GRACE="${LAG_GRACE_MINUTES:-60}"
case "$GRACE" in
  ''|*[!0-9]*) fail "LAG_GRACE_MINUTES must be a non-negative integer, got '${GRACE}'" ;;
esac

command -v git >/dev/null 2>&1 || fail "git not found"

# A shallow clone has no tags, and "no tags" would otherwise read as "nothing
# has ever been released", which is indistinguishable from all-clear. Refuse.
if [ -z "$(git tag --list 2>/dev/null | head -n1)" ]; then
  fail "no git tags visible -- checkout must use fetch-depth: 0, or this probe is blind"
fi

# ⚠ THE LOAD-BEARING ASSERTION. If a merge landed while this probe was running,
# the dry run above may have bailed with "local branch is behind" and reported
# no release -- the false all-clear. Refuse to rule rather than rule wrongly.
git fetch --quiet origin main 2>/dev/null || fail "could not fetch origin/main"
HEAD_SHA="$(git rev-parse HEAD)" || fail "could not resolve HEAD"
REMOTE_SHA="$(git rev-parse origin/main)" || fail "could not resolve origin/main"
if [ "$HEAD_SHA" != "$REMOTE_SHA" ]; then
  fail "HEAD (${HEAD_SHA:0:8}) != origin/main (${REMOTE_SHA:0:8}) -- a merge landed
during this run, so the release analysis may have bailed with 'local branch is
behind' and reported no release for that reason rather than for the real one.
Refusing to rule. The next scheduled firing will see a settled tree."
fi

# ⚠ Map the verdict EXPLICITLY. Anything that is not a clean true/false is a
# dry run that did not reach analyzeCommits -- treat it as inconclusive, never
# as "nothing to release".
case "${DRY_RUN_PUBLISHED:-}" in
  true)  DUE=1 ;;
  false) DUE=0 ;;
  *)     fail "DRY_RUN_PUBLISHED was '${DRY_RUN_PUBLISHED:-<unset>}', not true/false --
the dry run did not produce a verdict. That is what a bailed analysis looks
like, and reading it as 'no release due' is the false all-clear this probe
exists to prevent." ;;
esac

if [ "$DUE" -eq 0 ]; then
  echo "check-release-lag: OK -- semantic-release would not cut a release from ${HEAD_SHA:0:8}."
  exit 0
fi

# ⚠ THE GRACE WINDOW IS MANDATORY. Between a `fix:` merging and its release
# publishing, "main has releasable commits and no release covers them" is TRUE
# and CORRECT for ~10-25 minutes -- await-test-run.sh's own timeout is 1500s.
# Without a threshold this alarms on healthy pipelines and takes the whole
# alarm family's credibility with it.
COMMIT_EPOCH="$(git show -s --format=%ct HEAD)" || fail "could not read HEAD commit time"
NOW_EPOCH="$(date -u +%s)"
AGE_MIN=$(( (NOW_EPOCH - COMMIT_EPOCH) / 60 ))

if [ "$AGE_MIN" -lt "$GRACE" ]; then
  echo "check-release-lag: OK -- a release is due for ${HEAD_SHA:0:8} but HEAD is only ${AGE_MIN}m old (grace ${GRACE}m); a Release run is plausibly still in flight."
  exit 0
fi

LATEST_TAG="$(git describe --tags --abbrev=0 2>/dev/null || echo '<none>')"
WOULD_BE="${DRY_RUN_VERSION:-<unknown>}"

read -r -d '' LAG_REPORT <<EOF || true
A releasable commit has been sitting on \`main\` unreleased for **${AGE_MIN} minutes**.

| | |
|---|---|
| main HEAD | \`${HEAD_SHA:0:8}\` |
| latest tag | \`${LATEST_TAG}\` |
| semantic-release would cut | \`${WOULD_BE}\` |
| unreleased for | ${AGE_MIN} min (grace ${GRACE} min) |

This is **not** the same alarm as \`[deploy-drift]\`. There, a release exists and
production is not serving it. Here **no release was cut at all**, so production
and the latest tag agree with each other and both are behind \`main\`. Every
existing signal is green: \`notify-undeployed-release\` is gated on a release
having been published, so it cannot fire, and the drift probe compares
production against the latest tag, which matches.

**To reconcile:** re-run the most recent \`Release\` workflow run for
\`${HEAD_SHA:0:8}\`. That re-enters \`await-tests\` against this commit and then
runs semantic-release on an up-to-date checkout, preserving the TBD-391
interlock.

⚠ Do **NOT** reconcile by tagging manually (\`git tag && git push --tags\`).
That bypasses the post-merge test interlock entirely and publishes an immutable
tag for code no gate approved.

⚠ Do **NOT** reconcile with \`gh workflow run deploy.yml --ref main\`. That
deploys untagged code and pushes the committed \`.do/app.yaml\`, which can
overwrite live secrets (TBD-425).
EOF

export LAG_REPORT
printf '%s\n' "$LAG_REPORT"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '%s\n' "$LAG_REPORT" >> "$GITHUB_STEP_SUMMARY"
fi
exit 1
