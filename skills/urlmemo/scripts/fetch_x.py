#!/usr/bin/env python3
"""
urlmemo — X/Twitter 投稿の本文取得（無認証）。

Firecrawl(web_extract) は X 非対応(504)、xurl read は要認証(401)。
X 投稿は **syndication API**（埋め込みウィジェット用・無認証）で本文を取得する:

    GET https://cdn.syndication.twimg.com/tweet-result?id=<TWEET_ID>&lang=<lang>&token=<token>

標準ライブラリのみで動作し、Hermes ランタイム外でも使える。

CLI:
    python3 fetch_x.py <url|tweet_id> [--json] [--lang ja]

ライブラリ:
    is_x_url(url) -> bool
    extract_tweet_id(url) -> str | None
    fetch_tweet(tweet_id, lang="ja") -> (dict|None, error|None)
    render_markdown(tweet) -> str
    fetch_x_markdown(url, lang="ja") -> (text|None, charset, error|None)   # save_article.fetch_raw 互換
    title_for(markdown, url) -> str
"""

import re
import ssl
import sys
import json
import math
import shutil
import argparse
import subprocess
import urllib.parse
import urllib.request

X_HOSTS = {
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com",
    "vxtwitter.com", "fxtwitter.com", "fixupx.com", "fixvx.com",
    "nitter.net",
}

SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def is_x_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in X_HOSTS


def extract_tweet_id(url_or_id: str):
    s = url_or_id.strip()
    if s.isdigit():
        return s
    m = re.search(r"/status(?:es)?/(\d+)", s) or re.search(r"/i/web/status/(\d+)", s)
    return m.group(1) if m else None


def _float_base36(x: float) -> str:
    """JS Number.prototype.toString(36) 相当（react-tweet のトークン生成用）。"""
    if x <= 0:
        return "0"
    int_part = int(x)
    frac = x - int_part
    s = ""
    n = int_part
    if n == 0:
        s = "0"
    while n > 0:
        s = _DIGITS[n % 36] + s
        n //= 36
    out = s + "."
    count = 0
    while frac > 0 and count < 30:
        frac *= 36
        d = int(frac)
        out += _DIGITS[d]
        frac -= d
        count += 1
    return out


def _token_candidates(tweet_id: str):
    """react-tweet 方式のトークン → 失敗時の単純トークンの順で返す。"""
    try:
        derived = re.sub(r"(0+|\.)", "", _float_base36((int(tweet_id) / 1e15) * math.pi))
    except (ValueError, OverflowError):
        derived = ""
    cands = []
    if derived:
        cands.append(derived)
    cands += ["a", "0"]
    seen = set()
    return [c for c in cands if c and not (c in seen or seen.add(c))]


