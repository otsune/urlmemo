# X/Twitter 投稿の取り込み

## 背景

通常 URL は `web_extract`（Firecrawl）で取得するが、**X/Twitter は Firecrawl 非対応**で
取得試行が 504 になる。公式 CLI `xurl read` も認証が無いと 401。
そこで urlmemo は X 投稿を **syndication API（埋め込みウィジェット用・無認証）** で取得する。

## 取得方法（主経路: syndication API）

`scripts/fetch_x.py` が担当。無認証・標準ライブラリのみで動作する。

```
GET https://cdn.syndication.twimg.com/tweet-result?id=<TWEET_ID>&lang=ja&token=<token>
```

- `token` は `fetch_x._token_candidates()` が react-tweet 方式の派生値 →`a`→`0` の順で試行。
- 返り JSON から本文（長文は `note_tweet` 全文）、作者 `@screen_name`、`created_at`、
  メディア URL、引用ツイートを Markdown 化（`render_markdown`）。`t.co` は展開 URL に置換。
- SSL 検証は維持したまま、`certifi` があればその CA バンドルを使う。

対応ホスト: `x.com` / `twitter.com` / `mobile.*` / `vxtwitter.com` / `fxtwitter.com` /
`fixupx.com` / `fixvx.com` / `nitter.net`。
ID 抽出パターン: `/status/<id>`、`/statuses/<id>`、`/i/web/status/<id>`、純粋な数字 ID。

## 取り込みフロー

X URL は `save_article.py` / `ingest_url.py` が自動判定し、`web_extract` を経由せずに
`fetch_x.fetch_x_markdown(url)` で本文を得て保存する。ツイートは短いので要約は付けず、
本文を `## Raw` セクションに UNTRUSTED マーカーで包んで保存する。タイトルは
`@screen_name: 本文先頭40字`。

```bash
# 単体確認
python3 scripts/fetch_x.py https://x.com/jack/status/20
python3 scripts/fetch_x.py https://x.com/jack/status/20 --json   # 生 JSON
```

## X Article（長文記事）

ツイートが X Article の場合、syndication は `article`（タイトル・プレビュー・cover・rest_id）
までしか返さず **全文は含まない**。`render_markdown` は article 検出時に `## X Article`
（タイトル＋プレビュー＋記事URL）を出力する。

### 全文取得（主経路: xurl 公式 API・自動）

`save_article.py` が、本文に `## X Article` があり **xurl が認証済み**のとき自動で:

```
xurl "/2/tweets/<id>?tweet.fields=article&expansions=author_id"
  → data.article.plain_text（記事全文）＋ entities.code（コードブロック）＋ title
```

を取得し（`fetch_x.fetch_article_via_xurl` / `render_article_markdown`）、
`## Summary`＝ツイート/プレビュー、`## Raw`＝記事全文 として保存する。

- **通常ツイートでは xurl を呼ばない**（`## X Article` マーカーが無いため）。`xurl read` は
  API クレジットを消費するので、Article のときだけ使う。
- xurl 認証は `social-media/xurl` skill（OAuth2、ヘッドレスは下記）。tweet read には
  **有料ティア＋クレジット**が必要（無料枠は `CreditsDepleted`）。
- 単体確認: `fetch_x.py --probe <url>`（判定）/ `fetch_x.py --article <url>`（全文）。

### フォールバック（xurl 未認証 / クレジット切れ）

全文が取れないときは preview のみ保存。全文が要るならネイティブ **browser ツール**
（url_memo は browser-use クラウド＝ローカルサンドボックス非依存）で `article_url` を開き、
本文を一時ファイルへ → `save_article.py --url <url> --article-body-file <tmp>`。

### ヘッドレス Ubuntu での xurl OAuth2（要点）

- redirect は `http://localhost:8080/callback`。xurl はそのポートでコールバックを待つので、
  **8080 を他サービス（caddy 等）と衝突させない**。
- xurl はブラウザ自動起動を試み、ヘッドレスでは URL を表示しないことがある。手元PCから
  `ssh -L 8080:localhost:8080` でトンネルし、表示された認可 URL を手元ブラウザで開く。
  URL が出ない場合は `xdg-open` を「URL を `/dev/tty` とファイルに書くスタブ」に差し替えて取得。
- **`--app <name>` を必ず指定**し、`xurl auth default <app> <user>` で
  client_id 付き app に user トークンを紐付ける（`default` app は client 無しで refresh 不可）。
- `~/.xurl` は秘匿情報。**LLM は読まない**。確認は `xurl auth status` のみ。

## 失敗時

鍵付き・削除・年齢制限・404 等で本文が取れない場合、`fetch_x` はエラーを返し、
`save_article.py` / `ingest_url.py` は **URL・取得年月日・status・理由** を
`ingest_log.jsonl` に記録する（取得不可でも追跡は途切れさせない）。

## 公式 API 経路（xurl・任意）

`xurl` は X developer platform 公式 CLI（`social-media/xurl` skill）。read は要認証で、
現状 `xurl auth status` は未認証（`oauth2: (none)`）のため 401。使う場合のみ、
**ユーザーがエージェント外で** OAuth を完了する:

```bash
xurl auth oauth2 --app x-api        # ブラウザ無し環境は references/headless-oauth-setup.md
xurl auth default x-api
xurl auth status                    # 認証確認（秘匿情報は表示しない）
```

注意（xurl skill の必須ルール）:
- `~/.xurl`（YAML, 秘匿トークン）は **LLM が読まない・要約しない**。確認は `xurl auth status` のみ。
- `--verbose` や `--bearer-token` 等の inline secret 系フラグはエージェントから使わない。
- X API の read は tier 制約があるため、**主経路は syndication のまま**。xurl は補助。

## スコープ外（将来）

- スレッド全体の連結取得、引用の再帰展開、メディア実体のダウンロード保存、nitter 等の追加フォールバック。
