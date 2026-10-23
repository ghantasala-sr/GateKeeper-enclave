output "network_id" { value = google_compute_network.vpc.id }
output "network_name" { value = google_compute_network.vpc.name }
output "subnet_id" { value = google_compute_subnetwork.main.id }
output "subnet_name" { value = google_compute_subnetwork.main.name }
output "gateway_address_name" { value = google_compute_address.gateway.name }
output "gateway_ip" { value = google_compute_address.gateway.address }
output "private_services_connection" {
  description = "Cloud SQL must wait for this."
  value       = google_service_networking_connection.private_services.id
}
