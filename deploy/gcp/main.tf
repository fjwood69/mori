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

resource "google_storage_bucket" "mori_data" {
  name          = "mori-advisor-data-${var.project_id}"
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

resource "google_storage_bucket" "mori_backups" {
  name          = "mori-advisor-backups-${var.project_id}"
  location      = var.region
  storage_class = "STANDARD"
  force_destroy = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = var.backup_retention_days
    }
    action {
      type          = "SetStorageClass"
      storage_class = "ARCHIVE"
    }
  }
}

# ── Secret Manager ───────────────────────────────────────────────────────

resource "google_secret_manager_secret" "mori_api_key" {
  secret_id = "MORI_API_KEY"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_advisor_api_key" {
  secret_id = "MORI_ADVISOR_API_KEY"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_base_url" {
  secret_id = "MORI_BASE_URL"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_model" {
  secret_id = "MORI_MODEL"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_dream_model" {
  secret_id = "MORI_DREAM_MODEL"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_trusted_dreamers" {
  secret_id = "MORI_TRUSTED_DREAMERS"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "mori_nats_url" {
  secret_id = "MORI_NATS_URL"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "tailscale_auth_key" {
  secret_id = "TAILSCALE_AUTH_KEY"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "ghcr_token" {
  secret_id = "GHCR_TOKEN"
  replication {
    auto {}
  }
}

# ── IAM — Service Account ────────────────────────────────────────────────

resource "google_service_account" "mori" {
  account_id   = "mori-advisor"
  display_name = "Mori Advisor Service Account"
}

# GCS access
resource "google_storage_bucket_iam_member" "mori_data_admin" {
  bucket = google_storage_bucket.mori_data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_storage_bucket_iam_member" "mori_backup_admin" {
  bucket = google_storage_bucket.mori_backups.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.mori.email}"
}

# Secret Manager access
resource "google_secret_manager_secret_iam_member" "mori_api_key" {
  secret_id = google_secret_manager_secret.mori_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_advisor_api_key" {
  secret_id = google_secret_manager_secret.mori_advisor_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_base_url" {
  secret_id = google_secret_manager_secret.mori_base_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_model" {
  secret_id = google_secret_manager_secret.mori_model.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_dream_model" {
  secret_id = google_secret_manager_secret.mori_dream_model.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_trusted_dreamers" {
  secret_id = google_secret_manager_secret.mori_trusted_dreamers.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "mori_nats_url" {
  secret_id = google_secret_manager_secret.mori_nats_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "tailscale_auth_key" {
  secret_id = google_secret_manager_secret.tailscale_auth_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

resource "google_secret_manager_secret_iam_member" "ghcr_token" {
  secret_id = google_secret_manager_secret.ghcr_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mori.email}"
}

# ── GCE VM ───────────────────────────────────────────────────────────────

locals {
  # Startup script: install Podman, pull image, run container
  startup_script = templatefile("${path.module}/startup.sh.tpl", {
    container_image    = var.container_image
    data_bucket        = google_storage_bucket.mori_data.name
    backup_bucket      = google_storage_bucket.mori_backups.name
    tailscale_auth_key = var.tailscale_auth_key
    project_id         = var.project_id
  })
}

resource "google_compute_disk" "mori_data" {
  name  = "mori-advisor-data"
  type  = "pd-standard"
  zone  = var.zone
  size  = var.disk_size_gb

  labels = {
    service = "mori-advisor"
  }
}
resource "google_compute_firewall" "mori_tailscale" {
  name    = "mori-advisor-tailscale"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8968"]
  }

  # Tailscale uses WireGuard (UDP 41641) and DERP relays (TCP 443/80)
  # The actual mori port only needs to be reachable from the tailnet
  source_ranges = ["100.64.0.0/10", "10.1.0.0/16"]
  target_tags   = ["mori-advisor"]
}

resource "google_compute_firewall" "mori_ssh" {
  name    = "mori-advisor-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Tailscale-only. Day 1 access via gcloud compute ssh or Tailscale SSH.
  source_ranges = ["100.64.0.0/10"]
  target_tags   = ["mori-advisor"]
}

# ── Cloud NAT (egress for no-external-IP VMs) ──────────────────────────────
# Required because the VM has no external IP but needs to reach:
#   - apt repositories (podman, sqlite3 install)
#   - GHCR (container image pull)
#   - Tailscale (install script)
#   - Provider API endpoints (Novita, etc.)

resource "google_compute_router" "mori" {
  name    = "mori-advisor-nat-router"
  network = "default"
  region  = var.region
}

resource "google_compute_router_nat" "mori" {
  name                               = "mori-advisor-nat"
  router                             = google_compute_router.mori.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_instance" "mori" {
  name         = "mori-advisor"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["mori-advisor"]

  boot_disk {
    auto_delete = true
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 10
      type  = "pd-standard"
    }
  }

  attached_disk {
    source      = google_compute_disk.mori_data.id
    device_name = "mori-data"
  }

  network_interface {
    network    = "default"
    network_ip = "10.188.0.16"
    # No external IP — all access via Tailscale
  }

  metadata = {
    ssh-keys              = "${var.ssh_user}:${var.ssh_public_key}"
    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
  }

  service_account {
    email  = google_service_account.mori.email
    scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
    ]
  }

  metadata_startup_script = local.startup_script

  allow_stopping_for_update = true

  labels = {
    service = "mori-advisor"
  }
}