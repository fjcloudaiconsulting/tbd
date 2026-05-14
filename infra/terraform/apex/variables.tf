variable "aws_account_id" {
  description = "12-digit AWS account ID that owns the apex bucket, CloudFront distribution, ACM cert, and IAM roles. No default; must be set explicitly in the TFC workspace to prevent cross-account accidents."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "aws_region" {
  description = "AWS region for the S3 bucket and the home of the default provider. CloudFront is global; ACM for CloudFront is pinned to us-east-1 in providers.tf regardless of this value."
  type        = string
  default     = "eu-west-1"
}

variable "domain" {
  description = "Apex domain to serve. Both apex and www.<apex> are added as CloudFront aliases and ACM SANs."
  type        = string
  default     = "thebetterdecision.com"
}

variable "github_repo" {
  description = "GitHub repo (owner/name) allowed to assume the GitHub Actions deploy role via OIDC."
  type        = string
  default     = "flamarion/pfv"
}

variable "github_main_branch" {
  description = "Branch on github_repo whose workflow runs are allowed to assume the deploy role. The OIDC trust policy uses StringEquals on the sub claim, so only this exact branch ref can deploy. PR contexts are rejected at the trust level (not just by workflow-level guards), since PR authors could otherwise edit the workflow to bypass guards."
  type        = string
  default     = "main"
}

variable "tfc_organization" {
  description = "Terraform Cloud organization whose workspaces are allowed to assume the apex provisioner role via OIDC workload identity."
  type        = string
  default     = "FlamaCorp"
}

variable "tfc_workspace_pattern" {
  description = "TFC workspace name pattern (supports glob via wildcard suffix on the OIDC sub claim) allowed to assume the apex provisioner role. Default pfv-apex* covers the apex workspace plus any future split (e.g. pfv-apex-staging)."
  type        = string
  default     = "pfv-apex*"
}

variable "noncurrent_version_expiration_days" {
  description = "Days after which noncurrent S3 object versions expire. Versioning stays on for rollback; this caps storage growth."
  type        = number
  default     = 90
}
