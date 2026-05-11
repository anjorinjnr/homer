"""Tests for skyvern_webhook.py — signature validation and path traversal."""

import hashlib
import hmac
import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import skyvern_webhook as wh


API_KEY = "test-secret-key"


def _make_handler(body: bytes, headers: dict, results_dir: Path):
    """Build a WebhookHandler with mocked socket/request for testing."""
    wh.RESULTS_DIR = results_dir

    handler = wh.WebhookHandler.__new__(wh.WebhookHandler)
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.headers = headers
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def _sign(body: bytes, key: str = API_KEY) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def _payload(run_id="tsk_v2_abc123", status="completed", output=None) -> bytes:
    return json.dumps({
        "run_id": run_id,
        "status": status,
        "output": output or {"price": "$30"},
        "app_url": "https://app.skyvern.com/runs/wr_1",
    }).encode()


@pytest.fixture(autouse=True)
def set_api_key():
    original = wh.SKYVERN_API_KEY
    wh.SKYVERN_API_KEY = API_KEY
    yield
    wh.SKYVERN_API_KEY = original


@pytest.fixture()
def results_dir(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------

def test_valid_signature_accepted(results_dir):
    body = _payload()
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(200)


def test_invalid_signature_rejected(results_dir):
    body = _payload()
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": "deadbeef",
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(401)


def test_oversized_payload_rejected(results_dir):
    """Content-Length over 5MB must return 413 before reading the body."""
    body = _payload()
    headers = {
        "Content-Length": str(5 * 1024 * 1024 + 1),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(413)
    assert not list(results_dir.iterdir())  # no file written


def test_invalid_content_length_returns_400(results_dir):
    """Non-numeric Content-Length must return 400, not crash the handler."""
    body = _payload()
    headers = {"Content-Length": "abc", "x-skyvern-signature": _sign(body)}
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(400)


def test_missing_signature_rejected(results_dir):
    body = _payload()
    headers = {"Content-Length": str(len(body))}
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(401)


def test_missing_api_key_fails_closed(results_dir):
    """Webhook must reject when SKYVERN_API_KEY is not configured."""
    original = wh.SKYVERN_API_KEY
    try:
        wh.SKYVERN_API_KEY = ""
        body = _payload()
        headers = {
            "Content-Length": str(len(body)),
            "x-skyvern-signature": _sign(body),
        }
        handler = _make_handler(body, headers, results_dir)
        handler.do_POST()
        handler.send_response.assert_called_with(401)
    finally:
        wh.SKYVERN_API_KEY = original


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

def test_path_traversal_run_id_rejected(results_dir):
    body = _payload(run_id="../../../tmp/pwn")
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(400)
    assert not list(results_dir.iterdir())  # no file written


def test_run_id_with_slash_rejected(results_dir):
    body = _payload(run_id="foo/bar")
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()
    handler.send_response.assert_called_with(400)


# ---------------------------------------------------------------------------
# Happy path — result written correctly
# ---------------------------------------------------------------------------

def test_result_file_written(results_dir):
    body = _payload(run_id="tsk_v2_abc123", output={"price": "$29"})
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()

    result_file = results_dir / "tsk_v2_abc123.json"
    assert result_file.exists()
    data = json.loads(result_file.read_text())
    assert data["status"] == "completed"
    assert data["output"]["price"] == "$29"


def test_failed_task_includes_failure_reason(results_dir):
    body = json.dumps({
        "run_id": "tsk_v2_fail",
        "status": "failed",
        "failure_reason": "Could not find button",
        "app_url": "",
    }).encode()
    headers = {
        "Content-Length": str(len(body)),
        "x-skyvern-signature": _sign(body),
    }
    handler = _make_handler(body, headers, results_dir)
    handler.do_POST()

    data = json.loads((results_dir / "tsk_v2_fail.json").read_text())
    assert data["failure_reason"] == "Could not find button"
