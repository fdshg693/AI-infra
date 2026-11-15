# infra/

アンブレラ（[fdshg693/AI](https://github.com/fdshg693/AI)）で使うインフラの定義（Docker 構成・Terraform）と運用スクリプトの置き場。

ツール（CLI）ではないので `registry/repo-tools.yaml`・`ai-tools.yaml` に登録せず、uv / pnpm のワークスペースにも入れない。公開サイト自身のインフラ（Bicep）はここではなく `site/skills-site/infra/` にある。

## 中身

- [ai-logs](ai-logs/README.md) — Claude Code・Codex CLI の OpenTelemetry ログを集める基盤（さくらのクラウド VM 上の OTel Collector・Loki・Grafana、Azure Blob への日次アーカイブ）

移行前は `tools/infra/` にあった（AI-tools から `git subtree split` で履歴ごと切り出した）。
