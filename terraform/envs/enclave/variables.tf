variable "project_id" {
  type    = string
  default = "enclave-gk"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "ranges" {
  type = object({
    main             = string
    pods             = string
    services         = string
    proxy_only       = string
    private_services = string
  })
  default = {
    main             = "10.10.0.0/20"
    pods             = "10.20.0.0/16"
    services         = "10.30.0.0/20"
    proxy_only       = "10.40.0.0/23"
    private_services = "10.50.0.0/16"
  }
}

variable "master_cidr" {
  description = "The GKE control plane's private range."
  type        = string
  default     = "172.16.0.0/28"
}

variable "gateway_ip" {
  description = "The Gateway's internal VIP (in ranges.main)."
  type        = string
  default     = "10.10.0.100"
}

# The app's internal names (Module C7: HTTPS at the Gateway, from a private CA). Both
# are under one registrable domain, so they're the same site and the SAML login
# cookie survives the IdP's POST.
variable "app_domain" {
  type    = string
  default = "enclave.internal."
}

variable "app_hostnames" {
  type    = list(string)
  default = ["gatekeeper.enclave.internal", "auth.enclave.internal"]
}

variable "node_count" {
  description = "0 between sessions keeps the cluster but stops paying for nodes."
  type        = number
  default     = 2
}

variable "binauthz_enforce" {
  description = "Module C6: GKE refuses images without the release attestation."
  type        = bool
  default     = false
}

variable "ha" {
  description = "Module C1 drills: one node in each of three zones, and Cloud SQL with a standby. Costs more; turn off after."
  type        = bool
  default     = false
}

variable "cloudsql_running" {
  description = "false stops Cloud SQL between sessions."
  type        = bool
  default     = true
}

variable "alert_notification_channels" {
  description = "Where alerts go (Cloud Monitoring notification channel IDs). Empty: console only."
  type        = list(string)
  default     = []
}
