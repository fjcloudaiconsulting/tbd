#!/usr/bin/env bash
# Open or update a GitHub issue when a release was PUBLISHED but never
# DEPLOYED.
#
# Runs from .github/workflows/release.yml (and .github/workflows/deploy.yml,
# the manual escape hatch) in the `notify-undeployed-release` job.
#
# WHY THIS EXISTS, AND WHY IT IS NOT notify-smoke-failure.sh:
#   `smoke-tests` carries `needs: deploy`. A FAILED deploy therefore SKIPS
#   smoke-tests, and notify-smoke-failure.sh never runs. Before TBD-424 the
#   repo had an alarm for "deployed but not serving" and NONE for "did not
#   deploy at all".
#
#   The signal here is not "the deploy failed" -- a failed deploy that cut no
#   tag leaves nothing inconsistent behind. It is the THREE-WAY DIVERGENCE
#   that a published release plus a failed deploy creates:
#     1. an immutable git tag + GitHub Release exist for this version,
#     2. production is still serving the code from BEFORE that tag,
#     3. `main` matches neither.
#   Every downstream artefact (changelog, "what's in prod?", the next
#   release's diff baseline) is wrong until somebody reconciles it.
#
# ⚠ DELIBERATE DUPLICATION OF scripts/notify-smoke-failure.sh, NOT A MISSED
# EXTRACTION. The two scripts do NOT need to stay in sync and should not be
# refactored into one on the grounds that they look alike:
#   - different firing conditions (a failed deploy vs a failed post-deploy
#     probe), which are diagnosed and remediated differently;
#   - different issue BODIES -- the whole value of each is the specific
#     sentence it writes about what is now inconsistent;
#   - different DEDUPE BUCKETS on purpose ("[undeployed-release]" vs
#     "[smoke-fail]"): collapsing them would let one alarm silence the other,
#     and these two can legitimately be open at the same time.
# This is unlike the path allowlists, which must mirror each other because
# they partition one surface. Extract only on the third of these.
#
# Inputs (env vars provided by the workflow):
#   GH_TOKEN       — github.token
#   GH_REPO        — owner/repo (github.repository)
#   RUN_ID         — github.run_id
#   SHA            — github.sha
#   REF_NAME       — github.ref_name (branch)
#   ACTOR          — github.actor
#   DEPLOY_RESULT  — needs.deploy.result (failure / cancelled / skipped)
#   RELEASE_TAG    — OPTIONAL. needs.release.outputs.new_release_git_tag.
#                    Empty on the manual deploy.yml path, which has no
#                    `release` job. Not in the required list on purpose: a
#                    manual deploy that fails is still worth an alarm, and
#                    exiting 2 there would trade the alarm for a red step.
#
# Behavior:
#   - Dedupes against any open issue whose title starts with
#     "[undeployed-release]", appending a comment rather than opening a fresh
#     issue for each repeated failure.
#   - Title-based dedupe (not label-based) so this works even before the
#     `undeployed-release` label exists in the repo.
#   - Best-effort label: tries `--label undeployed-release` first and retries
#     without it if the label is not present.
#
# Exit codes:
#   0  issue opened or commented successfully
#   1  unexpected gh failure
#   2  required env var missing

set -uo pipefail

for var in GH_TOKEN GH_REPO RUN_ID SHA REF_NAME ACTOR DEPLOY_RESULT; do
  if [[ -z "${!var:-}" ]]; then
    echo "✗ ${var} is not set"
    exit 2
  fi
done

TITLE="[undeployed-release] Release published but not deployed"
RUN_URL="https://github.com/${GH_REPO}/actions/runs/${RUN_ID}"
TAG="${RELEASE_TAG:-}"

if [[ -n "$TAG" ]]; then
  TAG_LINE="- **Release tag:** \`${TAG}\` (published, immutable)"
  DIVERGENCE="Production is running PRE-\`${TAG}\` code, and \`${TAG}\` corresponds to nothing deployed."
else
  TAG_LINE="- **Release tag:** none — this was a manual \`deploy.yml\` run, which cuts no tag."
  DIVERGENCE="Production is running whatever it served before this run; \`${REF_NAME}\` corresponds to nothing deployed."
fi

BODY="$(cat <<EOM
The \`deploy\` job ended \`${DEPLOY_RESULT}\` for \`${SHA}\` on \`${REF_NAME}\`.

${TAG_LINE}
- **Deploy result:** \`${DEPLOY_RESULT}\`
- **Workflow run:** ${RUN_URL}
- **Triggered by:** @${ACTOR}

${DIVERGENCE}

This is not the same alarm as \`[smoke-fail]\`. There, the deploy succeeded
and the live app failed its post-deploy probes. Here the deploy never
completed, so \`smoke-tests\` was SKIPPED and reports nothing at all.

**What is now inconsistent:** the published release, the running production
app, and \`main\` are three different states. Changelogs and any "what is in
production?" answer derived from the tag are wrong until this is reconciled.

**To reconcile:** fix the deploy failure, then re-run the deploy with
\`gh workflow run deploy.yml --ref main\` (deliberately ungated — see
scripts/ci/await-test-run.sh). The git tag is intentionally NOT deleted; it
is immutable by policy and other artefacts already reference it.

New failures are appended to this issue as comments. Close it manually once
production is serving the released code.
EOM
)"

# Dedupe: look for an open issue whose title contains "[undeployed-release]".
# Title search rather than a label so this works whether or not the label has
# been created in the repo. Separate bucket from "[smoke-fail]" on purpose.
EXISTING="$(gh issue list \
  --repo "$GH_REPO" \
  --state open \
  --search '"[undeployed-release]" in:title' \
  --json number \
  --jq '.[0].number // empty' || true)"

if [[ -n "$EXISTING" ]]; then
  if gh issue comment "$EXISTING" --repo "$GH_REPO" --body "$BODY"; then
    echo "Appended undeployed-release comment to existing issue #${EXISTING}"
    echo "Run URL: ${RUN_URL}"
    exit 0
  fi
  echo "✗ failed to comment on issue #${EXISTING}"
  exit 1
fi

# No open undeployed-release issue. Try with the label first; if the label
# doesn't exist yet, gh exits non-zero — fall back to no label so the signal
# still lands.
if gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" --label undeployed-release >/dev/null 2>&1; then
  echo "Created new undeployed-release issue (with label)"
elif gh issue create --repo "$GH_REPO" \
       --title "$TITLE" --body "$BODY" >/dev/null; then
  echo "Created new undeployed-release issue (label not present in repo)"
else
  echo "✗ failed to create undeployed-release issue"
  exit 1
fi

echo "Run URL: ${RUN_URL}"
