output "id" {
  description = "Numeric droplet ID."
  value       = digitalocean_droplet.this.id
}

output "urn" {
  description = "DO URN (used to attach the droplet to a project)."
  value       = digitalocean_droplet.this.urn
}

output "public_ipv4" {
  description = "Public IPv4 of the droplet."
  value       = digitalocean_droplet.this.ipv4_address
}

output "private_ipv4" {
  description = "Private (VPC) IPv4 of the droplet."
  value       = digitalocean_droplet.this.ipv4_address_private
}
