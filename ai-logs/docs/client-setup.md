# クライアント側のセットアップ（Claude Code）

新しい端末で Claude Code のログを本基盤に送り始めるまでの手順。対象は Windows の CLI・VS Code 拡張と、WSL（既定 `Ubuntu-24.04`）の CLI・Remote-WSL。

何が送られるかは [telemetry-data.md](telemetry-data.md)。VM 側の構築は [README の初期構築](../README.md#初期構築再デプロイの流れ)。

Codex CLI は今は使っていないので扱わない（参考情報は [README の Codex CLI 節](../README.md#codex-cli)）。

## 送信先

| 項目           | 値                                                                                                                  |
| -------------- | ------------------------------------------------------------------------------------------------------------------- |
| エンドポイント | `https://<VMのIP>:4318`                                                                                             |
| プロトコル     | OTLP/HTTP（`http/protobuf`）                                                                                        |
| 認証           | `Authorization: Bearer <OTLP_AUTH_TOKEN>`                                                                           |
| 証明書         | Let's Encrypt の IP アドレス証明書。公的 CA なので、クライアントに CA の設定は要らない（Windows・WSL とも確認済み） |

### なぜ OTLP/HTTP か

gRPC（4317）は閉じてある。Claude Code の gRPC エクスポータ（grpc-js）は接続先の IP をそのまま TLS の servername に渡し、Node が `ERR_INVALID_ARG_VALUE: The property 'options.servername'` で拒否する（`14 UNAVAILABLE`）。OTLP/HTTP では Node が IP 宛てに SNI を付けないので通る。ドメインを持たず IP 証明書で TLS にしている限り、gRPC は使えない。

## 1. `.env` を用意する

`infra/ai-logs/.env` に送信先とトークンを置く（gitignore 済み）。

```sh
cd infra/ai-logs
cp .env.example .env
just ip              # SERVER_IP に入れる値
just show-secrets    # OTLP_AUTH_TOKEN に入れる値（画面にトークンが出るので、人がいる端末でだけ実行する）
```

`just ip` / `just show-secrets` は Terraform の状態と VM への SSH 鍵（`~/.ssh/ai_logs_ed25519`）が要る。無い端末では、ある端末の `.env` から 2 つの値を安全な経路で写す。

## 2. 変数を入れる（`just env-install`）

```sh
just env-install              # Windows と WSL（既定）
just env-install windows      # Windows だけ
just env-install wsl Ubuntu-22.04   # 別のディストリビューション
```

- 書き込み先は `~/.claude/settings.json` の `env`（WSL は WSL の中の `~/.claude/settings.json`）。既存のキーは残し、書く前に `settings.json.ai-logs.bak` を作る。値は画面に出さない
- WSL へは `wsl.exe` の標準入力で JSON を渡し、WSL 側の `jq` でマージする。トークンはコマンドライン引数に載らない。WSL に `jq` が要る
- 以前の版が書いていた Windows のユーザー環境変数が残っていれば消す
- **反映には Claude Code の再起動が要る**。開いている VS Code のウィンドウ（Remote-WSL も）と、実行中の `claude` を閉じて開き直す

入る変数は [`scripts/client_env.py`](../scripts/client_env.py) の `derive_vars` が出す 5 つ（`CLAUDE_CODE_ENABLE_TELEMETRY` / `OTEL_LOGS_EXPORTER` / `OTEL_EXPORTER_OTLP_PROTOCOL` / `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`）。値の意味は [telemetry-data.md の送信設定](telemetry-data.md#本基盤の送信設定)。

### ほかのレシピ

| レシピ                             | 使うとき                                                                                                                                                                                      |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `just env-emit [pwsh\|bash]`       | いまのシェルだけで試す。`just env-emit pwsh \| Invoke-Expression`、Git Bash なら `eval "$(just env-emit bash)"`。ただし `~/.claude/settings.json` の `env` が入っているとそちらが勝つ（下記） |
| `just env-purge [target] [distro]` | 送信をやめる。`env-install` が入れた 5 キーだけを消す（同じくバックアップを作る）                                                                                                             |

### 単発で送信先を変えて試す

`~/.claude/settings.json` の `env` はプロセスの環境変数より優先される。`env-install` 済みの端末で環境変数を変えても効かないので、試験用の値は設定ファイルで渡す。

```sh
claude --settings ./otel-test.json -p "hello"   # otel-test.json に {"env": {...}} を書く。トークンを含むなら 0600 にして使い終わったら消す
```

`just verify-client` はこの形で動いている。

## 3. VS Code

OTel 変数は上の `~/.claude/settings.json` の `env` だけで VS Code 拡張にも効く。VS Code 側に追加の設定は要らない。

`claudeCode.environmentVariables`（VS Code の設定）には OTel 変数を書かない。

- 公式ドキュメントが、拡張と CLI で共有する設定（環境変数を含む）は `~/.claude/settings.json` に書くよう案内している
- VS Code の設定はプロファイルごとにあるので、書き漏れるプロファイルが出る
- トークンが VS Code の `settings.json` に平文で残る

Windows のユーザー環境変数でも渡さない。VS Code が古い環境を引き継いで起動していると拡張に届かないことを実測した（2026-11-18）。

### プロファイル間で Claude Code の設定が揃っているか確かめる

[vscode-profiles](../../../tools/vscode-profiles/README.md) を PowerShell から使う（Git Bash は UNC パスを潰す）。

```powershell
# どのプロファイルにどのキーがあるか（値は出さない）
uv run vscode-profiles diff --prefix claudeCode. --by-profile

# 誤って環境変数を書いたプロファイルが無いか（値を伏せて表示）
uv run vscode-profiles diff --key claudeCode.environmentVariables --mask
```

揃えたいときは `apply` で予告を見てから、`--emit-ps1` で書き込み用スクリプトを作り、VS Code を閉じて外部ターミナルで実行する（`--write` は VS Code の起動中は失敗する）。

```powershell
uv run vscode-profiles apply --from Default --to all --prefix claudeCode. --exclude-key claudeCode.environmentVariables --emit-ps1 apply.ps1
uv run vscode-profiles remove --profile <名前> --key claudeCode.environmentVariables --mask --emit-ps1 apply.ps1
# VS Code を閉じてから
pwsh -File apply.ps1
```

## 4. WSL

Windows のユーザー環境変数は WSL に伝わらない。`WSLENV` で渡す方法もあるが、効くのはその `wsl.exe` を起動したプロセスの環境にある変数だけで、VS Code Server は最初に起動したときの環境を持ち越すため、VS Code と同じ漏れ方をする。そのため WSL でも WSL の中の `~/.claude/settings.json` の `env` を使い、`just env-install`（既定で WSL を含む）で Windows 側と一緒に入れる。

Remote-WSL の VS Code 設定（Machine 設定）は `--wsl` で Windows のプロファイルと並べて確かめられる。

```powershell
uv run vscode-profiles --wsl Ubuntu-24.04 diff --prefix claudeCode. --by-profile
```

ここにも `claudeCode.environmentVariables` は書かない。

## 5. 届いたか確かめる

| レシピ                         | 確かめること                                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `just verify-tls`              | 4318 の証明書（発行者・期限・SAN の IP）、4317 が閉じていること、平文の HTTP が Collector に届かないこと                    |
| `just verify-otlp-auth`        | トークン無しが 401 になる（証明書の検証込み）                                                                               |
| `just verify-client`           | 実際の `claude -p` が送れるか。一時の `--settings` で渡すので、`env-install` の内容とは独立に「この端末から送れるか」を見る |
| `just logs 15m 50 claude-code` | 再起動後の普段の操作が Loki に届いているか                                                                                  |
| `just log-services`            | Loki にある `service_name` の一覧（`claude-code` があるか）                                                                 |

`verify-client` が通って普段の操作が届かないなら、`env-install` の書き込みか再起動に問題がある。

WSL から届いたかは、イベントの `terminal_type`（`wsl-Ubuntu-24.04`）・`wsl_version`（`2`）・`os_type`（`linux`）で見分ける。Grafana なら `{service_name="claude-code"} | terminal_type=~"wsl-.*"`。

## 届かないとき

| 症状・原因                                      | 対処                                                                                                                                                                                                                                                         |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 再起動していない                                | Claude Code は起動時に `settings.json` を読む。VS Code のウィンドウや `claude` を開き直す                                                                                                                                                                    |
| Bash ツール・hook・MCP サーバーのイベントが無い | 仕様。OTel 変数は子プロセスに渡らず、計装されるのは Claude Code 本体のイベントだけ。Claude Code の Bash ツールで `env` を見ても `OTEL_*` は出ないので、設定の確認にもならない（`settings.json` を見る）                                                      |
| 環境変数で入れた値が効かない                    | `~/.claude/settings.json` の `env` が勝っている。`--settings` で渡す（上記）                                                                                                                                                                                 |
| 404 になる                                      | エンドポイントに信号別のパスを付けている、または付け忘れている。Claude Code は `OTEL_EXPORTER_OTLP_ENDPOINT` にベース URL（`https://<IP>:4318`）を書けば `/v1/logs` を自分で足す。信号別の `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` を使うなら `/v1/logs` まで書く |
| `14 UNAVAILABLE` / `servername` のエラー        | プロトコルが `grpc` になっている。`http/protobuf` にする                                                                                                                                                                                                     |
| 証明書エラー                                    | `just verify-tls` で期限を見る。証明書は 160 時間で、Caddy が 80 番の HTTP-01 で更新する。80 番を閉じた、VM が止まっていたなどで切れていないか確かめる（VM 側の `docker compose logs caddy`）                                                                |
| 401                                             | トークンの誤り。`.env` の `OTLP_AUTH_TOKEN` を VM の値（`just show-secrets`）と照らし、`just env-install` し直す                                                                                                                                             |
| 何も起きず、原因が分からない                    | `claude --debug-file <パス> -p "hello"` の `[3P telemetry]` の行を見る。トークンに `%` が入っていて初期化に失敗した例がある（`Telemetry init failed: URI error`）                                                                                            |
