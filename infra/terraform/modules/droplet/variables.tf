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

# TBD-399: DO takes backups on ITS OWN schedule -- weekly by default -- so
# enabling `backups` creates nothing immediately and may not yield a restorable
# image for days. `backup_policy` makes the timing knowable instead of assumed,
# which is what a migration window needs. Null keeps the provider default.
variable "backup_policy" {
  description = <<-EOT
    Optional DO backup schedule. plan: daily|weekly. Null omits the block entirely
    (provider default: weekly).

    WARNING: when a policy IS supplied, `hour` is NOT optional in practice even
    though it is declared optional. A null attribute inside an SDKv2 nested block
    decodes to the type's ZERO VALUE, not absent, and the provider then sends
    hour=0 on the wire. So leaving it unset silently pins backups to 00:00 UTC.
    Set it deliberately. DO accepts 0, 4, 8, 12, 16, 20.
  EOT
  type = object({
    plan    = string
    weekday = optional(string)
    hour    = optional(number)
  })
  default = null

  validation {
    condition     = var.backup_policy == null || contains(["daily", "weekly"], try(var.backup_policy.plan, ""))
    error_message = "backup_policy.plan must be \"daily\" or \"weekly\"."
  }
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
