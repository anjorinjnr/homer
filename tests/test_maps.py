"""
Tests for maps.py — Google Maps Places search and Distance Matrix.

Tests mock urlopen so no real API calls are made.
Verifies URL/param construction, response parsing, and error handling.
"""

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from io import BytesIO

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import maps


def make_response(data: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def args(**kwargs):
    defaults = dict(
        mode="places",
        query=None,
        near=None,
        max_results=5,
        place_id=None,
        origin=None,
        destination=None,
        travel_mode="driving",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


PLACES_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "name": "Circa Coffee",
            "formatted_address": "865 Main St, Anytown, ST 12345, USA",
            "rating": 4.8,
            "user_ratings_total": 337,
            "opening_hours": {"open_now": True},
            "place_id": "ChIJabc123",
            "types": ["cafe", "food", "establishment"],
        },
        {
            "name": "Kakao Cafe",
            "formatted_address": "998 Main St, Anytown, ST 12345, USA",
            "rating": 4.6,
            "user_ratings_total": 553,
            "opening_hours": {"open_now": False},
            "place_id": "ChIJdef456",
            "types": ["cafe", "food", "establishment"],
        },
    ],
}

DETAILS_RESPONSE = {
    "status": "OK",
    "result": {
        "name": "Circa Coffee",
        "formatted_address": "865 Main St, Anytown, ST 12345, USA",
        "formatted_phone_number": "(770) 683-7991",
        "website": "http://www.instagram.com/circa.coffee.roswell",
        "url": "https://maps.google.com/?cid=123",
        "rating": 4.8,
        "user_ratings_total": 337,
        "opening_hours": {
            "open_now": True,
            "weekday_text": [
                "Monday: 6:30 AM – 6:00 PM",
                "Tuesday: 6:30 AM – 6:00 PM",
            ],
        },
    },
}

DISTANCE_RESPONSE = {
    "status": "OK",
    "origin_addresses": ["Anytown, ST, USA"],
    "destination_addresses": ["Hartsfield-Jackson Atlanta International Airport, Atlanta, GA 30320, USA"],
    "rows": [
        {
            "elements": [
                {
                    "status": "OK",
                    "distance": {"text": "29.9 km", "value": 29900},
                    "duration": {"text": "25 mins", "value": 1500},
                }
            ]
        }
    ],
}


# ── API key ───────────────────────────────────────────────────────────────────

class TestGetApiKey(unittest.TestCase):

    def test_reads_from_env(self):
        with patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-maps-key"}):
            self.assertEqual(maps.get_api_key(), "test-maps-key")

    def test_exits_when_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                maps.get_api_key()


# ── Places search ─────────────────────────────────────────────────────────────

