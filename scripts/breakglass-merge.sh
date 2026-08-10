#!/usr/bin/env bash
#
# Break-glass merge for `main` (TBD-347).
#
# `main` runs with `enforce_admins: true`, so a genuinely wedged required
# check — a hung runner, a GitHub incident, a flake that will not clear —
# cannot be clicked past. This script is the sanctioned escape hatch.
#
# ⚠ IT IS A SINGLE SCRIPTED OPERATION ON PURPOSE. Do not do this by hand as
# two API calls. The hand-run form is:
#
#     gh api -X DELETE .../protection/enforce_admins
#     <merge>
#     gh api -X POST   .../protection/enforce_admins
#
# and the third command is enforced by nothing. Forget it once, at night, on
# an emergency, and admin enforcement is off indefinitely with no alarm. That
# failure mode is not hypothetical here: `allow_deletions: true` was found set
# on `main` with no one having chosen it, and an interrupted partial update is
# the most likely explanation. The `trap` below re-arms on EVERY exit path,
# including Ctrl-C and an unexpected error.
#
# Usage:  scripts/breakglass-merge.sh <pr-number> "<reason>"
#
set -euo pipefail

REPO="fjcloudaiconsulting/tbd"
PR="${1:?usage: breakglass-merge.sh <pr-number> \"<reason>\"}"
REASON="${2:?a reason is required — it goes into the audit trail}"

rearm() {
  local rc=$?
  echo
  echo "==> Re-arming enforce_admins on ${REPO}:main"
  if gh api -X POST "repos/${REPO}/branches/main/protection/enforce_admins" >/dev/null; then
    echo "==> enforce_admins is ON."
  else
    echo "!!! FAILED TO RE-ARM enforce_admins. Do this NOW, by hand:" >&2
    echo "    gh api -X POST repos/${REPO}/branches/main/protection/enforce_admins" >&2
    exit 1
  fi
  exit $rc
}
trap rearm EXIT INT TERM

echo "==> Break-glass merge of PR #${PR}"
echo "==> Reason: ${REASON}"
echo
echo "Current required checks:"
gh api "repos/${REPO}/branches/main/protection/required_status_checks" \
  --jq '.checks[] | "  - \(.context) (app \(.app_id))"'
echo
read -r -p "Disable admin enforcement and merge #${PR}? [y/N] " ok
[ "${ok}" = "y" ] || { echo "Aborted."; exit 1; }

echo "==> Disabling enforce_admins"
gh api -X DELETE "repos/${REPO}/branches/main/protection/enforce_admins" >/dev/null

echo "==> Merging PR #${PR} (squash)"
gh pr merge "${PR}" --repo "${REPO}" --squash

# `trap rearm EXIT` restores enforcement from here regardless of how we leave.
