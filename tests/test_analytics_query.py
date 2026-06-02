"""Tests for analytics_query.py — sync engine and query engine."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Patch DB and sessions path before importing
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import analytics_query as aq


@pytest.fixture
def tmp_db(tmp_path):
    """Return an open in-memory-like SQLite connection via a temp file."""
    db_path = tmp_path / "test.db"
    aq.DB_PATH = db_path
    conn = aq.open_db()
    yield conn
    conn.close()


@pytest.fixture
def session_dir(tmp_path):
    """Create a temp sessions directory with a sample JSONL file."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    aq.SESSIONS_DIR = sessions
    return sessions


def write_session(sessions_dir: Path, filename: str, records: list) -> Path:
    p = sessions_dir / filename
    with open(p, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return p


# -- parse_session_filename ---------------------------------------------------

def test_parse_telegram():
    sk, uid, ch = aq.parse_session_filename("telegram_5550000001.jsonl")
    assert sk == "telegram:5550000001"
    assert uid == "5550000001"
    assert ch == "telegram"


def test_parse_heartbeat():
    sk, uid, ch = aq.parse_session_filename("heartbeat.jsonl")
    assert sk == "heartbeat"
    assert uid is None
    assert ch == "heartbeat"


def test_parse_cli_direct():
    sk, uid, ch = aq.parse_session_filename("cli_direct.jsonl")
    assert sk == "cli_direct"
    assert uid is None
    assert ch == "cli"  # channel is the part before the underscore


def test_parse_whatsapp():
    sk, uid, ch = aq.parse_session_filename("whatsapp_9876543210.jsonl")
    assert sk == "whatsapp:9876543210"
    assert uid == "9876543210"
    assert ch == "whatsapp"


# -- resolve_user -------------------------------------------------------------

def test_resolve_known_user(monkeypatch):
    monkeypatch.setattr(aq, "USER_MAP", {"5550000001": "alex"})
    assert aq.resolve_user("5550000001", "telegram") == "alex"


def test_resolve_unknown_user(monkeypatch):
    monkeypatch.setattr(aq, "USER_MAP", {})
    assert aq.resolve_user("9999999999", "telegram") == "user_9999999999"


def test_resolve_system(monkeypatch):
    monkeypatch.setattr(aq, "USER_MAP", {})
    assert aq.resolve_user(None, "heartbeat") == "system"


# -- infer_skill --------------------------------------------------------------

def test_infer_skill_finance():
    tcs = [{"function": {"name": "plaid_fetch", "arguments": "{}"}}]
    assert aq.infer_skill(tcs) == "finance"


def test_infer_skill_multiple_same():
    tcs = [
        {"function": {"name": "plaid_fetch", "arguments": "{}"}},
        {"function": {"name": "plaid_fetch", "arguments": "{}"}},
        {"function": {"name": "gmail_fetch", "arguments": "{}"}},
    ]
    assert aq.infer_skill(tcs) == "finance"


def test_infer_skill_none():
    tcs = [{"function": {"name": "unknown_tool", "arguments": "{}"}}]
    assert aq.infer_skill(tcs) is None


def test_infer_skill_from_args():
    # exec tool embeds script name in arguments
    tcs = [{"function": {"name": "exec", "arguments": "plaid_fetch --balances"}}]
    assert aq.infer_skill(tcs) == "finance"


# -- estimate_cost ------------------------------------------------------------

def test_estimate_cost_known_model():
    cost = aq.estimate_cost("claude-sonnet-4-6", 1000, 500)
    expected = (1000 * 3.00 + 500 * 15.00) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_estimate_cost_deepseek_default_tier():
    """Regression: the default tier emits a bare `deepseek/...` id. Before
    the canonical-pricing switch this fell through to the $1/$5 Haiku
    fallback and overcounted cost ~10-25x. It must never price at the Haiku
    fallback again — whether nanobot's canonical table is current (DeepSeek
    rate) or stale (0.0), the cost is far below the old $6/MTok-pair."""
    cost = aq.estimate_cost("deepseek/deepseek-v4-flash", 1_000_000, 1_000_000)
    haiku_fallback = (1_000_000 * 1.00 + 1_000_000 * 5.00) / 1_000_000  # = 6.0
    assert cost < haiku_fallback / 10


def test_estimate_cost_deepseek_fallback_path():
    """With nanobot unavailable, the local fallback table still prices the
    DeepSeek default tier (input 0.0983 + output 0.1966 per MTok)."""
    import pytest as _pytest
    saved = aq._canonical_cost_usd
    aq._canonical_cost_usd = None
    try:
        cost = aq.estimate_cost("deepseek/deepseek-v4-flash", 1_000_000, 1_000_000)
    finally:
        aq._canonical_cost_usd = saved
    assert cost == _pytest.approx(0.0983 + 0.1966)


def test_estimate_cost_unknown_model():
    cost = aq.estimate_cost("unknown-model", 1000, 0)
    # Unpriced models contribute 0.0 — never a phantom fallback charge.
    assert cost == 0.0


# -- ts_delta_ms --------------------------------------------------------------

def test_ts_delta_ms():
    a = "2026-03-20T10:00:00.000000"
    b = "2026-03-20T10:00:01.500000"
    assert aq.ts_delta_ms(a, b) == 1500


def test_ts_delta_ms_bad_input():
    assert aq.ts_delta_ms("", "") == 0


def test_ts_delta_ms_no_microseconds():
    """Timestamps exactly on the second (no .f component) must not return 0."""
    a = "2026-03-20T10:00:00"
    b = "2026-03-20T10:00:02"
    assert aq.ts_delta_ms(a, b) == 2000


def test_ts_delta_ms_with_timezone_offset():
    """Timestamps with a UTC offset must be parsed correctly."""
    a = "2026-03-20T10:00:00.000000+00:00"
    b = "2026-03-20T10:00:01.500000+00:00"
    assert aq.ts_delta_ms(a, b) == 1500


def test_ts_delta_ms_with_z_suffix():
    """Timestamps ending in 'Z' (Zulu / UTC) must be handled."""
    a = "2026-03-20T10:00:00Z"
    b = "2026-03-20T10:00:03Z"
    assert aq.ts_delta_ms(a, b) == 3000


# -- sync_session_file --------------------------------------------------------

def test_sync_user_message(tmp_db, session_dir, monkeypatch):
    monkeypatch.setattr(aq, "USER_MAP", {"5550000001": "alex"})
    records = [
        {"_type": "metadata", "key": "telegram:5550000001", "created_at": "2026-03-20T10:00:00"},
        {"role": "user", "content": "Hello Homer", "timestamp": "2026-03-20T10:00:01.000000"},
        {"role": "assistant", "content": "Hello! How can I help?",
         "timestamp": "2026-03-20T10:00:02.000000"},
    ]
    write_session(session_dir, "telegram_5550000001.jsonl", records)

    n = aq.sync_session_file(tmp_db, session_dir / "telegram_5550000001.jsonl", "claude-sonnet-4-6")
    assert n == 1

    rows = tmp_db.execute("SELECT * FROM messages").fetchall()
    assert len(rows) == 1
    assert rows[0]["user_name"] == "alex"
    assert rows[0]["channel"] == "telegram"
    assert rows[0]["direction"] == "inbound"


def test_sync_heartbeat_assistant(tmp_db, session_dir):
    records = [
        {"_type": "metadata", "key": "heartbeat"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "exec", "arguments": "plaid_balance_check.py"}}
            ],
            "timestamp": "2026-03-20T09:00:00.000000",
        },
        {
            "role": "tool",
            "tool_call_id": "tc1",
            "name": "exec",
            "content": "balance OK",
            "timestamp": "2026-03-20T09:00:01.000000",
        },
    ]
    write_session(session_dir, "heartbeat.jsonl", records)

    n = aq.sync_session_file(tmp_db, session_dir / "heartbeat.jsonl", "claude-sonnet-4-6")
    assert n == 1

    rows = tmp_db.execute("SELECT * FROM messages").fetchall()
    assert rows[0]["user_name"] == "system"
    assert rows[0]["direction"] == "system"


