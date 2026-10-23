# Step B8: private Cloud SQL for PostgreSQL. No public IP, TLS required, reachable
# only from inside the VPC through Private Services Access.
#
# Its built-in users are NOT superusers (Gatekeeper's non-superuser fixes, A2.1);
# gatekeeper_owner gets CREATEROLE/CREATEDB via cloudsqlsuperuser, which is all
# migrations need. pgvector 0.8.x ships with Cloud SQL for PostgreSQL 13+.

resource "google_sql_database_instance" "gatekeeper" {
  project             = var.project_id
  name                = "enclave-pg"
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = false # a lab; production keeps it on

  settings {
    edition           = "ENTERPRISE" # shared-core tiers need Enterprise, not Enterprise Plus
    tier              = var.tier
    availability_type = var.high_availability ? "REGIONAL" : "ZONAL" # REGIONAL: a standby in another zone
    disk_size         = 10
    disk_type         = "PD_SSD"
    activation_policy = var.running ? "ALWAYS" : "NEVER" # stop it between sessions

    ip_configuration {
      ipv4_enabled    = false # no public IP
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "07:00"
    }
  }

  depends_on = [var.private_services_connection]
}

resource "google_sql_database" "gatekeeper" {
  project  = var.project_id
  instance = google_sql_database_instance.gatekeeper.name
  name     = "gatekeeper"
}

resource "google_sql_user" "owner" {
  project  = var.project_id
  instance = google_sql_database_instance.gatekeeper.name
  name     = "gatekeeper_owner"
  password = var.owner_password
}
