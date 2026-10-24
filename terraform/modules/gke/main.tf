# Step B9: a private GKE cluster. Nodes have no external IPs, and the control plane
# has no public endpoint: only machines inside the VPC (the bastion) can reach it.

resource "google_container_cluster" "gk" {
  project    = var.project_id
  name       = "enclave-gke"
  location   = var.zone # zonal for Part B (covered by the GKE free tier); Part C goes regional
  network    = var.network_id
  subnetwork = var.subnet_id

  deletion_protection      = false # a lab
  remove_default_node_pool = true
  initial_node_count       = 1
  datapath_provider        = "ADVANCED_DATAPATH" # Dataplane V2: enforces NetworkPolicies

  release_channel {
    channel = "REGULAR"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true
    master_ipv4_cidr_block  = var.master_cidr
  }

  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = var.authorized_cidr
      display_name = "enclave-main"
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Gateway API (the gke-l7-rilb class: an internal Application Load Balancer).
  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  # Module C6: refuse any image without the release attestation (Binary Authorization).
  # Switched on only once the running release's images are attested (var.binauthz_enforce):
  # enforcing first would refuse every pod that restarts.
  binary_authorization {
    evaluation_mode = var.binauthz_enforce ? "PROJECT_SINGLETON_POLICY_ENFORCE" : "DISABLED"
  }

  # Module C5: Secret Manager secrets mounted into pods as files (a CSI driver), so the
  # app's secrets never exist as Kubernetes Secret objects.
  secret_manager_config {
    enabled = true
  }

  # Cloud Logging and Managed Service for Prometheus: Google APIs, reached through the
  # restricted range like everything else. Nothing is sent outside Google.
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = true
    }
  }

  # The default pool is removed right away; give it the same locked-down identity anyway.
  node_config {
    service_account = var.node_service_account
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

resource "google_container_node_pool" "main" {
  project    = var.project_id
  name       = "main"
  location   = var.zone
  cluster    = google_container_cluster.gk.name
  node_count = var.node_count # per zone
  # One zone normally; several for the HA drills (Module C1), so losing a zone
  # (or draining a node) leaves the others serving.
  node_locations = var.node_zones

  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    image_type      = "COS_CONTAINERD"
    service_account = var.node_service_account
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"] # access is decided by IAM roles
    workload_metadata_config {
      mode = "GKE_METADATA" # pods get Workload Identity, never the node's account
    }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

# Each Kubernetes service account may act as exactly one Google service account:
#   gatekeeper/gk-backend -> gk-backend (Vertex AI, and its own secrets)
#   gatekeeper/gk-setup   -> gk-setup   (the database owner's password, nothing else)
resource "google_service_account_iam_member" "workload_identity" {
  for_each           = var.workload_identities
  service_account_id = each.value
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${each.key}]"
  depends_on         = [google_container_cluster.gk] # the identity pool exists once the cluster does
}

# The backend's binding used to be its own resource: same binding, new address. Without
# this, Terraform would delete the binding after "creating" it again, cutting the
# backend off from Vertex AI.
moved {
  from = google_service_account_iam_member.backend_workload_identity
  to   = google_service_account_iam_member.workload_identity["gatekeeper/gk-backend"]
}
