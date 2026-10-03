# アニメ化速報：公式確認済みニュースの配信

旧RSS・正規表現方式を廃止し、**外部で公式確認したニュースをDiscordへ届け、Xには手動で投稿する**構成です。既存の `DISCORD_WEBHOOK_URL` はそのまま使います。Webhookの作り直し・変更は不要です。

**初期状態は投稿無効・ドライランです。このリポジトリ単体ではニュースを発見・公式確認しません。** 外部のAIアシスタント／編集担当が一次情報を読み、時刻・新規性・対象範囲・重複を確認して、承認済みJSONを投入します。GitHub ActionsにLLMやRSS収集を装うコードはありません。未確認の記事が「approved」と書くだけで事実になるわけではありません。

**最初はDiscordでフォーマットを整え、カード内の「X投稿用（手動コピー）」を確認してから運用者がXへ投稿します。X API運用はアカウントと収益が安定した段階で改めて判断します。収益条件を検知して自動で切り替える処理はありません。**

## 何を送るか

- A：新規アニメ化、続編、アニメ映画の制作決定
- B：初出のPV・ビジュアル、放送／配信／公開日、追加キャスト、スタッフ、主題歌、制作会社、正式タイトル、延期
- 除外：グッズ、イベント、再告知、カウントダウン、通常回の予告、原作書籍の発売、噂・希望・未確認情報
- 同じ発表でPV・ビジュアル・キャストなどが同時解禁された場合は1件にまとめる
- **公式の最初の公開時刻が、投入時と送信直前の両方で過去1時間以内**であること。JSTを含む明示的なタイムゾーン付き日時を使い、内部比較はUTCで行う
- ニュースサイトの後追い時刻、検索に出た時刻、記事の更新時刻を初出時刻に置き換えない。日付だけ・公開時刻不明の情報は送らない
- GitHubの遅延やAPIの制限により1時間を超えたニュースは失効し、後からまとめて送らない

## 全体の流れ

1. 外部のAIアシスタント／編集担当が公式サイト・制作会社・出版社・公式X等の一次情報を確認する
2. 作品・シーズン固有のキーと発表固有のキーを決め、既存キュー、配信履歴、既存のX／Discord投稿と突き合わせる
3. 根拠、元の公開日時、確認日時、確認済み事実、**承認済み本文と必要に応じたメディア情報**をJSONにする
4. 権限を持つ担当者が `queue/events/<event_id>.json` をレビューし、mainへ追加する
5. mainのキュー変更pushまたは15分間隔のキュー消化で検証する。公開実行を有効にした場合だけ送信する
6. 宛先ごとの送信直前・直後にGitへ状態を保存する

GitHubの現在の接続ツールからworkflow_dispatchを呼べない場合でも、承認したキューファイルのpushで起動できます。状態保存に使う `GITHUB_TOKEN` のpushは通常ワークフローを再起動しません。スケジュールは厳密な定刻を保証せず、長期無活動の公開リポジトリでは停止する場合があります。外部の収集担当の定期起動は別途必要です。

## ローカル確認（外部投稿なし）

