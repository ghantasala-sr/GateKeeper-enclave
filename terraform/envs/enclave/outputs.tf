output "registry" { value = module.iam.registry }
output "cloudsql_private_ip" { value = module.cloudsql.private_ip }
output "cluster_name" { value = module.gke.cluster_name }
output "zone" { value = var.zone }
output "bastion" { value = module.bastion.name }
output "gateway_ip" { value = module.network.gateway_ip }
output "gateway_address_name" { value = module.network.gateway_address_name }
output "backend_service_account" {
  description = "Workload Identity annotation for the backend's Kubernetes service account."
  value       = module.iam.backend_sa_email
}
output "secret_ids" { value = module.secrets.secret_ids }
output "bundle_bucket" { value = google_storage_bucket.bundles.name }
output "internal_ca_cert_pem" { value = module.tls.ca_cert_pem }
output "gateway_certificate_name" { value = module.tls.gateway_certificate_name }
