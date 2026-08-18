output "droplet_public_ipv4" {
  description = "Public IPv4 of the data droplet (for SSH bootstrap)."
  value       = module.data_droplet.public_ipv4
}

output "droplet_private_ipv4" {
  description = "Private IPv4 of the data droplet (for App Platform DATABASE_URL/REDIS_URL)."
  value       = module.data_droplet.private_ipv4
}

output "droplet_id" {
  description = "Numeric ID of the data droplet."
  value       = module.data_droplet.id
}

output "vpc_id" {
  description = "UUID of the VPC."
  value       = module.vpc.id
}

output "vpc_ip_range" {
  description = "CIDR of the VPC (echoed for convenience in Ansible inventory generation)."
  value       = module.vpc.ip_range
}

# TBD-207 — consumed by infra/ansible/bin/run-playbook.sh, never written to disk.
# `terraform output -json` DOES include sensitive values (plain `terraform
# output` redacts them), which is what makes this readable by the runner.
output "mysql_app_password" {
  description = "Generated password for the MySQL application user."
  value       = random_password.mysql_app.result
  sensitive   = true
}

output "mysql_backup_password" {
  description = "Generated password for the MySQL backup user."
  value       = random_password.mysql_backup.result
  sensitive   = true
}

output "redis_password" {
  description = "Generated password for Valkey/Redis."
  value       = random_password.redis.result
  sensitive   = true
}