def test_sync_incremental(tmp_db, session_dir):
    """Second sync should not re-insert already-synced lines."""
    records = [
        {"role": "user", "content": "Hello", "timestamp": "2026-03-20T10:00:00.000000"},
        {"role": "assistant", "content": "Hi!", "timestamp": "2026-03-20T10:00:01.000000"},
    ]
    path = write_session(session_dir, "telegram_5550000001.jsonl", records)

    aq.sync_session_file(tmp_db, path, "claude-sonnet-4-6")
    n2 = aq.sync_session_file(tmp_db, path, "claude-sonnet-4-6")
    assert n2 == 0  # no new messages on second sync

    count = tmp_db.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
    assert count == 1


def test_sync_appended_lines(tmp_db, session_dir):
    """New lines appended after first sync should be picked up on second sync."""
    path = session_dir / "telegram_5550000001.jsonl"
    path.write_text(
        json.dumps({"role": "user", "content": "first", "timestamp": "2026-03-20T10:00:00.000000"}) + "\n"
        + json.dumps({"role": "assistant", "content": "ok", "timestamp": "2026-03-20T10:00:01.000000"}) + "\n"
    )
    aq.sync_session_file(tmp_db, path, "test-model")

    # Append a new interaction
    with open(path, "a") as f:
        f.write(json.dumps({"role": "user", "content": "second", "timestamp": "2026-03-20T10:01:00.000000"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "got it", "timestamp": "2026-03-20T10:01:01.000000"}) + "\n")

    n2 = aq.sync_session_file(tmp_db, path, "test-model")
    assert n2 == 1
    count = tmp_db.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
    assert count == 2


