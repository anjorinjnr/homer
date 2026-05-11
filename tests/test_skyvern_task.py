"""Tests for skyvern_task.py — mocks the Skyvern client."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import skyvern_task as st


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setattr(st, "SKYVERN_API_KEY", "test-key")
    monkeypatch.setattr(st, "SKYVERN_WEBHOOK_URL", "")


@pytest.fixture()
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "RESULTS_DIR", tmp_path)
    return tmp_path


def _mock_submit_result(run_id="tsk_v2_abc123", app_url="https://app.skyvern.com/runs/wr_1"):
    result = MagicMock()
    result.run_id = run_id
    result.app_url = app_url
    return result


def _mock_get_result(run_id, status="completed", output=None, failure_reason=None):
    result = MagicMock()
    result.run_id = run_id
    result.status = status
    result.output = output
    result.app_url = "https://app.skyvern.com/runs/wr_1"
    result.failure_reason = failure_reason
    return result


# ---------------------------------------------------------------------------
# submit_task
# ---------------------------------------------------------------------------

def test_submit_returns_run_id():
    mock_result = _mock_submit_result()
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.run_task = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        result = st.submit_task("Check ticket prices", url="https://example.com")

    assert result["status"] == "submitted"
    assert result["run_id"] == "tsk_v2_abc123"
    assert "app_url" in result


def test_submit_passes_webhook_url(monkeypatch):
    monkeypatch.setattr(st, "SKYVERN_WEBHOOK_URL", "https://myhomer.com/skyvern/webhook")
    mock_result = _mock_submit_result()
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.run_task = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        st.submit_task("Check prices")
        call_kwargs = client.run_task.call_args.kwargs

    assert call_kwargs["webhook_url"] == "https://myhomer.com/skyvern/webhook"
    assert call_kwargs["wait_for_completion"] is False


def test_submit_reads_data_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
    (tmp_path / "tmp").mkdir()
    data_file = tmp_path / "tmp" / "data.json"
    data_file.write_text('{"adult_qty": 2, "date": "2026-04-05"}')
    mock_result = _mock_submit_result()
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.run_task = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        st.submit_task("Buy tickets", data_file=str(data_file))
        call_kwargs = client.run_task.call_args.kwargs

    assert call_kwargs["data"] == {"adult_qty": 2, "date": "2026-04-05"}


def test_submit_missing_data_file_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
    (tmp_path / "tmp").mkdir()
    with pytest.raises(SystemExit):
        st.submit_task("Buy tickets", data_file=str(tmp_path / "tmp" / "missing.json"))
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_submit_data_file_is_directory_exits(tmp_path, monkeypatch, capsys):
    """Passing a directory path as --data-file must return JSON error, not raise IsADirectoryError."""
    monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
    (tmp_path / "tmp").mkdir()
    with pytest.raises(SystemExit):
        st.submit_task("Buy tickets", data_file=str(tmp_path / "tmp"))
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_submit_data_file_outside_tmp_rejected(tmp_path, monkeypatch, capsys):
    """--data-file outside workspace tmp/ must be rejected to prevent arbitrary file reads."""
    monkeypatch.setenv("HOMER_WORKSPACE", str(tmp_path))
    evil_file = tmp_path / "secrets" / ".env"
    evil_file.parent.mkdir()
    evil_file.write_text("ANTHROPIC_API_KEY=sk-real-key")
    with pytest.raises(SystemExit):
        st.submit_task("Buy tickets", data_file=str(evil_file))
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_submit_no_webhook_when_url_empty(monkeypatch):
    monkeypatch.setattr(st, "SKYVERN_WEBHOOK_URL", "")
    mock_result = _mock_submit_result()
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.run_task = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        st.submit_task("Check prices")
        call_kwargs = client.run_task.call_args.kwargs

    assert "webhook_url" not in call_kwargs


def test_submit_missing_api_key_exits(monkeypatch, capsys):
    monkeypatch.setattr(st, "SKYVERN_API_KEY", "")
    with pytest.raises(SystemExit):
        st.submit_task("anything")
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


# ---------------------------------------------------------------------------
# check_task — from local file
# ---------------------------------------------------------------------------

def test_check_reads_local_file(results_dir):
    run_id = "tsk_v2_xyz"
    expected = {"status": "completed", "run_id": run_id, "output": {"price": "$29"}, "app_url": ""}
    (results_dir / f"{run_id}.json").write_text(json.dumps(expected))

    result = st.check_task(run_id)
    assert result["status"] == "completed"
    assert result["output"]["price"] == "$29"


def test_check_falls_back_to_api_when_no_file(results_dir):
    mock_result = _mock_get_result("tsk_v2_abc", status="completed", output={"price": "$30"})
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.get_run = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        result = st.check_task("tsk_v2_abc")

    assert result["status"] == "completed"


def test_check_failed_task_includes_failure_reason(results_dir):
    mock_result = _mock_get_result("tsk_v2_fail", status="failed",
                                   failure_reason="Could not find checkout button")
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.get_run = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        result = st.check_task("tsk_v2_fail")

    assert result["status"] == "failed"
    assert "failure_reason" in result


def test_check_running_task_no_failure_reason(results_dir):
    mock_result = _mock_get_result("tsk_v2_running", status="running")
    with patch("skyvern_task._client") as mock_client_fn:
        client = MagicMock()
        client.get_run = AsyncMock(return_value=mock_result)
        mock_client_fn.return_value = client

        result = st.check_task("tsk_v2_running")

    assert result["status"] == "running"
    assert "failure_reason" not in result
