#!/usr/bin/env bash
# Open or update a GitHub issue when the post-deploy smoke job fails.
#
# Runs from .github/workflows/deploy.yml as an `if: failure()` step
# inside the smoke-tests job. The deploy itself ran (DO marked it
# ACTIVE); the post-deploy smoke verification did not pass — the live
# app may not be serving authenticated traffic correctly.
#
# Inputs (env vars provided by the workflow):
#   GH_TOKEN     — github.token
#   GH_REPO      — owner/repo (github.repository)
#   RUN_ID       — github.run_id
#   SHA          — github.sha
#   REF_NAME     — github.ref_name (branch)
#   ACTOR        — github.actor (whoever pushed)
#
# Behavior:
#   - Dedupes against any open issue whose title starts with
#     "[smoke-fail]". Prevents repeated failed deploys from spamming
#     a fresh issue every run; instead appends a comment to the open
#     one with the new run's details.
#   - Title-based dedupe (not label-based) so this works even before
#     the `smoke-fail` label is created in the repo.
#   - Best-effort label: tries `--label smoke-fail` first; if the
#     label doesn't exist yet, retries without the flag and the
#     issue still lands.
#
# Exit codes:
#   0  issue opened or commented successfully
#   1  unexpected gh failure
#   2  required env var missing

set -uo pipefail

for var in GH_TOKEN GH_REPO RUN_ID SHA REF_NAME ACTOR; do
  if [[ -z "${!var:-}" ]]; then
    echo "✗ ${var} is not set"
    exit 2
  fi
done

TITLE="[smoke-fail] Post-deploy smoke failed"
RUN_URL="https://github.com/${GH_REPO}/actions/runs/${RUN_ID}"

BODY="$(cat <<EOM
Smoke-test job failed after deploy of \`${SHA}\` on \`${REF_NAME}\`.

- **Workflow run:** ${RUN_URL}
- **Triggered by:** @${ACTOR}

The DigitalOcean deploy itself ran (DO marks ACTIVE before this job
fires). The post-deploy verification — \`/health\`, \`/ready\`, login
round-trip, authenticated read — did not pass. The live app may not be
serving authenticated traffic correctly.

Re-run the workflow once the underlying issue is fixed; this issue can
be closed manually when the next smoke run goes green, or left open and
new failures will be appended as comments.
EOM
)"

# Dedupe: look for an open issue whose title contains "[smoke-fail]".
# Falls back to title search instead of relying on a label so this
# works whether or not the label has been created in the repo.
EXISTING="$(gh issue list \
  --repo "$GH_REPO" \
  --state open \
  --search '"[smoke-fail]" in:title' \
  --json number \
  --jq '.[0].number // empty' || true)"

if [[ -n "$EXISTING" ]]; then
  if gh issue comment "$EXISTING" --repo "$GH_REPO" --body "$BODY"; then
    echo "Appended failure comment to existing issue #${EXISTING}"
    echo "Run URL: ${RUN_URL}"
    exit 0
  fi
  echo "✗ failed to comment on issue #${EXISTING}"
  exit 1
fi

# No open smoke-fail issue. Try to create with the label first; if
# the label doesn't exist yet, gh exits non-zero — fall back to no
# label so the failure signal still lands.
if gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" --label smoke-fail >/dev/null 2>&1; then
  echo "Created new smoke-fail issue (with label)"
elif gh issue create --repo "$GH_REPO" \
       --title "$TITLE" --body "$BODY" >/dev/null; then
  echo "Created new smoke-fail issue (label not present in repo)"
else
  echo "✗ failed to create smoke-fail issue"
  exit 1
fi

echo "Run URL: ${RUN_URL}"
