variable "project_id" { type = string }
variable "zone" { type = string }
variable "network_id" { type = string }
variable "subnet_id" { type = string }
variable "master_cidr" { type = string }

variable "authorized_cidr" {
  description = "Who may reach the (private) control plane: the main subnet, i.e. the bastion."
  type        = string
}

variable "node_service_account" { type = string }
variable "backend_sa_name" { type = string }

variable "backend_ksa" {
  description = "namespace/name of the backend's Kubernetes service account."
  type        = string
  default     = "gatekeeper/gk-backend"
}

variable "node_count" {
  type    = number
  default = 2
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}
