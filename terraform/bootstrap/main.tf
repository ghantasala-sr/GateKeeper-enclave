# Step B1, part 1: the project itself, billing, budget alerts, and the bucket that
# holds the Enclave's Terraform state. Run once, from an admin's laptop; its own
# (small) state stays local. Everything else lives in ../envs/enclave.
#
#   terraform init && terraform apply -var billing_account=XXXXXX-XXXXXX-XXXXXX

terraform {
  required_version = ">= 1.9"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.8" }
  }
}

provider "google" {
  region = var.region
  # The budget API is billed to a project; use the new one as soon as it exists.
  user_project_override = true
  billing_project       = var.project_id
}

resource "google_project" "enclave" {
  project_id      = var.project_id
  name            = "Gatekeeper Enclave"
  billing_account = var.billing_account
  deletion_policy = "DELETE" # a lab: `terraform destroy` really removes it

  # No default network: the Enclave builds its own sealed VPC.
  auto_create_network = false
}

resource "google_project_service" "bootstrap" {
  for_each = toset([
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])
  project            = google_project.enclave.project_id
  service            = each.value
  disable_on_destroy = false
}

# Alerts by email to the billing account's admins at $10, $25, $50 and $100.
resource "google_billing_budget" "enclave" {
  billing_account = var.billing_account
  display_name    = "Gatekeeper Enclave"

  budget_filter {
    projects = ["projects/${google_project.enclave.number}"]
  }
  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = "100"
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.10, 0.25, 0.50, 1.00]
    content {
      threshold_percent = threshold_rules.value
    }
  }

  depends_on = [google_project_service.bootstrap]
}

# Remote state for ../envs/enclave: versioned (a bad apply can be undone), never public.
resource "google_storage_bucket" "tfstate" {
  project                     = google_project.enclave.project_id
  name                        = "${google_project.enclave.project_id}-tfstate"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # a lab; production would keep it

  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.bootstrap]
}
