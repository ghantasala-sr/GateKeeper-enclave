variable "project_id" { type = string }
variable "network" { type = string }

variable "internal_ranges" {
  description = "Everything inside the Enclave that egress may reach."
  type        = list(string)
}

variable "proxy_only_range" {
  description = "Where the internal load balancer's proxies connect from."
  type        = string
}
