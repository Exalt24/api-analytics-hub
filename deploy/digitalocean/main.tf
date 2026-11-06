resource "digitalocean_volume" "backups" {
  name                    = "${var.name}-backups"
  region                  = var.region
  size                    = var.data_volume_gb
  initial_filesystem_type = "ext4"
  description             = "Postgres dumps from scripts/db_backup.py, kept off the droplet's own disk"
}

resource "digitalocean_droplet" "hub" {
  name       = var.name
  region     = var.region
  size       = var.size
  image      = "ubuntu-24-04-x64"
  ssh_keys   = var.ssh_key_fingerprints
  monitoring = true # installs the DigitalOcean agent so the alerts below have data
  backups    = true # weekly whole-droplet snapshots, on top of the database dumps on the volume
  ipv6       = true

  user_data = templatefile("${path.module}/cloud-init.yaml", {
    hostname = var.name
    # DigitalOcean mounts a formatted volume at /mnt/<volume name> with every hyphen turned into an underscore.
    backup_mount = "/mnt/${replace("${var.name}-backups", "-", "_")}"
  })
}

resource "digitalocean_volume_attachment" "backups" {
  droplet_id = digitalocean_droplet.hub.id
  volume_id  = digitalocean_volume.backups.id
}

# Only the web ports are open to the world. SSH is open to the admin ranges and nothing else, and Postgres and the API
# are not listed at all, so they are unreachable from outside the droplet.
resource "digitalocean_firewall" "hub" {
  name        = "${var.name}-fw"
  droplet_ids = [digitalocean_droplet.hub.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_cidrs
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

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

resource "digitalocean_monitor_alert" "cpu" {
  description = "${var.name}: CPU above 80 percent for 5 minutes"
  type        = "v1/insights/droplet/cpu"
  compare     = "GreaterThan"
  value       = 80
  window      = "5m"
  enabled     = true
  entities    = [digitalocean_droplet.hub.id]

  alerts {
    email = var.alert_emails
  }
}

resource "digitalocean_monitor_alert" "memory" {
  description = "${var.name}: memory above 90 percent for 5 minutes"
  type        = "v1/insights/droplet/memory_utilization_percent"
  compare     = "GreaterThan"
  value       = 90
  window      = "5m"
  enabled     = true
  entities    = [digitalocean_droplet.hub.id]

  alerts {
    email = var.alert_emails
  }
}

resource "digitalocean_monitor_alert" "disk" {
  description = "${var.name}: disk above 85 percent for 5 minutes"
  type        = "v1/insights/droplet/disk_utilization_percent"
  compare     = "GreaterThan"
  value       = 85
  window      = "5m"
  enabled     = true
  entities    = [digitalocean_droplet.hub.id]

  alerts {
    email = var.alert_emails
  }
}
