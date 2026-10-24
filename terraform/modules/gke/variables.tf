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
variable "workload_identities" {
  description = "Kubernetes service account (namespace/name) -> Google service account name it may act as."
  type        = map(string)
}

variable "binauthz_enforce" {
  description = "Refuse unattested images (Binary Authorization). Turn on after attesting the running release."
  type        = bool
}

variable "node_zones" {
  description = "Zones for the nodes (node_count in EACH)."
  type        = list(string)
}

variable "node_count" {
  type    = number
  default = 2
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}
