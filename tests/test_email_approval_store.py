"""Tests for email_approval_store.py — SQLite approval store for email drafts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
from email_approval_store import (
    create_approval,
    check_approval,
    approve,
    reject,
    mark_sent,
    list_pending,
    get_approval,
)


def test_create_approval(tmp_path):
    db = tmp_path / "approvals.db"
    result = create_approval(
        draft_id="draft123",
        recipient="vendor@example.com",
        subject="Test",
        body_preview="Hello",
        db_path=db,
    )
    assert result["approval_id"]
    assert result["status"] == "pending"
    assert result["draft_id"] == "draft123"
    assert result["recipient"] == "vendor@example.com"
    assert result["subject"] == "Test"


def test_check_approval(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft456",
        recipient="ext@example.com",
        subject="Check me",
        body_preview="Body",
        db_path=db,
    )
    record = check_approval("draft456", db_path=db)
    assert record is not None
    assert record["draft_id"] == "draft456"
    assert record["approval_id"] == created["approval_id"]
    assert record["status"] == "pending"


def test_check_approval_missing(tmp_path):
    db = tmp_path / "approvals.db"
    # Ensure table exists by creating a dummy record first
    create_approval(
        draft_id="other", recipient="x@x.com", subject="", body_preview="", db_path=db
    )
    result = check_approval("nonexistent_draft", db_path=db)
    assert result is None


def test_approve(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_approve",
        recipient="ext@example.com",
        subject="Approve me",
        body_preview="Please",
        db_path=db,
    )
    ok = approve(created["approval_id"], approved_by="alex", db_path=db)
    assert ok is True
    record = get_approval(created["approval_id"], db_path=db)
    assert record["status"] == "approved"
    assert record["approved_by"] == "alex"
    assert record["decided_at"] is not None


def test_approve_already_decided(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_double",
        recipient="ext@example.com",
        subject="Double",
        body_preview="Body",
        db_path=db,
    )
    approve(created["approval_id"], approved_by="alex", db_path=db)
    ok = approve(created["approval_id"], approved_by="alex", db_path=db)
    assert ok is False


def test_reject(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_reject",
        recipient="ext@example.com",
        subject="Reject me",
        body_preview="Nope",
        db_path=db,
    )
    ok = reject(created["approval_id"], rejected_by="alex", db_path=db)
    assert ok is True
    record = get_approval(created["approval_id"], db_path=db)
    assert record["status"] == "rejected"


def test_reject_already_decided(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_decided",
        recipient="ext@example.com",
        subject="Decided",
        body_preview="Body",
        db_path=db,
    )
    approve(created["approval_id"], approved_by="alex", db_path=db)
    ok = reject(created["approval_id"], rejected_by="alex", db_path=db)
    assert ok is False


def test_mark_sent(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_sent",
        recipient="ext@example.com",
        subject="Send me",
        body_preview="Body",
        db_path=db,
    )
    approve(created["approval_id"], approved_by="alex", db_path=db)
    mark_sent("draft_sent", message_id="msg_abc123", db_path=db)
    record = get_approval(created["approval_id"], db_path=db)
    assert record["status"] == "sent"
    assert record["sent_message_id"] == "msg_abc123"
    assert record["sent_at"] is not None


def test_list_pending(tmp_path):
    db = tmp_path / "approvals.db"
    create_approval(
        draft_id="d1", recipient="a@x.com", subject="A", body_preview="", db_path=db
    )
    c2 = create_approval(
        draft_id="d2", recipient="b@x.com", subject="B", body_preview="", db_path=db
    )
    create_approval(
        draft_id="d3", recipient="c@x.com", subject="C", body_preview="", db_path=db
    )
    # Approve one so it's no longer pending
    approve(c2["approval_id"], approved_by="alex", db_path=db)

    pending = list_pending(db_path=db)
    assert len(pending) == 2
    draft_ids = {p["draft_id"] for p in pending}
    assert draft_ids == {"d1", "d3"}


def test_get_approval(tmp_path):
    db = tmp_path / "approvals.db"
    created = create_approval(
        draft_id="draft_get",
        recipient="ext@example.com",
        subject="Get me",
        body_preview="Preview text",
        db_path=db,
    )
    record = get_approval(created["approval_id"], db_path=db)
    assert record is not None
    assert record["approval_id"] == created["approval_id"]
    assert record["draft_id"] == "draft_get"
    assert record["recipient"] == "ext@example.com"
    assert record["subject"] == "Get me"
    assert record["body_preview"] == "Preview text"
    assert record["status"] == "pending"


def test_full_body_preserved_no_truncation(tmp_path):
    """HIL approver must see exactly what gogcli will send — never a substring."""
    db = tmp_path / "approvals.db"
    long_body = "X" * 5000 + "\n--- mid-marker ---\n" + "Y" * 5000
    created = create_approval(
        draft_id="draft_long",
        recipient="ext@example.com",
        subject="Long body",
        body_preview=long_body,
        db_path=db,
    )
    record = get_approval(created["approval_id"], db_path=db)
    assert record["body_preview"] == long_body
    assert "mid-marker" in record["body_preview"]
