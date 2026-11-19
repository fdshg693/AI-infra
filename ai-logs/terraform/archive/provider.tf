terraform {
  required_providers {
    azurerm = {
      source = "hashicorp/azurerm"
      # 2026-11時点の最新は 5.14.0。v5 ではコンテナは storage_account_id のみ、RP 登録の既定は none。
      version = "~> 5.14"
    }
  }
}

# 認証は az CLI（`az login` 済みであること。use_cli の既定 true）。
provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # v5 の既定と同じだが明示する。Microsoft.Storage はサブスクリプションで登録済み
  resource_provider_registrations = "none"
}
