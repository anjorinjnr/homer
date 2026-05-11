"""
Tests for payee_label_add.py — add/update payee labels.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import payee_label_add


def test_add_new_label(tmp_path, monkeypatch):
    labels_file = tmp_path / "payee_labels.json"
    monkeypatch.setattr(payee_label_add, "PAYEE_LABELS_FILE", labels_file)

    sys.argv = ["payee_label_add.py", "--payee", "Check Paid", "--label", "Personal Checks"]
    output = _capture_output(payee_label_add.main)

    result = json.loads(output)
    assert result["status"] == "added"
    assert result["payee"] == "Check Paid"
    assert result["label"] == "Personal Checks"
    assert json.loads(labels_file.read_text())["Check Paid"] == "Personal Checks"


def test_update_existing_label(tmp_path, monkeypatch):
    labels_file = tmp_path / "payee_labels.json"
    labels_file.write_text(json.dumps({"ZELLE": "Transfers"}))
    monkeypatch.setattr(payee_label_add, "PAYEE_LABELS_FILE", labels_file)

    sys.argv = ["payee_label_add.py", "--payee", "ZELLE", "--label", "P2P Payments"]
    output = _capture_output(payee_label_add.main)

    result = json.loads(output)
    assert result["status"] == "updated"
    assert result["previous"] == "Transfers"
    assert result["label"] == "P2P Payments"
    assert json.loads(labels_file.read_text())["ZELLE"] == "P2P Payments"


def test_update_same_label_no_previous_key(tmp_path, monkeypatch):
    labels_file = tmp_path / "payee_labels.json"
    labels_file.write_text(json.dumps({"AT&T": "Utilities"}))
    monkeypatch.setattr(payee_label_add, "PAYEE_LABELS_FILE", labels_file)

    sys.argv = ["payee_label_add.py", "--payee", "AT&T", "--label", "Utilities"]
    output = _capture_output(payee_label_add.main)

    result = json.loads(output)
    assert result["status"] == "updated"
    assert "previous" not in result  # same label, no previous key


def test_creates_file_if_missing(tmp_path, monkeypatch):
    labels_file = tmp_path / "context" / "payee_labels.json"
    monkeypatch.setattr(payee_label_add, "PAYEE_LABELS_FILE", labels_file)

    sys.argv = ["payee_label_add.py", "--payee", "Costco", "--label", "Groceries"]
    _capture_output(payee_label_add.main)

    assert labels_file.exists()
    assert json.loads(labels_file.read_text())["Costco"] == "Groceries"


def test_empty_payee_exits_with_error(monkeypatch, capsys):
    sys.argv = ["payee_label_add.py", "--payee", "  ", "--label", "Something"]
    with pytest.raises(SystemExit) as exc:
        payee_label_add.main()
    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert "error" in output


# ── helpers ───────────────────────────────────────────────────────────────────

def _capture_output(fn):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue().strip()
