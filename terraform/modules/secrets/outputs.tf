output "owner_password" {
  value     = random_password.this["owner-password"].result
  sensitive = true
}
output "secret_ids" {
  value = { for k, s in google_secret_manager_secret.this : k => s.secret_id }
}
