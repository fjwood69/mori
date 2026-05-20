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
  default     = "localhost/mori-advisor:latest"
}

variable "backup_retention_days" {
  description = "Days to retain database backups in standard storage before archiving"
  type        = number
  default     = 90
}
