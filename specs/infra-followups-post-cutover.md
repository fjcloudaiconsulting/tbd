---
name: Infra Follow-ups (post-cutover) — PARTIAL
description: App Platform droplet upgrade to 2GB SHIPPED via PR #143. Ansible CI + DO dynamic inventory and Terraform-generated data-plane passwords still PENDING. Both also tracked in the project_roadmap.md Tech Debt section to avoid double-counting.
type: project
originSessionId: 2a9f7d60-8bf7-49a0-873b-232cfcd5e16a
---
After PR #116 (self-hosted MySQL + Redis droplet) ships and the managed → droplet cutover completes, three follow-up PRs are queued in this order. Architect-endorsed sequencing 2026-05-04.

**Why:** The cutover window is the worst possible time to introduce new secret-management or CI mechanisms. Land on the known-good `EV[...]`-via-`doctl` + manual Ansible flow first; iterate after.

**How to apply:** Don't bundle these into PR #116 or its cutover. Open them as separate PRs after the droplet is in production and stable for a few days.

---

## Follow-up A: Ansible CI + DO dynamic inventory

Replace the manual `ansible-playbook` step and the static `inventory.yml` with:

- **Dynamic inventory plugin: `digitalocean.cloud.droplets`** (DO's official collection — NOT `community.digitalocean.digitalocean`, which is the older community fork). Filter by Terraform tag `data` via `api_filters.tag_name`; use `compose` to set `ansible_host` from `networks.v4` private IP.
- `.github/workflows/ansible-config.yml` triggers on `infra/ansible/**` push to `main`, runs `ansible-playbook -i inventory/do.yml playbooks/site.yml`.
- Vault-encrypt `mysql_app_password`, `mysql_backup_password`, `redis_password`; move from inventory vars to `group_vars/all/vault.yml`.
- New GH Secrets: `DO_API_TOKEN_READ` (read-only droplets scope, separate from the TFC token), `ANSIBLE_VAULT_PASSWORD`, `ANSIBLE_SSH_KEY` (private key matching `fjorge-home` or a dedicated CI key).
- Delete `inventory.yml.example`; update `infra/README.md` Step 2 to reflect dynamic inventory.

Auto-committing a generated inventory is the wrong default — creates CI loops and noisy history. Dynamic inventory removes the drift problem entirely.

## Follow-up B: Terraform-generated data-plane passwords

`random_password` for MySQL app user, MySQL backup user, and Redis. Outputs marked `sensitive = true`. Ansible (running in CI from follow-up A) reads them via `terraform output -raw` and configures MySQL/Redis accordingly.

**Scope strictly limited to the data plane.** Do NOT touch App Platform env vars in this PR.

**Caveat (architect-flagged):** This is *not* secretless. Terraform docs are explicit: sensitive values still live in state, and `terraform output -raw` prints sensitive outputs. TFC state is encrypted at rest and access is RBAC-gated, but workspace permissions matter. Discipline: every output `sensitive = true`, no read-only collaborators on the workspace who shouldn't see the passwords.

**Bootstrap order**: Terraform creates the droplet + `random_password` resources first; Ansible CI runs second and pulls passwords from TFC outputs.

## Follow-up C: App Platform → single droplet migration (Option A)

After follow-ups A and B settle, migrate App Platform (backend + frontend services + migrate PRE_DEPLOY job) onto a single droplet alongside MySQL + Redis. Architect-endorsed 2026-05-04, with explicit preference for **Option A (one 2GB droplet for everything)** over Option B (separate app + data droplets).

**Why Option A over B for this project's size:**
- Simpler than two 1GB droplets; avoids private networking between app and data boxes.
- Gives MySQL more breathing room (1GB is tight, 2GB has headroom).
- $12/mo for one `s-1vcpu-2gb` vs $13.20/mo for two `s-1vcpu-1gb` — within rounding error.
- If the box runs hot, vertical scale to `s-2vcpu-4gb` ($24/mo) is still cheaper than App Platform.
- If separation later becomes necessary, split data back out then.

**Why this triggered (cost math, 2026-05-04):**
- Current: $30/mo managed DB+Redis + ~$10-15/mo App Platform = **~$40-45/mo**
- After PR #116 only: $7.20 data droplet + ~$10-15 App Platform = **~$17-22/mo** (saves ~$23/mo)
- After Follow-up C (Option A): one $12/mo droplet for everything = **~$12/mo** (saves ~$28/mo total, $336/yr)

**What this PR has to build (operational surface that App Platform was giving for free):**
- TLS termination + cert auto-renewal (Caddy, or nginx + certbot)
- Reverse proxy / routing (`/api` → backend, `/` → frontend)
- Process supervision (systemd units, or Docker Compose with `restart: always`)
- GH Actions workflow that builds backend + frontend container images, pushes to GHCR, SSHes to droplet, `docker compose pull && docker compose up -d`
- Secret delivery: GH Secrets → SSH → `.env` file on droplet, OR continue using DO Secrets / external store. NOT `EV[...]` since App Platform is gone.
- Health checks (existing `/api/v1/health` + `/api/v1/ready` endpoints work; just need the proxy to use them)
- Backup story: nightly mysqldump (already done via Ansible), plus app-side log rotation
- Rollback: keep last N image tags in GHCR, `docker compose up` with previous tag

**Trigger conditions before starting:**
- PR #116 cutover stable for 3-4 days
- Data droplet CPU/RAM/disk metrics observed; confirms 2GB is enough headroom for MySQL + Redis + app
- No outstanding incidents from the data cutover

If the data droplet is already at >70% RAM with just MySQL+Redis, abort Option A and reconsider Option B (separate droplets) instead.

## Follow-up D (CONDITIONAL, only if Follow-up C is NOT taken): App Platform spec ownership

Only relevant if we decide to *stay* on App Platform long-term. Would replace the current `.do/app.yaml` + `doctl apps update --spec` flow with `digitalocean_app` resource end-to-end. Architect's framing: don't half-own it.

If Follow-up C ships (move off App Platform), this whole question is moot — `.do/app.yaml`, the deploy GH Action, and the `EV[...]` re-encryption dance all go away.

---

## What stays out of scope even with all four follow-ups

External-issued secrets — `MAILGUN_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — are issued by their respective platforms. Terraform can't generate them. They'll always need an external source (DO Secrets, 1Password CLI, GH Secrets, manual `EV[...]` blobs while still on App Platform). Pattern stays: Terraform-generated for things it can mint; external for everything else.
