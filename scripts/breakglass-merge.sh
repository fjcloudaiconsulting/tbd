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
# the most likely explanation. The `trap` below re-arms on Ctrl-C, on an
# unexpected error, and on an untrapped SIGHUP (bash runs the EXIT trap, then
# exits 129) -- so closing the terminal does NOT defeat it.
#
# ⚠⚠ IT DOES NOT RE-ARM ON EVERY EXIT PATH, AND THIS COMMENT USED TO CLAIM IT
# DID. Two windows survive: `SIGKILL` or power loss, and -- far likelier -- the
# re-arm POST itself RETURNING NON-ZERO, which is probable precisely because you
# break glass when the API is unwell. In that case line 38 prints `!!! FAILED TO
# RE-ARM enforce_admins`, and the evidence is that such a message can be printed
# and not read: `allow_deletions: true` sat unnoticed once, and
# `allow_force_pushes: true` sat unnoticed for at least three weeks after it.
#
# The out-of-band check for that window is the branch-protection posture probe
# (`.github/workflows/branch-protection-probe.yml`, TBD-420). It runs on every
# push to `main` -- which a break-glass merge produces -- and compares the whole
# protection payload against `.github/branch-protection/main.json`.
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
