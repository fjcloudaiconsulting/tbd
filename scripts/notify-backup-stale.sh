#!/usr/bin/env bash
# Open or update a GitHub issue when the off-host backup is stale, missing, or
# could not be checked (TBD-400).
#
# Inputs (env): GH_TOKEN, GH_REPO, RUN_ID, VERDICT, DETAIL
#
# ⚠ SEPARATE DEDUPE BUCKET from the smoke-failure and deploy-drift notifiers,
# and it deliberately duplicates ~15 lines of `gh issue` plumbing rather than
# sharing them. A stale backup and a failed deploy are independent conditions
# that can legitimately be open at the same time; collapsing the buckets would
# let one alarm silence the other. The shared code would sit on the exact path
# whose failure mode is silence during an incident.
set -euo pipefail

TITLE_PREFIX="[backup-stale]"
TITLE="${TITLE_PREFIX} off-host MySQL backup is not fresh"
RUN_URL="https://github.com/${GH_REPO}/actions/runs/${RUN_ID}"

BODY="$(cat <<BODY_EOF
The scheduled backup freshness probe did not find a healthy off-host backup.

**Verdict:** \`${VERDICT}\`

\`\`\`
${DETAIL}
\`\`\`

Probe run: ${RUN_URL}

---

**What this means.** The nightly \`mysqldump\` on the data droplet is the
durability floor, and the copy in S3 is the only part of it that survives losing
that droplet. A \`stale\` verdict means at least one night has not completed end
to end. A \`could-not-run\` verdict means the probe could not answer the question
at all, which is not evidence of health.

**Where to look.**
1. \`/var/log/mysql-backup.log\` on the droplet.
2. Whether the ansible play has been converged since the backup role changed --
   an unconverged droplet uploads nothing, and this alarm is the intended way to
   find that out.
3. Whether the \`FlamaCorp/tbd-backups\` workspace has been applied.

⚠ Do not silence this by widening the probe's thresholds.
BODY_EOF
)"

existing="$(gh issue list --repo "$GH_REPO" --state open --search "$TITLE_PREFIX in:title" \
             --json number,title --jq ".[] | select(.title | startswith(\"$TITLE_PREFIX\")) | .number" \
             2>/dev/null | head -1 || true)"

if [[ -n "$existing" ]]; then
  gh issue comment "$existing" --repo "$GH_REPO" --body "$BODY"
  echo "commented on existing issue #${existing}"
  exit 0
fi

# Best-effort label: create without it if the label does not exist yet, so the
# alarm still lands rather than failing on a cosmetic.
if ! gh issue create --repo "$GH_REPO" --title "$TITLE" --body "$BODY" --label backup-stale 2>/dev/null; then
  gh issue create --repo "$GH_REPO" --title "$TITLE" --body "$BODY"
fi
echo "opened a new ${TITLE_PREFIX} issue"
