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
