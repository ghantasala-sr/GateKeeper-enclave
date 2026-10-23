output "cluster_name" { value = google_container_cluster.gk.name }
output "endpoint" {
  value     = google_container_cluster.gk.private_cluster_config[0].private_endpoint
  sensitive = true
}
