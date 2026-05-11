"""
Tests for tavily.py — web search, research, and URL extraction.

Tests mock urlopen so no real API calls are made.
Verifies payload construction, response parsing, truncation, and error handling.
"""

import argparse
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import tavily


def make_response(data: dict, status: int = 200):
    """Return a mock urlopen context manager with a JSON response."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def args(**kwargs):
    """Build a minimal argparse.Namespace with defaults for search."""
    defaults = dict(
        mode="search",
        query=None,
        depth="basic",
        max_results=10,
        time_range=None,
        start_date=None,
        end_date=None,
        include_domains=None,
        exclude_domains=None,
        country=None,
        include_raw_content=False,
        model=None,
        urls=None,
        chunks_per_source=None,
        extract_depth=None,
        timeout=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ── API key ───────────────────────────────────────────────────────────────────

class TestGetApiKey(unittest.TestCase):

    def test_reads_from_env(self):
        with patch.dict("os.environ", {"TAVILY_API_KEY": "tvly-test-key"}):
            self.assertEqual(tavily.get_api_key(), "tvly-test-key")

    def test_exits_when_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                tavily.get_api_key()


# ── Search ────────────────────────────────────────────────────────────────────

class TestDoSearch(unittest.TestCase):

    def _run(self, captured_payload, a, response_data=None):
        """Run do_search, capture the payload sent and stdout."""
        if response_data is None:
            response_data = {"results": [], "response_time": 0.5}

        def fake_urlopen(req, timeout=None):
            captured_payload.append(json.loads(req.data.decode()))
            return make_response(response_data)

        with patch("tavily.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                tavily.do_search(a, "tvly-key")
                return json.loads(mock_print.call_args[0][0])

    def test_minimal_payload(self):
        payload = []
        a = args(query="mortgage rates", mode="search")
        self._run(payload, a)
        self.assertEqual(payload[0]["query"], "mortgage rates")
        self.assertEqual(payload[0]["search_depth"], "basic")
        self.assertEqual(payload[0]["max_results"], 10)
        self.assertNotIn("time_range", payload[0])
        self.assertNotIn("include_domains", payload[0])

    def test_optional_params_included_when_set(self):
        payload = []
        a = args(
            query="AI news",
            mode="search",
            time_range="week",
            start_date="2026-01-01",
            end_date="2026-03-01",
            include_domains="arxiv.org,github.com",
            exclude_domains="spam.com",
            country="US",
            depth="advanced",
            max_results=5,
        )
        self._run(payload, a)
        p = payload[0]
        self.assertEqual(p["time_range"], "week")
        self.assertEqual(p["start_date"], "2026-01-01")
        self.assertEqual(p["end_date"], "2026-03-01")
        self.assertEqual(p["include_domains"], ["arxiv.org", "github.com"])
        self.assertEqual(p["exclude_domains"], ["spam.com"])
        self.assertEqual(p["country"], "US")
        self.assertEqual(p["search_depth"], "advanced")
        self.assertEqual(p["max_results"], 5)

    def test_include_raw_content_flag(self):
        payload = []
        a = args(query="test", mode="search", include_raw_content=True)
        self._run(payload, a)
        self.assertTrue(payload[0].get("include_raw_content"))

    def test_raw_content_preferred_over_snippet(self):
        payload = []
        response = {
            "results": [{"title": "T", "url": "https://x.com", "content": "snippet", "raw_content": "full text", "score": 0.9}],
            "response_time": 0.5,
        }
        a = args(query="test", mode="search", include_raw_content=True)
        out = self._run(payload, a, response)
        self.assertEqual(out["results"][0]["content"], "full text")

    def test_result_parsing(self):
        payload = []
        response = {
            "results": [
                {"title": "Page A", "url": "https://a.com", "content": "snippet A", "score": 0.95},
                {"title": "Page B", "url": "https://b.com", "content": "snippet B", "score": 0.80},
            ],
            "response_time": 1.2,
        }
        a = args(query="test", mode="search")
        out = self._run(payload, a, response)
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["results"][0]["title"], "Page A")
        self.assertEqual(out["results"][0]["score"], 0.95)
        self.assertEqual(out["response_time"], 1.2)

    def test_content_truncated_at_max_chars(self):
        long_content = "x" * (tavily.MAX_CONTENT_CHARS + 500)
        payload = []
        response = {
            "results": [{"title": "T", "url": "https://x.com", "content": long_content, "score": 0.9}],
            "response_time": 0.5,
        }
        a = args(query="test", mode="search")
        out = self._run(payload, a, response)
        self.assertLessEqual(len(out["results"][0]["content"]), tavily.MAX_CONTENT_CHARS + 50)
        self.assertIn("[truncated]", out["results"][0]["content"])

    def test_score_rounded_to_3dp(self):
        payload = []
        response = {
            "results": [{"title": "T", "url": "https://x.com", "content": "c", "score": 0.849999}],
            "response_time": 0.1,
        }
        a = args(query="test", mode="search")
        out = self._run(payload, a, response)
        self.assertEqual(out["results"][0]["score"], 0.85)


# ── Research ──────────────────────────────────────────────────────────────────

class TestDoResearch(unittest.TestCase):

    def _run(self, captured_payload, a, response_data=None):
        if response_data is None:
            response_data = {"answer": "Some answer.", "sources": []}

        def fake_urlopen(req, timeout=None):
            captured_payload.append(json.loads(req.data.decode()))
            return make_response(response_data)

        with patch("tavily.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                tavily.do_research(a, "tvly-key")
                return json.loads(mock_print.call_args[0][0])

    def test_minimal_payload(self):
        payload = []
        a = args(query="quantum computing trends", mode="research")
        self._run(payload, a)
        self.assertEqual(payload[0]["query"], "quantum computing trends")
        self.assertEqual(payload[0]["search_depth"], "advanced")
        self.assertNotIn("model", payload[0])

    def test_model_included_when_set(self):
        payload = []
        a = args(query="compare X vs Y", mode="research", model="pro")
        self._run(payload, a)
        self.assertEqual(payload[0]["model"], "pro")

    def test_answer_and_sources_parsed(self):
        payload = []
        response = {
            "answer": "Detailed answer here.",
            "sources": [
                {"title": "Source A", "url": "https://a.com"},
                {"title": "Source B", "url": "https://b.com"},
            ],
        }
        a = args(query="test", mode="research")
        out = self._run(payload, a, response)
        self.assertEqual(out["answer"], "Detailed answer here.")
        self.assertEqual(len(out["sources"]), 2)
        self.assertEqual(out["sources"][0]["title"], "Source A")

    def test_response_field_fallback(self):
        """Some Tavily research responses use 'response' instead of 'answer'."""
        payload = []
        response = {"response": "Alternative answer field.", "sources": []}
        a = args(query="test", mode="research")
        out = self._run(payload, a, response)
        self.assertEqual(out["answer"], "Alternative answer field.")

    def test_results_used_as_sources_fallback(self):
        """If 'sources' absent, fall back to 'results' for source list."""
        payload = []
        response = {
            "answer": "Answer.",
            "results": [{"title": "R1", "url": "https://r1.com"}],
        }
        a = args(query="test", mode="research")
        out = self._run(payload, a, response)
        self.assertEqual(out["sources"][0]["title"], "R1")


# ── Extract ───────────────────────────────────────────────────────────────────

class TestDoExtract(unittest.TestCase):

    def _run(self, captured_payload, a, response_data=None):
        if response_data is None:
            response_data = {"results": [], "failed_results": []}

        def fake_urlopen(req, timeout=None):
            captured_payload.append(json.loads(req.data.decode()))
            return make_response(response_data)

        with patch("tavily.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                tavily.do_extract(a, "tvly-key")
                return json.loads(mock_print.call_args[0][0])

    def test_single_url_payload(self):
        payload = []
        a = args(urls="https://example.com/article", mode="extract")
        self._run(payload, a)
        self.assertEqual(payload[0]["urls"], ["https://example.com/article"])
        self.assertNotIn("query", payload[0])
        self.assertNotIn("chunks_per_source", payload[0])

    def test_multiple_urls_split_correctly(self):
        payload = []
        a = args(urls="https://a.com, https://b.com , https://c.com", mode="extract")
        self._run(payload, a)
        self.assertEqual(payload[0]["urls"], ["https://a.com", "https://b.com", "https://c.com"])

    def test_query_and_chunks_included_when_set(self):
        payload = []
        a = args(urls="https://example.com", mode="extract", query="pricing", chunks_per_source=3)
        self._run(payload, a)
        self.assertEqual(payload[0]["query"], "pricing")
        self.assertEqual(payload[0]["chunks_per_source"], 3)

    def test_extract_depth_and_timeout(self):
        payload = []
        a = args(urls="https://app.example.com", mode="extract", extract_depth="advanced", timeout=60.0)
        self._run(payload, a)
        self.assertEqual(payload[0]["extract_depth"], "advanced")
        self.assertEqual(payload[0]["timeout"], 60.0)

    def test_result_parsing(self):
        payload = []
        response = {
            "results": [{"url": "https://a.com", "raw_content": "# Title\n\nContent here."}],
            "failed_results": [],
        }
        a = args(urls="https://a.com", mode="extract")
        out = self._run(payload, a, response)
        self.assertEqual(out["results"][0]["url"], "https://a.com")
        self.assertIn("Content here.", out["results"][0]["content"])

    def test_failed_urls_reported(self):
        payload = []
        response = {
            "results": [{"url": "https://a.com", "raw_content": "content"}],
            "failed_results": [{"url": "https://bad.com"}],
        }
        a = args(urls="https://a.com,https://bad.com", mode="extract")
        out = self._run(payload, a, response)
        self.assertIn("https://bad.com", out["failed"])

    def test_content_truncated_at_max_chars(self):
        long_content = "y" * (tavily.MAX_CONTENT_CHARS + 200)
        payload = []
        response = {
            "results": [{"url": "https://a.com", "raw_content": long_content}],
            "failed_results": [],
        }
        a = args(urls="https://a.com", mode="extract")
        out = self._run(payload, a, response)
        self.assertIn("[truncated]", out["results"][0]["content"])


# ── HTTP error handling ───────────────────────────────────────────────────────

class TestHttpErrors(unittest.TestCase):

    def test_http_error_exits_with_message(self):
        from urllib.error import HTTPError
        err = HTTPError(url="https://api.tavily.com/search", code=401,
                        msg="Unauthorized", hdrs=None, fp=BytesIO(b'{"error":"invalid key"}'))
        with patch("tavily.urlopen", side_effect=err):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    tavily.post(tavily.SEARCH_URL, {}, "bad-key")
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("401", out["error"])

    def test_url_error_exits_with_message(self):
        from urllib.error import URLError
        with patch("tavily.urlopen", side_effect=URLError("Name or service not known")):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    tavily.post(tavily.SEARCH_URL, {}, "tvly-key")
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
