#!/usr/bin/env bash
# Open (or comment on) a deduped GitHub issue when the drift probe reports that
# production is not serving the latest published release.
#
# Required env: GH_TOKEN, GH_REPO, RUN_ID, DRIFT_REPORT.
#
# Modelled on scripts/notify-smoke-failure.sh: title-prefix dedupe rather than
# label-based, so it works before the label exists in the repo; best-effort
# label with a no-label retry so the signal always lands.
#
# ⚠ NOTE ON THE THIRD COPY. This is the third notifier of this shape
# (notify-smoke-failure.sh, notify-undeployed-release.sh, and now this one), and
# notify-undeployed-release.sh says extraction should happen "on the third".
# It is deliberately NOT extracted here, and the reason is scope rather than
# laziness: the shared part is ~15 lines of `gh issue` plumbing, while the risk
# of the refactor lands squarely on two INCIDENT-notification paths whose
# failure mode is silence during an outage. Bundling it into a PR that adds a
# probe would also mean a reviewer assessing a new probe is silently reviewing a
# refactor of two live notifiers. Tracked separately.
set -uo pipefail

for v in GH_TOKEN GH_REPO RUN_ID DRIFT_REPORT; do
  if [ -z "${!v:-}" ]; then
    echo "notify-deploy-drift: $v is required" >&2
    exit 2
  fi
done

TITLE_PREFIX="[deploy-drift]"
TITLE="${TITLE_PREFIX} production is not serving the latest release"
RUN_URL="https://github.com/${GH_REPO}/actions/runs/${RUN_ID}"

BODY="${DRIFT_REPORT}

Detected by the scheduled drift probe: ${RUN_URL}

<sub>Deduped by title prefix \`${TITLE_PREFIX}\`; repeated detections comment here rather than opening new issues.</sub>"

EXISTING="$(gh issue list \
  --repo "$GH_REPO" \
  --state open \
  --search "in:title ${TITLE_PREFIX}" \
  --json number \
  --jq '.[0].number' 2>/dev/null)"

if [ -n "${EXISTING:-}" ] && [ "$EXISTING" != "null" ]; then
  if gh issue comment "$EXISTING" --repo "$GH_REPO" --body "$BODY"; then
    echo "Commented on existing deploy-drift issue #${EXISTING}"
    exit 0
  fi
  echo "notify-deploy-drift: failed to comment on #${EXISTING}" >&2
  exit 1
fi

if gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" --label deploy-drift >/dev/null 2>&1; then
  echo "Created new deploy-drift issue (with label)"
elif gh issue create --repo "$GH_REPO" \
     --title "$TITLE" --body "$BODY" >/dev/null 2>&1; then
  echo "Created new deploy-drift issue (label not present in repo)"
else
  echo "notify-deploy-drift: failed to create issue" >&2
  exit 1
fi
