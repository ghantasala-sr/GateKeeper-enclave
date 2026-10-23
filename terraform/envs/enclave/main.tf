# The Enclave: a sealed VPC, private GKE and Cloud SQL, a bastion, and nothing
# that can reach the internet. Built on the project from ../../bootstrap.
#
#   terraform init -backend-config="bucket=<project>-tfstate"
#   terraform plan -out plan.out && terraform apply plan.out
#   terraform destroy            # at the end of every session

terraform {
  required_version = ">= 1.9"
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 8.8" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 8.8" }
    random      = { source = "hashicorp/random", version = "~> 3.9" }
    tls         = { source = "hashicorp/tls", version = "~> 4.4" }
  }
  backend "gcs" {
    prefix = "enclave"
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  zone                  = var.zone
  user_project_override = true
  billing_project       = var.project_id
}

# Only for resources not yet in the GA provider (the Monitoring service agent).
provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "dns.googleapis.com",
    "iap.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

module "network" {
  source     = "../../modules/network"
  project_id = var.project_id
  region     = var.region
  ranges     = var.ranges
  gateway_ip = var.gateway_ip
  depends_on = [google_project_service.apis]
}

module "firewall" {
  source     = "../../modules/firewall"
  project_id = var.project_id
  network    = module.network.network_name
  internal_ranges = [
    var.ranges.main,
    var.ranges.pods,
    var.ranges.services,
    var.ranges.proxy_only,
    var.ranges.private_services,
    var.master_cidr,
  ]
  proxy_only_range = var.ranges.proxy_only
}

module "dns" {
  source        = "../../modules/dns"
  project_id    = var.project_id
  network_id    = module.network.network_id
  app_domain    = var.app_domain
  app_hostnames = [for h in var.app_hostnames : "${h}."]
  gateway_ip    = module.network.gateway_ip
}

module "iam" {
  source     = "../../modules/iam"
  project_id = var.project_id
  region     = var.region
  depends_on = [google_project_service.apis]
}

module "secrets" {
  source           = "../../modules/secrets"
  project_id       = var.project_id
  region           = var.region
  bastion_sa_email = module.iam.bastion_sa_email
  workload_sa_emails = {
    gk-backend = module.iam.backend_sa_email
    gk-setup   = module.iam.setup_sa_email
  }
  depends_on = [google_project_service.apis]
}

module "cloudsql" {
  source                      = "../../modules/cloudsql"
  project_id                  = var.project_id
  region                      = var.region
  network_id                  = module.network.network_id
  private_services_connection = module.network.private_services_connection
  owner_password              = module.secrets.owner_password
  running                     = var.cloudsql_running
  high_availability           = var.ha
}

module "gke" {
  source               = "../../modules/gke"
  project_id           = var.project_id
  zone                 = var.zone
  network_id           = module.network.network_id
  subnet_id            = module.network.subnet_id
  master_cidr          = var.master_cidr
  authorized_cidr      = var.ranges.main
  node_service_account = module.iam.nodes_sa_email
  workload_identities = {
    "gatekeeper/gk-backend" = module.iam.backend_sa_name
    "gatekeeper/gk-setup"   = module.iam.setup_sa_name
  }
  node_count = var.ha ? 1 : var.node_count
  node_zones = var.ha ? ["${var.region}-a", "${var.region}-b", "${var.region}-c"] : [var.zone]
  # Nodes need private DNS (Google APIs -> restricted range) from their first boot.
  depends_on = [module.dns, module.firewall]
}

module "bastion" {
  source          = "../../modules/bastion"
  project_id      = var.project_id
  zone            = var.zone
  subnet_id       = module.network.subnet_id
  service_account = module.iam.bastion_sa_email
  depends_on      = [module.dns, module.firewall]
}

# Step B11: where the offline bundle is dropped for the bastion to fetch. Faster than
# copying gigabytes over an IAP SSH tunnel, and the bastion reaches it over Private
# Google Access (storage is served on the restricted range). Bundles are checksummed.
resource "google_storage_bucket" "bundles" {
  name                        = "${var.project_id}-bundles"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  depends_on                  = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "bastion_reads_bundles" {
  bucket = google_storage_bucket.bundles.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${module.iam.bastion_sa_email}"
}

module "monitoring" {
  source                = "../../modules/monitoring"
  project_id            = var.project_id
  notification_channels = var.alert_notification_channels
  depends_on            = [google_project_service.apis]
}

module "tls" {
  source     = "../../modules/tls"
  project_id = var.project_id
  region     = var.region
  hostnames  = var.app_hostnames
  depends_on = [google_project_service.apis]
}