def _ssl_context():
    """検証を維持したまま、certifi があればその CA バンドルを使う。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — certifi 不在時は標準 CA にフォールバック
        return ssl.create_default_context()


def _http_get_json(url: str, timeout: int):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; urlmemo/0.1)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def fetch_tweet(tweet_id: str, lang: str = "ja", timeout: int = 20):
    """syndication API から tweet を取得。(tweet_dict|None, error|None)。"""
    last_err = None
    for token in _token_candidates(tweet_id):
        q = urllib.parse.urlencode({"id": tweet_id, "lang": lang, "token": token})
        try:
            data = _http_get_json(f"{SYNDICATION_URL}?{q}", timeout)
        except Exception as e:  # noqa: BLE001 — ネットワーク/HTTP/JSON すべてを次トークンへ
            last_err = f"{type(e).__name__}: {e}"
            continue
        typename = data.get("__typename")
        if typename == "Tweet" or data.get("text"):
            return data, None
        if typename == "TweetTombstone":
            return None, "tweet unavailable (deleted/protected/age-restricted)"
        last_err = f"unexpected payload (__typename={typename!r})"
    return None, last_err or "tweet fetch failed"


def _tweet_full_text(tweet: dict) -> str:
    """note_tweet（長文）があれば全文、無ければ text。t.co を展開。"""
    text = tweet.get("text", "") or ""
    note = (((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {})
    if note.get("text"):
        text = note["text"]
    # t.co → expanded_url 置換
    urls = ((tweet.get("entities") or {}).get("urls")) or []
    if note.get("entity_set", {}).get("urls"):
        urls = urls + note["entity_set"]["urls"]
    for u in urls:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    return text.strip()


def render_markdown(tweet: dict) -> str:
    user = tweet.get("user") or {}
    screen = user.get("screen_name", "")
    name = user.get("name", "")
    created = tweet.get("created_at", "")
    header = f"**@{screen}**" + (f" ({name})" if name else "") + (f" ・ {created}" if created else "")
    parts = [header, "", _tweet_full_text(tweet)]

    media = tweet.get("mediaDetails") or []
    if media:
        parts.append("")
        for m in media:
            mtype = m.get("type", "media")
            murl = m.get("media_url_https") or m.get("video_info", {}).get("variants", [{}])[0].get("url", "")
            if murl:
                parts.append(f"- [{mtype}] {murl}")

    quoted = tweet.get("quoted_tweet")
    if quoted:
        q_user = (quoted.get("user") or {}).get("screen_name", "")
        q_text = _tweet_full_text(quoted)
        parts += ["", f"> 引用 @{q_user}: {q_text}"]

    # X Article（長文記事）: syndication はタイトルとプレビューのみ返す（全文は要認証）
    article = tweet.get("article")
    if isinstance(article, dict) and (article.get("title") or article.get("preview_text")):
        rest_id = article.get("rest_id", "")
        parts += ["", "## X Article"]
        if article.get("title"):
            parts.append(f"**{article['title']}**")
        if article.get("preview_text"):
            parts += ["", article["preview_text"]]
        if rest_id:
            parts += ["", f"記事 URL: https://x.com/i/article/{rest_id}",
                      "（注: 記事全文は syndication では取得不可。プレビューのみ。）"]

    return "\n".join(parts).strip() + "\n"


def fetch_x_markdown(url: str, lang: str = "ja"):
    """save_article.fetch_raw 互換: (text|None, charset, error|None)。"""
    tid = extract_tweet_id(url)
    if not tid:
        return None, None, "could not extract tweet id from URL"
    tweet, err = fetch_tweet(tid, lang=lang)
    if err:
        return None, None, err
    return render_markdown(tweet), "utf-8(x-syndication)", None


def article_info(tweet: dict) -> dict:
    """tweet が X Article なら記事 URL/タイトルを返す。"""
    a = tweet.get("article")
    if isinstance(a, dict) and a.get("rest_id"):
        return {
            "is_article": True,
            "article_url": f"https://x.com/i/article/{a['rest_id']}",
            "article_title": a.get("title", ""),
        }
    return {"is_article": False}


def xurl_authed() -> bool:
    """xurl が認証済み（oauth2/oauth1/bearer のいずれかが設定済み）か。秘匿情報は読まない。"""
    if not shutil.which("xurl"):
        return False
    try:
        out = subprocess.run(["xurl", "auth", "status"],
                             capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    txt = (out.stdout or "") + (out.stderr or "")
    for m in re.finditer(r"(?:oauth2|oauth1|bearer):\s*(.+)", txt):
        val = m.group(1).strip()
        if val and val not in ("(none)", "–", "-"):
            return True
    return False


def render_article_markdown(art: dict) -> str:
    """X Article（xurl `tweet.fields=article`）の本文を Markdown 化。"""
    text = art.get("plain_text", "") or ""
    ent = art.get("entities", {}) or {}
    for u in ent.get("urls", []) or []:
        exp = u.get("expanded_url") or u.get("unwound_url")
        if u.get("url") and exp:
            text = text.replace(u["url"], exp)
    parts = [text.strip()]
    codes = ent.get("code", []) or []
    if codes:
        parts.append("## Code blocks")
        for c in codes:
            content = c.get("content") or f"```\n{c.get('code', '')}\n```"
            parts.append(content)
    return "\n\n".join(p for p in parts if p).strip() + "\n"


def fetch_article_via_xurl(url: str, timeout: int = 30):
    """X Article の全文を公式 API（xurl）で取得。(markdown|None, title|None, error|None)。

    要認証・要 API クレジット。通常ツイートではなく Article のときだけ呼ぶこと
    （クレジット節約のため）。
    """
    tid = extract_tweet_id(url)
    if not tid:
        return None, None, "could not extract tweet id"
    if not shutil.which("xurl"):
        return None, None, "xurl not installed"
    path = (f"/2/tweets/{tid}?tweet.fields=article,created_at,text,entities"
            "&expansions=author_id")
    try:
        proc = subprocess.run(["xurl", path], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, None, f"xurl failed: {type(e).__name__}: {e}"
    if not (proc.stdout or "").strip():
        return None, None, f"xurl error: {(proc.stderr or '').strip()[:200]}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, None, "xurl returned non-JSON"
    if isinstance(data, dict) and str(data.get("type", "")).endswith("credits"):
        return None, None, "X API credits depleted"
    d = (data or {}).get("data") or {}
    art = d.get("article") or {}
    if not art.get("plain_text"):
        return None, None, "no article plain_text in API response"
    return render_article_markdown(art), art.get("title", ""), None


def probe(url: str, lang: str = "ja") -> dict:
    """X URL を判定し、X Article なら記事 URL を返す（ブラウザ取得の要否判定用）。

    返り値例:
      {"is_x": true, "is_article": true, "article_url": "...", "article_title": "..."}
      {"is_x": true, "is_article": false}
      {"is_x": false}
    """
    if not is_x_url(url):
        return {"is_x": False}
    tid = extract_tweet_id(url)
    if not tid:
        return {"is_x": True, "error": "could not extract tweet id"}
    tweet, err = fetch_tweet(tid, lang=lang)
    if err:
        return {"is_x": True, "error": err}
    info = {"is_x": True}
    info.update(article_info(tweet))
    return info


def title_for(markdown: str, url: str) -> str:
    """記事タイトル: '@screen_name: 本文先頭40字' を組み立てる。"""
    screen = ""
    m = re.search(r"\*\*@([A-Za-z0-9_]+)\*\*", markdown)
    if m:
        screen = m.group(1)
    # ヘッダ行（**@..** 行）を除いた最初の非空行を本文とみなす
    body_line = ""
    for line in markdown.splitlines():
        if line.startswith("**@") or not line.strip():
            continue
        body_line = line.strip()
        break
    snippet = re.sub(r"\s+", " ", body_line)[:40]
    if screen and snippet:
        return f"@{screen}: {snippet}"
    if screen:
        return f"@{screen} post"
    tid = extract_tweet_id(url) or "post"
    return f"X post {tid}"


def main():
    ap = argparse.ArgumentParser(description="X/Twitter 投稿を syndication API で取得")
    ap.add_argument("target", help="tweet の URL または ID")
    ap.add_argument("--json", action="store_true", help="生 JSON を出力")
    ap.add_argument("--probe", action="store_true",
                    help="X 判定・記事URL の有無のみ JSON で出力（ブラウザ取得の要否判定）")
    ap.add_argument("--article", action="store_true",
                    help="X Article の全文を xurl(公式API) で取得（要認証・要クレジット）")
    ap.add_argument("--lang", default="ja")
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(probe(args.target, lang=args.lang), ensure_ascii=False))
        return 0

    if args.article:
        md, title, err = fetch_article_via_xurl(args.target)
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print(f"# {title}\n\n{md}" if title else md)
        return 0

    tid = extract_tweet_id(args.target)
    if not tid:
        print(f"ERROR: tweet id を抽出できません: {args.target}", file=sys.stderr)
        return 2
    tweet, err = fetch_tweet(tid, lang=args.lang)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(tweet, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(tweet))
    return 0


if __name__ == "__main__":
    sys.exit(main())
