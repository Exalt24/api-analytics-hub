variable "name" {
  description = "Name for the droplet, volume and firewall."
  type        = string
  default     = "hub"
}

variable "region" {
  description = "DigitalOcean region slug."
  type        = string
  default     = "nyc3"
}

variable "size" {
  description = "Droplet size slug. Two vCPUs and 4 GB hold the API, the dashboard, Postgres and Caddy with room for a restore test."
  type        = string
  default     = "s-2vcpu-4gb"
}

variable "ssh_key_fingerprints" {
  description = "Fingerprints of SSH keys already uploaded to the account. Password login is off, so a droplet with none is unreachable and the plan is rejected."
  type        = list(string)

  validation {
    condition     = length(var.ssh_key_fingerprints) > 0
    error_message = "Give at least one SSH key fingerprint: the droplet has no password login."
  }
}

variable "admin_cidrs" {
  description = "Source ranges allowed to reach SSH. Never 0.0.0.0/0: the rule is rejected."
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0 && !contains(var.admin_cidrs, "0.0.0.0/0") && !contains(var.admin_cidrs, "::/0")
    error_message = "admin_cidrs must name real ranges and must not open SSH to the whole internet."
  }
}

variable "alert_emails" {
  description = "Addresses that get the droplet monitoring alerts (CPU, memory, disk)."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "Give at least one address, or the alerts go nowhere."
  }
}

variable "data_volume_gb" {
  description = "Size of the block volume mounted at /var/backups/hub, so the backups live off the droplet's own disk."
  type        = number
  default     = 50
}
