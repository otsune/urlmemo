#!/usr/bin/env python3
"""
urlmemo: agentの要約と、スクリプト側で取得する raw の両方を保存する。
- 要約: エージェントが一時ファイルで渡す (--summary-file)
- raw  : Hermes venv 内の tools.web_tools.web_extract_tool で取得し、UNTRUSTED マーカーで包む
既存の ingest_url.py から、正規化・重複判定・index/log 更新のロジックを再利用。
"""
import asyncio
import os
import sys
import json
import hashlib
import argparse
from datetime import datetime, timezone

# 同階層の normalizer / X 取得モジュールを import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize_encoding import to_utf8
import fetch_x

WIKI_PATH = os.environ.get("WIKI_PATH", os.path.expanduser("~/wiki"))
RAW_ARTICLES_DIR = os.path.join(WIKI_PATH, "raw", "articles")
INDEX_PATH = os.path.join(WIKI_PATH, "index.md")
LOG_PATH = os.path.join(WIKI_PATH, "log.md")
INGEST_LOG = os.path.join(WIKI_PATH, "raw", "ingest_log.jsonl")
SAVED_URLS_FILE = os.path.expanduser("~/saved-urls.txt")

UNTRUSTED_BEGIN = "--- BEGIN UNTRUSTED EXTERNAL CONTENT ---"
UNTRUSTED_END = "--- END UNTRUSTED EXTERNAL CONTENT ---"


def log(msg: str) -> None:
    print(f"[urlmemo/save_article] {msg}")


def load_saved_urls() -> set:
    if not os.path.exists(SAVED_URLS_FILE):
        return set()
    with open(SAVED_URLS_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def load_existing_meta():
    urls = set()
    hashes = set()
    if not os.path.isdir(RAW_ARTICLES_DIR):
        return urls, hashes
    for fname in os.listdir(RAW_ARTICLES_DIR):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(RAW_ARTICLES_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            continue
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                front = parts[1]
                for key, target in (("source_url", urls), ("sha256", hashes)):
                    import re
                    m = re.search(rf"^{key}:\s*(.+)$", front, re.MULTILINE)
                    if m:
                        raw = m.group(1).strip()
                        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
                            try:
                                raw = json.loads(raw)
                            except json.JSONDecodeError:
                                raw = raw[1:-1]
                        target.add(raw)
    return urls, hashes


def slugify(text: str, max_length: int = 80) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_length] if s else "untitled"


def compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_title_from_content(content: str, url: str) -> str:
    import re, html
    m = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    if m:
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip()
        if t and len(t) < 200:
            return t
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', content, re.IGNORECASE)
    if m and m.group(1).strip():
        return html.unescape(m.group(1).strip())
    m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL)
    if m:
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip()
        if t and len(t) < 150:
            return t
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if m:
        t = m.group(1).strip()
        if t and len(t) < 150:
            return t
    from urllib.parse import urlparse
    parsed = urlparse(url)
    seg = parsed.path.rstrip("/").split("/")[-1]
    if seg:
        seg = re.sub(r"([a-z])([A-Z])", r"\1 \2", seg).replace("-", " ").replace("_", " ")
        return seg.title()
    return parsed.netloc.replace("www.", "").title()


def _extract_content_from_web_result(result_json: str, url: str):
    try:
        payload = json.loads(result_json)
    except json.JSONDecodeError as e:
        return None, f"web_extract returned invalid JSON: {e}"

    if payload.get("success") is False:
        return None, payload.get("error") or "web_extract failed"

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None, "web_extract returned no results"

    result = results[0]
    if result.get("error"):
        return None, str(result["error"])

    content = result.get("content") or result.get("raw_content") or ""
    if not content:
        return None, "web_extract returned empty content"

    title = result.get("title") or ""
    source_url = result.get("url") or url
    return {"content": content, "title": title, "url": source_url}, None


