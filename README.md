# urlmemo

Hermes Agent カスタムスキル。**URL を投稿すると、その内容を取得し UTF-8 に統一して
LLM-Wiki に知識データとして蓄積する。**

- 取得は決定論的な Python スクリプトが行い、**取得した Web 本文を LLM が実行/解釈しない**
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
        │   ├── ingest_url.py          # 取得 → UTF-8 → LLM-Wiki 保存 → ログ
        │   └── normalize_encoding.py  # UTF-8 正規化ユーティリティ
        └── references/
            ├── injection-hardening.md # インジェクション対策方針
            └── activation.md          # url_memo モードの発火設定
```

## 自動発火（url_memo モード）

`url_memo` プロファイル、または Discord `url_memo` チャンネルでは、**URL だけ**の
メッセージも取り込み依頼として自動発火する。設定方法は
[`skills/urlmemo/references/activation.md`](skills/urlmemo/references/activation.md) を参照。

## 使い方（手動）

```bash
# 取得・正規化のみ（保存しない）
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/ingest_url.py --dry-run "https://example.org/"

# LLM-Wiki に取り込む
WIKI_PATH=~/wiki python3 skills/urlmemo/scripts/ingest_url.py "https://example.org/article"
```

`WIKI_PATH` 未指定時は `~/wiki`。`hermes_tools.web_extract` が利用できる環境
（Hermes Agent 同梱）で動作する。未導入環境では `--dry-run` 時のみ標準ライブラリで取得を試みる。

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
