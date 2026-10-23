# Step B7: Artifact Registry, and one service account per job with only the roles
# that job needs. No service account keys exist anywhere: GKE nodes and the bastion
# use their attached accounts, the backend uses Workload Identity.

resource "google_artifact_registry_repository" "gatekeeper" {
  project       = var.project_id
  location      = var.region
  repository_id = "gatekeeper"
  format        = "DOCKER" # images, and the Helm chart as an OCI artifact
  description   = "Gatekeeper images and chart, loaded from the offline bundle"
}

resource "google_service_account" "this" {
  for_each = {
    gk-backend = "Gatekeeper backend pods (Workload Identity): Vertex AI only"
    gk-nodes   = "GKE nodes: pull images, write logs and metrics"
    gk-bastion = "Operator bastion: load the bundle, run kubectl/helm"
  }
  project      = var.project_id
  account_id   = each.key
  display_name = each.key
  description  = each.value
}

locals {
  project_roles = {
    gk-backend = ["roles/aiplatform.user"]
    gk-nodes = [
      "roles/logging.logWriter",
      "roles/monitoring.metricWriter",
      "roles/monitoring.viewer",
      "roles/stackdriver.resourceMetadata.writer",
    ]
    gk-bastion = [
      "roles/container.developer", # kubectl
      "roles/logging.viewer",      # read the firewall's DENIED logs (the proof)
      "roles/cloudsql.client",     # inspect the private instance
    ]
  }
  project_bindings = merge([
    for sa, roles in local.project_roles : { for r in roles : "${sa} ${r}" => { sa = sa, role = r } }
  ]...)
}

resource "google_project_iam_member" "this" {
  for_each = local.project_bindings
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.this[each.value.sa].email}"
}

# Registry access is granted on the repository, not the project.
resource "google_artifact_registry_repository_iam_member" "nodes_read" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.gatekeeper.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.this["gk-nodes"].email}"
}

resource "google_artifact_registry_repository_iam_member" "bastion_write" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.gatekeeper.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.this["gk-bastion"].email}"
}