async def fetch_raw_async(url: str):
    """
    Hermes Agent 本体の web_extract_tool を「呼ぶだけ」で raw を取得する。
    use_llm_processing=False により、大ページでも LLM 要約せず抽出本文を保存する。
    """
    try:
        from tools.web_tools import web_extract_tool
    except Exception as e:
        venv_python = os.path.expanduser("~/.hermes/venv/bin/python3.11")
        hint = (
            f"tools.web_tools.web_extract_tool import failed: {type(e).__name__}: {e}. "
            f"Run with Hermes venv python if available: {venv_python}"
        )
        return None, None, hint

    try:
        result_json = await web_extract_tool([url], "markdown", use_llm_processing=False)
    except Exception as e:
        return None, None, f"web_extract_tool failed: {type(e).__name__}: {e}"

    extracted, err = _extract_content_from_web_result(result_json, url)
    if err:
        return None, None, err
    return extracted["content"], "utf-8(str)", None


def fetch_raw(url: str):
    """raw を取得し、(text, charset, error) を返す。

    X/Twitter は Firecrawl 非対応(504)のため web_extract を使わず、
    無認証の syndication API（fetch_x）で本文を取得する。
    """
    if fetch_x.is_x_url(url):
        return fetch_x.fetch_x_markdown(url)
    return asyncio.run(fetch_raw_async(url))


def frontmatter_value(value: str) -> str:
    return json.dumps(str(value).replace("\n", " ").strip(), ensure_ascii=False)


