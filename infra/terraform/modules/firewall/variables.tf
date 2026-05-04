variable "name" {
  description = "Firewall name."
  type        = string
}

variable "droplet_ids" {
  description = "Droplet IDs to attach the firewall to."
  type        = list(number)
}

variable "vpc_ip_range" {
  description = "VPC CIDR allowed for MySQL/Redis/ICMP inbound."
  type        = string
}
