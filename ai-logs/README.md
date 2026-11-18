# AI ログ基盤

- AI コーディングツール等の実行ログを集約する基盤（さくらのクラウド VM 上で稼働中）
- 調査した参考URL・要点は [references.md](references.md) にまとめている

## 背景・目的

- Claude Code / Codex CLI など、複数の AI コーディングツールを日常的に使っており、それぞれが個別にログ・使用状況を溜め込んでいる
- ツールをまたいで「いつ・何を・どれだけ使ったか」を横断的に把握できる場所がなく、振り返りや改善につなげにくい
- 各ツールが標準サポートしている OpenTelemetry (OTel) のテレメトリ送信機能を使って、自前の基盤にログを集約し、Grafana で横断的に見られるようにする

## スコープ

対象（v1）:

- Claude Code
- Codex CLI

対象外（将来検討）:

- GitHub Copilot（CLI / VS Code 拡張）
  - CLI は `COPILOT_OTEL_EXPORTER_TYPE` 等で OTel 送信に対応しているが、VS Code 拡張側は別設定（`github.copilot.chat.otel.*`）が必要で、対応が二系統に分かれる
  - 組織の Audit Log（`copilot.chat_message_sent` 等）は Enterprise/Business プラン前提かつローカルのプロンプト本文までは含まれないため、個人利用の本基盤とは相性が悪い
  - 上記の複雑さから v1 では見送り、需要が出たら別途対応する
- 複数人での共有利用（v1 は自分一人での利用を前提とした設計）

## 要件定義

### 機能要件

- Claude Code / Codex CLI から OTLP でログを受信し、永続化できること
- Grafana から時系列・ツール別・セッション別にログを検索・閲覧できること
- IaC（Terraform）でインフラを再現可能な形で構築・破棄できること

### 非機能要件

- 個人利用規模のログ量を、最安クラスの VM 1 台で運用できること
- クラウドプロバイダ（さくらのクラウド）や自前 VM から他環境への移行が、Docker Compose の構成を持っていけば大きな手戻りなく行えること
- OTLP 受信エンドポイントを外部に公開する場合、認証なしでログを注入されない設計であること

## アーキテクチャ

### データフロー

```text
[Claude Code]  --OTLP/HTTP over TLS, Bearer Token-->            (:4318)
[Codex CLI]    --OTLP/HTTP over TLS, Bearer Token-->  [Caddy] --(compose network, plaintext)--> [OpenTelemetry Collector] --OTLP--> [Loki] <-- [Grafana]
```

- 開発者のローカルマシンから、さくらのクラウド VM 上の Caddy へ `https://<VMのIP>:4318` で送る。Caddy が Let's Encrypt の IP アドレス証明書で TLS を終端し、コンテナ間ネットワークだけで Collector に渡す。認証は Collector の Bearer Token
- Collector はログを Loki のネイティブ OTLP エンドポイントへ転送する
- Grafana は Loki をデータソースとして参照し、閲覧用 UI を提供する（SSH トンネル経由でのみアクセス可能）
- トレース（Claude Code は beta、Codex CLI は正式対応）は v1 スコープ外。Collector を中心に据えた構成にしてあるため、将来追加する場合は Collector から Tempo/Jaeger 等への転送を追加すればよい

### コンポーネント（Docker Compose）

| コンポーネント                                | 役割                                 | 備考                                                                                                                                                      |
| --------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caddy`（2.11.9）                             | OTLP の TLS 終端                     | Let's Encrypt の IP 証明書（`shortlived`、160 時間）を HTTP-01（80 番）で取得・自動更新。4318 だけを公開し Collector へ転送。証明書は volume `caddy-data` |
| `otel-collector`（`otelcol-contrib` 0.153.0） | OTLP 受信・認証・整形・Loki への転送 | `opentelemetry-collector-contrib` イメージ（Loki 向け OTLP エクスポータ・bearertokenauth extension を使うため contrib 版が必要）                          |
| `loki`（3.5.0）                               | ログの永続化・クエリ                 | 単一バイナリ構成（filesystem ストレージ）。保持は 14 日（Compactor の retention）で、それ以前は Blob アーカイブ側にだけ残る                               |
| `grafana`（12.4.10）                          | 可視化・閲覧 UI                      | データソースは Loki のみ。ホストのループバックにのみポート公開                                                                                            |
| `archive-exporter`                            | Loki → Azure Blob の日次コピー       | Compose 常駐。毎日 01:15 UTC に昨日（UTC）分を gzip JSONL で Cool へ。Loki HTTP はホスト非公開のまま `http://loki:3100` を使う                            |

