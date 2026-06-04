import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "urlmemo" / "scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class UrlmemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.wiki = self.base / "wiki"
        self.home = self.base / "home"
        self.home.mkdir()

        self.ingest = load_module("ingest_url")
        self.save_article = load_module("save_article")
        self.search_wiki = load_module("search_wiki")

        self.ingest.WIKI_PATH = str(self.wiki)
        self.ingest.RAW_ARTICLES_DIR = str(self.wiki / "raw" / "articles")
        self.ingest.INDEX_PATH = str(self.wiki / "index.md")
        self.ingest.LOG_PATH = str(self.wiki / "log.md")
        self.ingest.INGEST_LOG = str(self.wiki / "raw" / "ingest_log.jsonl")
        self.ingest.SAVED_URLS_FILE = str(self.home / "saved-urls.txt")
        self.ingest.IMPORT_LOG = str(self.home / ".hermes" / "url_import_log.txt")

        self.save_article.WIKI_PATH = str(self.wiki)
        self.save_article.RAW_ARTICLES_DIR = str(self.wiki / "raw" / "articles")
        self.save_article.INDEX_PATH = str(self.wiki / "index.md")
        self.save_article.LOG_PATH = str(self.wiki / "log.md")
        self.save_article.INGEST_LOG = str(self.wiki / "raw" / "ingest_log.jsonl")
        self.save_article.SAVED_URLS_FILE = str(self.home / "saved-urls.txt")

        self.search_wiki.WIKI_PATH = str(self.wiki)
        self.search_wiki.RAW_ARTICLES_DIR = str(self.wiki / "raw" / "articles")

    def tearDown(self):
        self.tmp.cleanup()

    def run_ingest(self, args):
        old_argv = sys.argv[:]
        try:
            sys.argv = ["ingest_url.py"] + args
            return self.ingest.main()
        finally:
            sys.argv = old_argv

    def run_save_article(self, args):
        old_argv = sys.argv[:]
        try:
            sys.argv = ["save_article.py"] + args
            return self.save_article.main()
        finally:
            sys.argv = old_argv

    def test_import_is_idempotent_across_runs(self):
        body = "<html><title>Needle Title</title><body>Needle body</body></html>"
        self.ingest.fetch_raw = lambda url: (body, None, None)

        rc = self.run_ingest(["https://example.net/a"])
        self.assertEqual(rc, 0)
        rc = self.run_ingest(["https://example.net/a"])

        self.assertEqual(rc, 0)
        articles = list((self.wiki / "raw" / "articles").glob("*.md"))
        self.assertEqual(len(articles), 1)
        saved_urls = (self.home / "saved-urls.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(saved_urls, ["https://example.net/a"])
        log_lines = (self.wiki / "raw" / "ingest_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status": "ok"', log_lines)
        self.assertIn('"reason": "already imported"', log_lines)

    def test_duplicate_body_sha_is_skipped(self):
        body = "<html><title>Same Body</title><body>same text</body></html>"
        self.ingest.fetch_raw = lambda url: (body, None, None)

        rc = self.run_ingest(["https://example.net/a", "https://example.net/b"])

        self.assertEqual(rc, 0)
        articles = list((self.wiki / "raw" / "articles").glob("*.md"))
        self.assertEqual(len(articles), 1)
        log_lines = (self.wiki / "raw" / "ingest_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"reason": "duplicate content"', log_lines)

    def test_search_reads_quoted_frontmatter(self):
        body = "<html><title>Quoted: Title</title><body>Needle body</body></html>"
        self.ingest.fetch_raw = lambda url: (body, None, None)
        self.assertEqual(self.run_ingest(["https://example.net/quoted"]), 0)

        results = self.search_wiki.search(["needle"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Quoted: Title")
        self.assertEqual(results[0]["source_url"], "https://example.net/quoted")
        self.assertIn("Needle", results[0]["untrusted_snippet"])

    def test_cp932_normalization(self):
        normalize_encoding = load_module("normalize_encoding")
        text, charset = normalize_encoding.to_utf8("日本語\r\n".encode("cp932"), "cp932")

        self.assertEqual(text, "日本語\n")
        self.assertEqual(charset, "cp932")

    def test_save_article_stores_summary_and_raw_sections(self):
        summary = self.base / "summary.txt"
        summary.write_text("要約本文\r\nsummary needle", encoding="utf-8")
        raw = "# Raw Title\n\nraw needle body"
        self.save_article.fetch_raw = lambda url: (raw, None, None)

        rc = self.run_save_article([
            "--url", "https://example.net/raw",
            "--summary-file", str(summary),
        ])

        self.assertEqual(rc, 0)
        articles = list((self.wiki / "raw" / "articles").glob("*.md"))
        self.assertEqual(len(articles), 1)
        content = articles[0].read_text(encoding="utf-8")
        self.assertIn("## Summary", content)
        self.assertIn("## Raw", content)
        self.assertIn("要約本文\nsummary needle", content)
        self.assertIn("raw needle body", content)
        self.assertEqual(content.count(self.save_article.UNTRUSTED_BEGIN), 2)
        log_lines = (self.wiki / "raw" / "ingest_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status": "ok"', log_lines)
        self.assertIn('"url": "https://example.net/raw"', log_lines)

    def test_save_article_is_idempotent_by_url_and_sha(self):
        summary = self.base / "summary.txt"
        summary.write_text("summary", encoding="utf-8")
        body = "# Same\n\nsame body"
        self.save_article.fetch_raw = lambda url: (body, None, None)

        self.assertEqual(self.run_save_article([
            "--url", "https://example.net/one",
            "--summary-file", str(summary),
        ]), 0)
        self.assertEqual(self.run_save_article([
            "--url", "https://example.net/one",
            "--summary-file", str(summary),
        ]), 0)
        self.assertEqual(self.run_save_article([
            "--url", "https://example.net/two",
            "--summary-file", str(summary),
        ]), 0)

        articles = list((self.wiki / "raw" / "articles").glob("*.md"))
        self.assertEqual(len(articles), 1)
        log_lines = (self.wiki / "raw" / "ingest_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"reason": "duplicate content"', log_lines)

    def test_search_reads_summary_and_raw_blocks(self):
        article_dir = self.wiki / "raw" / "articles"
        article_dir.mkdir(parents=True)
        article = article_dir / "2026-06-05-two-blocks.md"
        article.write_text(
            "\n".join([
                "---",
                'source_url: "https://example.net/two-blocks"',
                "ingested: 2026-06-05",
                'title: "Two Blocks"',
                "---",
                "",
                "# Two Blocks",
                "",
                "## Summary",
                self.save_article.UNTRUSTED_BEGIN,
                "summary-only-needle",
                self.save_article.UNTRUSTED_END,
                "",
                "## Raw",
                self.save_article.UNTRUSTED_BEGIN,
                "raw-only-needle",
                self.save_article.UNTRUSTED_END,
                "",
            ]),
            encoding="utf-8",
        )

        summary_results = self.search_wiki.search(["summary-only-needle"])
        raw_results = self.search_wiki.search(["raw-only-needle"])

        self.assertEqual(len(summary_results), 1)
        self.assertEqual(len(raw_results), 1)


class FetchXTest(unittest.TestCase):
    """X/Twitter 取得（fetch_x）のネットワーク非依存テスト。"""

    def setUp(self):
        self.fetch_x = load_module("fetch_x")

    def test_is_x_url_and_id_extraction(self):
        fx = self.fetch_x
        self.assertTrue(fx.is_x_url("https://x.com/jack/status/20"))
        self.assertTrue(fx.is_x_url("https://twitter.com/i/web/status/12345"))
        self.assertTrue(fx.is_x_url("https://mobile.twitter.com/a/status/9"))
        self.assertFalse(fx.is_x_url("https://example.com/x/status/1"))
        self.assertEqual(fx.extract_tweet_id("https://x.com/jack/status/20"), "20")
        self.assertEqual(fx.extract_tweet_id("https://twitter.com/i/web/status/12345"), "12345")
        self.assertEqual(fx.extract_tweet_id("1779999999999999999"), "1779999999999999999")
        self.assertIsNone(fx.extract_tweet_id("https://x.com/jack"))

    def test_render_markdown_from_payload(self):
        tweet = {
            "__typename": "Tweet",
            "text": "hello https://t.co/abc world",
            "created_at": "2026-01-02T03:04:05.000Z",
            "user": {"name": "Jane", "screen_name": "jane"},
            "entities": {"urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.com/full"}]},
            "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/x.jpg"}],
            "quoted_tweet": {"text": "quoted body", "user": {"screen_name": "bob"}},
        }
        md = self.fetch_x.render_markdown(tweet)
        self.assertIn("**@jane** (Jane) ・ 2026-01-02T03:04:05.000Z", md)
        self.assertIn("https://example.com/full", md)          # t.co 展開
        self.assertNotIn("https://t.co/abc", md)
        self.assertIn("https://pbs.twimg.com/x.jpg", md)       # メディア
        self.assertIn("> 引用 @bob: quoted body", md)           # 引用

    def test_render_markdown_uses_note_tweet_full_text(self):
        tweet = {
            "__typename": "Tweet",
            "text": "truncated…",
            "user": {"name": "Long", "screen_name": "long"},
            "note_tweet": {"note_tweet_results": {"result": {"text": "the full long body"}}},
        }
        md = self.fetch_x.render_markdown(tweet)
        self.assertIn("the full long body", md)
        self.assertNotIn("truncated…", md)

    def test_title_for(self):
        md = "**@jack** (jack) ・ 2006-03-21T20:50:14.000Z\n\njust setting up my twttr\n"
        self.assertEqual(
            self.fetch_x.title_for(md, "https://x.com/jack/status/20"),
            "@jack: just setting up my twttr",
        )

    def test_article_render_and_info(self):
        tweet = {
            "__typename": "Tweet", "text": "https://t.co/x",
            "user": {"name": "Y", "screen_name": "ibu"},
            "entities": {"urls": [{"url": "https://t.co/x", "expanded_url": "http://x.com/i/article/777"}]},
            "article": {"rest_id": "777", "title": "GREAT TITLE", "preview_text": "preview here"},
        }
        md = self.fetch_x.render_markdown(tweet)
        self.assertIn("## X Article", md)
        self.assertIn("GREAT TITLE", md)
        self.assertIn("preview here", md)
        info = self.fetch_x.article_info(tweet)
        self.assertTrue(info["is_article"])
        self.assertEqual(info["article_url"], "https://x.com/i/article/777")

    def test_render_article_markdown_xurl(self):
        art = {
            "title": "T", "plain_text": "para one see https://t.co/z end",
            "entities": {
                "urls": [{"url": "https://t.co/z", "expanded_url": "https://example.com/deep"}],
                "code": [{"language": "sh", "content": "```sh\nls -la\n```", "code": "ls -la"}],
            },
        }
        md = self.fetch_x.render_article_markdown(art)
        self.assertIn("para one see https://example.com/deep end", md)  # 本文＋t.co展開
        self.assertNotIn("https://t.co/z", md)
        self.assertIn("## Code blocks", md)
        self.assertIn("ls -la", md)


class SaveArticleXTest(unittest.TestCase):
    """X Article のブラウザ全文取得（--article-body-file）の保存テスト。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.wiki = self.base / "wiki"
        self.home = self.base / "home"
        self.home.mkdir()
        self.save_article = load_module("save_article")
        self.save_article.WIKI_PATH = str(self.wiki)
        self.save_article.RAW_ARTICLES_DIR = str(self.wiki / "raw" / "articles")
        self.save_article.INDEX_PATH = str(self.wiki / "index.md")
        self.save_article.LOG_PATH = str(self.wiki / "log.md")
        self.save_article.INGEST_LOG = str(self.wiki / "raw" / "ingest_log.jsonl")
        self.save_article.SAVED_URLS_FILE = str(self.home / "saved-urls.txt")
        # X 判定を強制し、ツイート md（プレビュー）を返すようにする。
        # fetch_x は共有モジュールなので tearDown で必ず復元する（テスト汚染防止）。
        self._orig_is_x = self.save_article.fetch_x.is_x_url
        self.save_article.fetch_x.is_x_url = lambda u: True
        self.save_article.fetch_raw = lambda u: ("**@ibu** ・ d\n\npreview-needle\n", "utf-8(x)", None)

    def tearDown(self):
        self.save_article.fetch_x.is_x_url = self._orig_is_x
        self.tmp.cleanup()

    def test_article_body_goes_to_raw_preview_to_summary(self):
        body_file = self.base / "article.txt"
        body_file.write_text("FULL ARTICLE BODY needle", encoding="utf-8")
        old = sys.argv[:]
        try:
            sys.argv = ["save_article.py", "--url", "https://x.com/ibu/status/2062101068842975409",
                        "--article-body-file", str(body_file)]
            rc = self.save_article.main()
        finally:
            sys.argv = old
        self.assertEqual(rc, 0)
        article = list((self.wiki / "raw" / "articles").glob("*.md"))[0].read_text(encoding="utf-8")
        self.assertIn("## Summary", article)
        self.assertIn("preview-needle", article)            # ツイート/プレビュー → Summary
        self.assertIn("## Raw", article)
        self.assertIn("FULL ARTICLE BODY needle", article)   # 全文 → Raw

    def test_x_article_autofetches_full_text_via_xurl(self):
        fx = self.save_article.fetch_x
        orig_authed, orig_fetch = fx.xurl_authed, fx.fetch_article_via_xurl
        self.addCleanup(lambda: setattr(fx, "xurl_authed", orig_authed))
        self.addCleanup(lambda: setattr(fx, "fetch_article_via_xurl", orig_fetch))
        # syndication 由来本文（プレビュー＋ ## X Article マーカー）を返す
        self.save_article.fetch_raw = lambda u: (
            "**@ibu** ・ d\n\nhttp://x.com/i/article/777\n\n## X Article\n**T**\n\nprev-needle\n",
            "utf-8(x)", None,
        )
        fx.xurl_authed = lambda: True
        fx.fetch_article_via_xurl = lambda u: ("FULL-ARTICLE-needle body", "ARTICLE TITLE", None)

        old = sys.argv[:]
        try:
            sys.argv = ["save_article.py", "--url", "https://x.com/ibu/status/2062101068842975409"]
            rc = self.save_article.main()
        finally:
            sys.argv = old

        self.assertEqual(rc, 0)
        article = list((self.wiki / "raw" / "articles").glob("*.md"))[0].read_text(encoding="utf-8")
        self.assertIn('title: "ARTICLE TITLE"', article)            # 記事タイトル採用
        self.assertIn("## Summary", article)
        self.assertIn("prev-needle", article)                       # プレビュー → Summary
        self.assertIn("## Raw", article)
        self.assertIn("FULL-ARTICLE-needle body", article)          # xurl 全文 → Raw
        self.assertIn("x-article+xurl", article)


if __name__ == "__main__":
    unittest.main()
