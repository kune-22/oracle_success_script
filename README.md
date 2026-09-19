# OCI A1 capacity retry notifier

Windowsタスクスケジューラから1回ずつ実行し、Resource Managerスタックを適用します。`Out of host capacity` で失敗した場合だけ、次回のスケジュールで新しい適用を試みます。常駐ループではありません。

## GitHub Actions

`.github/workflows/oci-retry.yml` がActions用のworkflowです。`workflow_dispatch`で手動起動でき、定期実行はUTCの17分・47分です。成功すると`success.marker`を保存し、このworkflow自身を無効化します。容量不足の場合だけ次回の定期実行へ進みます。GitHub Actionsのスケジュールは混雑時に遅れることがあります。

### Secrets

リポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret` から登録します。値はSecretsの入力欄に貼り付け、READMEやIssueには書きません。

| Secret名 | 入れる値 | 確認場所 |
| --- | --- | --- |
| `OCI_STACK_ID` | Resource ManagerスタックのOCID (`ocid1.ormstack...`) | OCIコンソール → Developer Services → Resource Manager → Stacks → 対象スタック |
| `OCI_TENANCY` | テナンシーOCID (`ocid1.tenancy...`) | OCIコンソール右上のプロフィール → Tenancy → OCID |
| `OCI_USER` | APIを実行するユーザーのOCID (`ocid1.user...`) | Identity & Security → Users → 対象ユーザー → OCID |
| `OCI_FINGERPRINT` | APIキーのフィンガープリント | 対象ユーザー → API keys → 登録済みキー |
| `OCI_REGION` | リージョン識別子（例 `ap-tokyo-1`） | OCIコンソールのリージョン選択、または`.oci/config` |
| `OCI_PRIVATE_KEY` | OCI APIキーの秘密鍵PEM全文 | `oci setup config`で生成した秘密鍵ファイル |
| `OCI_PRIVATE_KEY_PASSPHRASE` | 秘密鍵を暗号化した場合のパスフレーズ。暗号化していなければ空欄 | 秘密鍵作成時に設定した値 |
| `SMTP_HOST` | SMTPサーバー名。Gmailなら `smtp.gmail.com` | メールサービスの仕様 |
| `SMTP_PORT` | SMTPポート。通常 `587` | メールサービスの仕様 |
| `SMTP_USER` | SMTPログインユーザー | メールサービスの仕様 |
| `SMTP_PASSWORD` | SMTPパスワード。Gmailはアプリパスワード | メールサービスの仕様 |
| `MAIL_FROM` | 送信元メールアドレス | SMTPアカウント |
| `MAIL_TO` | 成功通知の宛先 | 通知を受けたいアドレス |
| `MAIL_SUBJECT` | 任意。省略時は `OCIインスタンス作成成功` | 任意 |

`OCI_PRIVATE_KEY`には公開鍵ではなく秘密鍵を入れます。OCIのAPIキーは、ユーザーのAPI Keys画面に公開鍵を登録し、その対になる秘密鍵をSecretへ登録してください。[Oracleの設定仕様](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm)

### 初回起動

Secrets登録後、GitHubの `Actions` → `OCI capacity retry` → `Run workflow` → `Run workflow` の順に押します。`Configure OCI CLI`と`Run retry script`が成功し、OCI Resource ManagerにApply Jobが作成されれば起動成功です。成功後はworkflowが自動的に無効化されます。

## 準備と実行

1. Python 3.10以降とOCI CLIをインストールし、`oci setup config`で認証を設定します。
2. `.env`がまだない場合のみ、`.env.example`をコピーして設定します。既存の`.env`はそのまま利用できます。
3. タスク実行ユーザーから `oci` にPATHが通り、OCI認証設定を読めることを確認します。
4. Gmailではアプリパスワードを設定します。SMTPは証明書検証付きSTARTTLSを使用します。

```powershell
cd C:\Users\Norar\Documents\Codex\oracle_success_script
python .\oci_retry_notify.py
```

このコマンドは実際にスタックを適用します。適用はTerraform構成全体に作用し、構成に応じて既存リソースの変更や削除もあり得ます。成功判定は適用ジョブの `SUCCEEDED` です。インスタンスの稼働やIPはOCIコンソールで確認してください。

`.env`を自動読み込みします。同名のプロセス環境変数がある場合はそちらを優先します。追加Pythonパッケージは不要です。`OCI_CLI_PROFILE`も利用できます。

## タスクスケジューラ

例として08:00、13:00、22:00のトリガーを設定します。

- プログラム: インストール済みPython実行ファイルの絶対パス
- 引数: `"C:\Users\Norar\Documents\Codex\oracle_success_script\oci_retry_notify.py"`
- 開始場所: `C:\Users\Norar\Documents\Codex\oracle_success_script`
- 「タスクが既に実行中の場合」は「新しいインスタンスを開始しない」
- 実行時間制限は監視時間（既定30分）とCLI処理時間に余裕を加えた値にする

失敗時に短時間で繰り返す設定は不要です。次の定刻実行で再開します。既存タスクや実行時刻はこの更新では変更していません。

## 動作と保存ファイル

- `retry-state.json`: 作成要求、ジョブID、成功・通知状態。監視タイムアウトや通信失敗後も同じジョブを追跡します。
- `success.marker`: 適用成功時、メール送信前に保存します。メール失敗後は次回に通知だけを再試行します。旧版のマーカーも尊重します。
- `stop.marker`: 容量不足以外のジョブ失敗やキャンセルで保存します。手動作成でも処理を停止できます。実行中のOCIジョブ自体はキャンセルしません。
- `last-job.log`: 最後に取得した失敗ジョブのログ。機密情報が含まれる可能性があるため共有前に確認してください。
- `retry.lock`: 同一フォルダからの同時実行をOSロックで防ぎます。終了・強制終了でロックは解放されますが、ファイルは残ります。削除しないでください。

別の既存ジョブが動いている場合は適用を見送ります。別PCや別フォルダ、手動操作との同時実行まではロックできません。容量判定はログの `Out of host capacity` という文字列に基づきます。複数エラーが混在する場合の厳密な分類は行いません。

作成要求の送信前に一意のジョブ名を保存します。応答が不明な場合は次回に同じ名前のAPPLYジョブを検索します。見つからない、または複数ある場合は、新しい適用を作らず終了コード1で停止します。通信回復後の次回実行で再照合します。

SMTP受付直後にプロセスが停止した場合など、メールの厳密な一度限りの配信は保証できません。通知が重複しても、適用は再実行しません。

## 問題発生時と再開

まずタスクを一時無効にし、実行中のスクリプトがないことを確認してください。`.env`は削除しないでください。

- メール失敗: SMTP設定を修正し、通常どおり再実行します。状態ファイルは残します。
- 監視タイムアウト・ログ取得失敗: 状態ファイルを残して再実行します。
- `stop.marker`: OCIコンソールと`last-job.log`で原因を確認します。失敗原因を修正し、新規適用を再開する場合は、対象ジョブが終了済みであることを確認して`stop.marker`と`retry-state.json`を退避します。手動停止した未完了ジョブを引き続き追跡する場合は`stop.marker`だけを取り除きます。
- 作成結果が未確定: コンソールで保存されたジョブ名を検索します。作成されていないと確認できた場合だけ`retry-state.json`を退避して再試行します。成功済みジョブがあるのに状態ファイルを消さないでください。
- 成功後に意図して新しく適用する場合: 対象スタックを確認し、`success.marker`と`retry-state.json`の両方を退避します。
- スタック変更: 古いジョブが終了済みであることを確認し、マーカーと状態ファイルを退避してから`OCI_STACK_ID`を変更します。

終了コード: `0` = 成功・停止済み・実行中につきスキップ、`1` = 設定/通信/監視/通知/未確定エラー、`2` = 容量不足（次回再試行）、`3` = その他のジョブ失敗（要確認）。

## オフラインテスト

```powershell
python -m unittest -v test_oci_retry_notify.py
```

テストは一時フォルダとモックを使い、OCIへの実際の適用やメール送信を行いません。

仕様参照: [Oracle Apply Job](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/create-job-apply.htm)、[OCI CLI](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/resource-manager/job/create-apply-job.html)。
