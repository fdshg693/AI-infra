# ai-logs の日次アーカイブ（Loki → gzip JSONL）を置く Azure Blob。
# もとは az CLI（旧 `just archive-provision`）で作り、imports.tf で取り込んだ。
# 書いていない属性（network_rules・blob_properties・暗号化系など）は既定値か実値のまま受け入れる。

locals {
  tags = {
    application = "ai-logs"
    managedBy   = "terraform"
  }
}

resource "azurerm_resource_group" "archive" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

resource "azurerm_storage_account" "archive" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.archive.name
  location                 = azurerm_resource_group.archive.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  # 書かないと既定の Hot へ変わる
  access_tier                     = "Cool"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  https_traffic_only_enabled      = true
  tags                            = local.tags

  lifecycle {
    # 作り直すと Blob（過去のログ）が消える
    prevent_destroy = true
  }
}

resource "azurerm_storage_container" "archive" {
  name                  = var.container_name
  storage_account_id    = azurerm_storage_account.archive.id
  container_access_type = "private"

  lifecycle {
    prevent_destroy = true
  }
}
