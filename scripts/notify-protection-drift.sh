#!/usr/bin/env bash
# Open (or comment on) a deduped GitHub issue when the branch-protection posture
# probe reports anything other than `in-posture` (TBD-420).
#
# Required env: GH_TOKEN, GH_REPO, RUN_ID, VERDICT, DETAIL.
#
# ⚠ THE FIFTH COPY, and declining to extract at five needs a different argument
# than declining at three. notify-smoke-failure.sh, notify-undeployed-release.sh,
# notify-deploy-drift.sh and notify-backup-stale.sh are the other four;
# notify-undeployed-release.sh said extraction should happen "on the third" and
# notify-deploy-drift.sh:11-19 declined at the third. The shared surface is now
# large enough that the NEXT probe should extract it. The reason not to do it
# HERE is that it would make a reviewer assessing a new probe silently review a
# refactor of four live incident notifiers whose failure mode is silence during
# an outage. Filed separately.
#
# ⚠⚠ ITS OWN DEDUPE BUCKET. Copying a sibling notifier and leaving TITLE_PREFIX
# unchanged would make each alarm silence the other -- the branch-protection
# alarm would land as a comment on the open deploy-drift issue and nobody would
# ever see it. Fenced.
#
# ⚠ NO AUTO-CLOSE. A healed reading does not close the issue: the operator
# decides when an incident is over. Auto-closing would also erase the record of
# a drift that healed only because someone re-armed it by hand.
set -uo pipefail

for v in GH_TOKEN GH_REPO RUN_ID VERDICT; do
  if [ -z "${!v:-}" ]; then
    echo "notify-protection-drift: $v is required" >&2
    exit 2
  fi
done

TITLE_PREFIX="[branch-protection]"
TITLE="${TITLE_PREFIX} main's protection no longer matches the recorded posture"
RUN_URL="https://github.com/${GH_REPO}/actions/runs/${RUN_ID}"

BODY="Verdict: **${VERDICT}**

${DETAIL:-(no detail was captured)}

Detected by the branch-protection posture probe: ${RUN_URL}

The recorded posture is \`.github/branch-protection/main.json\`. Nothing
regenerates it in place: read the difference, then either restore the setting in
the repository settings or commit the new posture.

⚠ This probe compares the normalized fields of main's CLASSIC branch protection
only. It does NOT cover \`allow_merge_commit\` / \`allow_rebase_merge\` (those
live on the repository object, not on /protection), repository or org rulesets,
or whether a red required check actually blocks a merge.

<sub>Deduped by title prefix \`${TITLE_PREFIX}\`; repeated detections comment here rather than opening new issues. Not closed automatically.</sub>"

# ⚠⚠ THE `startswith` FILTER IS NOT OPTIONAL. GitHub's `in:title` search is an
# AND over TOKENS, not a literal prefix match -- probed live, `in:title
# production` returns the deploy-drift issue. So `[branch-protection]` matches
# any open issue whose title contains both "branch" and "protection", and this
# alarm would land as a comment on somebody else's incident, where nobody is
# looking for it. `notify-backup-stale.sh:49-50` already carries the safe idiom;
# the weaker `notify-deploy-drift.sh` form was copied here by mistake.
#
# ⚠ F8 asserts the five dedupe LITERALS are pairwise distinct. That does NOT
# constrain what a fuzzy title search matches, so it is not a fence on this.
EXISTING="$(gh issue list \
  --repo "$GH_REPO" \
  --state open \
  --search "in:title ${TITLE_PREFIX}" \
  --json number,title \
  --jq ".[] | select(.title | startswith(\"$TITLE_PREFIX\")) | .number" \
  2>/dev/null | head -n1)"

if [ -n "${EXISTING:-}" ] && [ "$EXISTING" != "null" ]; then
  if gh issue comment "$EXISTING" --repo "$GH_REPO" --body "$BODY"; then
    echo "Commented on existing branch-protection issue #${EXISTING}"
    exit 0
  fi
  echo "notify-protection-drift: failed to comment on #${EXISTING}" >&2
  exit 1
fi

if gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" --label branch-protection >/dev/null 2>&1; then
  echo "Created new branch-protection issue (with label)"
elif gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" >/dev/null 2>&1; then
  echo "Created new branch-protection issue (label not present in repo)"
else
  echo "notify-protection-drift: failed to create issue" >&2
  exit 1
fi
