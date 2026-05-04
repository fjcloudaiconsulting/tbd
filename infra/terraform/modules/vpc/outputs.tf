output "id" {
  description = "VPC UUID."
  value       = digitalocean_vpc.this.id
}

output "ip_range" {
  description = "VPC CIDR (echoed from input)."
  value       = digitalocean_vpc.this.ip_range
}
