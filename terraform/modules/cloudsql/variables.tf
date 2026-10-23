variable "project_id" { type = string }
variable "region" { type = string }
variable "network_id" { type = string }

variable "private_services_connection" {
  description = "The instance can only be created once the VPC peering exists."
  type        = string
}

variable "owner_password" {
  type      = string
  sensitive = true
}

variable "tier" {
  type    = string
  default = "db-f1-micro" # smallest shared-core
}

variable "high_availability" {
  description = "A synchronous standby in another zone, with automatic failover (costs about double)."
  type        = bool
  default     = false
}

variable "running" {
  description = "false stops the instance (no compute charges) between sessions."
  type        = bool
  default     = true
}
