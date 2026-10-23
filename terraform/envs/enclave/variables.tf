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

# The names typed into the browser, reached through an IAP tunnel to the bastion.
# Browsers resolve *.localhost to loopback and treat it as a secure context, so
# Keycloak's Secure cookies work over plain HTTP (C7 moves to internal HTTPS).
variable "app_domain" {
  type    = string
  default = "gatekeeper.localhost."
}

variable "app_hostnames" {
  type    = list(string)
  default = ["gatekeeper.localhost", "auth.gatekeeper.localhost"]
}

variable "node_count" {
  description = "0 between sessions keeps the cluster but stops paying for nodes."
  type        = number
  default     = 2
}

variable "cloudsql_running" {
  description = "false stops Cloud SQL between sessions."
  type        = bool
  default     = true
}
