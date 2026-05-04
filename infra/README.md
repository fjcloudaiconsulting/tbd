# pfv infra

Self-hosted MySQL + Redis on a single DigitalOcean droplet, replacing the DO
Managed MySQL + Managed Redis pair (~$30/mo) with one `s-1vcpu-1gb` droplet
(~$6/mo + ~$1.20/mo for backups).

This folder owns the cloud and the box. App Platform spec lives elsewhere.

## What's here

- `terraform/` — DO VPC, droplet, cloud firewall, project attachment.
- `ansible/` — Ubuntu 24.04 bootstrap: hardening, MySQL, Redis, nightly backups.
- `MIGRATION.md` — runbook for moving data off the managed services.

## Architecture

```
                     ┌────────────────────────────┐
                     │  DO App Platform (ams)     │
                     │   pfv backend / frontend   │
                     └──────────────┬─────────────┘
                                    │ VPC peering
                                    ▼
              VPC 10.42.0.0/24 ┌────────────────────────────┐
                               │  pfv-data-01 (s-1vcpu-1gb) │
                               │   - MySQL 8 (3306)         │
                               │   - Redis  (6379)          │
                               │   - ufw + fail2ban         │
                               │   - nightly mysqldump      │
                               └────────────────────────────┘
                                    ▲
                                    │ SSH (key auth) — public IPv4
                                    │
                                  operator
```

DO Cloud Firewall: SSH 22 from any IPv4, MySQL 3306 + Redis 6379 from VPC CIDR
only. ICMP from VPC. ufw on the host repeats the same restrictions.

## Prerequisites

- A DO API token with read/write scope. Set `TF_VAR_do_token` or fill
  `terraform.tfvars`.
- An SSH key already registered in DO (Settings -> Security). Note its name.
- A DO project named `pfv` (or change `project_name`). Projects must be
  created from the UI or `doctl` first; we don't manage them in Terraform.
- Local tooling: `terraform >= 1.5`, `ansible >= 2.16`, `doctl` (optional but
  handy for sanity checks).

## Step-by-step

### 1. Provision (Terraform Cloud)

State and runs live in Terraform Cloud, workspace `FlamaCorp/pfv`, VCS-driven
against this repo with the working directory and trigger paths both scoped to
`infra/terraform/`. Workflow:

1. Open a PR that touches `infra/terraform/**`. TFC posts a speculative plan
   on the run page.
2. Merge to `main`. TFC starts an apply run. Apply method is **manual confirm**
   on the TFC UI for now (flip to auto-apply after a few clean cycles).

The workspace expects two variables to be set ahead of time:

- `do_token` — sensitive, the DO API token (scoped per `infra/README.md`).
- `ssh_key_name` — plaintext, the name of an SSH key already registered in DO.

After the apply succeeds, fetch the outputs from TFC (Workspace -> Outputs)
or via the CLI once `terraform login` is configured locally:

```bash
terraform -chdir=infra/terraform output droplet_public_ipv4
terraform -chdir=infra/terraform output droplet_private_ipv4
terraform -chdir=infra/terraform output -raw vpc_id
```

Local-CLI runs against the same workspace work too; `terraform login` once,
then `terraform plan` reaches the remote state. `terraform.tfvars` is for
local runs only and stays gitignored. The `.terraform.lock.hcl` IS committed
so TFC and laptops resolve identical provider versions.

### 2. Configure (Ansible)

```bash
cd ../ansible
cp inventory.yml.example inventory.yml
$EDITOR inventory.yml
# Fill in:
#   ansible_host          = $(terraform -chdir=../terraform output -raw droplet_public_ipv4)
#   private_ipv4          = $(terraform -chdir=../terraform output -raw droplet_private_ipv4)
#   mysql_app_password    = <generated>
#   mysql_backup_password = <generated>
#   redis_password        = <generated>
#
# Note: we intentionally do NOT manage a password for root@localhost.
# Ubuntu MySQL ships with auth_socket on root, and we keep that — local
# maintenance is `sudo mysql`. Cron mysqldump uses the dedicated
# mysql_backup user via /root/.my.cnf.

ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/site.yml
```

Recommended: `ansible-vault encrypt_string` the three passwords or move them
into a vault-encrypted vars file. `inventory.yml` is gitignored to keep
plain-text creds out of the repo even by accident.

### 3. Wire App Platform

Update App Platform secrets (separate PR / runbook step):

```
DATABASE_URL=mysql+aiomysql://pfv_app:<password>@<droplet_private_ipv4>:3306/pfv2
REDIS_URL=redis://default:<password>@<droplet_private_ipv4>:6379/0
```

See `MIGRATION.md` for the full data-move runbook (including rollback).

## Day-2

- **Inspect droplet metrics**: DO control panel -> Droplets -> pfv-data-01 ->
  Graphs. CPU, memory, disk, and network are graphed for free.
- **Watch backups**: `ls -lh /var/backups/mysql/` on the droplet. Logs at
  `/var/log/mysql-backup.log`.
- **Apply OS updates**: unattended-upgrades runs daily; reboots are manual.
  `sudo apt update && sudo apt upgrade && sudo reboot` during a quiet window.
- **Rotate creds**: re-run the playbook with new vault values; restart
  services as the handlers fire.

## Teardown

```bash
cd infra/terraform
terraform destroy
```

Warning: this destroys the droplet and its data. Pull a final `mysqldump`
first (see `MIGRATION.md`) and verify weekly snapshots are also gone if you
intend a clean break.
