# Step B4: deny all egress (and log every attempt), then allow only Google's
# restricted APIs and traffic inside the Enclave.
#
# GCP has a hidden "allow all egress" rule at priority 65535; the deny at 65000 wins.

resource "google_compute_firewall" "deny_all_egress" {
  project            = var.project_id
  name               = "deny-all-egress"
  network            = var.network
  direction          = "EGRESS"
  priority           = 65000
  destination_ranges = ["0.0.0.0/0"]
  deny {
    protocol = "all"
  }
  log_config {
    metadata = "INCLUDE_ALL_METADATA" # the DENIED log lines are the zero-egress proof
  }
}

resource "google_compute_firewall" "allow_restricted_apis" {
  project            = var.project_id
  name               = "allow-restricted-google-apis"
  network            = var.network
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["199.36.153.4/30"]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "allow_internal_egress" {
  project            = var.project_id
  name               = "allow-internal-egress"
  network            = var.network
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = var.internal_ranges
  allow {
    protocol = "all"
  }
}

# Operators reach the bastion only through Identity-Aware Proxy (SSH over IAP).
resource "google_compute_firewall" "allow_iap_ssh" {
  project       = var.project_id
  name          = "allow-iap-ssh"
  network       = var.network
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = ["35.235.240.0/20"] # IAP's TCP forwarding range
  target_tags   = ["bastion"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# The internal load balancer: its proxies (proxy-only subnet) and Google's health
# checkers must reach the pods behind it.
resource "google_compute_firewall" "allow_lb_to_backends" {
  project       = var.project_id
  name          = "allow-lb-to-backends"
  network       = var.network
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = concat([var.proxy_only_range], ["35.191.0.0/16", "130.211.0.0/22"])
  allow {
    protocol = "tcp"
  }
}
