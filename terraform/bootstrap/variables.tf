variable "project_id" {
  description = "Globally unique project ID for the Enclave."
  type        = string
  default     = "enclave-gk"
}

variable "billing_account" {
  description = "Billing account ID (gcloud billing accounts list)."
  type        = string
}

variable "region" {
  description = "Region for the state bucket (and the Enclave)."
  type        = string
  default     = "us-central1"
}

variable "budget_currency" {
  description = "Must match the billing account's currency."
  type        = string
  default     = "USD"
}
