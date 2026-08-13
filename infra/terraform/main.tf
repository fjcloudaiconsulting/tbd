terraform {
  required_version = ">= 1.5.0"

  # State + plan/apply runs live in Terraform Cloud (FlamaCorp/tbd).
  # The workspace is VCS-driven against this repo via the HCP Terraform
  # GitHub App, with working directory scoped to infra/terraform/ and
  # trigger pattern infra/terraform/** . Speculative plans fire on PR;
  # merges to main create runs that wait for manual Confirm & Apply in
  # the TFC UI (auto-apply is intentionally off). Set workspace variables
  # `do_token` (sensitive) and `ssh_key_name` in TFC; everything else
  # has sensible defaults in variables.tf.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "tbd"
    }
  }

  required_providers {
    digitalocean = {
      source = "digitalocean/digitalocean"
      # Pinned to 2.40+ to ensure project_resources, vpc, droplet backups,
      # and monitoring fields are all stable. Bump deliberately, not accidentally.
      version = "~> 2.40"
    }
  }
}

provider "digitalocean" {
  token = var.do_token
}

# Look up the SSH key already configured in DO by name. We never manage SSH keys
# from Terraform here; treating them as data keeps human-rotated keys out of state.
data "digitalocean_ssh_key" "primary" {
  name = var.ssh_key_name
}

# Look up an existing project to attach resources to. Projects are an org-level
# concept; we don't want Terraform owning them.
data "digitalocean_project" "pfv" {
  name = var.project_name
}

module "vpc" {
  source   = "./modules/vpc"
  name     = "${var.project_name}-vpc"
  region   = var.region
  ip_range = var.vpc_ip_range
}

module "data_droplet" {
  source = "./modules/droplet"

  name     = "${var.project_name}-data-01"
  region   = var.region
  size     = var.droplet_size
  image    = var.droplet_image
  vpc_uuid = module.vpc.id

  ssh_key_id = data.digitalocean_ssh_key.primary.id

  # ⚠⚠ TEMPORARY (TBD-399). Backups are ON only for the TBD-360 MySQL 8.0 -> 8.4
  # migration window. REVERT TO false ONCE THE CUTOVER IS VERIFIED.
  #
  # The standing decision is backups=false: the nightly mysqldump cron (see
  # ansible/roles/backups) plus the App Platform release pin cover the recovery
  # scenarios we care about for a single-user finance app, and DO backups cost
  # ~20% of droplet cost (daily, below, costs more than weekly). That decision
  # is unchanged -- this is a window, not a reversal. Toggling backups on/off
  # does not affect the size variable above, and the DO provider applies it
  # in place: no droplet recreation, no IP change.
  #
  # WHY THIS IS NOT MERELY "turn backups on": enabling the flag creates NOTHING.
  # DO takes backups on its own schedule, weekly by default, so a droplet
  # enabled shortly before a cutover can reach the window with zero restorable
  # images while looking configured. `plan = "daily"` bounds that wait to ~24h
  # so the window can actually be scheduled.
  #
  # ⚠ THE GATE IS OUTPUT, NOT THIS APPLY:
  #     doctl compute droplet backups <droplet-id>   # must return >= 1 row
  # Do not start the cutover on the strength of this file alone.
  #
  # ⚠ This is the SECOND net. The load-bearing rollback artifact for the window
  # is the manual cold snapshot in infra/MYSQL-84-CUTOVER.md step 3 -- taken
  # deliberately, immediately pre-cutover, guaranteed to exist. Note the two
  # restore with DIFFERENT verbs: a backup with `droplet-action restore`, a
  # snapshot with `droplet-action rebuild`.
  enable_backups = true
  backup_policy = {
    plan = "daily"
  }
  enable_monitoring = true

  tags = ["pfv", "data", "managed-by-terraform"]
}

# Attach the droplet to the existing DO project for cost/visibility grouping.
resource "digitalocean_project_resources" "pfv" {
  project   = data.digitalocean_project.pfv.id
  resources = [module.data_droplet.urn]
}

module "firewall" {
  source = "./modules/firewall"

  name         = "${var.project_name}-data-fw"
  droplet_ids  = [module.data_droplet.id]
  vpc_ip_range = var.vpc_ip_range
}
