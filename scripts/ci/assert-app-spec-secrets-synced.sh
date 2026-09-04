#!/usr/bin/env bash
# Refuse to deploy when the committed app spec would OVERWRITE a live secret.
#
# ⚠⚠ WHY THIS EXISTS — 2026-08-20 production incident (TBD-425).
#
# `digitalocean/app_action/deploy@v2` pushes the committed `.do/app.yaml` as the
# AUTHORITATIVE spec on every deploy. Every `type: SECRET` env var in that file
# carries an encrypted `EV[...]` value, so a deploy silently replaces whatever
# is live with whatever was committed.
#
# `infra/MIGRATION.md` step 9 has warned about this since the 2026-05 cutover:
#
#   "If you skip this step, the next normal deploy reverts the live spec back
#    to the committed file, [...] pointing secrets at whatever was there before."
#
# It was skipped. Data-plane credentials were rotated on the droplet, corrected
# in the DO console so the app kept working, and never synced back to the repo.
# For roughly a day nothing deployed (release.yml's paths filter meant infra-only
# merges never triggered one), so nobody noticed. The first real deploy pushed
# the stale blobs and took production's database AND redis credentials with it:
#
#   (1045, "Access denied for user 'pfv_app'@'10.42.0.3' (using password: YES)")
#   scheduler.tick.error: "invalid username-password pair or user is disabled."
#
# A comment in a runbook did not prevent it. This does: the deploy now fails
# BEFORE pushing, naming every secret that would change.
#
# ⚠ Comparing the ciphertext is sound here, and that is not obvious. App Platform
# does NOT re-encrypt on read — a value fetched via `doctl apps spec get` is
# byte-identical to the one that was pushed. Verified 2026-08-20 across all 15
# secrets. If DO ever changes that, this guard turns into a permanent false
# positive rather than a silent pass, which is the right way round to fail.
set -euo pipefail

SPEC_FILE="${SPEC_FILE:-.do/app.yaml}"
APP_ID="${APP_ID:-}"
APP_NAME="${APP_NAME:-}"

# ⚠ Deliberate override for the break-glass path only. `deploy.yml` is
# documented as ungated (its escape hatch is `gh workflow run
# deploy.yml --ref main`, which stays deliberately ungated), so this guard
# must be refusable there -- but only on purpose, never by default.
if [ "${ALLOW_SECRET_DRIFT:-false}" = "true" ]; then
  echo "assert-app-spec-secrets-synced: SKIPPED - ALLOW_SECRET_DRIFT=true."
  echo "⚠ The deploy may overwrite live secrets with the committed spec."
  exit 0
fi

if [ -z "${APP_ID}" ] && [ -n "${APP_NAME}" ]; then
  APP_ID="$(doctl apps list --format ID,Spec.Name --no-header 2>/dev/null \
            | awk -v n="${APP_NAME}" '$2 == n { print $1; exit }')"
fi
if [ -z "${APP_ID}" ]; then
  echo "assert-app-spec-secrets-synced: could not determine the app id." >&2
  echo "Set APP_ID, or APP_NAME resolvable via \`doctl apps list\`." >&2
  exit 2
fi
if [ ! -f "${SPEC_FILE}" ]; then
  echo "assert-app-spec-secrets-synced: ${SPEC_FILE} not found." >&2
  exit 2
fi

LIVE_SPEC="$(mktemp)"
trap 'rm -f "${LIVE_SPEC}"' EXIT

if ! doctl apps spec get "${APP_ID}" > "${LIVE_SPEC}" 2>/dev/null; then
  # ⚠ Fail CLOSED. An unreadable live spec means we cannot prove the deploy is
  # safe, and this guard exists precisely because "assume it is fine" cost a
  # production outage.
  echo "assert-app-spec-secrets-synced: could not read the live spec for ${APP_ID}." >&2
  echo "Refusing to deploy: cannot prove the committed secrets match production." >&2
  exit 1
fi

if ! python3 -c "import yaml" 2>/dev/null; then
  echo "assert-app-spec-secrets-synced: PyYAML is required but not importable." >&2
  echo "In CI the calling step installs it (see release.yml / deploy.yml)." >&2
  exit 2
fi

python3 - "${SPEC_FILE}" "${LIVE_SPEC}" <<'PY'
import sys, yaml

def secrets(path):
    """{(component, key): value} for every type: SECRET env var."""
    spec = yaml.safe_load(open(path))
    out = {}
    for kind in ("services", "jobs", "workers", "functions", "static_sites"):
        for comp in spec.get(kind) or []:
            for env in comp.get("envs") or []:
                if env.get("type") == "SECRET":
                    out[(comp["name"], env["key"])] = env.get("value") or ""
    return out

committed, live = secrets(sys.argv[1]), secrets(sys.argv[2])

# Anti-vacuity floor: a parse that yields nothing must not pass silently. This
# is the same reason test.yml's wiring guard asserts a minimum job count.
if len(committed) < 5:
    sys.exit(
        f"only {len(committed)} SECRET env var(s) parsed from the committed "
        "spec; expected the real app to have many. Refusing to certify."
    )

problems = []
for key in sorted(set(committed) | set(live)):
    comp, name = key
    c, l = committed.get(key), live.get(key)
    if c is None:
        problems.append(f"  {comp}/{name}: live has a secret the committed spec does not declare -> the deploy would DELETE it")
    elif l is None:
        problems.append(f"  {comp}/{name}: committed only (new secret) -> will be created")
    elif c != l:
        problems.append(f"  {comp}/{name}: committed value DIFFERS from live -> the deploy would OVERWRITE production's value")

if problems:
    print("assert-app-spec-secrets-synced: REFUSING TO DEPLOY.\n", file=sys.stderr)
    print("The committed app spec disagrees with the live app on these secrets:\n", file=sys.stderr)
    print("\n".join(problems), file=sys.stderr)
    print(
        "\nThis is how the 2026-08-20 outage happened: a deploy pushed stale\n"
        "credentials over the working ones and took the database and redis down.\n\n"
        "If the LIVE values are correct (someone fixed them in the console),\n"
        "sync them into the repo -- infra/MIGRATION.md step 9:\n"
        "    doctl apps spec get <APP_ID> > /tmp/live-app.yaml\n"
        "    # copy the EV[...] blobs into .do/app.yaml, commit, re-run\n\n"
        "If the COMMITTED values are correct, someone changed production by hand\n"
        "and that change needs to be understood before it is overwritten.",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"assert-app-spec-secrets-synced: all {len(committed)} secrets match the live app.")
PY
