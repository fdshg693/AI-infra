@./README.md

## このフォルダだけを開いたとき

アンブレラ（fdshg693/AI）の `infra/` として clone されている前提。共通の手順（Junction・フックの作り方、`_umbrella/` の扱い、2 段階コミット）は [repo-layout](../maintenance/docs/repo-layout.md#サブモジュールを単体で開く)。

- 各基盤の運用詳細はそのディレクトリの README を見る（ai-logs は [`ai-logs/README.md`](ai-logs/README.md)）。
- `.env`・`terraform.tfvars`・`terraform.tfstate` などのローカル設定と状態は基盤ごとのディレクトリに置く（gitignore 済み）。コミットしない。