## ディレクトリ構成

```text
infra/ai-logs/
├── README.md                 # このファイル
├── references.md             # 調査メモ
├── docs/
│   ├── client-setup.md        # クライアント側（Claude Code・VS Code・WSL）のセットアップと動作確認
│   └── telemetry-data.md      # Claude Code から何が送られ、本基盤に何が残るか
├── docker/                   # VM にそのまま配置している構成一式
│   ├── docker-compose.yml
│   ├── Caddyfile              # OTLP の TLS 終端（Let's Encrypt の IP 証明書）
│   ├── otel-collector-config.yaml
│   ├── loki-config.yaml     # Compactor の retention（14日）もここ
│   ├── .env.example           # 実体の .env は VM 上でのみ作成（OTLP_AUTH_TOKEN, GRAFANA_ADMIN_PASSWORD, Azure Blob）
│   ├── grafana/provisioning/datasources/datasources.yaml
│   └── archive-exporter/      # 日次エクスポート・ローカル pull/list の正本（just deploy は docker/ しかコピーしない）
│       ├── archive_logs.py
│       ├── Dockerfile
│       └── requirements.txt
├── terraform/                 # さくらのクラウド VM・ディスクの IaC
│   ├── provider.tf
│   ├── variables.tf
│   ├── main.tf
│   ├── outputs.tf
│   └── terraform.tfvars.example   # 実際の値は terraform.tfvars（gitignore対象）
├── scripts/
│   ├── client_env.py          # Claude Code の OTLP 設定を ~/.claude/settings.json（Windows・WSL）へ入れるスクリプト（justfileから呼ばれる）
│   └── archive_azure.sh       # archive-provision / archive-env の az CLI 実体（ローカル実行。VM にはコピーしない）
├── justfile                   # Terraform・VMセットアップ・デプロイ・動作確認のレシピ集
└── .env.example                # ローカル開発マシン用（SERVER_IP, OTLP_AUTH_TOKEN, Azure Blob）
```

## インフラ構成（さくらのクラウド）

- VM: 石狩第1ゾーン（`is1a`）、1 コア / 2GB プラン + SSD 20GB（月額 440円）で稼働中。OS は AlmaLinux 9
  - 2026-11-18 に 1GB（月額 1,540円）から 2GB に上げた。1GB では swap を常時約 400MB 使い、Loki の内部 gRPC に `DeadlineExceeded` が出ていた。`memory` の変更は in-place（`terraform plan` は 0 add / 1 change / 0 destroy）で、VM は停止・起動されるがディスクとグローバル IP はそのまま。サーバーのリソース ID だけ変わる
  - 2GB では kernel の `crashkernel`（2G〜64G で 256M）が効くため、`free` の total は約 1.7GB になる
  - OS 更新: 2026-11-18 に `dnf upgrade -y`（343 件、kernel 5.14.0-741.22.1）→ 再起動。5 コンテナは `restart: unless-stopped` で自動で戻り、`verify-tls` / `verify-otlp-auth` / `verify-client` も通った。openssh-server の更新で sshd が再起動され、`just ssh` 経由で流した `dnf` の SSH セッションは切れる（トランザクション自体は完了する）ので、長い更新は `nohup` か `tmux` で流す
- Terraform provider は `sacloud/sakura`（v3系、Terraform Plugin Framework採用、Terraform 1.11以降が必要。適用時点の実績は 3.12.5）
  - `sacloud/sakuracloud`（v2系）は 2026-12-31 でメンテナンス終了予定（v2/v3に互換性なし）のため、最初からv3を採用した
  - リソース名は `sakuracloud_*` ではなく `sakura_*`（`sakura_server` / `sakura_disk`）。`network_interface` / `disk_edit_parameter` はオブジェクト構文（`= { ... }` / `= [{ ... }]`）
