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
        self.search_wiki = load_module("search_wiki")

        self.ingest.WIKI_PATH = str(self.wiki)
        self.ingest.RAW_ARTICLES_DIR = str(self.wiki / "raw" / "articles")
        self.ingest.INDEX_PATH = str(self.wiki / "index.md")
        self.ingest.LOG_PATH = str(self.wiki / "log.md")
        self.ingest.INGEST_LOG = str(self.wiki / "raw" / "ingest_log.jsonl")
        self.ingest.SAVED_URLS_FILE = str(self.home / "saved-urls.txt")
        self.ingest.IMPORT_LOG = str(self.home / ".hermes" / "url_import_log.txt")

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


if __name__ == "__main__":
    unittest.main()
