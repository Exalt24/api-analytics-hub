output "droplet_ipv4" {
  description = "Public address to point the DNS record for HUB_SITE at."
  value       = digitalocean_droplet.hub.ipv4_address
}

output "backup_volume_name" {
  description = "Name of the block volume the backups are written to."
  value       = digitalocean_volume.backups.name
}
