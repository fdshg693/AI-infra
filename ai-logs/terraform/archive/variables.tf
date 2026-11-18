variable "subscription_id" {
  type        = string
  description = "アーカイブを置く Azure サブスクリプションの ID。コミットしないので terraform.tfvars にだけ書く"
}

variable "resource_group_name" {
  type        = string
  default     = "rg-ai-logs-archive"
  description = "アーカイブ用のリソースグループ。skills-site の SWA の RG（rg-ai-skill-catalog）とは別"
}

variable "storage_account_name" {
  type        = string
  description = "ストレージアカウント名（グローバルに一意。作り直しで変える可能性があるので変数にする）"
}

variable "container_name" {
  type        = string
  default     = "ai-logs"
  description = "日次の gzip JSONL を置く Blob コンテナ"
}

variable "location" {
  type        = string
  default     = "japaneast"
  description = "Azure リージョン（変えると作り直しになる）"
}
