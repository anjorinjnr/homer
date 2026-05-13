"""Tests for detect_conflicts.py — overlapping-event detection."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import detect_conflicts as dc


def _ev(title, start_minutes, end_minutes, *, date="2026-05-13",
        account="primary", calendar="Family", event_id=None, is_opaque=False,
        is_all_day=False, time="", end_time="", location=""):
    return {
        "title": title,
        "date": date,
        "time": time,
        "end_time": end_time,
        "start_minutes": start_minutes,
        "end_minutes": end_minutes,
        "is_all_day": is_all_day,
        "account": account,
        "calendar": calendar,
        "event_id": event_id or "",
        "is_opaque": is_opaque,
        "location": location,
    }


class TestDetect:
    def test_no_events_no_conflicts(self):
        assert dc.detect_conflicts([]) == []

    def test_non_overlapping_pair(self):
        a = _ev("Standup", 540, 570)
        b = _ev("Review", 600, 660)
        assert dc.detect_conflicts([a, b]) == []

    def test_simple_overlap(self):
        a = _ev("Standup", 540, 600)
        b = _ev("Review", 570, 630)
        out = dc.detect_conflicts([a, b])
        assert len(out) == 1
        c = out[0]
        assert c["overlap_start_minutes"] == 570
        assert c["overlap_end_minutes"] == 600
        assert c["cross_account"] is False
        assert c["both_opaque"] is False

    def test_touching_intervals_do_not_conflict(self):
        a = _ev("A", 540, 600)
        b = _ev("B", 600, 660)
        assert dc.detect_conflicts([a, b]) == []

    def test_cross_account_overlap_flagged(self):
        a = _ev("Standup", 540, 600, account="primary",
                event_id="evt-a")
        b = _ev("Dentist", 570, 630, account="personal",
                event_id="evt-b")
        out = dc.detect_conflicts([a, b])
        assert out[0]["cross_account"] is True

    def test_both_opaque_flagged(self):
        a = _ev("(busy)", 540, 600, is_opaque=True,
                event_id="a-opaque")
        b = _ev("(busy)", 555, 600, is_opaque=True,
                event_id="b-opaque")
        out = dc.detect_conflicts([a, b])
        assert out[0]["both_opaque"] is True

    def test_one_side_opaque_not_both(self):
        a = _ev("Real meeting", 540, 600, is_opaque=False,
                event_id="real")
        b = _ev("(busy)", 555, 600, is_opaque=True,
                event_id="opaque")
        out = dc.detect_conflicts([a, b])
        assert out[0]["both_opaque"] is False

    def test_all_day_events_ignored(self):
        a = _ev("Holiday", 0, 1440, is_all_day=True)
        b = _ev("Standup", 540, 600)
        assert dc.detect_conflicts([a, b]) == []

    def test_missing_start_minutes_skipped(self):
        a = _ev("A", 540, 600)
        b = {"title": "B", "date": "2026-05-13"}
        assert dc.detect_conflicts([a, b]) == []

    def test_zero_duration_skipped(self):
        a = _ev("Bogus", 540, 540)
        b = _ev("Standup", 540, 600)
        assert dc.detect_conflicts([a, b]) == []

    def test_different_dates_dont_conflict(self):
        a = _ev("A", 540, 600, date="2026-05-13")
        b = _ev("B", 540, 600, date="2026-05-14")
        assert dc.detect_conflicts([a, b]) == []


class TestCrossAccountDedup:
    def test_same_event_id_collapsed_before_detection(self):
        # Shared family calendar visible on two accounts — same event_id
        a = _ev("Family dinner", 1080, 1200,
                account="primary", event_id="shared-1")
        b = _ev("Family dinner", 1080, 1200,
                account="personal", event_id="shared-1")
        assert dc.detect_conflicts([a, b]) == []

    def test_same_shape_no_id_collapsed(self):
        a = _ev("Family dinner", 1080, 1200, account="primary")
        b = _ev("Family dinner", 1080, 1200, account="personal")
        # Both have empty event_id → shape-based dedup kicks in
        assert dc.detect_conflicts([a, b]) == []

    def test_different_id_same_shape_does_conflict(self):
        a = _ev("Coincidence", 1080, 1200, account="primary",
                event_id="a-unique")
        b = _ev("Coincidence", 1080, 1200, account="personal",
                event_id="b-unique")
        # event_ids differ → no dedup → genuine cross-account clash
        out = dc.detect_conflicts([a, b])
        assert len(out) == 1
        assert out[0]["cross_account"] is True


class TestThreeWay:
    def test_three_overlapping_pairs(self):
        a = _ev("A", 540, 600, event_id="ea")
        b = _ev("B", 570, 630, event_id="eb")
        c = _ev("C", 580, 660, event_id="ec")
        out = dc.detect_conflicts([a, b, c])
        # 3 pairs: (a,b), (a,c), (b,c)
        assert len(out) == 3


class TestEventView:
    def test_view_includes_required_fields(self):
        a = _ev("Standup", 540, 600, time="9:00 AM", end_time="10:00 AM",
                calendar="Work", location="HQ", event_id="evt-1")
        b = _ev("Review", 570, 630, time="9:30 AM", end_time="10:30 AM",
                calendar="Family", event_id="evt-2")
        out = dc.detect_conflicts([a, b])
        view = out[0]["event_a"]
        for key in ("title", "time", "end_time", "location",
                    "calendar", "account", "event_id", "is_opaque"):
            assert key in view

    def test_view_substitutes_no_title(self):
        a = _ev("", 540, 600, event_id="evt-a")
        b = _ev("Standup", 570, 630, event_id="evt-b")
        out = dc.detect_conflicts([a, b])
        assert out[0]["event_a"]["title"] == "(no title)"


class TestCLI:
    def test_reads_stdin(self, monkeypatch, capsys):
        events = [
            _ev("A", 540, 600, event_id="ea"),
            _ev("B", 570, 630, event_id="eb"),
        ]
        monkeypatch.setattr(sys, "stdin",
                            type("S", (), {"read": staticmethod(lambda: json.dumps(events))})())
        monkeypatch.setattr(sys, "argv", ["detect_conflicts.py"])
        dc.main()
        out = json.loads(capsys.readouterr().out)
        assert "conflicts" in out
        assert len(out["conflicts"]) == 1

    def test_reads_events_file(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "events.json"
        path.write_text(json.dumps([
            _ev("A", 540, 600, event_id="ea"),
            _ev("B", 570, 630, event_id="eb"),
        ]))
        monkeypatch.setattr(sys, "argv",
                            ["detect_conflicts.py", "--events-file", str(path)])
        dc.main()
        out = json.loads(capsys.readouterr().out)
        assert len(out["conflicts"]) == 1

    def test_date_filter(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "events.json"
        path.write_text(json.dumps([
            _ev("Today-A", 540, 600, event_id="ta", date="2026-05-13"),
            _ev("Today-B", 570, 630, event_id="tb", date="2026-05-13"),
            _ev("Tomorrow-A", 540, 600, event_id="ma", date="2026-05-14"),
            _ev("Tomorrow-B", 570, 630, event_id="mb", date="2026-05-14"),
        ]))
        monkeypatch.setattr(sys, "argv", [
            "detect_conflicts.py", "--events-file", str(path),
            "--date", "2026-05-14",
        ])
        dc.main()
        out = json.loads(capsys.readouterr().out)
        assert len(out["conflicts"]) == 1
        assert out["conflicts"][0]["event_a"]["title"] == "Tomorrow-A"

    def test_empty_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin",
                            type("S", (), {"read": staticmethod(lambda: "")})())
        monkeypatch.setattr(sys, "argv", ["detect_conflicts.py"])
        dc.main()
        out = json.loads(capsys.readouterr().out)
        assert out == {"conflicts": []}

    def test_malformed_input_errors(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin",
                            type("S", (), {"read": staticmethod(lambda: "{not json")})())
        monkeypatch.setattr(sys, "argv", ["detect_conflicts.py"])
        with pytest.raises(SystemExit):
            dc.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_non_array_input_errors(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin",
                            type("S", (), {"read": staticmethod(lambda: '{"not":"array"}')})())
        monkeypatch.setattr(sys, "argv", ["detect_conflicts.py"])
        with pytest.raises(SystemExit):
            dc.main()
        out = json.loads(capsys.readouterr().out)
        assert "error" in out
