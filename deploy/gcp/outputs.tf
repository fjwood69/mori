output "vm_internal_ip" {
  description = "Internal IP of the mori-advisor VM"
  value       = google_compute_instance.mori.network_interface[0].network_ip
}

output "data_bucket" {
  description = "GCS bucket for SQLite data"
  value       = google_storage_bucket.mori_data.name
}

output "backup_bucket" {
  description = "GCS bucket for database backups"
  value       = google_storage_bucket.mori_backups.name
}

output "service_account" {
  description = "Service account email"
  value       = google_service_account.mori.email
}

output "ssh_command" {
  description = "SSH command to access the VM"
  value       = "gcloud compute ssh mori-advisor --zone ${var.zone}"
}