Python 3.12 / Linuxを使用します。ファイルロックはLinuxの `fcntl` を使うため、Windows単体ではなくWSLまたはActionsで実行してください。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q bot.py news_delivery tests
python bot.py --dry-run --mode both
```

引数なしもドライランです。ドライランはAPIアクセス・送信・状態保存をせず、XやDiscordの秘密情報を必要としません。`--test-post` は廃止しました。架空ニュースを実チャンネルに投稿する動作確認はありません。通常の単体テスト内のニュース・資格情報は架空です。専用の表示テストでは、過去の実在する公式PVを明確に「動作確認」と表示して使います。

## Discordの画像・動画付きカード

新しいイベントに任意の `media` 配列を指定すると、**作品名 → 短い新情報 → 発表区分 → 公式初出（JST）→ 出典**のカードになります。`media: []` なら画像なしカード、`media` と `manual_x_text` を両方省略した既存JSONは従来の本文だけの送信を維持します。`manual_x_text` に公式URL入りのX向け承認文を入れると、同じカード内の「X投稿用（手動コピー）」欄にコピー用の本文が付きます。X標準280文字の保守的な重みで検証し、切り詰めません。この欄はXへ送信しません。

- 画像は最大1枚。公式一次ソースと**ホスト名が完全一致する元画像URL**のみ。PNG/JPEG/WebP/GIFに限定し、明示的な埋め込み許可と根拠を確認した場合だけ表示します。公式公開＝自由な転載許可とは扱いません
- 動画・投稿は公式YouTube動画1件と公式X投稿1件まで。同一発表との関係、公式投稿者の根拠、対応する一次ソースを記録します。転載チャンネル・切り抜き・ミラーは使いません
- YouTube／XのURLはカード外にそのままリンクとして置き、Discordの標準プレビューを利用します。追跡情報は除き、YouTubeはwatch URL、Xはx.comへ正規化します。X標準の動画コピーURL（`/status/投稿ID/video/番号`）も受け付け、動画番号付きのリンクを保持します。重複判定では元投稿と同じIDにまとめます。同じ原典URLを本文と出典欄に重ねません（全文コピーに必要な手動Xコピー欄は除く）
- Webhookの `video` フィールドは指定できません。YouTubeの埋め込み制限、削除・非公開化、Discord設定やプレビュー取得状態などにより、画像・動画が展開されない場合があります。**特にXのDiscord内再生を保証しません**。その場合も公式URLから原典を開けます
- 画像のホットリンクが表示されなくても出典リンクを残します。別ホストの公式CDNや署名付き画像は現在受け付けません。その場合は画像指定を省き、公式サイト／公式Xへのリンクを使ってください
- メディアのダウンロード、動画・画像の再アップロード、fx/vx等の第三者プレビューサービスは使いません。メディアURLの追加だけで再投稿されることはなく、登録後の素材差し替えは台帳照合で停止します

表示イメージ（架空例。実際に送信しません）:

```text
公式YouTube
https://www.youtube.com/watch?v=TEST_ONLY01

公式X
https://x.com/OfficialTest/status/1234567890

