output "storage_account_name" {
  value = azurerm_storage_account.archive.name
}

output "container_name" {
  value = azurerm_storage_container.archive.name
}

# `just archive-env` が読んで VM とローカルの .env に書く
output "primary_connection_string" {
  value     = azurerm_storage_account.archive.primary_connection_string
  sensitive = true
}
