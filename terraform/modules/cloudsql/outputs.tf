output "private_ip" { value = google_sql_database_instance.gatekeeper.private_ip_address }
output "connection_name" { value = google_sql_database_instance.gatekeeper.connection_name }
