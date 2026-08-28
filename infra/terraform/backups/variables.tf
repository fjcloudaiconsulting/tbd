variable "aws_account_id" {
  # No default, deliberately, and copied from apex/variables.tf's reasoning:
  # this repo now spans TWO AWS accounts, so a wrong-account apply is a real
  # and easy mistake. main.tf additionally asserts the caller matches, so a
  # mismatch dies at plan rather than creating a bucket in the wrong place.
  description = "12-digit AWS account ID that owns the backup bucket, KMS key and IAM identities. Must be set explicitly in the TFC workspace. This is NOT the account infra/terraform/apex/ uses."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account ID."
  }
}

variable "aws_region" {
  description = "Region for the backup bucket and KMS key."
  type        = string
  default     = "eu-central-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for the nightly MySQL dumps."
  type        = string
  default     = "tbd-mysql-backups-884686184019"
}

variable "backup_prefix" {
  description = "Key prefix the droplet may write under. The uploader's IAM policy is scoped to this prefix and nothing else."
  type        = string
  default     = "pfv-data-01"
}

variable "retention_days" {
  # ⚠ Must exceed object_lock_days, or the lifecycle rule fights the lock and
  # expiration silently fails on every locked object.
  description = "Days after which a current backup object expires."
  type        = number
  default     = 8
}

variable "object_lock_days" {
  description = "Days a written object is immutable under Object Lock GOVERNANCE. Matches the operational 7-day retention the nightly cron assumes."
  type        = number
  default     = 7
}

variable "tfc_workspace_name" {
  # ⚠ THIS VALUE IS THE AWS TRUST BOUNDARY, and it is also the workspace name in
  # versions.tf. The two MUST agree: the value lands in the
  # `app.terraform.io:sub` condition on the provisioner role, which is managed
  # by the workspace it authorizes. Renaming the TFC workspace without widening
  # this first denies AssumeRoleWithWebIdentity and the workspace cannot apply
  # its own fix (TBD-372, 2026-08-11, recovered by an out-of-band edit).
  #
  # ⚠ HOW TO RENAME THIS WORKSPACE SAFELY. Widen by ADDING A SECOND STATEMENT to
  # tfc-backups-trust.json naming the new workspace, apply, rename the workspace,
  # update this value, apply again, then delete the old statement. Do NOT widen
  # by globbing the workspace segment (`tbd-backups*`): apex carries such a
  # wildcard only as scar tissue from the rename that caused TBD-372, a glob is
  # indistinguishable from a permanent widening once the rename is over, and
  # backend/tests/test_backup_offhost.py rejects one that matches the declared
  # name. The two-statement form is explicit, reviewable, and self-cleaning.
  # NEVER rename first.
  description = "TFC workspace name allowed to assume the provisioner role. Must equal the workspace in versions.tf -- fenced by backend/tests/test_backup_trust_anchor.py."
  type        = string
  default     = "tbd-backups"
}

variable "tfc_organization" {
  description = "Terraform Cloud organization whose workspace may assume the provisioner role."
  type        = string
  default     = "FlamaCorp"
}

variable "github_repo" {
  description = "GitHub repo (owner/name) whose scheduled workflow may assume the read-only freshness probe role."
  type        = string
  default     = "fjcloudaiconsulting/tbd"
}

variable "github_main_branch" {
  description = "Branch whose workflow runs may assume the probe role. Scheduled runs carry this ref. StringEquals, so a PR context cannot assume it even by editing the workflow."
  type        = string
  default     = "main"
}
