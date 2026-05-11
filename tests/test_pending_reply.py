"""Tests for pending_reply.py — tracking outbound messages awaiting a reply."""

import json

import pytest

import tools.pending_reply as pr


@pytest.fixture(autouse=True)
def pending_file(tmp_path, monkeypatch):
    f = tmp_path / "pending_replies.json"
    monkeypatch.setattr(pr, "PENDING_FILE", f)
    # Suppress build_context.py subprocess call — we're not testing that here
    monkeypatch.setattr(pr, "_rebuild_context", lambda: None)
    return f


# ---------------------------------------------------------------------------
# --add
# ---------------------------------------------------------------------------

def test_add_creates_file(pending_file, capsys):
    pr.cmd_add("sam", "weekend plans", "whatsapp", "123@s.whatsapp.net")
    assert pending_file.exists()
    data = json.loads(pending_file.read_text())
    assert len(data) == 1
    assert data[0]["from"] == "sam"
    assert data[0]["topic"] == "weekend plans"
    assert data[0]["notify_channel"] == "whatsapp"
    assert data[0]["notify_recipient"] == "123@s.whatsapp.net"
    assert "id" in data[0]
    assert "created_at" in data[0]


def test_add_normalises_name_to_lowercase(pending_file, capsys):
    pr.cmd_add("Sam", "school pickup", "telegram", "9876543")
    data = json.loads(pending_file.read_text())
    assert data[0]["from"] == "sam"


def test_add_multiple_entries(pending_file, capsys):
    pr.cmd_add("sam", "topic A", "whatsapp", "aaa")
    pr.cmd_add("sam", "topic B", "whatsapp", "aaa")
    pr.cmd_add("alex", "birthday plans", "telegram", "bbb")
    data = json.loads(pending_file.read_text())
    assert len(data) == 3


def test_add_prints_json(pending_file, capsys):
    pr.cmd_add("sam", "weekend", "whatsapp", "123@s.whatsapp.net")
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "added"
    assert out["from"] == "sam"
    assert out["topic"] == "weekend"
    assert "id" in out


def test_add_with_party_id_persists_it(pending_file, capsys):
    pr.cmd_add(
        "sam", "weekend", "whatsapp", "123@s.whatsapp.net",
        party_id="12345678901@s.whatsapp.net",
    )
    data = json.loads(pending_file.read_text())
    assert data[0]["party_id"] == "12345678901@s.whatsapp.net"


def test_add_without_party_id_omits_field(pending_file, capsys):
    pr.cmd_add("sam", "weekend", "whatsapp", "123@s.whatsapp.net")
    data = json.loads(pending_file.read_text())
    assert "party_id" not in data[0]


# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------

def test_list_empty(pending_file, capsys):
    pr.cmd_list(None)
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_list_all(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("alex", "B", "telegram", "y")
    capsys.readouterr()
    pr.cmd_list(None)
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 2


def test_list_filtered_by_from(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("alex", "B", "telegram", "y")
    capsys.readouterr()
    pr.cmd_list("sam")
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["from"] == "sam"


def test_list_filter_case_insensitive(pending_file, capsys):
    pr.cmd_add("sam", "C", "whatsapp", "x")
    capsys.readouterr()
    pr.cmd_list("SAM")
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1


def test_list_no_match_returns_empty(pending_file, capsys):
    pr.cmd_add("alex", "D", "whatsapp", "z")
    capsys.readouterr()
    pr.cmd_list("sam")
    out = json.loads(capsys.readouterr().out)
    assert out == []


# ---------------------------------------------------------------------------
# --complete --from
# ---------------------------------------------------------------------------

def test_complete_removes_entries_for_sender(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("sam", "B", "whatsapp", "x")
    pr.cmd_add("alex", "C", "telegram", "y")
    capsys.readouterr()
    pr.cmd_complete("sam", None)
    data = json.loads(pending_file.read_text())
    assert len(data) == 1
    assert data[0]["from"] == "alex"


def test_complete_prints_removed_count(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("sam", "B", "whatsapp", "x")
    capsys.readouterr()
    pr.cmd_complete("sam", None)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert out["removed"] == 2


def test_complete_case_insensitive(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    capsys.readouterr()
    pr.cmd_complete("SAM", None)
    data = json.loads(pending_file.read_text())
    assert data == []


def test_complete_not_found_exits_nonzero(pending_file, capsys):
    pr.cmd_add("alex", "A", "telegram", "y")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc_info:
        pr.cmd_complete("sam", None)
    assert exc_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "not_found"


def test_complete_leaves_other_senders_intact(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("alex", "B", "telegram", "y")
    capsys.readouterr()
    pr.cmd_complete("sam", None)
    data = json.loads(pending_file.read_text())
    assert len(data) == 1
    assert data[0]["from"] == "alex"


# ---------------------------------------------------------------------------
# --complete --id (targeted)
# ---------------------------------------------------------------------------

def test_complete_by_id_removes_only_that_entry(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("sam", "B", "whatsapp", "x")
    capsys.readouterr()
    data = json.loads(pending_file.read_text())
    first_id = data[0]["id"]
    pr.cmd_complete(None, first_id)
    remaining = json.loads(pending_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["topic"] == "B"


def test_complete_by_id_prints_status(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    capsys.readouterr()
    entry_id = json.loads(pending_file.read_text())[0]["id"]
    pr.cmd_complete(None, entry_id)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert out["id"] == entry_id
    assert out["removed"] == 1


def test_complete_by_id_not_found_exits_nonzero(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc_info:
        pr.cmd_complete(None, "nonexistent-id")
    assert exc_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "not_found"


def test_complete_by_id_leaves_sibling_entries_intact(pending_file, capsys):
    """Two pending entries for same person — only the targeted one is removed."""
    pr.cmd_add("sam", "weekend plans", "whatsapp", "x")
    pr.cmd_add("sam", "grocery run", "whatsapp", "x")
    capsys.readouterr()
    data = json.loads(pending_file.read_text())
    pr.cmd_complete(None, data[0]["id"])
    remaining = json.loads(pending_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["topic"] == "grocery run"


# ---------------------------------------------------------------------------
# main() — JSON error contract (no argparse stderr)
# ---------------------------------------------------------------------------

def test_main_missing_add_args_outputs_json(pending_file, capsys):
    with pytest.raises(SystemExit) as exc_info:
        import sys as _sys
        orig = _sys.argv
        _sys.argv = ["pending_reply.py", "--add", "--from", "sam"]
        try:
            pr.main()
        finally:
            _sys.argv = orig
    assert exc_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_main_complete_no_target_outputs_json(pending_file, capsys):
    import sys as _sys
    orig = _sys.argv
    _sys.argv = ["pending_reply.py", "--complete"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            pr.main()
    finally:
        _sys.argv = orig
    assert exc_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_main_no_verb_outputs_json(pending_file, capsys):
    import sys as _sys
    orig = _sys.argv
    _sys.argv = ["pending_reply.py"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            pr.main()
    finally:
        _sys.argv = orig
    assert exc_info.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty(pending_file, capsys):
    assert not pending_file.exists()
    pr.cmd_list(None)
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_load_corrupt_file_returns_empty(pending_file, capsys):
    pending_file.write_text("not valid json", encoding="utf-8")
    pr.cmd_list(None)
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_each_add_generates_unique_id(pending_file, capsys):
    pr.cmd_add("sam", "A", "whatsapp", "x")
    pr.cmd_add("sam", "B", "whatsapp", "x")
    data = json.loads(pending_file.read_text())
    ids = [e["id"] for e in data]
    assert len(set(ids)) == 2
