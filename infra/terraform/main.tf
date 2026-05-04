terraform {
  required_version = ">= 1.5.0"

  # State + plan/apply runs live in Terraform Cloud (FlamaCorp/pfv).
  # The workspace is VCS-driven against this repo with the working directory
  # scoped to infra/terraform/ and trigger paths the same. Set workspace
  # variables `do_token` (sensitive) and `ssh_key_name` in TFC; everything
  # else has sensible defaults in variables.tf.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "pfv"
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

  # backups=true is intentionally default-on: this droplet is the only copy of
  # production data outside the nightly mysqldump cron, so weekly DO snapshots
  # are cheap insurance.
  enable_backups    = true
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
