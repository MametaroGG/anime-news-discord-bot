# v2 情報源拡張について

既存の3本に加えて、コミックナタリー、アニメ！アニメ！、アニメイトタイムズ、MANTANWEB、ORICON NEWS、PR TIMES、集英社、講談社、小学館、KADOKAWA、スクウェア・エニックスを対象にした **Googleニュースのサイト限定RSS検索** を11本追加しました。これらのサイトを直接スクレイピングするものではなく、Googleニュースに反映された記事だけが対象です。掲載・反映には遅延や欠落があります。

同じ作品名が『』または「」で明記され、発表区分が同じ場合、記事URLが違っても重複投稿を抑えます。異なる表記・引用符なし・同名作品などは完全には判定できません。AIや公式確認は行いません。初回の新フィード追加後、6時間以内の記事は投稿対象になり得ます。必要なら先にドライランしてください。

**更新方法**: 既存の `data/seen.json` は絶対に上書きせず、`feeds.json`、`bot.py`、`tests/test_bot.py`、`README.md` をGitHub上で更新してください。`.github/workflows/monitor.yml` と Secrets は変更不要です。

---

# アニメ化速報 Discord Bot（RSS版）

**無料構成：GitHub Actions + RSS + Discord Webhook。Grok / xAI APIは不要です。**

## 導入

1. GitHubでリポジトリを作成し、このフォルダの中身をアップロード（`.github` フォルダも含める）。
2. Discordで非公開テストチャンネルを作り、「チャンネル設定 → 連携サービス → ウェブフック」でWebhook URLを取得。
3. GitHubリポジトリの `Settings → Secrets and variables → Actions → New repository secret` で、名前 `DISCORD_WEBHOOK_URL`、値にWebhook URLを登録。**URLはソースコードやチャットに貼らない。**
4. `Settings → Actions → General → Workflow permissions` で `Read and write permissions` を選択（組織ポリシーで不可の場合は別途状態保存方式が必要）。
5. `Actions → Anime news RSS monitor → Run workflow` で `test_post=true` を指定し、Discordへのテスト投稿を確認。
6. `test_post=false` で実行。初回は既存記事を既読登録するだけで、投稿しない。
7. 以後15分間隔（毎時03/18/33/48分 UTC）でRSSを確認。GitHubの混雑時は遅延・欠落の可能性あり。

## 重要な制限

- デフォルトの `feeds.json` はGoogleニュースのRSS検索を使用します。**出版社の一次ソースRSSではありません**。公式発表の検証は未実装なので、Discord上の投稿は「候補」と明示します。一般公開前に一次ソース照合を追加してください。
- **X検索・Grok・公式画像の取得や転載は未実装**。画像は権利確認が必要です。
- 初回は既読登録だけです。新規の投稿だけが通知されます。
- RSSに公開日時がないもの、6時間より古いものは安全のため投稿しません。
- 重複防止は正規化した記事URL単位。同一発表が別サイトから出る場合は複数投稿され得ます。
- GitHub Actionsの `schedule` は厳密な定刻実行ではありません。公開リポジトリは60日活動がないと自動停止する場合があります。
- `data/seen.json` をGitHub Actionsがcommitして履歴を維持します。GitHubのリポジトリ履歴にニュースURLのハッシュが残ります。
- 投稿先Webhookの秘密情報はGitHub Secretsに登録。漏れたらDiscordで再発行してください。

## ローカル実行

```powershell
py -m pip install -r requirements.txt
$env:DISCORD_WEBHOOK_URL = 'DiscordのWebhook URL'
py bot.py --test-post
py bot.py --dry-run
py bot.py
py -m unittest discover -s tests -v
```

## ファイル構成

- `bot.py`：RSS取得、キーワード判定、Discord投稿、既読管理
- `feeds.json`：監視RSS一覧。必要に応じて追加・変更可能
- `data/seen.json`：初回既読と投稿済み記事の状態
- `.github/workflows/monitor.yml`：15分間隔の自動実行
- `tests/test_bot.py`：簡単なユニットテスト

## 本番公開に向けて

出版社・作品の一次ソースを検証する処理、同一作品・同一発表の横断重複排除、画像使用許諾管理、エラー通知を追加してください。
