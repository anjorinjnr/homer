"""Tests for breeze_roster.py — BreezeRoster API client and CLI."""

import json
import sys
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tools.breeze_roster as br


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ENV_VARS = {
    "BREEZE_BASE_URL": "https://breeze.example.com",
    "BREEZE_CLIENT_ID": "brzid_test",
    "BREEZE_CLIENT_SECRET": "brzsk_test",
}

FAKE_TOKEN = "test_access_token_abc"
TOKEN_RESPONSE = json.dumps({"access_token": FAKE_TOKEN}).encode()


@pytest.fixture(autouse=True)
def breeze_env(monkeypatch, tmp_path):
    for k, v in ENV_VARS.items():
        monkeypatch.setenv(k, v)
    # Redirect token cache to tmp dir to avoid leaking between tests
    monkeypatch.setattr(
        br.BreezeClient,
        "_token_cache_path",
        lambda self: tmp_path / ".breeze_token_test.json",
    )


def _mock_urlopen(responses: list[bytes]):
    """Return a context-manager mock that yields responses in sequence."""
    call_count = 0

    class _Resp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _open(req, timeout=None):
        nonlocal call_count
        resp = responses[call_count % len(responses)]
        call_count += 1
        return _Resp(resp)

    return _open


def _capture(argv: list[str]) -> dict:
    old_stdout, sys.stdout = sys.stdout, StringIO()
    try:
        br.main()
    except SystemExit:
        pass
    finally:
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
    sys.argv = ["breeze_roster.py"]
    return json.loads(output)


# ---------------------------------------------------------------------------
# BreezeClient — token management
# ---------------------------------------------------------------------------

class TestTokenExchange:
    def test_fetches_and_caches_token(self, tmp_path):
        client = br.BreezeClient()
        responses = [TOKEN_RESPONSE]
        with patch.object(urllib.request, "urlopen", side_effect=_mock_urlopen(responses)):
            token = client._fetch_token()
        assert token == FAKE_TOKEN
        cache = json.loads(client._token_cache_path().read_text())
        assert cache["access_token"] == FAKE_TOKEN
        assert cache["expires_at"] > 0

    def test_uses_cached_token(self, tmp_path):
        import time
        client = br.BreezeClient()
        cache_data = {"access_token": "cached_token", "expires_at": time.time() + 3600}
        client._token_cache_path().write_text(json.dumps(cache_data))
        with patch.object(urllib.request, "urlopen") as mock_open:
            token = client._token()
        mock_open.assert_not_called()
        assert token == "cached_token"

    def test_expired_cache_refetches(self, tmp_path):
        import time
        client = br.BreezeClient()
        cache_data = {"access_token": "old_token", "expires_at": time.time() - 10}
        client._token_cache_path().write_text(json.dumps(cache_data))
        with patch.object(urllib.request, "urlopen", side_effect=_mock_urlopen([TOKEN_RESPONSE])):
            token = client._token()
        assert token == FAKE_TOKEN

    def test_missing_env_vars_exits(self, monkeypatch):
        monkeypatch.delenv("BREEZE_CLIENT_ID")
        with pytest.raises(SystemExit):
            br.BreezeClient()

    def test_token_exchange_http_error_exits(self):
        client = br.BreezeClient()
        err = urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)
        err.read = lambda: b'{"message":"bad credentials"}'
        with patch.object(urllib.request, "urlopen", side_effect=err):
            with pytest.raises(SystemExit):
                client._fetch_token()


# ---------------------------------------------------------------------------
# BreezeClient — request dispatch
# ---------------------------------------------------------------------------

