variable "name" {
  description = "VPC name."
  type        = string
}

variable "region" {
  description = "DO region slug."
  type        = string
}

variable "ip_range" {
  description = "CIDR for the VPC."
  type        = string
}