- `disk_edit_parameter.ssh_keys` にSSH公開鍵（専用鍵 `~/.ssh/ai_logs_ed25519`）を直接注入し、`disable_pw_auth = true` でパスワード認証を無効化した状態で構築している（構築後に手動でSSH鍵に切り替える手順は不要）

### リソース使用量の実測値

Loki + Grafana + OTel Collector を 1コア/1GB の VM で同時稼働させる想定に対して、ローカルDocker検証時点で以下を計測している（Phase 1検証時の記録、2026-07-09）:

- Claude Code のみからイベント送信後: 合計 約145MiB（Grafana 58MiB + otel-collector 35MiB + Loki 52MiB）
- Claude Code + Codex CLI 双方からイベント送信後: 合計 約172MiB（Grafana 65MiB + otel-collector 37MiB + Loki 70MiB）

いずれもアイドル起動直後・少数イベント送信直後の値。

継続稼働の実測（2026-11-18、1GB VM で約 2 か月の連続稼働後）: collector 74MiB / grafana 107MiB / loki 81MiB / archive-exporter 1MiB（合計約 260MiB）、volume `loki-data` 816K（保持 14 日が効いている）、ディスク使用 51%（20GB 中 9.4GB）、swap は常時約 400MB 使用。2GB に上げて Caddy を足した直後は caddy 17MiB、swap 0。

## セキュリティ・アクセス制御

- 利用者は自分一人のみを想定
- Grafana: `docker-compose.yml` でポートを `127.0.0.1:3000:3000` にバインドし外部非公開。閲覧するときは `just tunnel` でSSHトンネルを張ってアクセスする
- OTLP 受信エンドポイントは TLS（Caddy）+ `bearertokenauth`。外に出すポートは compose の `ports` で決まり、`80`（ACME の HTTP-01。証明書の取得・更新に要る）と `4318`（OTLP/HTTP over TLS）だけ。Collector の 4317/4318 はホストに公開せず、平文の OTLP は外から届かない。トークンなしのリクエストは 401（`just verify-otlp-auth`）、証明書と平文の拒否は `just verify-tls` で確認できる
- 証明書は Let's Encrypt の IP アドレス証明書（`shortlived` プロファイル、有効 160 時間）。公的 CA なのでクライアント側に CA の設定は要らない（Windows・WSL の Claude Code で確認済み）。Caddy が期限前に自動更新し、VM が長く止まっていても起動時に取り直す。80 番を閉じると更新できなくなる
- gRPC（4317）は公開していない。Claude Code の gRPC エクスポータ（grpc-js）は接続先の IP をそのまま TLS の servername に渡し、Node が `ERR_INVALID_ARG_VALUE` で拒否するため、IP 証明書では使えない。Claude Code は OTLP/HTTP（`http/protobuf`）で送る

## 収集対象ツールのクライアント設定

### Claude Code

- 送信先は `https://<VMのIP>:4318`、プロトコルは OTLP/HTTP（`http/protobuf`）。gRPC は IP 証明書の TLS で失敗するので使わない（上記「セキュリティ・アクセス制御」）
- 変数は `infra/ai-logs/.env`（`SERVER_IP` / `OTLP_AUTH_TOKEN`）から [`scripts/client_env.py`](scripts/client_env.py) が作り、`just env-install` で Windows と WSL の `~/.claude/settings.json` の `env` に入れる。CLI と VS Code 拡張で共有され、起動のされ方に関係なく効くため、Windows のユーザー環境変数や `claudeCode.environmentVariables` は使わない
- 手順・VS Code と WSL の確認・動作確認・トラブルシュートは [docs/client-setup.md](docs/client-setup.md)
- 何が送られ、Loki に何が残るか（イベント・属性・本文の伏せられ方・LogQL の例）は [docs/telemetry-data.md](docs/telemetry-data.md)。Loki でインデックス化されるラベルは `service_name`（`claude-code`）だけで、ほかの属性は structured metadata

### Codex CLI

- 現在は使っていない（参考情報。下の例は TLS 化後の形に直してあるが、TLS 化後の Codex からの送信は未検証）
- `~/.codex/config.toml` に `[otel]` セクションを追加して有効化（環境変数ではなく設定ファイル方式。`client_env.py` の対象外なので手動編集する）

