terraform {
  required_version = ">= 1.6"

  # State + plan/apply runs live in Terraform Cloud (FlamaCorp/pfv-apex).
  # The workspace is VCS-driven against this repo via the HCP Terraform
  # GitHub App, with working directory scoped to infra/terraform/apex/ and
  # trigger pattern infra/terraform/apex/**. Speculative plans fire on PR;
  # merges to main create runs that wait for manual Confirm & Apply in the
  # TFC UI (auto-apply is intentionally off, matching the FlamaCorp/pfv
  # workspace policy for the data droplet).
  #
  # NOTE: a dedicated workspace (pfv-apex) keeps the AWS apex provisioning
  # isolated from the DigitalOcean data plane in FlamaCorp/pfv. Different
  # cloud, different blast radius, different state.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "pfv-apex"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
