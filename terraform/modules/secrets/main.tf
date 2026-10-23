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
  )
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

# The bastion reads them to create the Kubernetes Secrets; nothing else can.
resource "google_secret_manager_secret_iam_member" "bastion" {
  for_each  = local.values
  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.bastion_sa_email}"
}