```toml
[otel]
environment = "production"
log_user_prompt = false

[otel.exporter.otlp-http]
endpoint = "https://<VMのIP>:4318/v1/logs"
protocol = "binary"
headers = { "Authorization" = "Bearer <OTLP_AUTH_TOKENの値>" }
```

- **`endpoint` には信号別のパス（ログなら `/v1/logs`）まで明示すること**。Codex CLI 0.143.0で検証したところ、ベースURLのみ（例: `https://<VMのIP>:4318`）だとログエクスポーターがパスを自動付与せず、Collector側で404になり送信に失敗した
- `otel.exporter.<id>.headers`（`map<string,string>`）でカスタムヘッダーを指定できる。キー名は任意なので `Authorization` に `Bearer <token>` を指定すればBearer Token認証に対応できる
- 0.143.0時点でOTLPメトリクスのエクスポート自体は成功する（`HttpMetricsClient.ExportSucceeded`）が、本基盤のCollector設定は `logs` パイプラインのみのため、メトリクスは受信されても破棄される
- Loki に取り込まれた際の `service_name` ラベル値は `codex_exec`（`codex exec` サブコマンドで検証したため。対話モードでの値は未検証）

## 運用

### justfile レシピ一覧

`cd infra/ai-logs` してから実行する（`just -f infra/ai-logs/justfile <recipe>` でリポジトリルートから直接呼ぶことも可能）。

| レシピ                               | 内容                                                                                                                                                                                                                                                                 |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `just tf-init`                       | `terraform init`                                                                                                                                                                                                                                                     |
| `just tf-plan`                       | `terraform plan`                                                                                                                                                                                                                                                     |
| `just tf-apply`                      | `terraform apply`                                                                                                                                                                                                                                                    |
| `just tf-destroy`                    | `terraform destroy`                                                                                                                                                                                                                                                  |
| `just ip`                            | VMのグローバルIPを表示                                                                                                                                                                                                                                               |
| `just ssh`                           | VMにSSH接続                                                                                                                                                                                                                                                          |
| `just provision`                     | VM上にDocker / Docker Composeをインストール（初回のみ、冪等）                                                                                                                                                                                                        |
| `just vm-env`                        | VM上に `/opt/ai-logs/.env` を生成（既存があれば何もしない）                                                                                                                                                                                                          |
| `just deploy`                        | `docker/` をVMへ転送し `docker compose up -d --build`（ローカル `docker/.env` は載せない。VM の `.env` は上書きせず、`SERVER_IP=<VMのIP>` の行だけ書き直す。`loki-config.yaml` / `Caddyfile` だけを変えたときは別途 `docker compose restart loki` / `caddy` が必要） |
| `just archive-provision`             | Azure にアーカイブ用 RG + LRS Cool ストレージアカウント + コンテナを作成（skills-site の SWA 用 RG とは別）                                                                                                                                                          |
| `just archive-env`                   | 既存の VM `/opt/ai-logs/.env` とローカル `.env` へ Blob 接続文字列を追記（`vm-env` は既存ファイルを触らない）                                                                                                                                                        |
| `just archive-export <date>`         | VM 上で 1 UTC 日分をワンショット export（`docker compose run --rm`）                                                                                                                                                                                                 |
| `just archive-backfill [... ]`       | VM 上で既存 Loki ログを日付単位でバックフィル（直列。`--from` / `--to` を渡せる）                                                                                                                                                                                    |
| `just archive-list`                  | ローカルから Blob の日付と `_status` マーカーを一覧                                                                                                                                                                                                                  |
| `just archive-pull <date>`           | ローカルの `temp/ai-logs-archive/` へ 1 UTC 日分を落とす（gitignore 済み。リポジトリにログ本文を入れない）                                                                                                                                                           |
| `just show-secrets`                  | VM上の `/opt/ai-logs/.env` の中身を表示                                                                                                                                                                                                                              |
| `just tunnel [local] [remote]`       | SSHトンネル（既定は Grafana の `localhost:3000`）。例: `just tunnel 3001`（ローカルだけ変更）                                                                                                                                                                        |
| `just stats`                         | VM上の `docker stats --no-stream`                                                                                                                                                                                                                                    |
| `just verify-otlp-auth`              | OTLPエンドポイント（`https://<IP>:4318`）のトークン必須化を確認（401期待。証明書の検証込み）                                                                                                                                                                         |
| `just verify-tls`                    | 4318 の証明書（発行者・期限・SAN の IP）を表示し、4317 が閉じていること・平文 HTTP が Collector に届かない（Caddy の 400）ことを確認                                                                                                                                 |
| `just verify-pipeline`               | 合成ログを `https://<IP>:4318/v1/logs` に送り Loki まで届くかを確認（AI CLI を使わない）                                                                                                                                                                             |
| `just verify-client`                 | 実際の `claude -p` を一時 `--settings` ファイル付きで実行し、送信できたかを debug ログと Loki で確認                                                                                                                                                                 |
| `just verify-grafana-private`        | Grafanaが外部非公開であることを確認                                                                                                                                                                                                                                  |
| `just env-emit [bash\|pwsh]`         | Claude Code用OTLP環境変数をCURRENTシェル向けに出力（一時追加、evalして使う）                                                                                                                                                                                         |
| `just env-install [target] [distro]` | 上記を `~/.claude/settings.json` の `env` にマージ（永続追加。target は `all`（既定）/ `windows` / `wsl`、distro の既定は `Ubuntu-24.04`）                                                                                                                           |
| `just env-purge [target] [distro]`   | 上記のキーを `~/.claude/settings.json` の `env` から削除（パージ。旧方式の Windows ユーザー環境変数も消す）                                                                                                                                                          |

