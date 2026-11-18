# Claude Code から何が送られ、本基盤に何が残るか

Claude Code の OpenTelemetry（OTel）送信のうち、**本基盤の設定（[client-setup.md](client-setup.md) の `just env-install` が入れる変数だけ）のもとで** 何が届き、Loki に何が残るかをまとめる。

- 確認日: 2026-11-18。実データは Claude Code 2.1.325（Windows・VS Code 拡張）と 2.1.322（WSL の CLI）。イベントや属性は版で変わるので、古くなったら下の「確かめ方」で取り直す
- 環境変数・メトリクス・イベント・属性の**全量のカタログ**は [otel-reference.md](../../../skills/plugins/cc-knowledge/skills/claude-code-debugging/otel-reference.md) が正。ここには重複して書かない
- 一次情報の URL は [references.md](../references.md#claude-code-の-otel-テレメトリ) にある

## 本基盤の送信設定

[`scripts/client_env.py`](../scripts/client_env.py) の `derive_vars` が出す 5 変数だけを設定している。

| 変数                           | 値                             | 意味                                                                              |
| ------------------------------ | ------------------------------ | --------------------------------------------------------------------------------- |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1`                            | 送信を有効にする。これが無いと他の `OTEL_*` は無視される                          |
| `OTEL_LOGS_EXPORTER`           | `otlp`                         | イベント（ログ）だけを送る                                                        |
| `OTEL_EXPORTER_OTLP_PROTOCOL`  | `http/protobuf`                | OTLP/HTTP。gRPC は使えない（[client-setup.md](client-setup.md#なぜ-otlphttp-か)） |
| `OTEL_EXPORTER_OTLP_ENDPOINT`  | `https://<VMのIP>:4318`        | VM の Caddy（TLS 終端）                                                           |
| `OTEL_EXPORTER_OTLP_HEADERS`   | `Authorization=Bearer <token>` | Collector の認証                                                                  |

設定していないもの（つまり Claude Code の既定のまま）:

- `OTEL_METRICS_EXPORTER` / `OTEL_TRACES_EXPORTER` — メトリクスとトレースは送っていない
- `OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_ASSISTANT_RESPONSES` / `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_TOOL_CONTENT` / `OTEL_LOG_RAW_API_BODIES` — 本文・詳細のオプトインはすべて無効
- `OTEL_METRICS_INCLUDE_*` — 属性の出し入れは既定のまま

## イベントの要約

直近 24 時間の Loki（437 件）で見えたイベントと、本基盤の設定での中身。件数は普段使いの量の目安。

| イベント（`claude_code.*`）          | 件数    | いつ出るか                   | 主な属性                                                                                                         | 既定で含まれないもの                                                                                         |
| ------------------------------------ | ------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `hook_execution_start` / `_complete` | 82 / 82 | hook の実行前後              | `hook_event`・`hook_name`（`PreToolUse:Write` など）・`num_hooks`・成功／ブロックの件数                          | hook の定義・出力                                                                                            |
| `tool_decision`                      | 68      | ツール呼び出しの許可・拒否   | `tool_name`・`decision`・`source`・`tool_source`                                                                 | ツールの入力。MCP ツールの `tool_name` は `mcp_tool` になる                                                  |
| `tool_result`                        | 68      | ツールの実行後               | `tool_name`・`success`・`duration_ms`                                                                            | ツールの入力・出力（`tool_input` は `OTEL_LOG_TOOL_DETAILS` で解禁）。MCP ツールの `tool_name` は `mcp_tool` |
| `api_request`                        | 67      | モデル API の呼び出しごと    | `model`・トークン数・`cost_usd`・`duration_ms`・`ttft_ms`・`query_source`                                        | リクエスト・レスポンスの本文。`mcp_server_name` / `mcp_tool_name` は `custom` に置き換わる                   |
| `assistant_response`                 | 24      | 応答の完了                   | `response`（`<REDACTED>`）                                                                                       | 応答の本文                                                                                                   |
| `hook_registered`                    | 10      | 起動時の hook 登録           | hook のイベント名・出所                                                                                          |                                                                                                              |
| `plugin_loaded`                      | 10      | 起動時のプラグイン読み込み   | `plugin_name`・`marketplace_name`（**実名**）                                                                    |                                                                                                              |
| `mcp_server_connection`              | 8       | MCP サーバーの接続・切断     | `status`・`transport_type`・`is_plugin`・`plugin.name`                                                           | `server_name` とエラーメッセージ（`OTEL_LOG_TOOL_DETAILS` で解禁）                                           |
| `user_prompt`                        | 6       | プロンプトの送信             | `prompt` / `prompt_text`（どちらも `<REDACTED>`）・`prompt_length`・`command_name`（`mcp` などのプレースホルダ） | プロンプト本文、スラッシュコマンドの実名                                                                     |
| `managed_settings_resolved`          | 5       | managed settings の解決      | 解決状態                                                                                                         | 設定の中身（`OTEL_LOG_MANAGED_SETTINGS` で解禁）                                                             |
| `retention_sweep`                    | 2       | ローカル履歴の保持期間の掃除 | `result`                                                                                                         |                                                                                                              |

このほか、発生したときだけ出るもの（拒否が起きたときの `safety_event`、`api_error`、`compaction`、`subagent_completed`、`skill_activated` など）がある。この期間は bypassPermissions / auto で使っていて拒否が起きず、`safety_event` は 0 件だった。一覧は [otel-reference.md](../../../skills/plugins/cc-knowledge/skills/claude-code-debugging/otel-reference.md#イベント名ログとして送信されるもの)。

ドキュメントに無いが届く属性: 全イベントの `anthropic_plan` / `anthropic_provider` / `anthropic_custom_base_url`、`api_request` の `ttft_ms`。

## 全イベントに付く共通属性

Loki では属性名のドットが `_` になる（`session.id` → `session_id`）。

| 区分       | 属性                                                                                                                                                     | 止められるか                                                                                                                     |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| セッション | `session_id`・`prompt_id`（イベントのみ）・`event_sequence`・`event_timestamp`                                                                           | `session_id` は `OTEL_METRICS_INCLUDE_SESSION_ID=false`                                                                          |
| アカウント | `user_id`・`user_email`・`user_account_id`・`user_account_uuid`・`organization_id`                                                                       | `user_account_*` は `OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false`。**`user_email`・`organization_id`・`user_id` は止める変数が無い** |
| 端末       | `terminal_type`（`vscode` / `non-interactive` / `wsl-Ubuntu-24.04` など）・`os_type`・`os_version`・`host_arch`・`wsl_version`（WSL から送ったときだけ） | 止められない（リソース属性）                                                                                                     |
| 版数       | `service_name`（`claude-code`）・`service_version`                                                                                                       | 止められない。`app_version` は既定で付かない（`OTEL_METRICS_INCLUDE_VERSION`）                                                   |
| 契約       | `anthropic_plan`・`anthropic_provider`・`anthropic_custom_base_url`                                                                                      | 未記載の属性で、止め方も無い                                                                                                     |

`OTEL_METRICS_INCLUDE_*` は名前に反して**イベントの属性にも効く**。既定値の一覧は [otel-reference.md のカーディナリティ制御](../../../skills/plugins/cc-knowledge/skills/claude-code-debugging/otel-reference.md#カーディナリティ制御メトリクスの属性を絞る)。

`user_email` は OAuth でログインしていると全イベントに載る。送り先は自分の VM だけで、TLS で運ばれる。

## 本文・ファイル内容の扱い

| 内容                             | Claude Code の既定                                                                                     | 本基盤                                         |
| -------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| プロンプト本文                   | `<REDACTED>`（`OTEL_LOG_USER_PROMPTS=1` で解禁）                                                       | 既定のまま。実データの 6 件すべて `<REDACTED>` |
| 応答本文                         | `<REDACTED>`（`OTEL_LOG_ASSISTANT_RESPONSES`。未設定なら上に従う）                                     | 既定のまま。24 件すべて `<REDACTED>`           |
| ツールの入力・MCP 名・コマンド名 | 省くかプレースホルダ（`custom` / `mcp` / `mcp_tool`）。`OTEL_LOG_TOOL_DETAILS=1` で実名と `tool_input` | 既定のまま                                     |
| ツールの入出力の中身             | 送らない（`OTEL_LOG_TOOL_CONTENT` はトレースが前提）                                                   | トレースを送っていないので届かない             |
| API の生ボディ                   | 送らない（`OTEL_LOG_RAW_API_BODIES`）                                                                  | 既定のまま                                     |
| ファイル内容・コード片           | メトリクス・イベントには含まれない（トレースは別）                                                     | 届かない                                       |

プラグイン名・マーケットプレイス名は既定でも実名で届く。

## メトリクス

Claude Code は `claude_code.token.usage` / `cost.usage` / `session.count` など 8 種のメトリクスを送れる（一覧は [otel-reference.md](../../../skills/plugins/cc-knowledge/skills/claude-code-debugging/otel-reference.md#メトリクス名8種)）。本基盤は受けていない。

- クライアントは `OTEL_METRICS_EXPORTER` を設定していないので送らない
- 仮に送っても、Collector は `logs` パイプラインだけなので捨てる
- トークン数・費用は `api_request` イベントの属性で集計できる

## Loki での見え方

- ログの本文（line）はイベント名（`claude_code.api_request` など）
- インデックス化されるラベルは `service_name` だけ。ほかの属性はすべて structured metadata で、クエリのフィルタには使えるが絞り込みは遅い
- 保持は 14 日。それより古いものは Azure Blob のアーカイブにだけ残る（[README のログの保持期間](../README.md#ログの保持期間ホット-14-日--コールド-blob)）

代表的な LogQL（Grafana の Explore で使う）:

```logql
# イベント種別ごとの件数（直近 24 時間）
sum by (event_name) (count_over_time({service_name="claude-code"} [24h]))

# モデルごとの費用（api_request の cost_usd を合計）
sum by (model) (sum_over_time({service_name="claude-code"} | event_name="api_request" | unwrap cost_usd [24h]))

# WSL から届いたイベントだけ
{service_name="claude-code"} | terminal_type=~"wsl-.*"

# 1 セッションの流れ
{service_name="claude-code"} | session_id="<セッション ID>"
```

Grafana を開かずに見るなら `just logs 15m 50 claude-code`、`service_name` の一覧は `just log-services`。

## 確かめ方

版が上がって中身が変わったか見たいときは、属性の**キー名**だけを数える（値は出さない）。`just logs 24h 5000 claude-code` の出力からキー名と `event_name` を集計するか、Grafana の Explore でイベントを 1 件開いて structured metadata のキーを見る。取り直したら、この文書の確認日と版数、イベントの表を更新する。全量のカタログに足りない点があれば [otel-reference.md](../../../skills/plugins/cc-knowledge/skills/claude-code-debugging/otel-reference.md) のほうを直す。
