variable "project_id" { type = string }
variable "region" { type = string }

variable "ranges" {
  description = "Every internal range the Enclave uses."
  type = object({
    main             = string # nodes, bastion, the Gateway's VIP
    pods             = string
    services         = string
    proxy_only       = string # the Gateway's managed proxies
    private_services = string # Cloud SQL (Private Services Access)
  })
}

variable "gateway_ip" {
  description = "The Gateway's internal VIP, inside ranges.main."
  type        = string
}
