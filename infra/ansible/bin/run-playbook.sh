#!/usr/bin/env bash
# TBD-207 — run the data-plane playbook with credentials sourced from Terraform.
#
# WHY A WRAPPER. The credentials must come from exactly one place (TFC state)
# and must not come to rest anywhere else. Three tempting shortcuts are all
# wrong, so they are closed here rather than left to discipline:
#
#   * writing them into inventory.yml -> plaintext at rest on every laptop,
#     forever. That habit is what produced the CHANGE_ME footgun and the
#     unrecoverable-password dead end of 2026-08-18.
#   * passing them as `--extra-vars key=value` -> visible in the process table
#     to every local user for the life of the run.
#   * committing an ansible-vault file -> this repository is PUBLIC, so it
#     becomes a permanent harvestable artefact regardless of passphrase strength.
#
# Instead: a mode-0600 temp file created with a restrictive umask, passed by
# reference, and removed on EVERY exit path including interrupt.
#
# Usage:
# ⚠⚠ A TARGET IS MANDATORY. There is deliberately no default.
# Since TBD-207 the credentials are Terraform-generated, so applying this play
# to production ROTATES them -- and the app keeps using the old password until
# BOTH DATABASE_URL bindings in .do/app.yaml (the backend service and the
# migrate PRE_DEPLOY job) are re-encrypted and redeployed. Between those two
# moments the app cannot authenticate. That is a deliberate, sequenced
# operation, so it must never be what you get by typing the command with no
# arguments and hitting return.
#
# Usage:
#   bin/run-playbook.sh --scratch-host 1.2.3.4 --scratch-private-ip 10.x.x.x
#   bin/run-playbook.sh --production             # the real data droplet
#   bin/run-playbook.sh --production --check --diff   # dry run, changes nothing
#   bin/run-playbook.sh --production -- --tags patch  # deliberate, windowed OS patch
#
# ⚠ THERE ARE NO TOPIC TAGS. This banner used to advertise
# `-- --tags mysql`; that invocation has never done anything useful. Measured
# with `--list-tasks --tags mysql`: no role in playbooks/site.yml and no task in
# roles/mysql, roles/redis or roles/backups carries a topic tag, so it runs only
# the `always`-tagged tasks -- the MySQL package holds and the repo-track fence
# -- and ZERO mysql-role tasks. `patch`, `never` and `always` are the only tags
# in this tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(dirname "$HERE")"
TERRAFORM_DIR="$(dirname "$ANSIBLE_DIR")/terraform"
VENV_ANSIBLE="${VENV_ANSIBLE:-$HOME/.virtualenvs/ansible/bin}"

SCRATCH_HOST=""; SCRATCH_PRIVATE_IP=""; HOST_NAME="pfv-data-01"; PRODUCTION=0; PASSTHRU=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scratch-host) SCRATCH_HOST="${2:-}"; HOST_NAME="scratch"; shift 2 ;;
    --scratch-private-ip) SCRATCH_PRIVATE_IP="${2:-}"; shift 2 ;;
    --production)   PRODUCTION=1; shift ;;
    --) shift; PASSTHRU+=("$@"); break ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done

if [[ -n "$SCRATCH_HOST" && $PRODUCTION -eq 1 ]]; then
  echo "!! --scratch-host and --production are mutually exclusive"; exit 2
fi
if [[ -z "$SCRATCH_HOST" && $PRODUCTION -eq 0 ]]; then
  cat >&2 <<'USAGE'
!! No target given, and there is no default. Pick one explicitly:

     --scratch-host <ip> --scratch-private-ip <ip>
                           rehearse against a throwaway droplet.
                           BOTH flags are required: the redis role binds to
                           private_ipv4 and there is deliberately no fallback.
     --production          the real data droplet

   ⚠ --production ROTATES the data-plane credentials to the Terraform-generated
     values. The app authenticates with the OLD password until both
     DATABASE_URL bindings in .do/app.yaml -- the backend service AND the
     migrate PRE_DEPLOY job -- are re-encrypted and redeployed. Sequence it;
     pick a quiet hour. (TBD-360's window closed 2026-08-19. Scaling the
     backend to 0 is not available at all: the console refuses it, and `doctl
     apps update --spec` SILENTLY IGNORES instance_count: 0 -- exits 0, changes
     nothing, and no plan tier fixes that CLI route. See TBD-416.)
USAGE
  exit 2
fi

PLAYBOOK="$ANSIBLE_DIR/playbooks/site.yml"
[[ -f "$PLAYBOOK" ]] || { echo "!! no playbook at $PLAYBOOK"; exit 2; }

# umask BEFORE creating the file: mktemp is 0600 on Linux/macOS, but do not
# depend on a platform default for a file holding production credentials.
umask 077
VARS_FILE="$(mktemp -t tbd-dataplane-vars.XXXXXX)"
cleanup() { rm -f "$VARS_FILE"; }
trap cleanup EXIT INT TERM HUP

echo "==> regenerating inventory from Terraform"
if [[ -n "$SCRATCH_HOST" ]]; then
  # --private-ip is not optional: the redis role binds to it, so omitting it
  # fails the play at its LAST task, after MySQL is already provisioned.
  # Measured on the 2026-08-18 scratch run, which is why this is threaded.
  "$HERE/gen-inventory.py" --host "$SCRATCH_HOST" \
      --private-ip "$SCRATCH_PRIVATE_IP" --name "$HOST_NAME"
else
  "$HERE/gen-inventory.py"
fi

echo "==> reading credentials from Terraform state"
# `terraform output -json` includes sensitive values; plain `terraform output`
# redacts them. Piped straight into python so no secret is ever an argv element.
terraform -chdir="$TERRAFORM_DIR" output -json 2>/dev/null | python3 -c '
import json, sys
raw = json.load(sys.stdin)
WANT = ("mysql_app_password", "mysql_backup_password", "redis_password")
missing = [k for k in WANT if not (raw.get(k) or {}).get("value")]
if missing:
    sys.exit(
        "!! Terraform has not produced these outputs yet: " + ", ".join(missing) +
        "\n   TBD-207 adds them. They exist only after a TFC apply, which is"
        "\n   operator-gated (manual Confirm & Apply). Refusing to run with"
        "\n   role defaults -- that would set the production password to CHANGE_ME."
    )
out = {k: raw[k]["value"] for k in WANT}
json.dump(out, open(sys.argv[1], "w"))
' "$VARS_FILE"

echo "==> running playbook against ${SCRATCH_HOST:-the data droplet}"
PATH="$VENV_ANSIBLE:$PATH" \
ANSIBLE_CONFIG="$ANSIBLE_DIR/ansible.cfg" \
  ansible-playbook \
    -i "$ANSIBLE_DIR/inventory.yml" \
    --private-key "${SSH_KEY:-$HOME/.ssh/id_rsa.home}" \
    --extra-vars "@$VARS_FILE" \
    "${PASSTHRU[@]+"${PASSTHRU[@]}"}" \
    "$PLAYBOOK"
