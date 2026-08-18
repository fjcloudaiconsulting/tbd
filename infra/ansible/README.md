# <data-droplet> Ansible

Configuration management for the pfv data droplet (MySQL + Redis).

Provisioning lives in Terraform (`infra/terraform/`); this playbook handles
post-boot config: packages, MySQL/Redis tuning, backups, fail2ban, swap.

## Run

```bash
infra/ansible/bin/run-playbook.sh              # apply
infra/ansible/bin/run-playbook.sh --check --diff   # dry run, changes nothing
```

That is the whole procedure. Nothing needs to be filled in by hand.

### Why it is a wrapper and not a bare `ansible-playbook`

`inventory.yml` is **generated**, gitignored, and holds **no secrets**:

```bash
infra/ansible/bin/gen-inventory.py            # from Terraform outputs
infra/ansible/bin/gen-inventory.py --stdout   # inspect without writing
```

Credentials come from Terraform state (TBD-207). `random_password` resources in
`infra/terraform` generate them; `run-playbook.sh` reads them via
`terraform output -json` into a mode-0600 temp file, passes it by reference, and
removes it on every exit path.

⚠ **Do not reintroduce hand-maintained credentials.** This layout exists because
of a measured failure on 2026-08-18: the passwords lived only in a gitignored
`inventory.yml` on one laptop, in a MySQL hash, and in a write-only App Platform
secret. When the file went missing they were unrecoverable from anything and the
data plane could not be configured at all. Three shortcuts are specifically
closed:

| Shortcut | Why it is refused |
|---|---|
| Secrets in `inventory.yml` | Plaintext at rest on every laptop, forever. The habit that caused the outage. |
| `--extra-vars key=value` | Visible in the process table to every local user for the run's lifetime. |
| Committed `ansible-vault` file | **This repo is public.** A permanent harvestable artefact regardless of passphrase strength. |

The roles also **fail closed**: `mysql` and `redis` assert as their first task
that their secrets are set and are not the `CHANGE_ME` default, so a missing or
mis-pointed inventory aborts instead of writing `CHANGE_ME` into production.

### Rehearsing against a throwaway box

```bash
infra/ansible/bin/run-playbook.sh --scratch-host <ip>
```

Regenerates the inventory pointed at that address instead of the data droplet,
so the play can be exercised end to end without touching production.

### Prerequisites

- `terraform login` (or `TF_TOKEN_app_terraform_io`) — the runner reads TFC state
- Ansible on `PATH`, or a venv at `~/.virtualenvs/ansible` (override `VENV_ANSIBLE`)
- SSH key registered in DO (override with `SSH_KEY`; defaults to `~/.ssh/id_rsa.home`)

## Firewall: single layer, DO cloud firewall only

The DigitalOcean cloud firewall `<data-firewall>` is the single source of truth
for inbound rules on managed droplets. UFW is intentionally **disabled** by
the `common` role.

### Why

Layering UFW on top of the DO cloud firewall risks silent drops during VPC
NAT translation: a TCP SYN can reach the droplet's VPC interface from a
rewritten source address that UFW's CIDR rule no longer matches. Symptoms
look like generic connectivity timeouts (App Platform to Redis on
`<vpc-cidr>` was the case that triggered this consolidation on 2026-05-13).

### Rules enforced by `<data-firewall>`

- TCP 3306 (MySQL): from `<vpc-cidr>`
- TCP 6379 (Redis): from `<vpc-cidr>`
- TCP 22 (SSH): from `0.0.0.0/0`
- ICMP: from the VPC subnet

If you need to add a rule, edit the DO cloud firewall in Terraform (or via
`doctl compute firewall update`). Do not re-add UFW tasks to the `common`
role.