# -- query engine -------------------------------------------------------------

def _seed_db(conn):
    """Insert a few test messages for query tests."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    rows = [
        (
            (now - timedelta(hours=2)).isoformat(),
            "telegram:5550000001", "5550000001", "alex", "telegram", "inbound",
            "finance", "claude-sonnet-4-6", 100, 50, 0.0005, 800, "telegram_5550000001.jsonl", 2
        ),
        (
            (now - timedelta(hours=1)).isoformat(),
            "telegram:5550000001", "5550000001", "alex", "telegram", "inbound",
            "email", "claude-sonnet-4-6", 80, 40, 0.0004, 600, "telegram_5550000001.jsonl", 4
        ),
        (
            (now - timedelta(minutes=30)).isoformat(),
            "heartbeat", None, "system", "heartbeat", "system",
            "tasks", "claude-sonnet-4-6", 0, 20, 0.0001, 0, "heartbeat.jsonl", 2
        ),
    ]
    conn.executemany(
        "INSERT INTO messages (timestamp, session_key, user_id, user_name, channel, direction, "
        "skill_used, model_used, tokens_in, tokens_out, cost_usd, duration_ms, source_file, source_line) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_query_summary(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_summary(tmp_db, days=7)
    assert result["total_messages"] == 3
    assert result["by_user"]["alex"] == 2
    assert result["by_user"]["system"] == 1
    assert "finance" in result["by_skill"]


def test_query_summary_filter_user(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_summary(tmp_db, days=7, user="alex")
    assert result["total_messages"] == 2
    assert "system" not in result["by_user"]


def test_query_breakdown_skill(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_breakdown(tmp_db, "skill", days=7)
    skills = [d["skill"] for d in result["data"]]
    assert "finance" in skills
    assert "email" in skills


def test_query_breakdown_user(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_breakdown(tmp_db, "user", days=7)
    users = [d["user"] for d in result["data"]]
    assert "alex" in users


def test_query_cost_report(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_cost_report(tmp_db, days=7)
    assert result["total_cost_usd"] > 0
    assert len(result["by_model"]) > 0
    assert len(result["by_user"]) > 0


def test_query_daily_trend(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_daily_trend(tmp_db, days=7)
    assert len(result["data"]) >= 1
    day = result["data"][0]
    assert "date" in day
    assert "messages" in day
    assert "user_messages" in day
    assert "system_messages" in day
    assert "cost_usd" in day
    # All messages should be accounted for
    total = sum(d["messages"] for d in result["data"])
    assert total == 3


def test_recost_all_fixes_stale_deepseek_rows(tmp_db, monkeypatch):
    """A row synced with the wrong (Haiku-fallback) cost gets corrected to
    the DeepSeek rate when recost runs — this is the path that makes the
    pricing fix visible in the weekly report's historical sum.

    Forces the local-fallback price path so the assertion is deterministic
    regardless of which nanobot pricing version is installed in the venv."""
    monkeypatch.setattr(aq, "_canonical_cost_usd", None)
    stale_cost = (1_000_000 * 1.00 + 1_000_000 * 5.00) / 1_000_000  # old $1/$5 fallback = 6.0
    tmp_db.execute(
        "INSERT INTO messages (timestamp, session_key, user_id, user_name, channel, "
        "direction, skill_used, model_used, tokens_in, tokens_out, cost_usd, "
        "duration_ms, source_file, source_line) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-06-01T00:00:00+00:00", "wa_1", "u1", "alex", "whatsapp", "inbound",
         None, "deepseek/deepseek-v4-flash", 1_000_000, 1_000_000, stale_cost,
         0, "s.jsonl", 1),
    )
    tmp_db.commit()

    result = aq.recost_all(tmp_db)
    assert result["rows_updated"] == 1
    assert result["net_cost_delta_usd"] < 0  # cost went down

    new_cost = tmp_db.execute("SELECT cost_usd FROM messages").fetchone()["cost_usd"]
    # v4-flash via the fallback table: 0.0983 in + 0.1966 out per MTok.
    assert new_cost == pytest.approx(0.0983 + 0.1966)
    assert new_cost < stale_cost


def test_query_daily_trend_user_filter(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_daily_trend(tmp_db, days=7, user="alex")
    total = sum(d["messages"] for d in result["data"])
    assert total == 2


def test_query_weekly_report(tmp_db):
    _seed_db(tmp_db)
    result = aq.query_weekly_report(tmp_db)
    assert isinstance(result, dict)
    assert result["total_messages"] == 3
    assert result["top_skill"] is not None


def test_query_weekly_report_empty(tmp_db):
    result = aq.query_weekly_report(tmp_db)
    assert isinstance(result, str)
    assert result.startswith("SKIP:")


# -- main() error contract ----------------------------------------------------

def test_main_invalid_month_returns_json_error(tmp_path, capsys):
    """--month with a full date string (not YYYY-MM) must print JSON error and exit 1."""
    import sys
    aq.DB_PATH = tmp_path / "test.db"
    aq.SESSIONS_DIR = tmp_path / "sessions"
    aq.SESSIONS_DIR.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["analytics_query.py", "--cost-report", "--month", "2026-03-01"]
        aq.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "error" in output
    assert "2026-03-01" in output["error"]


def test_main_valid_month_does_not_error(tmp_path, capsys):
    """--month with a valid YYYY-MM string must succeed and return JSON (not an error)."""
    import sys
    aq.DB_PATH = tmp_path / "test.db"
    aq.SESSIONS_DIR = tmp_path / "sessions"
    aq.SESSIONS_DIR.mkdir()

    sys.argv = ["analytics_query.py", "--cost-report", "--month", "2026-03"]
    aq.main()

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "error" not in output
    assert "period" in output
