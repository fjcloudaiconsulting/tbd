resource "digitalocean_droplet" "this" {
  name     = var.name
  region   = var.region
  size     = var.size
  image    = var.image
  vpc_uuid = var.vpc_uuid

  ssh_keys = [var.ssh_key_id]

  backups    = var.enable_backups
  monitoring = var.enable_monitoring
  ipv6       = false

  tags = var.tags
}
