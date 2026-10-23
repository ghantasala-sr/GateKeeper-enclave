output "ca_cert_pem" {
  description = "The private CA's certificate (public): what clients trust."
  value       = tls_self_signed_cert.ca.cert_pem
}
output "gateway_certificate_name" {
  description = "The regional SSL certificate the Gateway uses (networking.gke.io/pre-shared-certs)."
  value       = google_compute_region_ssl_certificate.gateway.name
}
