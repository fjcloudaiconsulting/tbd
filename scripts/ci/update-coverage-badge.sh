#!/usr/bin/env bash
# Write one coverage percentage to the shields.io endpoint gist (TBD-500).
#
# Usage: update-coverage-badge.sh <area> <percent>
#   area    : backend | frontend   (selects the gist filename)
#   percent : bare number, no % sign, e.g. 83 or 88.83
#
# ⚠ NUMBER ONLY, DELIBERATELY. The repo is public. A coverage *report* is a map
# of which code paths the tests do not exercise, which is the class of
# information TBD-495/496 removed from this tree. A bare percentage carries no
# such signal, which is why this writes to a gist rather than uploading reports
# to a third-party service.
#
# ⚠ The README badge must use the VERSIONLESS raw URL:
#     https://gist.githubusercontent.com/<user>/<gist-id>/raw/<filename>
# The `raw_url` the API returns embeds a revision SHA that changes on every
# update, so a badge built from it silently freezes at its first value and
# never reports drift again.
#
# ⚠ Fails SOFT. A badge is decoration; it must never fail a required check.
# Missing token or a GitHub API error exits 0 with a warning. The caller also
# carries `continue-on-error`, so this is belt and braces on purpose.
set -uo pipefail

AREA="${1:-}"
PCT="${2:-}"
GIST_ID="${GIST_ID:-f195ef7d7c927448bc7ccfae53fb6d58}"

warn() { echo "update-coverage-badge: $*" >&2; exit 0; }

case "$AREA" in
  backend|frontend) ;;
  *) warn "area must be 'backend' or 'frontend', got '${AREA}'" ;;
esac

# Reject anything that is not a plain number: an empty or malformed extraction
# upstream must not publish "coverage: unknown%" over a previously good value.
case "$PCT" in
  ''|*[!0-9.]*) warn "percent must be a bare number, got '${PCT}'" ;;
esac

[ -n "${GIST_TOKEN:-}" ] || warn "GIST_TOKEN not set; skipping badge update"

# shields.io's own convention, so the colour means the same here as elsewhere.
INT="${PCT%%.*}"
if   [ "$INT" -ge 95 ]; then COLOR=brightgreen
elif [ "$INT" -ge 90 ]; then COLOR=green
elif [ "$INT" -ge 80 ]; then COLOR=yellowgreen
elif [ "$INT" -ge 70 ]; then COLOR=yellow
elif [ "$INT" -ge 60 ]; then COLOR=orange
else                         COLOR=red
fi

FILE="tbd-coverage-${AREA}.json"
BODY=$(python3 -c '
import json, sys
area, pct, color, fname = sys.argv[1:5]
content = json.dumps({
    "schemaVersion": 1,
    "label": f"{area} coverage",
    "message": f"{pct}%",
    "color": color,
})
print(json.dumps({"files": {fname: {"content": content}}}))
' "$AREA" "$PCT" "$COLOR" "$FILE") || warn "could not build payload"

CODE=$(curl -sS -o /tmp/badge-resp.json -w '%{http_code}' \
  -X PATCH \
  -H "Authorization: Bearer ${GIST_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/gists/${GIST_ID}" \
  -d "$BODY" 2>/dev/null) || warn "curl failed"

if [ "$CODE" = "200" ]; then
  echo "update-coverage-badge: ${AREA} = ${PCT}% (${COLOR})"
else
  warn "gist update returned HTTP ${CODE}"
fi
