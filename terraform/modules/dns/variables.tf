variable "project_id" { type = string }
variable "network_id" { type = string }

variable "app_domain" {
  description = "Zone for the app's hostnames, e.g. gatekeeper.localhost."
  type        = string
}

variable "app_hostnames" {
  description = "Fully qualified names (trailing dot) that point at the Gateway."
  type        = list(string)
}

variable "gateway_ip" { type = string }