[カード]
テスト用の架空作品 第2期
第2期の制作が決定。新PVが公開されました。
発表：続編決定 / 新PV
公式初出（JST）：2026/10/03 11:30:00 JST
出典 1：https://official.example.jp/news/20261003-new/
[許可を確認した公式画像1枚]
X投稿用（手動コピー）：
架空作品の第2期制作が決定。新PVが公開されました。
https://official.example.jp/news/20261003-new/
```

入力の詳しい例は [メディア契約](docs/event-contract.md#任意のmedia画像動画引用) と [未承認のメディア付きテンプレート](docs/event-media-template.json) を参照してください。テンプレートのURL・権利根拠は架空の説明用です。実在ニュースの確認や権利確認の代わりに流用しないでください。画像・動画の見え方は模擬HTTPテストでは検証できないため、本番を有効にする最後の段階で別途確認します。

## 承認後の一度だけのDiscord表示テスト

一般ニュースの送信を有効にせず、`Discord one-time format test`（`.github/workflows/discord-smoke.yml`）で選択した固定の動作確認メッセージを1件だけ送れます。**送信先は既存の `DISCORD_WEBHOOK_URL` です。各ケースについて、先に対象チャンネルと1件送ることを承認してください。** 新しいワークフローはdefault branchへ反映されてから手動実行できます。

- Actions → Discord one-time format test → Run workflowを開く
- default branchと `test_case` を選び、`confirmation=preview_only`（初期値）で秘密情報・通信・投稿・状態保存なしの検証を実行できる
- `test_case=youtube`（初期値）の送信確認は `confirmation=send_one_format_test`。従来の固定ID `discord-format-test-20261003-v1`、本文、送信履歴は保持する。本文は「動作確認・ニュース速報ではありません」で、2026-04-30の[アニプレックス公式記事](https://www.aniplex.co.jp/news/detail/?id=70322)と[公式YouTube PV](https://www.youtube.com/watch?v=UmVTrrDVYV4)を過去の表示例として使う
- `test_case=x_video` の送信確認は `confirmation=send_one_x_video_test`。別の固定ID `discord-x-video-test-20261003-v1` で[指定のX動画コピーURL](https://x.com/hirayasumi0426/status/2104707978460803552/video/1)を1回だけ送る。「動作確認・過去の投稿・ニュース速報ではありません」と明示し、カード外リンクと手動コピー欄の `/video/1` を保持する。作品名、公開日時、公式性は新たに推定しない
- `test_case=x_link_only` の送信確認は `confirmation=send_one_x_link_only_test`。固定ID `discord-x-link-only-test-20261003-v1` で、同じ指定X動画URLだけを1回送る比較テスト。本文はURLと完全一致し、説明文、独自カード、別リンク、プレビュー抑制フラグは付けない。前2ケースの履歴は保持し、この比較投稿にも別途承認が必要
- ケースと確認値が一致し、テスト・ドライランが成功した場合だけ送信する。任意のURL、本文、IDを入力する機能はない。画像・動画の取得や添付、Xへの投稿は行わない
- `data/discord-smoke-state.json` に選択ケースの送信予約をリモート保存してからPOSTし、結果も保存する。他ケースの履歴は変更しない。送信済み・送信待ち・結果不明・拒否・429のどの状態でも同じ固定IDを再送しない。再度実行しても選択ケースの既存結果を報告するだけ
- 欠落・破損した台帳は初期化せず停止する。台帳削除、IDの付け替え、強制pushで再試行しない。送信後の保存失敗も、確認できた応答IDを報告して再送せず保留する
- ログと台帳に残るのは固定ID、状態、ペイロードハッシュと確認できた数値メッセージ／チャンネルID等。Webhook値、HTTP本文や認証情報は出力しない。guild IDも応答にあればメッセージへのリンクを出す。画像・動画の実際の再生はDiscord側の表示を別途確認する

通常ニュースの `DELIVERY_LIVE_ENABLED` やX設定は変更しません。ニュースの鮮度検証を緩めたり、架空ニュースを承認キューへ入れたりするテストではありません。

## 初回切り替え手順

この手順は運用者の承認後に行ってください。コードを作っただけでは設定変更も公開送信もされません。

1. 旧版の `data/seen.json` を**最新mainのまま保存**する。新しいブランチの古いコピーで上書きしない
2. Draft PRのコード・テスト・ワークフロー変更をレビューする。マージすると旧RSS収集は止まり、承認キュー方式へ切り替わる
3. `DELIVERY_LIVE_ENABLED` は未設定／`false`のままにする。新しい `data/delivery-state.json` は初回だけ空の台帳を採用し、以後リセットしない
4. キュー投入者とコード変更者を限定する。mainのbranch protection/rulesetでレビューを必須にし、CODEOWNERSを有効にすることを推奨。CODEOWNERSファイルだけでは強制されない。設定変更はこのコードでは実施しない
5. Actions → Reviewed anime news delivery → Run workflowを `dry_run=true` で実行し、検証成功を確認する
6. 現在の配信ワークフローはDiscord専用です。XのAPIキー等を渡さず、`X_ENABLED` が存在しても参照しません。既存Secret `DISCORD_WEBHOOK_URL` の名前・値は変更しない。既存Webhookの実際の送信先は運用者が確認する
7. 承認された運用開始時にActions Repository variable `DELIVERY_LIVE_ENABLED=true` を設定する。この設定後は承認キューのpush・スケジュール実行が実投稿になる。手動実行はさらに `dry_run=false` が必要
8. Git台帳へのpushが拒否される場合は送信前に止まる。強制pushや台帳初期化で回避しない。保護ルールと書込権限を運用者が確認する
9. Xへの投稿はDiscordカードのコピー欄を使って手動で行う。手動投稿の成功や重複をこのボットが自動検出するわけではないので、投稿済みか、公式初出からまだ1時間以内かを運用者が確認する（コピー後の経過時間はボットから制御できない）

公開リポジトリなので、キュー・本文・根拠・状態・照合メモは公開されます。秘密、非公開の下書き、個人情報を入れないでください。WebhookやAPIキーをチャット、コミット、ログ、Issue、PRへ貼らないでください。

## 将来のX API運用（現在は使用しない）

既存のXアダプターは将来用として残していますが、現在の配信ワークフローは `--mode discord` に固定され、X資格情報を渡しません。今はAPI申込・設定は不要です。以下は将来あらためて導入を決めた場合の参考であり、このPRでの有効化手順ではありません。API運用には別途レビュー済みのワークフロー変更と予算・投稿範囲の承認が必要です。

実装は公式X APIのOAuth 1.0a User Contextを使います。App-only Bearer Tokenやブラウザーのログイン状態では代用しません。

- GitHub Secrets：`X_API_KEY`、`X_API_KEY_SECRET`、`X_ACCESS_TOKEN`、`X_ACCESS_TOKEN_SECRET`
- 将来必要になるアカウント照合値：`X_EXPECTED_USER_ID`（@animeka_fast の確認済み数値ID）。`X_ENABLED` 変数だけでは現在のワークフローは有効になりません
- 投稿権限を持つ既存のDeveloper App・ユーザーAccess Tokenが必要。Read/Writeが対象で、DM権限は要求しません
- 送信前に `GET https://api.x.com/2/users/me` を呼び、数値IDとユーザー名 `animeka_fast` が両方一致した場合だけ `POST https://api.x.com/2/tweets` を使います
- 本実装はトークン発行、OAuth同意、権限拡張、支払い・APIプラン変更を行いません。必要なら所有者が公式Developer Consoleで内容と費用を確認し、許可された安全な入力方法で設定してください
- APIの利用可否・料金・利用枠はアカウントの契約を確認してください。無料とは保証しません。2026-10-03確認の[公式料金表](https://docs.x.com/x-api/getting-started/pricing)では通常の投稿作成はUSD 0.015、URL付きはUSD 0.200/回です。このニュース本文は出典URLを必須にしているため、投稿作成分だけで1日5/10/20件なら30日あたり約USD 30/60/120となります。本人確認などの読取料金・税等は別です。料金は変わるため、API設定を行う最後の段階で最新の単価と上限予算を再確認してください。自動購入や自動チャージは設定しません
- 返信、DM、いいね、フォロー、重複した宣伝投稿、ブラウザー自動操作、画像の取得・転載はありません
- X本文は承認後に切り詰めません。URLは23、その他は安全側に1文字2として280以内を検証します。英語本文等を本来の制限より早く拒否することがあります

Discordだけで送信した記事をXに後から一括投稿しません。JSONで意図した宛先と各宛先の状態を分け、Xが利用可能になっても公式初出から1時間を超えた分は失効します。現在のテンプレートは `destinations: ["discord"]` に固定し、X向け文は `manual_x_text` に分離しています。Xの手動投稿履歴は配信台帳に「X送信済み」として記録しません。

## 台帳・障害時の動作

`data/delivery-state.json` を全履歴の台帳として保持します。自動削除・90日リセットはありません。

| 状態 | 意味・対応 |
| --- | --- |
| ready | まだ送っていない。429で「未送信」が確定した場合も次回時刻付きでここへ戻す |
| pending | リモートGitへ送信予約を保存済み。POSTの直前から完了保存までの状態 |
| sent | APIから確認できたメッセージ／ポストIDを保存済み。同じ宛先へ再送しない |
| uncertain | タイムアウト、5xx、IDのない成功応答、途中終了。投稿済みの可能性があるため自動再送しない |
| blocked | 認証・権限・入力エラーや最大試行回数。原因を確認する |
| legacy_hold | 旧履歴に対応する候補があるが、送信済みとは証明できない。手動照合する |
| expired | 公式初出から1時間超。送らない |

- 同じイベントのDiscord成功・X失敗を分けて管理します。Discordを巻き戻したり再送したりしません
- 429だけはRetry-After／x-rate-limit-resetを尊重し、最低5分後の次回実行に回します。同一宛先は最大3試行。過去1時間の条件を超えたら失効します
- HTTPリダイレクトは拒否します。URLやHTTP例外に含まれる秘密情報を出力しません
- 送信予約の保存／pushが失敗・競合・不明になれば、送信せず終了します。送信後の保存失敗ではリモートに予約が残り、次回 `uncertain` になります
- GitHub concurrency、ローカルロック、最新リモート照合、通常のfast-forward pushを組み合わせます。force pushや自動rebaseで台帳競合を解消しません
- Exactly-onceの保証はできません。APIが受付後に切断すると判定できないため、**重複を避ける代わりに保留し、手動確認を求める**設計です
- 問題のある宛先を再送せず、独立した宛先／イベントは処理できます。live実行は保留・失敗があると失敗ステータスになり、Actionsに残ります。外部の障害通知先を勝手に追加しません

### uncertain / legacy_hold の照合

1. 運用者が該当時間のDiscord履歴またはXプロフィールを読み、同一本文・公式リンクの投稿を確認する。見つからないだけで未送信と断定しない
2. 最新mainを取得し、同時に配信しない状態でローカル台帳修正を作る（このコマンドは送信・pushしない）

```bash
python bot.py reconcile EVENT_ID discord sent \
  --reviewer OPERATOR --evidence '公開してよい確認根拠・メッセージURL等' \
  --remote-id CONFIRMED_NUMERIC_MESSAGE_ID
```

明確に未送信と確認した場合だけ `sent` を `not-sent` に変え、`--remote-id` を省きます。確認者と根拠を監査欄に保存します。差分レビュー後に通常の方法で反映してください。これでも初出1時間の制限は延長されません。送信済み台帳や承認JSONを編集して訂正投稿を装うことはできません。訂正は別途レビュー・承認が必要です。

## 旧履歴の扱い

旧 `data/seen.json` は変更せず、そのまま読んで移行時の衝突確認に使います。旧 `seen` は「取得した」だけの記事も含むため、送信済みという証拠にはなりません。旧URLハッシュ／旧制作決定ストーリーキーと一致したものは両宛先を `legacy_hold` にし、Xへ送信済みだったことにはしません。

旧ハッシュから元のURL・全投稿本文・同一発表を復元できません。URLや作品表記の違いは取りこぼし得るので、切り替え時は外部編集担当が既存投稿も読み、キーと重複を照合してください。旧履歴を消せば安全になる、完全移行できた、という扱いはしません。

## ファイル構成

- `bot.py`：互換エントリーポイント（デフォルトはドライラン）
- `news_delivery/schema.py`：厳密な入力・日時・対象区分の検証
- `news_delivery/publisher.py`：重複グラフ、宛先別配信、時刻制限、照合
- `news_delivery/state.py`：台帳の検証、原子的保存、リモート事前保存、旧履歴確認
- `news_delivery/transport.py`：Discord Webhook／公式X API
- `news_delivery/discord_media.py`：公式メディアURLの検証とDiscordカードの組み立て（ネットワークアクセスなし）
- `news_delivery/smoke.py`：固定1件・手動確認専用の表示テスト
- `data/discord-smoke-state.json`：一度限りの表示テストの消去しない台帳
- `queue/events/`：承認済みイベントのみ。初期状態は空
- `docs/event-contract.md`：外部の確認担当が作成する入力仕様
- `docs/event-template.json`：**送信できない未承認テンプレート**
- `.github/workflows/ci.yml`：PRで秘密情報を渡さずテスト。pull_request_targetは使わない
- `.github/workflows/monitor.yml`：default branchの承認キュー配信

## 公式資料（2026-10-03に確認）

- [X：投稿API](https://docs.x.com/x-api/posts/manage-tweets/introduction)
- [X：ログインユーザー確認](https://docs.x.com/x-api/users/get-my-user)
- [X：OAuth 1.0aのリクエスト認証](https://docs.x.com/fundamentals/authentication/oauth-1-0a/authorizing-a-request)
- [X：レート制限](https://docs.x.com/x-api/fundamentals/rate-limits)
- [X：自動化ルール](https://help.x.com/en/rules-and-policies/x-automation)
- [Discord：Webhook実行、waitと応答](https://docs.discord.com/developers/resources/webhook#execute-webhook)
- [Discord：メッセージとEmbedの上限](https://docs.discord.com/developers/resources/message#embed-object)
- [Discord：リンクの自動展開設定](https://support.discord.com/hc/en-us/articles/206342858--How-do-I-disable-auto-embed)
- [YouTube：埋め込みの許可・制限](https://support.google.com/youtube/answer/171780?hl=ja)
- [X：公式投稿の埋め込み](https://help.x.com/ja/using-x/how-to-embed-a-post)