### 初期構築・再デプロイの流れ

1. さくらのクラウド コントロールパネルでAPIキー（アクセスレベル「作成・削除」）を発行
2. `terraform/terraform.tfvars.example` を `terraform.tfvars` にコピーし、`sakura_token` / `sakura_secret` / `ssh_public_key`（`~/.ssh/ai_logs_ed25519.pub` の中身）を埋める
3. `just tf-init` → `just tf-plan` → `just tf-apply` → `just ip` でVMを構築
4. `just ssh` で疎通確認（別ターミナルでパスワード認証が拒否されることも確認）
5. `just provision` でDocker導入 → `just vm-env` で `.env`（`OTLP_AUTH_TOKEN` / `GRAFANA_ADMIN_PASSWORD`）を生成 → `just deploy` で `docker/` を配置・起動
6. `just verify-tls` / `just verify-otlp-auth` / `just verify-grafana-private` / `just stats` で動作確認（初回の証明書取得は数秒。試行を繰り返すときは VM の `.env` に `ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory` を入れて staging で試し、通ったら消して `docker compose up -d caddy`。volume `caddy-data` は消さない）
7. `just tunnel` を張った状態で `just show-secrets` を確認しつつ <http://localhost:3000> にログイン
8. ローカル開発マシン側: [docs/client-setup.md](docs/client-setup.md) の手順で `.env` を用意して `just env-install`（Claude Code 用。Windows と WSL）。Codex CLI は `~/.codex/config.toml` を手動編集
9. アーカイブ: `just archive-provision` → `just archive-env` → `just deploy` → `just archive-backfill` → `just archive-list`（詳細は上記「アーカイブの使い方」）

`docker/` の内容を更新した場合は `just deploy` を再実行するだけでよい（ローカルの `docker/.env` は転送せず、VM 上の `/opt/ai-logs/.env` は上書きしない）。ただし `loki-config.yaml` / `Caddyfile` のような bind mount された設定ファイルだけの変更では Compose がコンテナを再作成しないため、`docker compose restart loki`（または `caddy`）を別途実行する。`archive-exporter` は Dockerfile があるので、スクリプトや依存を変えたあとの `deploy` はイメージをビルドし直す。

### ログの保持期間（ホット 14 日 / コールド Blob）

Loki の保持は `docker/loki-config.yaml` の `compactor` + `limits_config` で行う。

