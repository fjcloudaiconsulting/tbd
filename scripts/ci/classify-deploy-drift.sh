#!/usr/bin/env bash
# Classify how a deployed commit relates to the latest release tag (TBD-499).
#
# Usage: classify-deploy-drift.sh <deployed-sha> <tag-sha>
# Prints exactly one of: same | behind | ahead | diverged | unknown
#
# ⚠ WHY THIS IS ITS OWN FILE. check-deploy-drift.sh needs doctl and a live
# APP_ID, so its classification could only ever be fenced by parsing the source
# -- and a fence that asserts an `if` exists is not a fence on behaviour. Split
# out, the real classifier runs against a real git repository in the test.
#
# ⚠ `unknown` is a DISTINCT answer, not a synonym for diverged. A history
# rewrite orphans the SHA the deployment platform recorded (TBD-496), so the
# deployed commit can be absent from the repo entirely. Calling that "diverged"
# would report the most serious state for the most benign cause.
set -uo pipefail

DEPLOYED="${1:-}"
TAG_SHA="${2:-}"

[ -n "$DEPLOYED" ] && [ -n "$TAG_SHA" ] || { echo "unknown"; exit 0; }
[ "$DEPLOYED" = "$TAG_SHA" ] && { echo "same"; exit 0; }

git cat-file -e "${DEPLOYED}^{commit}" 2>/dev/null || { echo "unknown"; exit 0; }
git cat-file -e "${TAG_SHA}^{commit}"  2>/dev/null || { echo "unknown"; exit 0; }

if git merge-base --is-ancestor "$DEPLOYED" "$TAG_SHA" 2>/dev/null; then
  echo "behind"
elif git merge-base --is-ancestor "$TAG_SHA" "$DEPLOYED" 2>/dev/null; then
  echo "ahead"
else
  echo "diverged"
fi
