# Step B5: private DNS, visible only inside the VPC.
#
# Without it, Google API names resolve to public IPs that the firewall blocks.
# These zones point them at the restricted range instead. The app zone gives the
# public hostnames (as typed in the browser) to the Gateway's internal VIP, so the
# backend checks exactly the issuer URL the browser sees.

locals {
  restricted_ips = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
  google_zones = {
    googleapis = { dns = "googleapis.com.", apex = "restricted.googleapis.com." }
    pkg-dev    = { dns = "pkg.dev.", apex = "pkg.dev." }
    gcr-io     = { dns = "gcr.io.", apex = "gcr.io." }
  }
}

resource "google_dns_managed_zone" "google" {
  for_each   = local.google_zones
  project    = var.project_id
  name       = "private-${each.key}"
  dns_name   = each.value.dns
  visibility = "private"
  private_visibility_config {
    networks {
      network_url = var.network_id
    }
  }
}

resource "google_dns_record_set" "google_a" {
  for_each     = local.google_zones
  project      = var.project_id
  managed_zone = google_dns_managed_zone.google[each.key].name
  name         = each.value.apex
  type         = "A"
  ttl          = 300
  rrdatas      = local.restricted_ips
}

resource "google_dns_record_set" "google_wildcard" {
  for_each     = local.google_zones
  project      = var.project_id
  managed_zone = google_dns_managed_zone.google[each.key].name
  name         = "*.${each.value.dns}"
  type         = "CNAME"
  ttl          = 300
  rrdatas      = [each.value.apex]
}

resource "google_dns_managed_zone" "app" {
  project    = var.project_id
  name       = "private-app"
  dns_name   = var.app_domain
  visibility = "private"
  private_visibility_config {
    networks {
      network_url = var.network_id
    }
  }
}

resource "google_dns_record_set" "app" {
  for_each     = toset(var.app_hostnames)
  project      = var.project_id
  managed_zone = google_dns_managed_zone.app.name
  name         = each.value
  type         = "A"
  ttl          = 300
  rrdatas      = [var.gateway_ip]
}