| 設定                                      | 値                | 意味                                                                        |
| ----------------------------------------- | ----------------- | --------------------------------------------------------------------------- |
| `compactor.retention_enabled`             | `true`            | これが `false` だと Compactor は compact のみで、ログは無期限に残る         |
| `compactor.delete_request_store`          | `filesystem`      | `object_store: filesystem` のときの必須値（`shared_store` は 3.5 系に無い） |
| `compactor.working_directory`             | `/loki/compactor` | volume `loki-data` の中。marker をコンテナ再作成で失わない                  |
| `compactor.retention_delete_delay`        | `2h`              | 誤設定の取り消し窓。削除は判定から 2 時間遅れて走る                         |
| `compactor.retention_delete_worker_count` | `4`               | 公式サンプルの 150 は 1コア/1GB の VM には過大                              |
| `limits_config.retention_period`          | `336h`（14 日）   | ホットで保持する期間                                                        |
| `limits_config.max_query_lookback`        | `336h`            | 14 日より前はクエリ自体を返さない（保持外を Grafana から引かせない）        |

注意点:

- **`reject_old_samples_max_age: 168h` は保持期間ではない。** 「168 時間より古いサンプルの新規取り込みを拒否する」設定であり、保存済みログを消さない。両者を混同しない
- **バックフィルが済んでいない状態で `retention_period` を入れない。** まだ Blob に無い過去ログが消える。`just archive-list` で日付と `_status` マーカーが揃っていることを先に確認する
- **`just deploy`（`docker compose up -d`）だけでは Loki は設定を読み直さない。** `loki-config.yaml` は bind mount なのでコンテナ定義が変わらず、Compose が再作成しない。設定を変えたら `just ssh` して `cd /opt/ai-logs && docker compose restart loki`
- 保持の変更は**遡及しない**（[公式](https://grafana.com/docs/loki/latest/operations/storage/retention/)）。有効化前に取り込み済みの 14 日超チャンクは自然には消えないことがある。バックフィル済みであることを確認したうえで、**一度きり**の掃除として次のどちらかを行う
  1. 推奨: Compactor の削除 API（`POST /loki/api/v1/delete`。`retention_enabled` と `delete_request_store` が前提）
  2. 最終手段: volume `loki-data` の作り直し。**未バックフィルの状態では絶対にしない**
- ディスクの減りはすぐには見えない（`compaction_interval` 10 分 + `retention_delete_delay` 2 時間のあとに sweeper が走る）。`just stats` と VM 上の `docker system df` で後追い確認する

有効化の確認（2026-09-17 実施）: 13 日前の窓は `query_range` でログが返り、14 日以上前の窓は 0 件。`just logs` の直近閲覧は従来どおり。

### アーカイブの使い方（Azure Blob）

直近のログは今までどおり Grafana / `just logs` で見る。それより古い分は日付単位の gzip JSONL として Azure Blob（GPv2 / LRS / Cool）へコピーし、必要になったときだけローカルへ落とす。

日付のキーは **UTC**（blob 名 `yyyy/mm/dd.jsonl.gz`）。JST の「昨日」とは 9 時間ずれる。日次ジョブは常駐コンテナが毎日 **01:15 UTC** に昨日（UTC）分を export する（`just deploy` で載る。ホスト cron は使わない）。同じ日の成功後は `_status/yyyy/mm/dd.ok` を置き、マーカーがある日はスキップする（Cool の上書き課金を避ける。やり直すときは `export --force`）。

セットアップ:

1. `az login` 済みであること（skills-site と同じサブスクリプションでよいが、**SWA 用 RG `rg-ai-skill-catalog` には足さない**）
2. `just archive-provision` — RG `rg-ai-logs-archive` + ストレージアカウント + コンテナ `ai-logs`
3. `just archive-env` — VM の `/opt/ai-logs/.env` とローカル `.env` へ接続文字列を追記（`just vm-env` は既存 `.env` を触らない）
4. `just deploy` — `archive-exporter` イメージを VM でビルドして常駐 `serve` を起動
5. `just archive-backfill` — 既存 Loki ログを一度バックフィル（1GB VM 向けに直列。未完了なら保持期間の変更はしない）
6. `just archive-list` で日付とマーカーの差を確認。見るときは `just archive-pull 2026-09-11`（UTC）→ `temp/ai-logs-archive/2026-09-11.jsonl` を `jq` / エディタで開く
7. ここまで揃ってから Loki の保持期間を入れる（上記「ログの保持期間」）

資格情報が無いときは `serve` は exit せずエラーを出して次の 01:15 UTC まで待つ（`restart: unless-stopped` の再起動ループを避ける）。ローカルの `archive-list` / `archive-pull` は `uv run --with-requirements docker/archive-exporter/requirements.txt` 経由で、`azure-storage-blob` を uv がその場で用意する（`.venv` への事前インストールは不要）。

## 既知の注意点・ハマりどころ

- `just env-install` で書いた `~/.claude/settings.json` の `env` は、Claude Code のセッション開始時に読まれる。**実行中のセッション**（開きっぱなしの VS Code の Claude Code、実行中の `claude`）には反映されないので、セッションを開き直す
- Claude Codeの `OTEL_*` 環境変数は、Bashツール・hook・MCPサーバーなど子プロセスには伝播しない（CLI本体のイベントのみ計装対象）
- Codex CLIの `endpoint` は信号別パス（`/v1/logs`）まで明示しないと404になる（上記「Codex CLI」参照）
- 1コア/1GBプランでは `dnf install` の依存解決だけでOOM Killed（exit 137）になることがあるため、`just provision` は先にswapfile（1GB）を作成してから `dnf` を実行する
- Claude Code の OTLP は gRPC だと IP 証明書の TLS で失敗する（`14 UNAVAILABLE ... ERR_INVALID_ARG_VALUE: The property 'options.servername' ...`）。`http/protobuf` で 4318 に送る（上記「セキュリティ・アクセス制御」）
- Caddy の `default_sni` は外せない。IP 宛ての接続は SNI を送らず、コンテナ内のローカルアドレスは compose ネットワークの IP なので、無いと証明書が見つからず TLS ハンドシェイクが失敗する
- `~/.claude/settings.json` の `env` はプロセスの環境変数より優先される。単発で別の送信先を試すときは、環境変数ではなく `claude --settings <一時ファイル>` で渡す（`verify_client.sh` がそうしている）
- PowerShell で `[Environment]::SetEnvironmentVariable(name, $null, "User")` を呼ぶと `$null` が空文字になり、変数は消えずに空値で残る。消すときは `[NullString]::Value` を渡す（`client_env.py` の旧方式ユーザー環境変数の削除）
- OTLPログの属性からLokiラベルへの変換規則はLoki/Collectorのバージョンに依存する。実データで確認したラベルは [docs/telemetry-data.md](docs/telemetry-data.md#loki-での見え方) の通り（`service_name` のみインデックス化）
- `loki-config.yaml` / `Caddyfile` は bind mount なので、それだけを変えたときは `just deploy` だけでは反映されない（`docker compose restart loki` / `caddy` が必要）
- Loki の `reject_old_samples_max_age` は取り込み拒否であり保持期間ではない。保持は Compactor の `retention_enabled` + `retention_period`
- Grafana 12.4.10 では Explore の Loki Label ドロップダウンが選択中の時間範囲に追従する（10.2.3 では範囲を変えても Label が空のまま残ることがあった）

## オープン課題・今後の TODO

- [x] ~~OTLPトークンがTLSなし平文HTTPを流れる~~: 2026-11-18 に Caddy + Let's Encrypt の IP 証明書で TLS 化した（上記「セキュリティ・アクセス制御」）
- [x] ~~メモリプラン変更時の挙動が未検証~~: 2026-11-18 に `memory = 2` へ変更。in-place（ディスク・IP は維持、サーバー ID だけ変わる）だった（上記「インフラ構成」）
- [ ] **継続稼働時のリソース使用量**: 2026-11-18 に約 2 か月分を実測した（上記「リソース使用量の実測値」。ディスクは 14 日分で `loki-data` 816K）。2GB + Caddy の構成での長期の推移は `just stats` で引き続き見る
- [x] ~~保持有効化前に取り込んだ 14 日超チャンクの一度きりの掃除~~: 2026-11-18 に確認し、`/loki/chunks` に 15 日超のチャンクは残っていなかったので対処不要
- [ ] （任意・将来検討）さくらのクラウドのパケットフィルタ（`sakura_packet_filter` / `sakura_packet_filter_rules`）によるソースIP制限は追加の防御層として有効だが、自宅IPが動的な場合は効果が限定的なため未導入。入れる場合も 80 番（ACME の HTTP-01）は Let's Encrypt の検証元が不定なので全開放のままにする
- [ ] GitHub Copilot対応要否を再検討する
