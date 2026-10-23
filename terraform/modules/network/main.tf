# Step B3: a VPC with no way to the internet.
#
# GCP gives every new network a 0.0.0.0/0 route; this one is created without it.
# The only route out goes to Google's restricted API range, which serves only
# Google APIs (Vertex AI, Artifact Registry, ...), over Google's own network.

resource "google_compute_network" "vpc" {
  project                         = var.project_id
  name                            = "enclave-vpc"
  auto_create_subnetworks         = false
  delete_default_routes_on_create = true # no internet route, ever
  routing_mode                    = "REGIONAL"
}

resource "google_compute_subnetwork" "main" {
  project                  = var.project_id
  name                     = "enclave-main"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = var.ranges.main
  private_ip_google_access = true # reach Google APIs without external IPs

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.ranges.pods
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.ranges.services
  }

  # Every flow is logged: part of the evidence that nothing leaves.
  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# GKE's internal Application Load Balancer (the Gateway) runs its Envoy proxies here.
resource "google_compute_subnetwork" "proxy_only" {
  project       = var.project_id
  name          = "enclave-proxy-only"
  network       = google_compute_network.vpc.id
  region        = var.region
  ip_cidr_range = var.ranges.proxy_only
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

# The ONLY route out. The next hop is called "default-internet-gateway", but traffic
# to this range never leaves Google's network, and nothing else is routable.
resource "google_compute_route" "restricted_google_apis" {
  project          = var.project_id
  name             = "restricted-google-apis"
  network          = google_compute_network.vpc.id
  dest_range       = "199.36.153.4/30"
  next_hop_gateway = "default-internet-gateway"
}

# Step B8 (network half): Private Services Access, so Cloud SQL gets a private IP
# from a range we reserve, and no public IP at all.
resource "google_compute_global_address" "private_services" {
  project       = var.project_id
  name          = "enclave-private-services"
  network       = google_compute_network.vpc.id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = split("/", var.ranges.private_services)[0]
  prefix_length = tonumber(split("/", var.ranges.private_services)[1])
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

# A fixed internal IP for the Gateway, so DNS and the NetworkPolicies can name it.
resource "google_compute_address" "gateway" {
  project      = var.project_id
  name         = "enclave-gateway"
  region       = var.region
  subnetwork   = google_compute_subnetwork.main.id
  address_type = "INTERNAL"
  address      = var.gateway_ip
  purpose      = "SHARED_LOADBALANCER_VIP"
}
