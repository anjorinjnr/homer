"""Tests for history_thread_pick.py — follow-up thread selection."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import tools.history_thread_pick as htp
import tools.history_store as hs
# Patch through htp.hs (the module reference history_thread_pick holds).


def _uuid() -> str:
    return str(uuid.uuid4())


def _ts(offset_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── _seconds_since ────────────────────────────────────────────────────────────

class TestSecondsSince:
    def test_returns_inf_for_none(self):
        assert htp._seconds_since(None) == float("inf")

    def test_returns_inf_for_bad_string(self):
        assert htp._seconds_since("not-a-date") == float("inf")

    def test_positive_for_past(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        secs = htp._seconds_since(past)
        assert 7000 < secs < 7500

    def test_zero_for_now(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        secs = htp._seconds_since(now)
        assert secs < 5


# ── _era_invitation ───────────────────────────────────────────────────────────

class TestEraInvitation:
    def test_returns_string_for_all_eras(self):
        for era in hs.ALL_ERAS:
            msg = htp._era_invitation(era)
            assert isinstance(msg, str)
            assert len(msg) > 10

    def test_unknown_era_returns_fallback(self):
        msg = htp._era_invitation("unknown-era-xyz")
        assert "unknown-era-xyz" in msg


# ── do_pick ───────────────────────────────────────────────────────────────────

class TestDoPick:
    def test_picks_highest_priority_open_thread(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        threads = [
            {"id": _uuid(), "prompt": "Ask about the wedding", "priority": 8,
             "status": "open", "last_asked_at": None},
        ]
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: threads)

        htp.do_pick(cid, hid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "open_thread"
        assert out["prompt"] == "Ask about the wedding"

    def test_skips_recently_asked_thread(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        # Thread asked 10 minutes ago — should be skipped
        threads = [
            {"id": _uuid(), "prompt": "Recent question", "priority": 9,
             "status": "asked", "last_asked_at": _ts(0)},
        ]
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: threads)
        monkeypatch.setattr(htp.hs, "get_era_coverage", lambda hid, cid: [])

        htp.do_pick(cid, hid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "era_gap"

    def test_falls_back_to_era_gap_when_no_threads(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: [])
        monkeypatch.setattr(htp.hs, "get_era_coverage", lambda hid, cid: [])

        htp.do_pick(cid, hid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "era_gap"
        assert out["prompt"] is not None
        assert out["era_label"] in hs.ALL_ERAS

    def test_returns_none_when_all_eras_covered_and_fresh(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: [])
        coverage = [
            {"era_label": era, "richness_score": 10.0, "last_touched_at": _ts(-1)}
            for era in hs.ALL_ERAS
        ]
        monkeypatch.setattr(htp.hs, "get_era_coverage", lambda hid, cid: coverage)

        htp.do_pick(cid, hid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "none"
        assert out["prompt"] is None

    def test_surface_stale_low_richness_era(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: [])
        coverage = []
        for i, era in enumerate(hs.ALL_ERAS):
            if i == 0:
                coverage.append({
                    "era_label": era, "richness_score": 2.0,
                    "last_touched_at": _ts(-20),
                })
            else:
                coverage.append({
                    "era_label": era, "richness_score": 10.0,
                    "last_touched_at": _ts(-1),
                })
        monkeypatch.setattr(htp.hs, "get_era_coverage", lambda hid, cid: coverage)

        htp.do_pick(cid, hid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "era_gap"

    def test_exclude_thread_is_skipped(self, monkeypatch, capsys):
        hid, cid = _uuid(), _uuid()
        tid = _uuid()
        threads = [
            {"id": tid, "prompt": "Excluded", "priority": 9,
             "status": "open", "last_asked_at": None},
        ]
        monkeypatch.setattr(htp.hs, "list_open_threads", lambda hid, cid, limit=10: threads)
        monkeypatch.setattr(htp.hs, "get_era_coverage", lambda hid, cid: [])

        htp.do_pick(cid, hid, exclude_thread=tid)
        out = json.loads(capsys.readouterr().out)
        assert out["kind"] == "era_gap"


# ── do_mark_asked ─────────────────────────────────────────────────────────────

class TestDoMarkAsked:
    def test_marks_thread_asked(self, monkeypatch, capsys):
        tid = _uuid()
        monkeypatch.setattr(htp.hs, "mark_thread_asked",
                           lambda thread_id: {"id": tid, "status": "asked"})
        htp.do_mark_asked(tid)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["thread_status"] == "asked"
