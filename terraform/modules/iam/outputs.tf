output "registry" {
  description = "Image and chart prefix, e.g. us-central1-docker.pkg.dev/PROJECT/gatekeeper"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.gatekeeper.repository_id}"
}
output "backend_sa_email" { value = google_service_account.this["gk-backend"].email }
output "backend_sa_name" { value = google_service_account.this["gk-backend"].name }
output "nodes_sa_email" { value = google_service_account.this["gk-nodes"].email }
output "bastion_sa_email" { value = google_service_account.this["gk-bastion"].email }
