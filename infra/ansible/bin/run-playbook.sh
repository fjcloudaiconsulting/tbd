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
#   bin/run-playbook.sh                          # against the real data droplet
#   bin/run-playbook.sh --check --diff           # dry run, changes nothing
#   bin/run-playbook.sh --scratch-host 1.2.3.4   # rehearse on a throwaway box
#   bin/run-playbook.sh -- --tags mysql          # anything after -- goes to ansible
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(dirname "$HERE")"
TERRAFORM_DIR="$(dirname "$ANSIBLE_DIR")/terraform"
VENV_ANSIBLE="${VENV_ANSIBLE:-$HOME/.virtualenvs/ansible/bin}"

SCRATCH_HOST=""; HOST_NAME="pfv-data-01"; PASSTHRU=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scratch-host) SCRATCH_HOST="${2:-}"; HOST_NAME="scratch"; shift 2 ;;
    --) shift; PASSTHRU+=("$@"); break ;;
    *) PASSTHRU+=("$1"); shift ;;
  esac
done

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
  "$HERE/gen-inventory.py" --host "$SCRATCH_HOST" --name "$HOST_NAME"
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