class TestClientRequests:
    def _client_with_token(self):
        import time
        client = br.BreezeClient()
        cache = {"access_token": FAKE_TOKEN, "expires_at": time.time() + 3600}
        client._token_cache_path().write_text(json.dumps(cache))
        return client

    def test_get_sets_bearer_header(self):
        client = self._client_with_token()
        captured_req = {}

        def _open(req, timeout=None):
            captured_req["headers"] = dict(req.headers)
            captured_req["url"] = req.full_url
            captured_req["method"] = req.get_method()

            class _R:
                def read(self):
                    return b'{"teams":[]}'
                def __enter__(self): return self
                def __exit__(self, *a): pass

            return _R()

        with patch.object(urllib.request, "urlopen", side_effect=_open):
            client.list_teams()

        assert f"Bearer {FAKE_TOKEN}" in captured_req["headers"].get("Authorization", "")
        assert captured_req["method"] == "GET"

    def test_post_sends_json_body(self):
        client = self._client_with_token()
        sent_body = {}

        def _open(req, timeout=None):
            sent_body["data"] = req.data

            class _R:
                def read(self): return b'{"status":"ok"}'
                def __enter__(self): return self
                def __exit__(self, *a): pass

            return _R()

        with patch.object(urllib.request, "urlopen", side_effect=_open):
            client.generate_schedule("sched-1")

        assert sent_body["data"] is not None
        body = json.loads(sent_body["data"])
        assert isinstance(body, dict)

    def test_http_error_on_get_exits(self):
        client = self._client_with_token()
        err = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        err.read = lambda: b'{"error":"not found"}'
        with patch.object(urllib.request, "urlopen", side_effect=err):
            with pytest.raises(SystemExit):
                client.get_team("bad-id")


# ---------------------------------------------------------------------------
# CLI — argument routing
# ---------------------------------------------------------------------------

class TestCLIRouting:
    def _run(self, argv: list[str], api_response: dict) -> dict:
        import time
        client = br.BreezeClient()
        cache = {"access_token": FAKE_TOKEN, "expires_at": time.time() + 3600}
        client._token_cache_path().write_text(json.dumps(cache))

        response_bytes = json.dumps(api_response).encode()
        sys.argv = ["breeze_roster.py"] + argv

        old_stdout, sys.stdout = sys.stdout, StringIO()
        try:
            with patch.object(urllib.request, "urlopen", side_effect=_mock_urlopen([response_bytes])):
                br.main()
        except SystemExit:
            pass
        finally:
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

        return json.loads(output)

    def test_list_teams(self):
        data = {"teams": [{"id": "t1", "name": "Worship"}]}
        result = self._run(["--list-teams"], data)
        assert result == data

    def test_get_team(self):
        data = {"id": "t1", "name": "Worship Team"}
        result = self._run(["--get-team", "t1"], data)
        assert result["id"] == "t1"

    def test_list_roster_requires_team(self):
        sys.argv = ["breeze_roster.py", "--list-roster"]
        old_stdout, sys.stdout = sys.stdout, StringIO()
        with pytest.raises(SystemExit):
            br.main()
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        err = json.loads(output)
        assert "error" in err

    def test_search_volunteers(self):
        data = [{"id": "v1", "name": "Jane Doe"}]
        result = self._run(["--search-volunteers", "Jane"], data)
        assert result == data

    def test_assign_slot_requires_volunteer(self):
        sys.argv = ["breeze_roster.py", "--assign-slot", "slot-1"]
        old_stdout, sys.stdout = sys.stdout, StringIO()
        with pytest.raises(SystemExit):
            br.main()
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        err = json.loads(output)
        assert "error" in err

    def test_unassign_slot_sends_null_volunteer(self):
        data = {"id": "slot-1", "volunteerId": None}
        sent_bodies = []

        import time
        client = br.BreezeClient()
        cache = {"access_token": FAKE_TOKEN, "expires_at": time.time() + 3600}
        client._token_cache_path().write_text(json.dumps(cache))

        def _open(req, timeout=None):
            if req.data:
                sent_bodies.append(json.loads(req.data))

            class _R:
                def read(self): return json.dumps(data).encode()
                def __enter__(self): return self
                def __exit__(self, *a): pass

            return _R()

        sys.argv = ["breeze_roster.py", "--unassign-slot", "slot-1"]
        old_stdout, sys.stdout = sys.stdout, StringIO()
        with patch.object(urllib.request, "urlopen", side_effect=_open):
            br.main()
        sys.stdout = old_stdout

        assert any(b.get("volunteerId") is None for b in sent_bodies)

    def test_get_availability_requires_schedule(self):
        sys.argv = ["breeze_roster.py", "--get-availability"]
        old_stdout, sys.stdout = sys.stdout, StringIO()
        with pytest.raises(SystemExit):
            br.main()
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        err = json.loads(output)
        assert "error" in err

    def test_parse_rule(self):
        data = {"rules": [{"type": "max_consecutive", "value": 1}]}
        result = self._run(["--parse-rule", "No one leads two Sundays in a row"], data)
        assert result == data

    def test_no_args_exits_nonzero(self):
        sys.argv = ["breeze_roster.py"]
        with pytest.raises(SystemExit) as exc:
            br.main()
        assert exc.value.code != 0
