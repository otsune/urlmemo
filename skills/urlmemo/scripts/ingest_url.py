#!/usr/bin/env python3
"""
urlmemo — ユーザーが投稿した URL を取得し UTF-8 に統一して LLM-Wiki に蓄積する。

使い方:
    python3 ingest_url.py [--dry-run] <URL> [<URL> ...]

- 取得は hermes_tools.web_extract を使う（Hermes Agent 同梱）。
  利用不可かつ --dry-run のときのみ urllib による簡易取得にフォールバック。
- 取得本文は UTF-8 に正規化し、UNTRUSTED マーカーで囲んで保存する（インジェクション対策）。
- URL と取得年月日を ingest_log.jsonl に記録する。

既存実装 ~/.hermes/scripts/import_urls_to_wiki.py のロジックを流用・拡張。
"""

import os
import re
import sys
import json
import hashlib
import argparse
import urllib.parse
import html
from datetime import datetime, timezone

# normalize_encoding をスクリプト同階層からインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize_encoding import to_utf8

# 環境設定
WIKI_PATH = os.environ.get("WIKI_PATH", os.path.expanduser("~/wiki"))
RAW_ARTICLES_DIR = os.path.join(WIKI_PATH, "raw", "articles")
INDEX_PATH = os.path.join(WIKI_PATH, "index.md")
LOG_PATH = os.path.join(WIKI_PATH, "log.md")
INGEST_LOG = os.path.join(WIKI_PATH, "raw", "ingest_log.jsonl")
SAVED_URLS_FILE = os.path.expanduser("~/saved-urls.txt")
IMPORT_LOG = os.path.expanduser("~/.hermes/url_import_log.txt")

PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "test.com",
    "localhost", "127.0.0.1", "0.0.0.0",
}

# インジェクション対策: 本文を信頼できないデータとして明示的に囲む
UNTRUSTED_BEGIN = "--- BEGIN UNTRUSTED EXTERNAL CONTENT ---"
UNTRUSTED_END = "--- END UNTRUSTED EXTERNAL CONTENT ---"


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] urlmemo: {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(IMPORT_LOG), exist_ok=True)
        with open(IMPORT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---- 既存ロジック流用（import_urls_to_wiki.py） ----

def slugify(text, max_length=80):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_length] if s else "untitled"


