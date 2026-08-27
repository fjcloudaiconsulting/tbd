output "backup_s3_bucket" {
  description = "Bucket the droplet uploads nightly dumps to."
  value       = aws_s3_bucket.backups.id
}

output "backup_s3_prefix" {
  description = "Key prefix the uploader is scoped to."
  value       = var.backup_prefix
}

output "backup_s3_kms_key_arn" {
  description = "CMK the droplet must name explicitly on every PUT. The uploader's policy conditions on it, so an absent or different key id is a 403."
  value       = aws_kms_key.backups.arn
}

output "backup_s3_region" {
  description = "Region for the droplet's AWS CLI configuration."
  value       = var.aws_region
}

# ⚠ These two are consumed by infra/ansible/bin/run-playbook.sh, which merges
# them into the same mode-0600 temp file as the data-plane credentials. They
# come to rest in exactly two places: TFC state and mode-0600 on the droplet.
# Never the repo, never inventory.yml, never an argv element.
output "backup_s3_access_key_id" {
  description = "Access key id for pfv-backup-uploader."
  value       = aws_iam_access_key.uploader.id
  sensitive   = true
}

output "backup_s3_secret_access_key" {
  description = "Secret access key for pfv-backup-uploader."
  value       = aws_iam_access_key.uploader.secret
  sensitive   = true
}

output "backup_probe_role_arn" {
  description = "Role the scheduled freshness workflow assumes via GitHub OIDC."
  value       = aws_iam_role.backup_probe.arn
}

output "tfc_provisioner_role_arn" {
  description = "Set as TFC_AWS_RUN_ROLE_ARN on the tbd-backups workspace."
  value       = aws_iam_role.tfc_backups_provisioner.arn
}

output "tfc_plan_role_arn" {
  description = "Set as TFC_AWS_PLAN_ROLE_ARN on the tbd-backups workspace. Read-only, and explicitly denied any path to backup content -- speculative plans run on unapproved PRs."
  value       = aws_iam_role.tfc_backups_plan.arn
}
