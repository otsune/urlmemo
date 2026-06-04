# urlmemo

Hermes Agent カスタムスキル。**URL を投稿すると、その内容を取得し UTF-8 に統一して
LLM-Wiki に知識データとして蓄積する。**

- 要約は Hermes Agent のネイティブ `web_extract`、raw は `save_article.py` が
  Hermes venv 内の `tools.web_tools.web_extract_tool(..., use_llm_processing=False)` で取得する。
- 取得した Web 本文は **信頼できない外部データ**として扱い、本文中の指示・コード・URL を実行しない
  （プロンプトインジェクション対策）。
- 取り込んだ URL と取得年月日はログに記録する。

詳細な設計と作業計画は [`作業計画書.md`](作業計画書.md) を参照。

## 構成

```
urlmemo/
├── README.md
├── LICENSE                     # MIT
├── 作業計画書.md
└── skills/
    └── urlmemo/
        ├── SKILL.md
        ├── scripts/
        │   ├── save_article.py        # 要約 + raw 取得 → UTF-8 → LLM-Wiki 保存 → ログ
        │   ├── ingest_url.py          # dry-run/手動検証用の旧取り込み経路
        │   ├── fetch_x.py             # X/Twitter 投稿を syndication API で取得（無認証）
        │   ├── normalize_encoding.py  # UTF-8 正規化ユーティリティ
        │   └── search_wiki.py         # 蓄積記事の横断検索（問い合わせ対応）
        └── references/
            ├── injection-hardening.md # インジェクション対策方針
            ├── x-twitter.md           # X/Twitter 取り込みの方式・制約
            └── activation.md          # url_memo モードの発火設定
```

## 自動発火（url_memo モード）

`url_memo` プロファイル、または Discord `url_memo` チャンネルでは、**URL だけ**の
メッセージも取り込み依頼として自動発火する。設定方法は
[`skills/urlmemo/references/activation.md`](skills/urlmemo/references/activation.md) を参照。

## 使い方（手動）

```bash
# 取得・正規化のみ（保存しない）
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/ingest_url.py --dry-run "https://www.gnu.org/licenses/gpl-3.0.html"

# LLM-Wiki に取り込む（要約ファイルは事前に web_extract 出力を保存しておく）
HERMES_HOME=~/.hermes/profiles/url_memo WIKI_PATH=~/wiki ~/.hermes/venv/bin/python3.11 \
  skills/urlmemo/scripts/save_article.py \
  --url "https://www.gnu.org/licenses/gpl-3.0.html" \
  --summary-file /tmp/urlmemo-summary.txt

# 自由文を渡しても http/https URL だけを抽出して取り込む
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/ingest_url.py "この URL 入れて https://www.gnu.org/licenses/gpl-3.0.html"

# 過去に蓄積したものを検索（URL 無しの問い合わせに対応）
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/search_wiki.py gpl license
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/search_wiki.py --url --json github

# X/Twitter は Firecrawl 非対応のため syndication API で取得（無認証・自動判定）
python3 skills/urlmemo/scripts/fetch_x.py https://x.com/jack/status/20
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/save_article.py --url "https://x.com/jack/status/20"
```

X/Twitter の取り込み仕様・制約は [`skills/urlmemo/references/x-twitter.md`](skills/urlmemo/references/x-twitter.md) を参照。

`WIKI_PATH` 未指定時は `~/wiki`。本番取り込みは Hermes Agent 0.12.0 以降の venv で
`from tools.web_tools import web_extract_tool` が成功する環境を前提にする。
`url_memo` プロファイルでは `toolsets:` に `web` が含まれ、`web.extract_backend: firecrawl` と
`web.use_gateway: true` が設定されている必要がある。

Discord チャンネルで自動発火させる場合は、Discord bot を持つ **default profile** の
`discord.channel_prompts["1490785854598287471"]` に urlmemo 手順を入れる。
`url_memo` プロファイルは raw 取得用の `HERMES_HOME` として使い、Discord bot token は置かない。
systemd では `hermes-gateway.service.d/override.conf` で `--profile default` と
`EnvironmentFile=~/.hermes/.env` を固定し、`BWS_ACCESS_TOKEN` 経由で Discord token を読む。

同一 URL は `saved-urls.txt` と既存記事 frontmatter の `source_url` で、同一本文は
frontmatter の `sha256` で重複排除する。

## テスト

```bash
python3 -m unittest discover -s tests -v
```

## Hermes へのインストール

```bash
# このリポジトリをクローン後
hermes skills install ./skills/urlmemo
# もしくは公開済みリポジトリから
hermes skills publish skills/urlmemo --to github --repo otsune/urlmemo
```

インストール後、Hermes に「この URL を wiki に入れて: <URL>」と投稿すると発火する。

## ライセンス

MIT. [`LICENSE`](LICENSE) を参照。
