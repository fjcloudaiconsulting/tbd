#!/usr/bin/env bash
# Fetch main's branch-protection state and report a verdict (TBD-420).
#
# READ-ONLY. Every call below is a GET. This probe must never acquire
# `Administration: write`: auto-remediation would make it the very unenumerated
# mutation path it exists to expose, and self-healing masks the drift.
#
# ⚠⚠ THE TWO READS USE DIFFERENT CREDENTIALS, AND THE DESIGN COLLAPSES WITHOUT
# IT. `.protected` is decided FIRST in the checker precisely so that nothing can
# mask a naked `main` -- but that buys nothing if both reads share one token: a
# suspended App fails EVERY call, `BRANCH_PROTECTED` comes back unknown, and a
# genuinely naked `main` reports `could-not-run`. That is the exact fail-open the
# reorder was performed to prevent. So:
#
#   * `.protected`      -> PROBE_METADATA_TOKEN (the workflow's github.token).
#                          Public repo metadata; needs only `contents: read`,
#                          and survives an App outage.
#   * `/protection`     -> PROBE_ADMIN_TOKEN (the App, `Administration: read`).
#   * the rules view    -> PROBE_METADATA_TOKEN.
#   * `gh issue` (in the notifier, not here) -> github.token. The App CANNOT
#                          write issues, so pointing GH_TOKEN at it job-wide
#                          makes the notifier 403 and exit 1: drift detected, no
#                          issue opened, a red square nobody opens.
#
# ⚠⚠ THE PROTECTION FETCH RETRIEVES THE WHOLE DOCUMENT. No `--jq`, no `-q`, no
# `--template`, and never a sub-resource. A server-side projection here is
# INVISIBLE to every fence that drives the checker from stdin: seed the posture
# from the same projected command and the comparison is total over four keys and
# green forever. That is precisely the four-field projection that hid
# `allow_force_pushes` for three weeks, moved one layer up. (The `.protected`
# fetch legitimately uses `--jq`; it is a one-bit oracle, not a posture source.)
#
# ⚠ `${GH:-gh}` indirection is not a nicety. It is what makes every path here --
# including the failed-App path -- drivable from a fixture with no credential. A
# probe exercisable only against healthy live state proves nothing about its
# unhealthy paths, and those are the only paths that matter.
#
# ⚠⚠ IT READS ONCE AND REPORTS ONCE. An earlier design re-read after any
# non-`in-posture` verdict and let the second reading CLEAR the first. That
# encoded "observed drift, zero alarms" -- the exact pattern this design calls
# fatal one page earlier to justify `cancel-in-progress: false` -- and it made
# the probe blind by construction to the one event it is uniquely placed to see,
# since a break-glass disarm has no other automated trace in this repo. The race
# it existed for was never measured, and the failure actually feared (a re-arm
# POST returning non-zero) is PERSISTENT and unaffected by re-reading. A rare
# stale alarm is absorbed by the notifier's title-prefix dedupe. Do not add a
# sleep, a retry, or a confirming read without measuring the window first.
set -uo pipefail

GH="${GH:-gh}"
# ⚠ NO DEFAULT. The workflow always passes REPO (`${{ github.repository }}`).
# A hardcoded fallback would silently probe the WRONG repository if this is
# ever run elsewhere, or if the repo is renamed or moved -- reporting
# `in-posture` about a branch nobody was asking about.
REPO="${REPO:?REPO must be set to owner/name}"
POSTURE_FILE="${POSTURE_FILE:-.github/branch-protection/main.json}"
ADMIN_TOKEN="${PROBE_ADMIN_TOKEN:-}"
METADATA_TOKEN="${PROBE_METADATA_TOKEN:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="$HERE/check-branch-protection.sh"

# The WHOLE protection document, with the ADMIN credential. See the projection
# warning above: no --jq, no --template, no sub-resource.
body="$(GH_TOKEN="$ADMIN_TOKEN" "$GH" api \
          "repos/$REPO/branches/main/protection" 2>/dev/null || true)"

# ⚠ EXACTLY `.protected`, never `.protection.*`, and on the METADATA credential
# so an App outage cannot suppress it. The same endpoint also exposes
# `protection.required_status_checks.enforcement_level`, which IS
# `enforce_admins`; reading it would turn a one-bit disambiguator into a partial
# source of posture, buying one field and selling the rest.
protected="$(GH_TOKEN="$METADATA_TOKEN" "$GH" api \
               "repos/$REPO/branches/main" --jq '.protected' 2>/dev/null || true)"

# The EFFECTIVE rule view, which includes org-level rules this repo cannot
# enumerate. Read only to explain a MISSING protection document.
rules="$(GH_TOKEN="$METADATA_TOKEN" "$GH" api \
           "repos/$REPO/rules/branches/main" 2>/dev/null || true)"

out="$(
  printf '%s' "$body" | \
    BRANCH_PROTECTED="$protected" \
    EFFECTIVE_RULES_JSON="$rules" \
    POSTURE_FILE="$POSTURE_FILE" \
    bash "$CHECK"
)"
rc=$?

printf '%s\n' "$out"
verdict="$(printf '%s\n' "$out" | head -n1)"

# ⚠⚠ THIS SCRIPT DOES NOT WRITE $GITHUB_OUTPUT, DELIBERATELY. It used to, and
# because it runs inside the check step's command substitution both it and the
# workflow appended `verdict=` to the SAME file -- correctness resting entirely
# on runner last-wins, with the auth override and the empty-verdict fallback
# silently staked on it. The contract is now one-way and explicit: this script
# emits `verdict=<token>` as the LAST line of stdout, and the workflow is the
# only writer of the step output.
echo "verdict=$verdict"
exit "$rc"
