# Module C7: HTTPS at the internal load balancer, from a private CA.
#
# The CA and the server certificate are created here; the certificate and its key go
# to Google as a REGIONAL SSL certificate that the GKE Gateway uses by name, so the
# key never lands in the cluster. Clients (the backend, operators) trust the CA's
# certificate, which is public. Production would issue from Certificate Authority
# Service (an HSM-backed CA, audited issuance) instead of a key in Terraform state.

resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24 * 365 * 3
  allowed_uses          = ["cert_signing", "crl_signing"]
  subject {
    common_name  = "Gatekeeper Enclave internal CA"
    organization = "Gatekeeper Enclave"
  }
}

resource "tls_private_key" "gateway" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "gateway" {
  private_key_pem = tls_private_key.gateway.private_key_pem
  dns_names       = var.hostnames
  subject {
    common_name  = var.hostnames[0]
    organization = "Gatekeeper Enclave"
  }
}

resource "tls_locally_signed_cert" "gateway" {
  cert_request_pem      = tls_cert_request.gateway.cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 24 * 90 # short-lived: rotate by re-applying (see RUNBOOK)
  early_renewal_hours   = 24 * 30
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]
}

resource "google_compute_region_ssl_certificate" "gateway" {
  project     = var.project_id
  region      = var.region
  name_prefix = "enclave-gateway-"
  certificate = tls_locally_signed_cert.gateway.cert_pem
  private_key = tls_private_key.gateway.private_key_pem
  lifecycle {
    create_before_destroy = true # renewals swap certificates without a gap
  }
}
