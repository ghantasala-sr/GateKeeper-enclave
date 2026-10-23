variable "project_id" { type = string }

variable "notification_channels" {
  description = "Cloud Monitoring notification channel IDs for alerts (email, chat, pager). Empty: incidents show in the console only."
  type        = list(string)
  default     = []
}