class TestDoPlaces(unittest.TestCase):

    def _run(self, captured_url, a, response=None):
        resp = response or PLACES_RESPONSE

        def fake_urlopen(url, timeout=None):
            captured_url.append(url)
            return make_response(resp)

        with patch("maps.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                maps.do_places(a.query, "maps-key", near=a.near, max_results=a.max_results)
                return json.loads(mock_print.call_args[0][0])

    def test_query_included_in_url(self):
        captured = []
        a = args(query="coffee shops near Anytown ST")
        self._run(captured, a)
        self.assertIn("coffee+shops+near+Anytown+ST", captured[0])

    def test_near_appended_to_query(self):
        captured = []
        a = args(query="plumbers", near="Anytown, ST")
        self._run(captured, a)
        self.assertIn("plumbers+near+Anytown%2C+ST", captured[0])

    def test_results_parsed_correctly(self):
        captured = []
        a = args(query="coffee near Anytown ST")
        out = self._run(captured, a)
        self.assertEqual(len(out["results"]), 2)
        r = out["results"][0]
        self.assertEqual(r["name"], "Circa Coffee")
        self.assertEqual(r["rating"], 4.8)
        self.assertEqual(r["open_now"], True)
        self.assertEqual(r["place_id"], "ChIJabc123")
        self.assertEqual(r["types"], ["cafe", "food", "establishment"])

    def test_max_results_limits_output(self):
        captured = []
        a = args(query="coffee near Anytown ST", max_results=1)
        out = self._run(captured, a)
        self.assertEqual(len(out["results"]), 1)

    def test_missing_opening_hours_handled(self):
        """Places without opening_hours don't crash."""
        resp = {
            "status": "OK",
            "results": [
                {"name": "Bob's Plumbing", "formatted_address": "123 Main St", "place_id": "abc",
                 "types": ["plumber"]},
            ],
        }
        captured = []
        a = args(query="plumbers near Anytown ST")
        out = self._run(captured, a, resp)
        self.assertIsNone(out["results"][0]["open_now"])

    def test_zero_results_returns_empty_list(self):
        resp = {"status": "ZERO_RESULTS", "results": []}
        captured = []
        a = args(query="obscure business near nowhere")
        out = self._run(captured, a, resp)
        self.assertEqual(out["results"], [])

    def test_types_truncated_to_3(self):
        resp = {
            "status": "OK",
            "results": [
                {"name": "Place", "formatted_address": "Addr", "place_id": "x",
                 "types": ["a", "b", "c", "d", "e"]},
            ],
        }
        captured = []
        a = args(query="test")
        out = self._run(captured, a, resp)
        self.assertEqual(len(out["results"][0]["types"]), 3)


# ── Place details ─────────────────────────────────────────────────────────────

class TestDoDetails(unittest.TestCase):

    def _run(self, captured_url, place_id, response=None):
        resp = response or DETAILS_RESPONSE

        def fake_urlopen(url, timeout=None):
            captured_url.append(url)
            return make_response(resp)

        with patch("maps.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                maps.do_details(place_id, "maps-key")
                return json.loads(mock_print.call_args[0][0])

    def test_place_id_in_url(self):
        captured = []
        self._run(captured, "ChIJabc123")
        self.assertIn("ChIJabc123", captured[0])

    def test_fields_in_url(self):
        captured = []
        self._run(captured, "ChIJabc123")
        self.assertIn("opening_hours", captured[0])
        self.assertIn("formatted_phone_number", captured[0])

    def test_response_parsed_correctly(self):
        captured = []
        out = self._run(captured, "ChIJabc123")
        self.assertEqual(out["name"], "Circa Coffee")
        self.assertEqual(out["phone"], "(770) 683-7991")
        self.assertEqual(out["website"], "http://www.instagram.com/circa.coffee.roswell")
        self.assertEqual(out["rating"], 4.8)
        self.assertEqual(out["open_now"], True)
        self.assertEqual(len(out["hours"]), 2)

    def test_missing_optional_fields_return_none(self):
        resp = {
            "status": "OK",
            "result": {"name": "No-Frills Place", "formatted_address": "123 St"},
        }
        captured = []
        out = self._run(captured, "ChIJxyz", resp)
        self.assertIsNone(out["phone"])
        self.assertIsNone(out["website"])
        self.assertIsNone(out["open_now"])
        self.assertEqual(out["hours"], [])


# ── Distance ──────────────────────────────────────────────────────────────────

class TestDoDistance(unittest.TestCase):

    def setUp(self):
        # Distance tests rely on a fake household.md unless they set their
        # own origin (test_custom_origin_used_when_provided) or point at a
        # household.md without an Address (test_no_origin_and_no_address_errors).
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._primary = Path(self._tmpdir.name) / "household.md"
        self._primary.write_text("## Home\n- **Address**: Anytown, ST\n", encoding="utf-8")
        self._fallback = Path(self._tmpdir.name) / "household_fallback.md"
        self._patch = patch.object(
            maps,
            "HOUSEHOLD_MD_CANDIDATES",
            [self._primary, self._fallback],
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _run(self, captured_url, a, response=None):
        resp = response or DISTANCE_RESPONSE

        def fake_urlopen(url, timeout=None):
            captured_url.append(url)
            return make_response(resp)

        with patch("maps.urlopen", fake_urlopen):
            with patch("builtins.print") as mock_print:
                maps.do_distance(a.destination, "maps-key", origin=a.origin, travel_mode=a.travel_mode)
                return json.loads(mock_print.call_args[0][0])

    def test_default_origin_from_household_md(self):
        captured = []
        a = args(destination="Atlanta Airport", travel_mode="driving")
        self._run(captured, a)
        self.assertIn("Anytown%2C+ST", captured[0])

    def test_no_origin_and_no_address_errors(self):
        # Point at a household.md without any Address line.
        self._primary.write_text("## Home\n- **Type**: house\n", encoding="utf-8")
        a = args(destination="Atlanta Airport", travel_mode="driving")
        with patch("builtins.print") as mock_print:
            with self.assertRaises(SystemExit):
                maps.do_distance(a.destination, "maps-key", travel_mode=a.travel_mode)
            out = json.loads(mock_print.call_args[0][0])
            self.assertIn("error", out)
            self.assertIn("household.md", out["error"])

    def test_address_parsed_case_insensitive_and_trims_whitespace(self):
        self._primary.write_text(
            "- **address**:   1600 Penn Ave, DC   \n",
            encoding="utf-8",
        )
        self.assertEqual(maps.get_default_origin(), "1600 Penn Ave, DC")

    def test_missing_household_md_returns_empty(self):
        with patch.object(maps, "HOUSEHOLD_MD_CANDIDATES", [Path("/nonexistent/household.md")]):
            self.assertEqual(maps.get_default_origin(), "")

    def test_falls_back_to_root_household_md_when_user_context_missing(self):
        # Primary (user_context) doesn't exist; fallback (root) does.
        self._primary.unlink()
        self._fallback.write_text("- **Address**: Atlanta, GA\n", encoding="utf-8")
        self.assertEqual(maps.get_default_origin(), "Atlanta, GA")

    def test_user_context_preferred_when_both_exist(self):
        self._fallback.write_text("- **Address**: Atlanta, GA\n", encoding="utf-8")
        # _primary already has "Anytown, ST" from setUp.
        self.assertEqual(maps.get_default_origin(), "Anytown, ST")

    def test_custom_origin_used_when_provided(self):
        captured = []
        a = args(destination="Atlanta Airport", origin="Othertown, ST", travel_mode="driving")
        self._run(captured, a)
        self.assertIn("Othertown%2C+ST", captured[0])

    def test_travel_mode_in_url(self):
        captured = []
        a = args(destination="Othertown ST", travel_mode="transit")
        self._run(captured, a)
        self.assertIn("transit", captured[0])

    def test_response_parsed_correctly(self):
        captured = []
        a = args(destination="Atlanta Airport", travel_mode="driving")
        out = self._run(captured, a)
        self.assertEqual(out["distance"], "29.9 km")
        self.assertEqual(out["duration"], "25 mins")
        self.assertEqual(out["travel_mode"], "driving")
        self.assertIn("Anytown", out["origin"])
        self.assertIn("Atlanta", out["destination"])

    def test_no_route_exits_with_error(self):
        resp = {
            "status": "OK",
            "origin_addresses": ["Anytown, ST"],
            "destination_addresses": ["Middle of Ocean"],
            "rows": [{"elements": [{"status": "ZERO_RESULTS"}]}],
        }
        captured = []
        a = args(destination="Middle of Ocean", travel_mode="driving")
        with patch("maps.urlopen", lambda url, timeout=None: make_response(resp)):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    maps.do_distance(a.destination, "maps-key", travel_mode=a.travel_mode)
                out = json.loads(mock_print.call_args[0][0])
                self.assertIn("error", out)


# ── API error handling ────────────────────────────────────────────────────────

class TestApiErrors(unittest.TestCase):

    def test_invalid_api_key_exits(self):
        resp = {"status": "REQUEST_DENIED", "error_message": "API key invalid"}
        with patch("maps.urlopen", lambda url, timeout=None: make_response(resp)):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    maps.get(maps.PLACES_SEARCH_URL, {"key": "bad"})
                out = json.loads(mock_print.call_args[0][0])
                self.assertIn("REQUEST_DENIED", out["error"])

    def test_http_error_exits(self):
        err = HTTPError(url=maps.PLACES_SEARCH_URL, code=403,
                        msg="Forbidden", hdrs=None, fp=BytesIO(b"forbidden"))
        with patch("maps.urlopen", side_effect=err):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    maps.get(maps.PLACES_SEARCH_URL, {"key": "k"})
                out = json.loads(mock_print.call_args[0][0])
                self.assertIn("403", out["error"])

    def test_url_error_exits(self):
        with patch("maps.urlopen", side_effect=URLError("Network unreachable")):
            with patch("builtins.print") as mock_print:
                with self.assertRaises(SystemExit):
                    maps.get(maps.PLACES_SEARCH_URL, {"key": "k"})
                out = json.loads(mock_print.call_args[0][0])
                self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