def compute_sha256(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def filter_url(url):
    """通れば None、はじけば理由文字列を返す。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "scheme not http/https"
    hostname = (parsed.hostname or "").lower()
    if hostname in PLACEHOLDER_DOMAINS:
        return "placeholder domain"
    if len(url) > 500:
        return "url too long"
    return None


def extract_urls(values):
    urls = []
    seen = set()
    for value in values:
        for match in re.findall(r"https?://[^\s<>\]\[\"')]+", value):
            url = match.rstrip(".,;:!?")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def parse_frontmatter_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def frontmatter_value(value):
    return json.dumps(str(value).replace("\n", " ").strip(), ensure_ascii=False)


def load_existing_article_metadata():
    """既存 raw 記事から source_url と sha256 を読む。"""
    urls = set()
    hashes = set()
    if not os.path.exists(RAW_ARTICLES_DIR):
        return urls, hashes
    for fname in os.listdir(RAW_ARTICLES_DIR):
        if not fname.endswith(".md"):
            continue
        try:
            with open(os.path.join(RAW_ARTICLES_DIR, fname), "r", encoding="utf-8") as fh:
                content = fh.read()
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for key, target in (("source_url", urls), ("sha256", hashes)):
                        m = re.search(rf"^{key}:\s*(.+)$", parts[1], re.MULTILINE)
                        if m:
                            target.add(parse_frontmatter_value(m.group(1)))
        except OSError:
            continue
    return urls, hashes


def load_existing_source_urls():
    urls, _ = load_existing_article_metadata()
    return urls


def extract_title_from_content(content, url):
    m = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    if m:
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip()
        if t and len(t) < 200:
            return t
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']',
                  content, re.IGNORECASE)
    if m and m.group(1).strip():
        return html.unescape(m.group(1).strip())
    m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL)
    if m:
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if t and len(t) < 150:
            return t
    parsed = urllib.parse.urlparse(url)
    seg = parsed.path.rstrip("/").split("/")[-1]
    if seg:
        seg = re.sub(r"([a-z])([A-Z])", r"\1 \2", seg).replace("-", " ").replace("_", " ")
        return seg.title()
    return parsed.netloc.replace("www.", "").title()


def update_index():
    lines = [
        "# Wiki Index", "",
        "> Content catalog.",
        f"> Last updated: {datetime.now().strftime('%Y-%m-%d')}", "",
    ]
    sections = {
        "Raw Articles": RAW_ARTICLES_DIR,
        "Entities": os.path.join(WIKI_PATH, "entities"),
        "Concepts": os.path.join(WIKI_PATH, "concepts"),
        "Comparisons": os.path.join(WIKI_PATH, "comparisons"),
        "Queries": os.path.join(WIKI_PATH, "queries"),
    }
    total = 0
    for name, dir_path in sections.items():
        lines.append(f"## {name}")
        lines.append("")
        if os.path.isdir(dir_path):
            for f in sorted(os.listdir(dir_path)):
                if f.endswith(".md"):
                    slug = f[:-3]
                    lines.append(f"[[{slug}]] - {slug.replace('-', ' ').title()}")
                    total += 1
        lines.append("")
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return total


def append_wiki_log(details):
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n## [{date_str}] urlmemo | URL取り込み\n")
        for d in details:
            f.write(f"- {d}\n")


# ---- 取得・正規化・保存（新規） ----

def fetch_raw(url):
    """(raw_bytes_or_str, declared_charset, error) を返す。"""
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes"))
        from hermes_tools import web_extract  # type: ignore

        result = web_extract(urls=[url])
        if result and "results" in result and result["results"]:
            item = result["results"][0]
            if item.get("error"):
                return None, None, item["error"]
            return item.get("content", ""), None, None
        return None, None, "web_extract returned no content"
    except ImportError:
        return None, None, "__fallback__"
    except Exception as e:
        return None, None, str(e)[:200]


def fetch_raw_fallback(url):
    """web_extract が無い環境（主に --dry-run 検証用）の簡易取得。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "urlmemo/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r"charset=([\w-]+)", ctype, re.IGNORECASE)
        declared = m.group(1) if m else None
    if declared is None and isinstance(raw, (bytes, bytearray)):
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.IGNORECASE)
        if m:
            declared = m.group(1).decode("ascii", "ignore")
    return raw, declared


def build_article(url, body_utf8, title, charset):
    date_str = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha256 = compute_sha256(body_utf8)
    frontmatter = (
        "---\n"
        f"source_url: {frontmatter_value(url)}\n"
        f"ingested: {date_str}\n"
        f"fetched_at: {fetched_at}\n"
        f"content_charset: {frontmatter_value(charset)}\n"
        f"sha256: {sha256}\n"
        f"title: {frontmatter_value(title)}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{UNTRUSTED_BEGIN}\n"
        f"{body_utf8}\n"
        f"{UNTRUSTED_END}\n"
    )
    return frontmatter, sha256, date_str, fetched_at


