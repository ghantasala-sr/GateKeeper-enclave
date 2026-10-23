variable "project_id" { type = string }
variable "region" { type = string }
variable "bastion_sa_email" { type = string }

variable "workload_sa_emails" {
  description = "Google service account emails of the workloads, by short name (gk-backend, gk-setup)."
  type        = map(string)
}
