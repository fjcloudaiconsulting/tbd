variable "do_token" {
  description = "DigitalOcean API token (set via TF_VAR_do_token env var or terraform.tfvars)"
  type        = string
  sensitive   = true
}

variable "region" {
  description = "DigitalOcean region slug. Match your App Platform region (ams for Europe)."
  type        = string
  default     = "ams3"
}

variable "droplet_size" {
  description = "Droplet size slug. s-1vcpu-1gb is the $6/mo entry tier and is enough for MySQL+Redis on a personal-finance-app workload."
  type        = string
  default     = "s-1vcpu-1gb"
}

variable "droplet_image" {
  description = "Droplet base image slug."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_key_name" {
  description = "Name of an SSH key already registered in DO (Account -> Settings -> Security)."
  type        = string
}

variable "project_name" {
  description = "DO project name (must already exist) and resource name prefix."
  type        = string
  default     = "pfv"
}

variable "vpc_ip_range" {
  description = "CIDR for the VPC. /24 is plenty for one droplet plus the App Platform attachment."
  type        = string
  default     = "10.42.0.0/24"
}
