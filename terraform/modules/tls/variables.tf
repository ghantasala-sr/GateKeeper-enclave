variable "project_id" { type = string }
variable "region" { type = string }

variable "hostnames" {
  description = "Names on the Gateway's certificate (no trailing dots)."
  type        = list(string)
}
