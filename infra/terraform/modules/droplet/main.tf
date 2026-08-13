resource "digitalocean_droplet" "this" {
  name     = var.name
  region   = var.region
  size     = var.size
  image    = var.image
  vpc_uuid = var.vpc_uuid

  ssh_keys = [var.ssh_key_id]

  backups    = var.enable_backups
  monitoring = var.enable_monitoring

  # Only emitted when a policy is supplied AND backups are on -- DO rejects a
  # policy on a droplet with backups disabled.
  dynamic "backup_policy" {
    for_each = var.enable_backups && var.backup_policy != null ? [var.backup_policy] : []
    content {
      plan    = backup_policy.value.plan
      weekday = try(backup_policy.value.weekday, null)
      hour    = try(backup_policy.value.hour, null)
    }
  }
  ipv6 = false

  tags = var.tags
}
