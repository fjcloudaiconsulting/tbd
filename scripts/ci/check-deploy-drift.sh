#!/usr/bin/env bash
# Read-only probe: is production running the latest RELEASED commit?
#
# Prints a one-line verdict and exits 0 (in sync) or 1 (drifted). Writes a
# markdown report to $GITHUB_STEP_SUMMARY when present, and exports
# DRIFT_REPORT for the notifier.
#
# ⚠⚠ READ-ONLY, AND THAT IS LOAD-BEARING (TBD-425). The 2026-08-20 production
# incident was caused by a DEPLOY: `digitalocean/app_action/deploy@v2` pushes
# the committed `.do/app.yaml` as authoritative, so it overwrote database and
# redis credentials that had been fixed in the console but never synced back to
# the repo. A deploy-based liveness probe would therefore have DESTROYED
# working credentials sooner, not detected drift sooner. This script only ever
# reads: `doctl apps get` / `list-deployments`, and `git`. It must never push a
# spec, create a deployment, or restart a component.
#
# ⚠ COMPARE AGAINST THE LATEST TAG, NOT `main` HEAD.
#
# TBD-434 was filed saying "compare the active deployment SHA against main
# HEAD". That is WRONG and would false-alarm continuously. Since TBD-424 made
# `.releaserc.json`'s scope suppressions actually work, a `ci(...)`, `fix(infra)`
# or `docs(...)` merge legitimately cuts NO release and therefore legitimately
# never deploys, so `main` runs ahead of production as a matter of correct
# design. Measured at the time of writing: main `8ab9036d`, latest tag v0.258.6
# = `b310402d`, deployed `b310402d` -- healthy, yet 2 commits behind main.
#
# The honest invariant is: the active deployment's commit == the commit the
# LATEST RELEASE TAG points at. If a release was published and production is not
# running it, something failed or rolled back, and THAT is drift.
set -uo pipefail

: "${APP_ID:?APP_ID is required}"

fail() { echo "check-deploy-drift: $*" >&2; exit 2; }

command -v doctl >/dev/null 2>&1 || fail "doctl not on PATH"
command -v git   >/dev/null 2>&1 || fail "git not on PATH"

TAG="$(git describe --tags --abbrev=0 2>/dev/null)" || fail "no tags found"
TAG_SHA="$(git rev-list -n1 "$TAG" 2>/dev/null)" || fail "cannot resolve $TAG"

DEPLOY_ID="$(doctl apps list-deployments "$APP_ID" --format ID --no-header 2>/dev/null | head -1)"
[ -n "$DEPLOY_ID" ] || fail "could not list deployments for $APP_ID"

DEPLOY_JSON="$(doctl apps get-deployment "$APP_ID" "$DEPLOY_ID" -o json 2>/dev/null)"
[ -n "$DEPLOY_JSON" ] || fail "could not read deployment $DEPLOY_ID"

# One commit per component; they are pushed together, but assert that rather
# than assume it -- a partial deploy is exactly the state worth alarming on.
read -r PHASE COMMITS <<EOF
$(printf '%s' "$DEPLOY_JSON" | python3 -c '
import sys, json
d = json.load(sys.stdin)
d = d[0] if isinstance(d, list) else d
shas = set()
for group in ("services", "jobs", "workers", "static_sites"):
    for c in (d.get(group) or []):
        if c.get("source_commit_hash"):
            shas.add(c["source_commit_hash"])
print((d.get("phase") or "UNKNOWN"), ",".join(sorted(shas)) or "none")
')
EOF

[ -n "${PHASE:-}" ] || fail "could not parse deployment phase"

DRIFTED=0
REASONS=""
add() { REASONS="${REASONS}- $1"$'\n'; }

if [ "$COMMITS" = "none" ]; then
  DRIFTED=1
  add "The active deployment reports **no source commit** for any component."
elif printf '%s' "$COMMITS" | grep -q ','; then
  DRIFTED=1
  add "Components are running **different commits**: \`${COMMITS}\`. That is a partial deploy."
elif [ "$COMMITS" != "$TAG_SHA" ]; then
  DRIFTED=1
  add "Production is running \`${COMMITS:0:8}\` but the latest release **${TAG}** is \`${TAG_SHA:0:8}\`."
  add "A release was published that production is not serving: a failed deploy, or an auto-rollback."
fi

if [ "$PHASE" != "ACTIVE" ]; then
  DRIFTED=1
  add "The newest deployment is in phase **${PHASE}**, not ACTIVE."
fi

# ── Secret-spec drift (TBD-434 DoD 2) ───────────────────────────────────────
#
# The commit comparison above answers "is production running the released
# code?". It CANNOT see the other half of the TBD-425 failure: the committed
# `.do/app.yaml` silently disagreeing with the live app's secrets. That drift is
# invisible until the next deploy, at which point it OVERWRITES production's
# working credentials -- which is exactly what took the database and redis down
# on 2026-08-20.
#
# `assert-app-spec-secrets-synced.sh` already answers that question and is
# read-only (it fetches the live spec and compares ciphertext; App Platform does
# not re-encrypt on read). Here it is a REPORTING input, not a gate: its
# non-zero exit must not fail this job, it must fold into the drift report so
# the notifier carries it.
SPEC_GUARD="$(dirname "$0")/assert-app-spec-secrets-synced.sh"
if [ -x "$SPEC_GUARD" ]; then
  SPEC_OUT="$(APP_ID="$APP_ID" "$SPEC_GUARD" 2>&1)"
  SPEC_STATUS=$?
  if [ "$SPEC_STATUS" -ne 0 ]; then
    DRIFTED=1
    add "The committed \`.do/app.yaml\` **disagrees with the live app's secrets**. The next deploy would overwrite production's values (this is the TBD-425 mechanism)."
    SPEC_DETAIL="

<details><summary>assert-app-spec-secrets-synced output</summary>

\`\`\`
${SPEC_OUT}
\`\`\`

</details>"
  fi
else
  DRIFTED=1
  add "Could not run \`assert-app-spec-secrets-synced.sh\` (missing or not executable), so secret drift is UNCHECKED. Failing loud rather than reporting a partial all-clear."
fi

if [ "$DRIFTED" -eq 0 ]; then
  echo "in-sync: ${TAG} (${TAG_SHA:0:8}) is live, phase ${PHASE}; secrets match"
  exit 0
fi

REPORT="Production has drifted from the latest published release.

${REASONS}
| | |
|---|---|
| latest release tag | \`${TAG}\` (\`${TAG_SHA:0:8}\`) |
| deployed commit(s) | \`${COMMITS}\` |
| deployment phase | \`${PHASE}\` |

⚠ This probe is READ-ONLY by design. Do **not** \"fix\" this by triggering a
deploy blindly: the committed \`.do/app.yaml\` is pushed as authoritative and can
overwrite live secrets (TBD-425). Check \`scripts/ci/assert-app-spec-secrets-synced.sh\`
first.${SPEC_DETAIL:-}"

export DRIFT_REPORT="$REPORT"
[ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$REPORT" >> "$GITHUB_STEP_SUMMARY"
printf '%s\n' "$REPORT"
exit 1
