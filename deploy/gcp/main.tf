terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── GCS buckets ──────────────────────────────────────────────────────────

resource "google_storage_bucket" "moku_data" {
  name          = "moku-advisor-data-${var.project_id}"
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }
}

resource "google_storage_bucket" "moku_backups" {
  name          = "moku-advisor-backups-${var.project_id}"
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition { age = var.backup_retention_days }
    action { type = "SetStorageClass" storage_class = "ARCHIVE" }
  }
}

# ── Secret Manager ───────────────────────────────────────────────────────

resource "google_secret_manager_secret" "moku_api_key" {
  secret_id = "MOKU_API_KEY"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_advisor_api_key" {
  secret_id = "MOKU_ADVISOR_API_KEY"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_base_url" {
  secret_id = "MOKU_BASE_URL"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_model" {
  secret_id = "MOKU_MODEL"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_dream_model" {
  secret_id = "MOKU_DREAM_MODEL"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_trusted_dreamers" {
  secret_id = "MOKU_TRUSTED_DREAMERS"
  replication { auto {} }
}

resource "google_secret_manager_secret" "moku_nats_url" {
  secret_id = "MOKU_NATS_URL"
  replication { auto {} }
}

resource "google_secret_manager_secret" "tailscale_auth_key" {
  secret_id = "TAILSCALE_AUTH_KEY"
  replication { auto {} }
}

# ── IAM — Service Account ────────────────────────────────────────────────

resource "google_service_account" "moku" {
  account_id   = "moku-advisor"
  display_name = "Moku Advisor Service Account"
}

# GCS access
resource "google_storage_bucket_iam_member" "moku_data_admin" {
  bucket = google_storage_bucket.moku_data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_storage_bucket_iam_member" "moku_backup_admin" {
  bucket = google_storage_bucket.moku_backups.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.moku.email}"
}

# Secret Manager access
resource "google_secret_manager_secret_iam_member" "moku_api_key" {
  secret_id = google_secret_manager_secret.moku_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_advisor_api_key" {
  secret_id = google_secret_manager_secret.moku_advisor_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_base_url" {
  secret_id = google_secret_manager_secret.moku_base_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_model" {
  secret_id = google_secret_manager_secret.moku_model.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_dream_model" {
  secret_id = google_secret_manager_secret.moku_dream_model.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_trusted_dreamers" {
  secret_id = google_secret_manager_secret.moku_trusted_dreamers.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "moku_nats_url" {
  secret_id = google_secret_manager_secret.moku_nats_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

resource "google_secret_manager_secret_iam_member" "tailscale_auth_key" {
  secret_id = google_secret_manager_secret.tailscale_auth_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.moku.email}"
}

# ── GCE VM ───────────────────────────────────────────────────────────────

locals {
  # Startup script: install Podman, pull image, run container
  startup_script = templatefile("${path.module}/startup.sh.tpl", {
    container_image    = var.container_image
    data_bucket        = google_storage_bucket.moku_data.name
    backup_bucket      = google_storage_bucket.moku_backups.name
    tailscale_auth_key = var.tailscale_auth_key
    project_id         = var.project_id
  })
}

resource "google_compute_disk" "moku_data" {
  name  = "moku-advisor-data"
  type  = "pd-standard"
  zone  = var.zone
  size  = var.disk_size_gb

  labels = {
    service = "moku-advisor"
  }
}
resource "google_compute_firewall" "moku_tailscale" {
  name    = "moku-advisor-tailscale"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8968"]
  }

  # Tailscale uses WireGuard (UDP 41641) and DERP relays (TCP 443/80)
  # The actual moku port only needs to be reachable from the tailnet
  source_ranges = ["100.64.0.0/10", "10.1.0.0/16"]
  target_tags   = ["moku-advisor"]
}

resource "google_compute_firewall" "moku_ssh" {
  name    = "moku-advisor-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Tailscale-only. Day 1 access via gcloud compute ssh or Tailscale SSH.
  source_ranges = ["100.64.0.0/10"]
  target_tags   = ["moku-advisor"]
}

resource "google_compute_instance" "moku" {
  name         = "moku-advisor"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["moku-advisor"]

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 10
      type  = "pd-standard"
    }
  }

  attached_disk {
    source      = google_compute_disk.moku_data.id
    device_name = "moku-data"
  }

  network_interface {
    network = "default"
    # No external IP — all access via Tailscale
  }

  metadata = {
    ssh-keys              = "nucadmin:${var.ssh_public_key}"
    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
  }

  service_account {
    email  = google_service_account.moku.email
    scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  metadata_startup_script = local.startup_script

  allow_stopping_for_update = true

  labels = {
    service = "moku-advisor"
  }
}