def build_article(url: str, body_utf8: str, title: str, charset: str, summary_text: str | None = None):
    date_str = datetime.now().strftime("%Y-%m-%d")
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha256 = compute_sha256(body_utf8)
    lines = [
        "---",
        f"source_url: {frontmatter_value(url)}",
        f"ingested: {date_str}",
        f"fetched_at: {fetched_at}",
        f"content_charset: {frontmatter_value(charset or 'utf-8')}",
        f"sha256: {sha256}",
        f"title: {frontmatter_value(title)}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    if summary_text:
        lines += [
            "## Summary",
            "",
            f"{UNTRUSTED_BEGIN}",
            summary_text,
            f"{UNTRUSTED_END}",
            "",
            "## Raw",
            "",
        ]
    else:
        lines += ["## Raw", ""]
    lines += [
        f"{UNTRUSTED_BEGIN}",
        body_utf8,
        f"{UNTRUSTED_END}",
        "",
    ]
    return "\n".join(lines), sha256, date_str, fetched_at


def save_article(url: str, body_utf8: str, title: str, charset: str, summary_text: str | None = None):
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
    article, sha256, ds, fetched_at = build_article(url, body_utf8, title, charset, summary_text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(article)
    return fname, sha256, ds, fetched_at


def append_ingest_log(record: dict) -> None:
    os.makedirs(os.path.dirname(INGEST_LOG), exist_ok=True)
    with open(INGEST_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def update_index() -> int:
    sections = {
        "Raw Articles": RAW_ARTICLES_DIR,
        "Entities": os.path.join(WIKI_PATH, "entities"),
        "Concepts": os.path.join(WIKI_PATH, "concepts"),
        "Comparisons": os.path.join(WIKI_PATH, "comparisons"),
        "Queries": os.path.join(WIKI_PATH, "queries"),
    }
    lines = [
        "# Wiki Index",
        "",
        "> Content catalog.",
        f"> Last updated: {datetime.now().strftime('%Y-%m-%d')}",
        "",
    ]
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


def now_record(status: str, url: str, **extra) -> dict:
    record = {
        "url": url,
        "fetched_date": datetime.now().strftime("%Y-%m-%d"),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
    }
    record.update(extra)
    return record


def main():
    ap = argparse.ArgumentParser(description="Save raw article with optional summary")
    ap.add_argument("--url", required=True, help="source URL")
    ap.add_argument("--summary-file", required=False, help="agent-generated summary text file path")
    ap.add_argument("--article-body-file", required=False,
                    help="X Article 等で、エージェントが browser ツールでレンダリングした本文ファイル")
    args = ap.parse_args()

    url = args.url
    summary_text = None
    if args.summary_file and os.path.exists(args.summary_file):
        with open(args.summary_file, "rb") as f:
            summary_text, _ = to_utf8(f.read())

    existing_urls, existing_hashes = load_existing_meta()
    saved_urls = load_saved_urls()

    if url in saved_urls or url in existing_urls:
        log(f"SKIP (already imported): {url}")
        append_ingest_log(now_record("skipped", url, reason="already imported"))
        print(json.dumps({"status": "skipped", "reason": "already imported", "url": url}, ensure_ascii=False))
        return 0

    raw, charset, ferr = fetch_raw(url)
    if ferr:
        log(f"ERROR fetching {url}: {ferr}")
        append_ingest_log(now_record("error", url, error=ferr))
        print(json.dumps({"status": "error", "error": ferr, "url": url}, ensure_ascii=False))
        return 1

    body_utf8, charset = to_utf8(raw, charset)

    # X Article: ツイート本体はリンク+プレビューのみ。全文は別経路で取得し
    # Summary=ツイート/プレビュー、Raw=記事全文 として保存する。
    #   優先1) --article-body-file（エージェントが browser ツールでレンダリングした全文）
    #   優先2) xurl(公式API) の tweet.fields=article（要認証・要クレジット）を自動取得
    # ツイートが Article かは syndication 由来本文の "## X Article" マーカーで無料判定し、
    # Article のときだけ xurl を呼ぶ（通常ツイートでクレジットを消費しない）。
    x_title = None
    if fetch_x.is_x_url(url) and args.article_body_file and os.path.exists(args.article_body_file):
        with open(args.article_body_file, "rb") as f:
            article_body, _ = to_utf8(f.read())
        if article_body.strip():
            summary_text = body_utf8          # ツイート本体/プレビュー/メタ → Summary
            body_utf8 = article_body          # browser レンダリング全文 → Raw
            charset = "utf-8(x-article+browser)"
    elif fetch_x.is_x_url(url) and "## X Article" in body_utf8 and fetch_x.xurl_authed():
        art_md, art_title, aerr = fetch_x.fetch_article_via_xurl(url)
        if art_md:
            summary_text = body_utf8          # ツイート/プレビュー → Summary
            body_utf8 = art_md                # xurl 由来の記事全文 → Raw
            charset = "utf-8(x-article+xurl)"
            x_title = art_title or None
            log(f"X Article 全文を xurl で取得 ({len(art_md)} 文字)")
        else:
            log(f"X Article 全文取得不可 (xurl: {aerr}) → プレビューのみ保存")

    sha256 = compute_sha256(body_utf8)
    if sha256 in existing_hashes:
        log(f"SKIP (duplicate content sha256={sha256[:12]}): {url}")
        append_ingest_log(now_record("skipped", url, reason="duplicate content", sha256=sha256, charset=charset))
        print(json.dumps({"status": "skipped", "reason": "duplicate content", "sha256": sha256, "fetched_date": datetime.now().strftime("%Y-%m-%d")}, ensure_ascii=False))
        return 0

    if fetch_x.is_x_url(url):
        # 記事は article.title を優先。無ければツイート(=summary_text)/本文からタイトル生成
        title = x_title or fetch_x.title_for(summary_text or body_utf8, url)
    else:
        title = extract_title_from_content(body_utf8, url)
    fname, sha256, ds, fetched_at = save_article(url, body_utf8, title, charset, summary_text)
    log(f"Saved: {fname} (charset={charset})")
    record = {
        "url": url,
        "fetched_date": ds,
        "fetched_at": fetched_at,
        "status": "ok",
        "file": fname,
        "sha256": sha256,
        "charset": charset,
        "title": title,
    }
    append_ingest_log(record)
    with open(SAVED_URLS_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")
    page_count = update_index()
    append_wiki_log([
        f"Imported 1 article(s) via save_article",
        f"File: {fname}",
    ])
    log(f"index.md 更新 (総ページ数 {page_count})")
    print(json.dumps({"status": "ok", "file": fname, "title": title, "url": url, "sha256": sha256}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
