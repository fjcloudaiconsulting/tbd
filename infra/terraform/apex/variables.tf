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
  description = "Branch on github_repo whose workflow runs can assume the deploy role for actual deploys (s3 sync + invalidation). PRs targeting this branch can also assume the role for plan-only operations via the github_pr_subject_pattern."
  type        = string
  default     = "main"
}

variable "github_pr_subject_pattern" {
  description = "OIDC subject pattern that pull request workflows on github_repo match. Used to allow plan-only access from PRs while keeping deploy-mutating actions scoped to main pushes. Empty string disables PR access entirely."
  type        = string
  default     = "pull_request"
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