def save_article(url, body_utf8, title, charset):
    os.makedirs(RAW_ARTICLES_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = slugify(title)
    fname = f"{date_str}-{slug}.md"
    path = os.path.join(RAW_ARTICLES_DIR, fname)
    counter = 1
    while os.path.exists(path):
        fname = f"{date_str}-{slug}-{counter}.md"
        path = os.path.join(RAW_ARTICLES_DIR, fname)
        counter += 1
    article, sha256, ds, fetched_at = build_article(url, body_utf8, title, charset)
    with open(path, "w", encoding="utf-8") as f:
        f.write(article)
    return fname, sha256, ds, fetched_at


def write_ingest_log(record):
    """URL と取得年月日を含む構造化ログを 1 行追記。"""
    os.makedirs(os.path.dirname(INGEST_LOG), exist_ok=True)
    with open(INGEST_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_one(url, dry_run, saved_urls, existing_raw, existing_hashes):
    err = filter_url(url)
    if err:
        log(f"SKIP ({err}): {url}")
        return {"status": "filtered", "reason": err}

    if not dry_run and (url in saved_urls or url in existing_raw):
        log(f"SKIP (already imported): {url}")
        return {"status": "skipped", "reason": "already imported"}

    raw, declared, ferr = fetch_raw(url)
    if ferr == "__fallback__":
        if not dry_run:
            log(f"ERROR: hermes_tools.web_extract 利用不可（実取り込みには必須）: {url}")
            return {"status": "error", "error": "web_extract unavailable"}
        try:
            raw, declared = fetch_raw_fallback(url)
            ferr = None
        except Exception as e:
            ferr = str(e)[:200]
    if ferr:
        log(f"ERROR fetching {url}: {ferr}")
        return {"status": "error", "error": ferr}

    body_utf8, charset = to_utf8(raw, declared)
    sha256 = compute_sha256(body_utf8)
    fetched_date = datetime.now().strftime("%Y-%m-%d")
    if not dry_run and sha256 in existing_hashes:
        log(f"SKIP (duplicate content sha256={sha256[:12]}): {url}")
        return {"status": "skipped", "reason": "duplicate content", "sha256": sha256,
                "charset": charset, "fetched_date": fetched_date}
    title = extract_title_from_content(body_utf8, url)

    if dry_run:
        log(f"DRY-RUN ok: {url} | charset={charset} | title={title!r} | {len(body_utf8)} chars")
        return {"status": "dry-run", "charset": charset, "title": title,
                "chars": len(body_utf8), "fetched_date": fetched_date}

    fname, sha256, ds, fetched_at = save_article(url, body_utf8, title, charset)
    log(f"Saved: {fname} (charset={charset})")
    return {"status": "ok", "file": fname, "sha256": sha256, "charset": charset,
            "title": title, "fetched_date": ds, "fetched_at": fetched_at}


def main():
    ap = argparse.ArgumentParser(description="URL を LLM-Wiki に取り込む")
    ap.add_argument("urls", nargs="+", help="取り込む URL（複数可）")
    ap.add_argument("--dry-run", action="store_true",
                    help="保存せず取得・正規化のみ確認")
    args = ap.parse_args()

    urls = extract_urls(args.urls)
    if not urls:
        print("ERROR: http/https URL が見つかりません")
        return 2

    log(f"=== ingest start (dry_run={args.dry_run}) urls={len(urls)} ===")

    saved_urls = set()
    if os.path.exists(SAVED_URLS_FILE):
        with open(SAVED_URLS_FILE, "r", encoding="utf-8") as f:
            saved_urls = {l.strip() for l in f if l.strip()}
    existing_raw, existing_hashes = load_existing_article_metadata()

    imported, errors = [], []
    for url in urls:
        res = process_one(url, args.dry_run, saved_urls, existing_raw, existing_hashes)
        # URL と取得年月日を必ずログに残す
        record = {
            "url": url,
            "fetched_date": res.get("fetched_date", datetime.now().strftime("%Y-%m-%d")),
            "fetched_at": res.get("fetched_at",
                                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            "status": res["status"],
        }
        for k in ("file", "sha256", "charset", "error", "reason"):
            if k in res:
                record[k] = res[k]
        if not args.dry_run:
            write_ingest_log(record)

        if res["status"] == "ok":
            imported.append((url, res["file"]))
            saved_urls.add(url)
            existing_raw.add(url)
            existing_hashes.add(res["sha256"])
        elif res["status"] == "error":
            errors.append(url)

    if not args.dry_run and imported:
        with open(SAVED_URLS_FILE, "a", encoding="utf-8") as f:
            for url, _ in imported:
                f.write(url + "\n")
        page_count = update_index()
        append_wiki_log([
            f"Imported {len(imported)} article(s)",
            f"Failed: {len(errors)}",
            "Files: " + ", ".join(fn for _, fn in imported),
        ])
        log(f"index.md 更新 (総ページ数 {page_count})")

    log(f"=== done: {len(imported)} ok, {len(errors)} error ===")
    return 1 if errors and not imported else 0


if __name__ == "__main__":
    sys.exit(main())
