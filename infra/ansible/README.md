# <data-droplet> Ansible

Configuration management for the pfv data droplet (MySQL + Redis).

Provisioning lives in Terraform (`infra/terraform/`); this playbook handles
post-boot config: packages, MySQL/Redis tuning, backups, fail2ban, swap.

## Run

```bash
infra/ansible/bin/run-playbook.sh --scratch-host <ip> --scratch-private-ip <ip>   # rehearse
infra/ansible/bin/run-playbook.sh --production --check --diff                     # dry run
infra/ansible/bin/run-playbook.sh --production                                    # apply
infra/ansible/bin/run-playbook.sh --production -- --tags patch                    # deliberate OS patch
```

Nothing needs to be filled in by hand.

⚠ **`--scratch-host` needs `--scratch-private-ip` too.** It is not optional and
there is no fallback: the redis role binds to `private_ipv4`, and
`bin/gen-inventory.py` exits 2 rather than silently reusing production's private
address, which the scratch box does not own. This README used to show the
one-flag form; it never worked.

⚠ **The play does NOT upgrade packages on a routine converge (TBD-419).** It
used to, unconditionally, as its very first task — so converging a Redis knob
also performed an unbounded package upgrade on the production database droplet.
OS patching is `unattended-upgrades` (daily, `noble-security`); a deliberate,
windowed upgrade is `-- --tags patch`. The MySQL packages are held in the dpkg
database by the `common` role, so they are `kept back` even then. Procedure and
rationale: `infra/MIGRATION.md`, "Data-plane package pins".

⚠ **`--check` skips the mysql and redis roles' verification fences**,
deliberately: they assert properties of the *converged* server, and check mode
converges nothing. A clean dry run is therefore not evidence that those fences
pass. The same is true of the package-hold read-back, for the same reason.

✅ **`--check` DOES run the MySQL repo-track fence, and that is the point.** It
asserts a property of the apt *repository* (candidate track == installed
track), which is identical before and after the play, so a dry run is exactly
where repo drift should surface — before anyone is in a window. Do not
"harmonise" its gating with the fences above. ⚠ `--check` is
only meaningful against an **already-provisioned** host; against a fresh scratch
droplet it cannot complete, because tasks downstream of a skipped one (the swap
file, a running mysqld) have nothing to act on.

⚠ **`--check --diff` prints the rotated MySQL and Redis passwords in cleartext**
— the template diffs are the payload and neither task is `no_log`. Do not tee it
to a world-readable file; see `infra/MYSQL-84-EXECUTE.md` 0.3.

⚠⚠ **A target is mandatory; there is no default.** Since TBD-207 the credentials
are Terraform-generated, so `--production` **rotates** them — and the app keeps
authenticating with the old password until **both** `DATABASE_URL` bindings in
`.do/app.yaml` (the backend service **and** the migrate PRE_DEPLOY job) are
re-encrypted and redeployed. Between those two moments the app cannot connect.
That is a sequenced operation, so it must never be what you get from typing the
command bare. Pick a quiet hour.

⚠ This used to point at the TBD-360 window, "where the backend is
already scaled to 0". Both halves were wrong: that window closed on 2026-08-19,
and scaling the backend to 0 was never possible on this component's plan — it
was attempted during the window and refused (TBD-416).

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
infra/ansible/bin/run-playbook.sh --scratch-host <ip> --scratch-private-ip <ip>
```

Regenerates the inventory pointed at that address instead of the data droplet,
so the play can be exercised end to end without touching production. Both flags
are required; see the warning above.

⚠ This is the only way to exercise `--tags patch` before using it on
production, and `infra/MIGRATION.md` makes that a required step of the
deliberate-upgrade procedure.

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
