# Cloud firewall sits in front of the droplet. We pair this with ufw on the
# droplet itself (defence in depth). MySQL/Redis are VPC-only here; SSH is open
# to the world but protected by key-only auth + fail2ban inside the droplet.
resource "digitalocean_firewall" "this" {
  name        = var.name
  droplet_ids = var.droplet_ids

  # Inbound: SSH from anywhere (key auth + fail2ban; tighten later if needed).
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # Inbound: MySQL — VPC only.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "3306"
    source_addresses = [var.vpc_ip_range]
  }

  # Inbound: Redis — VPC only.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "6379"
    source_addresses = [var.vpc_ip_range]
  }

  # Inbound: ICMP — VPC only (ping diagnostics from App Platform side).
  inbound_rule {
    protocol         = "icmp"
    source_addresses = [var.vpc_ip_range]
  }

  # Outbound: open. The droplet needs to reach apt mirrors, NTP, and DO metrics.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
