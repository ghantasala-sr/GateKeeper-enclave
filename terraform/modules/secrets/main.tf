# Every password and key the Enclave needs, generated here and kept in Secret
# Manager; the bastion's install script copies them into Kubernetes Secrets.
# Nobody types or sees them. (They are also in Terraform's state, which lives in
# the versioned, non-public state bucket.)

resource "random_password" "this" {
  for_each = toset([
    "owner-password",  # Cloud SQL user gatekeeper_owner (setup Job only)
    "app-db-password", # the gatekeeper_app role (backend)
    "session-secret",
    "keycloak-admin-password",
    "acme-client-secret",
    "acme-test-password",
    "globex-test-password",
  ])
  length  = 40
  special = false # used inside connection URLs and Keycloak's realm files
}

resource "tls_private_key" "token_signing" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

locals {
  values = merge(
    { for k, v in random_password.this : k => v.result },
    # Same format as scripts/gen_signing_key.py: base64 of a PKCS#8 PEM.
    { "token-signing-key-b64" = base64encode(tls_private_key.token_signing.private_key_pem_pkcs8) },
    # Previous signing keys still accepted during a rotation (proof/rotate-signing-key.sh);
    # "none" between rotations.
    { "token-verify-keys-b64" = "none" },
  )
  # Who may read what (Module C5): each workload only its own secrets.
  readers = {
    "gk-backend" = ["app-db-password", "session-secret", "token-signing-key-b64", "token-verify-keys-b64", "acme-client-secret"]
    "gk-setup"   = ["owner-password", "app-db-password"]
  }
  reader_bindings = merge([
    for sa, secrets in local.readers : { for s in secrets : "${sa} ${s}" => { sa = sa, secret = s } }
  ]...)
}

resource "google_secret_manager_secret" "this" {
  for_each  = local.values
  project   = var.project_id
  secret_id = "gk-${each.key}"
  replication {
    user_managed {
      replicas {
        location = var.region # the Enclave's data stays in its region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "this" {
  for_each    = local.values
  secret      = google_secret_manager_secret.this[each.key].id
  secret_data = each.value
}

# Workloads read their own secrets as files (GKE's Secret Manager CSI driver).
resource "google_secret_manager_secret_iam_member" "workloads" {
  for_each  = local.reader_bindings
  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.workload_sa_emails[each.value.sa]}"
}

# The bastion still reads them, for Keycloak's Kubernetes Secret and for rotations.
resource "google_secret_manager_secret_iam_member" "bastion" {
  for_each  = local.values
  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.bastion_sa_email}"
}

# Module C5: record every read of a secret (who, which secret, when) in Cloud Audit Logs.
resource "google_project_iam_audit_config" "secret_manager" {
  project = var.project_id
  service = "secretmanager.googleapis.com"
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "ADMIN_READ"
  }
}
