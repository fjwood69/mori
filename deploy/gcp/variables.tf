variable "project_id" {
  description = "GCP project ID"
  type        = string
  default     = "mori-genai"
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "northamerica-northeast2"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "northamerica-northeast2-a"
}

variable "machine_type" {
  description = "GCE machine type"
  type        = string
  default     = "e2-small"
}

variable "disk_size_gb" {
  description = "Persistent disk size in GB"
  type        = number
  default     = 20
}

variable "ssh_public_key" {
  description = "Public SSH key for VM access"
  type        = string
  sensitive   = true
}

variable "tailscale_auth_key" {
  description = "Tailscale pre-auth key for VM join"
  type        = string
  sensitive   = true
}

variable "container_image" {
  description = "Container image tag for mori-advisor"
  type        = string
  default     = "ghcr.io/fjwood69/mori:latest"
}

variable "ssh_user" {
  description = "Linux username for SSH key injection (used in GCE metadata)"
  type        = string
  default     = "mori"
}

variable "backup_retention_days" {
  description = "Days to retain database backups in standard storage before archiving"
  type        = number
  default     = 90
}

# ── Names (override only to match an existing/legacy deployment) ─────────────

variable "disk_name" {
  description = "Name of the persistent data disk. Override to match an existing disk so Terraform does not recreate it."
  type        = string
  default     = "mori-advisor-data"
}

variable "backup_bucket_name" {
  description = "Full GCS backup bucket name. Leave empty to use mori-advisor-backups-<project_id>; set to match an existing bucket."
  type        = string
  default     = ""
}

variable "extra_allowed_cidrs" {
  description = "Additional source CIDR ranges allowed to reach the mori port (beyond the Tailscale CGNAT range). E.g. a LAN subnet."
  type        = list(string)
  default     = []
}

# ── Tailscale ────────────────────────────────────────────────────────────

variable "tailscale_hostname" {
  description = "Hostname the VM registers with on the tailnet"
  type        = string
  default     = "mori-advisor"
}

# ── Custom startup script ───────────────────────────────────────────────────

variable "startup_template_path" {
  description = "Path to the startup script template. Leave empty to use the bundled generic deploy/gcp/startup.sh.tpl. Set to a custom script (e.g. one that fronts mori with an LLM gateway) to override."
  type        = string
  default     = ""
}

variable "network_ip" {
  description = "Static internal IP for the VM. Leave empty to let GCP auto-assign; set to match an existing instance."
  type        = string
  default     = ""
}
