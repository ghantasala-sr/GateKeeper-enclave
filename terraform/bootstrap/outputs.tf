output "project_id" {
  value = google_project.enclave.project_id
}

output "state_bucket" {
  description = "Use as the gcs backend bucket in ../envs/enclave."
  value       = google_storage_bucket.tfstate.name
}
