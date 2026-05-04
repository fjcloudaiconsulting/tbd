variable "name" {
  description = "Droplet name."
  type        = string
}

variable "region" {
  description = "DO region slug."
  type        = string
}

variable "size" {
  description = "Droplet size slug."
  type        = string
}

variable "image" {
  description = "Droplet base image slug."
  type        = string
}

variable "vpc_uuid" {
  description = "VPC UUID to attach the droplet to."
  type        = string
}

variable "ssh_key_id" {
  description = "DO SSH key fingerprint or numeric ID."
  type        = string
}

variable "enable_backups" {
  description = "Enable DO weekly backups (~20% droplet cost; cheap insurance for a single-droplet data tier)."
  type        = bool
  default     = true
}

variable "enable_monitoring" {
  description = "Enable DO droplet metrics agent (free)."
  type        = bool
  default     = true
}

variable "tags" {
  description = "DO tags to apply to the droplet."
  type        = list(string)
  default     = []
}
