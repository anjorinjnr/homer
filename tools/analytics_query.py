#!/usr/bin/env python3
"""
analytics_query.py — Usage analytics for Homer: sync session logs -> SQLite + query engine.

Parses nanobot session logs at context/.nanobot_workspace/sessions/*.jsonl and indexes
them into a SQLite database at data/analytics.db for efficient querying.

Every query command auto-runs an incremental sync first, so data is always fresh.

Usage:
    python tools/analytics_query.py --sync                        # sync only
    python tools/analytics_query.py --summary --days 7            # last 7 days
    python tools/analytics_query.py --summary --days 30 --user alex
    python tools/analytics_query.py --breakdown tool --days 30    # top tools
    python tools/analytics_query.py --breakdown skill --days 7    # top skills
    python tools/analytics_query.py --breakdown user --days 30    # usage by user
    python tools/analytics_query.py --breakdown channel --days 30
    python tools/analytics_query.py --cost-report --days 30
    python tools/analytics_query.py --cost-report --month 2026-03
    python tools/analytics_query.py --trend --days 30              # daily message/cost trend
    python tools/analytics_query.py --weekly-report               # for heartbeat

Output: JSON to stdout (SKIP: <reason> if nothing to report for heartbeat commands)
"""

import argparse
import hashlib
import json
import os
import sqlite3  # stdlib; requires system libsqlite3 (standard on most distros; apt install python3-sqlite3 if missing)
import sys
from calendar import monthrange
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT    = Path(__file__).parent.parent.resolve()
SESSIONS_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "sessions"
DB_PATH      = REPO_ROOT / "data" / "analytics.db"
MODEL_FILE   = REPO_ROOT / "context" / ".nanobot_workspace" / "CURRENT_MODEL"
CONFIG_PATH  = Path.home() / ".nanobot" / "config.json"

# -- User ID resolution -------------------------------------------------------
# USER_MAP is loaded at runtime from config.json ("user_id_map" key).
# Falls back to user_<id> for any ID not present in config.
# To update the map, edit config/config.json.template (not this file).
def _load_user_map() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in cfg.get("user_id_map", {}).items()}
    except Exception:
        return {}

USER_MAP = _load_user_map()

# -- Tool -> Skill mapping -----------------------------------------------------
TOOL_SKILL_MAP = {
    "plaid_fetch":           "finance",
    "plaid_monthly_report":  "finance",
    "plaid_balance_check":   "finance",
    "plaid_link":            "finance",
    "budget_check":          "finance",
    "gmail_fetch":           "email",
    "gmail_search":          "email",
    "calendar_fetch":        "calendar",
    "calendar_add":          "calendar",
    "tavily":                "web_search",
    "maps":                  "places",
    "drive_fetch":           "drive",
    "drive_read":            "drive",
    "drive_upload":          "drive",
    "sheets":                "sheets",
    "event_manage":          "events",
    "manage_event_guest":    "events",
    "context_updater":       "context",
    "context_scrub":         "context",
    "log_learning":          "learning",
    "switch_model":          "model",
    "version":               "system",
    "tasks_update":          "tasks",
    "announce_update":       "system",
    "export_context":        "context",
    "parse_vcard":           "contacts",
    "payee_label_add":       "finance",
}

# -- Model cost table (USD per 1M tokens) -------------------------------------
# Source: https://www.anthropic.com/pricing  (Anthropic models)
#         https://ai.google.dev/gemini-api/docs/pricing  (Gemini models)
# Last verified: 2026-04-06
# All rates are per-million-token (input / output) in USD.
MODEL_COSTS = {
    "claude-haiku-4-5-20251001":       {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6":               {"in": 3.00, "out": 15.00},
    "gemini/gemini-2.5-flash":         {"in": 0.30, "out": 2.50},
    "gemini/gemini-2.5-pro":           {"in": 1.25, "out": 10.00},
    "gemini/gemini-3-flash-preview":   {"in": 0.50, "out": 3.00},
    "gemini/gemini-3.1-flash-lite-preview": {"in": 0.25, "out": 1.50},
    "gemini/gemini-3.1-pro-preview":   {"in": 2.00, "out": 12.00},
}
DEFAULT_COST = {"in": 1.00, "out": 5.00}  # fallback

CHARS_PER_TOKEN = 4  # rough estimate (~4 chars per token)


# -- Database -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    session_key TEXT NOT NULL,
    user_id     TEXT,
    user_name   TEXT,
    channel     TEXT NOT NULL,
    direction   TEXT NOT NULL,
    skill_used  TEXT,
    model_used  TEXT,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    cost_usd    REAL    DEFAULT 0.0,
    duration_ms INTEGER DEFAULT 0,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    timestamp   TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    args_hash   TEXT,
    success     INTEGER NOT NULL,
    duration_ms INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_state (
    session_file TEXT PRIMARY KEY,
    last_line    INTEGER NOT NULL,
    last_sync    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup  ON messages(source_file, source_line);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp     ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_user          ON messages(user_name);
CREATE INDEX IF NOT EXISTS idx_messages_channel       ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool        ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_message     ON tool_calls(message_id);
"""


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# -- Helpers ------------------------------------------------------------------

def read_current_model():
    """Resolve the active model.

    CURRENT_MODEL is only present after switch_model.py runs (homer#247).
    Fall back to nanobot config (runtime source of truth), then
    HOMER_DEFAULT_MODEL.
    """
    if MODEL_FILE.exists():
        val = MODEL_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            val = cfg.get("agents", {}).get("defaults", {}).get("model", "")
            if val:
                return val
        except Exception:
            pass
    return os.environ.get("HOMER_DEFAULT_MODEL", "unknown")


def estimate_cost(model, tokens_in, tokens_out):
    rates = MODEL_COSTS.get(model, DEFAULT_COST)
    return (tokens_in * rates["in"] + tokens_out * rates["out"]) / 1_000_000


def ts_delta_ms(ts_start, ts_end):
    """Return millisecond delta between two ISO 8601 timestamps."""
    try:
        a = datetime.fromisoformat(ts_start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ts_end.replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds() * 1000))
    except (ValueError, TypeError, AttributeError):
        return 0


def parse_session_filename(filename):
    """
    Given a session filename (no extension), return (session_key, user_id, channel).

    telegram_5550000001  -> ("telegram:5550000001", "5550000001", "telegram")
    whatsapp_1234567890  -> ("whatsapp:1234567890", "1234567890", "whatsapp")
    heartbeat            -> ("heartbeat", None, "heartbeat")
    cli_direct           -> ("cli_direct", None, "cli")
    """
    stem = Path(filename).stem
    if "_" in stem:
        parts = stem.rsplit("_", 1)
        channel = parts[0]
        user_id = parts[1] if parts[1].isdigit() else None
        session_key = "{}:{}".format(channel, parts[1]) if user_id else stem
        return session_key, user_id, channel
    return stem, None, stem


def resolve_user(user_id, channel):
    if user_id is None:
        return "system"
    return USER_MAP.get(user_id, "user_{}".format(user_id))


def infer_skill(tool_calls_list):
    """Return the most prominent skill inferred from a list of tool call dicts."""
    skill_counts = {}
    for tc in tool_calls_list:
        name = tc.get("function", {}).get("name", "")
        skill = TOOL_SKILL_MAP.get(name)
        if not skill:
            # Try to extract skill from arguments (exec calls embed script name in args)
            args = tc.get("function", {}).get("arguments", "")
            if isinstance(args, str):
                for script, mapped_skill in TOOL_SKILL_MAP.items():
                    if script in args:
                        skill = mapped_skill
                        break
        if skill:
            skill_counts[skill] = skill_counts.get(skill, 0) + 1
    if not skill_counts:
        return None
    return max(skill_counts, key=skill_counts.__getitem__)


def hash_args(args):
    if args is None:
        return None
    if isinstance(args, dict):
        args = json.dumps(args, sort_keys=True)
    return hashlib.sha256(str(args).encode()).hexdigest()[:16]


# -- Sync engine --------------------------------------------------------------

def sync_session_file(conn, session_path, model):
    """
    Parse a session JSONL file incrementally (from last_line) and insert new records.
    Returns the number of new messages inserted.
    """
    filename = session_path.name
    session_key, user_id, channel = parse_session_filename(filename)

    row = conn.execute(
        "SELECT last_line FROM sync_state WHERE session_file = ?", (filename,)
    ).fetchone()
    start_line = row["last_line"] if row else 0

    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    if len(lines) <= start_line:
        return 0

    new_lines = lines[start_line:]

    records = []
    for raw in new_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    inserted = 0
    i = 0
    while i < len(records):
        rec = records[i]
        abs_line = start_line + i + 1  # 1-based line number in file

        role = rec.get("role")

        if role == "user":
            user_ts = rec.get("timestamp", "")
            user_content = rec.get("content", "") or ""
            tokens_in = len(user_content) // CHARS_PER_TOKEN

            # Collect subsequent assistant + tool records
            tool_calls_acc = []
            assistant_ts = user_ts
            tokens_out = 0
            api_usage = None  # from nanobot session log if available
            j = i + 1
            while j < len(records) and records[j].get("role") in ("assistant", "tool"):
                r = records[j]
                if r.get("role") == "assistant":
                    assistant_ts = r.get("timestamp", user_ts)
                    content = r.get("content", "") or ""
                    tokens_out += len(content) // CHARS_PER_TOKEN
                    for tc in r.get("tool_calls", []) or []:
                        tool_calls_acc.append(tc)
                    if "usage" in r:
                        api_usage = r["usage"]
                j += 1

            # Prefer actual API token counts when available
            if api_usage:
                tokens_in = api_usage.get("prompt_tokens", tokens_in)
                tokens_out = api_usage.get("completion_tokens", tokens_out)

            user_name = resolve_user(user_id, channel)
            skill = infer_skill(tool_calls_acc)
            duration = ts_delta_ms(user_ts, assistant_ts)
            cost = estimate_cost(model, tokens_in, tokens_out)

            cur = conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(timestamp, session_key, user_id, user_name, channel, direction, "
                "skill_used, model_used, tokens_in, tokens_out, cost_usd, "
                "duration_ms, source_file, source_line) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_ts, session_key, user_id, user_name, channel, "inbound",
                 skill, model, tokens_in, tokens_out, cost,
                 duration, filename, abs_line),
            )
            if cur.rowcount:
                msg_id = cur.lastrowid
                _insert_tool_calls(conn, msg_id, tool_calls_acc, records, i + 1)
                inserted += 1

            i = j
            continue

        elif role == "assistant" and channel == "heartbeat":
            # Heartbeat: assistant messages with tool calls are "system" interactions
            ts = rec.get("timestamp", "")
            content = rec.get("content", "") or ""
            tool_calls_acc = rec.get("tool_calls", []) or []
            tokens_in = 0
            tokens_out = len(content) // CHARS_PER_TOKEN
            api_usage = rec.get("usage")

            # Look ahead for tool results to get end timestamp
            j = i + 1
            result_ts = ts
            while j < len(records) and records[j].get("role") == "tool":
                result_ts = records[j].get("timestamp", result_ts)
                j += 1

            # Prefer actual API token counts when available
            if api_usage:
                tokens_in = api_usage.get("prompt_tokens", tokens_in)
                tokens_out = api_usage.get("completion_tokens", tokens_out)

            skill = infer_skill(tool_calls_acc)
            duration = ts_delta_ms(ts, result_ts)
            cost = estimate_cost(model, tokens_in, tokens_out)

            cur = conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(timestamp, session_key, user_id, user_name, channel, direction, "
                "skill_used, model_used, tokens_in, tokens_out, cost_usd, "
                "duration_ms, source_file, source_line) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, session_key, None, "system", channel, "system",
                 skill, model, tokens_in, tokens_out, cost,
                 duration, filename, abs_line),
            )
            if cur.rowcount:
                msg_id = cur.lastrowid
                _insert_tool_calls(conn, msg_id, tool_calls_acc, records, i + 1)
                inserted += 1

            i = j
            continue

        i += 1

    new_last_line = start_line + len(new_lines)
    conn.execute(
        "INSERT INTO sync_state (session_file, last_line, last_sync) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(session_file) DO UPDATE "
        "SET last_line=excluded.last_line, last_sync=excluded.last_sync",
        (filename, new_last_line, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return inserted


def _insert_tool_calls(conn, msg_id, tool_calls, records, start_idx):
    """Insert tool_calls rows for a message, matching them to tool result records."""
    result_map = {}
    for r in records[start_idx:]:
        if r.get("role") == "tool":
            tcid = r.get("tool_call_id", "")
            if tcid:
                result_map[tcid] = r
        elif r.get("role") == "user":
            break

    for tc in tool_calls:
        tc_id = tc.get("id", "")
        fn    = tc.get("function", {})
        name  = fn.get("name", "unknown")
        args  = fn.get("arguments", "")

        result    = result_map.get(tc_id)
        success   = 1
        result_ts = ""
        if result:
            result_ts = result.get("timestamp", "")
            content   = result.get("content", "")
            success   = 0 if (
                isinstance(content, str)
                and content.strip().lower().startswith("error")
            ) else 1

        conn.execute(
            "INSERT INTO tool_calls "
            "(message_id, timestamp, tool_name, args_hash, success, duration_ms) "
            "VALUES (?,?,?,?,?,?)",
            (msg_id, result_ts, name, hash_args(args), success, 0),
        )


def run_sync(conn):
    if not SESSIONS_DIR.exists():
        return {"synced_files": 0, "new_messages": 0,
                "note": "sessions directory not found"}

    model = read_current_model()
    session_files = sorted(SESSIONS_DIR.glob("*.jsonl"))
    total_new = 0
    synced = 0
    for sf in session_files:
        n = sync_session_file(conn, sf, model)
        total_new += n
        synced += 1

    return {"synced_files": synced, "new_messages": total_new, "model": model}


# -- Query engine -------------------------------------------------------------

def date_range(days=None, month=None):
    """Return (start_iso, end_iso) for a query window."""
    now = datetime.now(timezone.utc)
    if month:
        try:
            y, m = map(int, month.split("-"))
            last_day = monthrange(y, m)[1]
            start = datetime(y, m, 1, tzinfo=timezone.utc)
            end   = datetime(y, m, last_day, 23, 59, 59, tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            raise ValueError("Invalid --month '{}'. Use YYYY-MM.".format(month))
    else:
        d = days or 7
        start = now - timedelta(days=d)
        end   = now
    return start.isoformat(), end.isoformat()


def query_summary(conn, days=7, user=None, month=None):
    start, end = date_range(days=days, month=month)
    label = month or "last {} days".format(days)

    base_where = "timestamp BETWEEN ? AND ?"
    params = [start, end]
    if user:
        base_where += " AND user_name = ?"
        params.append(user)

    total = conn.execute(
        "SELECT COUNT(*) as c FROM messages WHERE {}".format(base_where), params
    ).fetchone()["c"]

    by_user = {
        r["user_name"]: r["c"]
        for r in conn.execute(
            "SELECT user_name, COUNT(*) as c FROM messages WHERE {} "
            "GROUP BY user_name ORDER BY c DESC".format(base_where),
            params,
        ).fetchall()
    }

    by_channel = {
        r["channel"]: r["c"]
        for r in conn.execute(
            "SELECT channel, COUNT(*) as c FROM messages WHERE {} "
            "GROUP BY channel ORDER BY c DESC".format(base_where),
            params,
        ).fetchall()
    }

    by_skill = {
        r["skill_used"]: r["c"]
        for r in conn.execute(
            "SELECT skill_used, COUNT(*) as c FROM messages WHERE {} "
            "AND skill_used IS NOT NULL "
            "GROUP BY skill_used ORDER BY c DESC".format(base_where),
            params,
        ).fetchall()
    }

    tool_params = [start, end] + ([user] if user else [])
    user_filter = "AND m.user_name = ?" if user else ""
    top_tools_rows = conn.execute(
        "SELECT tc.tool_name, COUNT(*) as calls, AVG(tc.duration_ms) as avg_dur "
        "FROM tool_calls tc "
        "JOIN messages m ON m.id = tc.message_id "
        "WHERE m.timestamp BETWEEN ? AND ? {} "
        "GROUP BY tc.tool_name ORDER BY calls DESC LIMIT 10".format(user_filter),
        tool_params,
    ).fetchall()
    top_tools = [
        {
            "tool": r["tool_name"],
            "calls": r["calls"],
            "avg_duration_ms": round(r["avg_dur"] or 0),
        }
        for r in top_tools_rows
    ]

    cost_row = conn.execute(
        "SELECT SUM(cost_usd) as total FROM messages WHERE {}".format(base_where), params
    ).fetchone()
    cost_total = round(cost_row["total"] or 0.0, 4)

    cost_by_model = {
        r["model_used"]: round(r["c"] or 0.0, 4)
        for r in conn.execute(
            "SELECT model_used, SUM(cost_usd) as c FROM messages WHERE {} "
            "GROUP BY model_used".format(base_where),
            params,
        ).fetchall()
    }

    avg_dur_row = conn.execute(
        "SELECT AVG(duration_ms) as a FROM messages WHERE {} "
        "AND duration_ms > 0".format(base_where),
        params,
    ).fetchone()

    return {
        "period": label,
        "total_messages": total,
        "by_user": by_user,
        "by_channel": by_channel,
        "by_skill": by_skill,
        "top_tools": top_tools,
        "cost": {
            "total_usd": cost_total,
            "by_model": cost_by_model,
        },
        "avg_response_ms": round(avg_dur_row["a"] or 0),
    }


def query_breakdown(conn, dimension, days=30):
    start, end = date_range(days=days)
    params = [start, end]

    if dimension == "tool":
        rows = conn.execute(
            "SELECT tc.tool_name, COUNT(*) as calls, "
            "AVG(tc.duration_ms) as avg_dur, "
            "SUM(CASE WHEN tc.success=1 THEN 1 ELSE 0 END) as successes "
            "FROM tool_calls tc "
            "JOIN messages m ON m.id = tc.message_id "
            "WHERE m.timestamp BETWEEN ? AND ? "
            "GROUP BY tc.tool_name ORDER BY calls DESC",
            params,
        ).fetchall()
        data = [
            {
                "tool": r["tool_name"],
                "calls": r["calls"],
                "avg_duration_ms": round(r["avg_dur"] or 0),
                "success_rate": round(
                    (r["successes"] / r["calls"] * 100) if r["calls"] else 0, 1
                ),
            }
            for r in rows
        ]
    elif dimension == "skill":
        rows = conn.execute(
            "SELECT skill_used, COUNT(*) as c FROM messages "
            "WHERE timestamp BETWEEN ? AND ? AND skill_used IS NOT NULL "
            "GROUP BY skill_used ORDER BY c DESC",
            params,
        ).fetchall()
        data = [{"skill": r["skill_used"], "messages": r["c"]} for r in rows]
    elif dimension == "user":
        rows = conn.execute(
            "SELECT user_name, COUNT(*) as c, SUM(cost_usd) as cost "
            "FROM messages WHERE timestamp BETWEEN ? AND ? "
            "GROUP BY user_name ORDER BY c DESC",
            params,
        ).fetchall()
        data = [
            {
                "user": r["user_name"],
                "messages": r["c"],
                "cost_usd": round(r["cost"] or 0.0, 4),
            }
            for r in rows
        ]
    elif dimension == "channel":
        rows = conn.execute(
            "SELECT channel, COUNT(*) as c FROM messages "
            "WHERE timestamp BETWEEN ? AND ? "
            "GROUP BY channel ORDER BY c DESC",
            params,
        ).fetchall()
        data = [{"channel": r["channel"], "messages": r["c"]} for r in rows]
    else:
        return {
            "error": "Unknown dimension '{}'. Use: tool, skill, user, channel".format(dimension)
        }

    return {
        "period": "last {} days".format(days),
        "dimension": dimension,
        "data": data,
    }


def query_cost_report(conn, days=30, month=None):
    start, end = date_range(days=days, month=month)
    label = month or "last {} days".format(days)

    total = conn.execute(
        "SELECT SUM(cost_usd) as t, SUM(tokens_in) as ti, SUM(tokens_out) as to_ "
        "FROM messages WHERE timestamp BETWEEN ? AND ?",
        [start, end],
    ).fetchone()

    by_model = conn.execute(
        "SELECT model_used, SUM(cost_usd) as cost, "
        "SUM(tokens_in) as ti, SUM(tokens_out) as to_ "
        "FROM messages WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY model_used ORDER BY cost DESC",
        [start, end],
    ).fetchall()

    by_user = conn.execute(
        "SELECT user_name, SUM(cost_usd) as cost, COUNT(*) as msgs "
        "FROM messages WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY user_name ORDER BY cost DESC",
        [start, end],
    ).fetchall()

    return {
        "period": label,
        "total_cost_usd": round(total["t"] or 0.0, 4),
        "total_tokens_in": total["ti"] or 0,
        "total_tokens_out": total["to_"] or 0,
        "by_model": [
            {
                "model": r["model_used"],
                "cost_usd": round(r["cost"] or 0.0, 4),
                "tokens_in": r["ti"] or 0,
                "tokens_out": r["to_"] or 0,
            }
            for r in by_model
        ],
        "by_user": [
            {
                "user": r["user_name"],
                "cost_usd": round(r["cost"] or 0.0, 4),
                "messages": r["msgs"],
            }
            for r in by_user
        ],
    }


def query_daily_trend(conn, days=30, user=None):
    """Return per-day message counts and cost for the given window."""
    start, end = date_range(days=days)
    base_where = "timestamp BETWEEN ? AND ?"
    params = [start, end]
    if user:
        base_where += " AND user_name = ?"
        params.append(user)

    rows = conn.execute(
        "SELECT DATE(timestamp) as day, COUNT(*) as messages, "
        "SUM(cost_usd) as cost, "
        "SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) as user_messages, "
        "SUM(CASE WHEN direction='system' THEN 1 ELSE 0 END) as system_messages "
        "FROM messages WHERE {} "
        "GROUP BY DATE(timestamp) ORDER BY day".format(base_where),
        params,
    ).fetchall()

    return {
        "period": "last {} days".format(days),
        "data": [
            {
                "date": r["day"],
                "messages": r["messages"],
                "user_messages": r["user_messages"],
                "system_messages": r["system_messages"],
                "cost_usd": round(r["cost"] or 0.0, 4),
            }
            for r in rows
        ],
    }


def query_weekly_report(conn):
    """Produce a structured weekly report dict. Returns SKIP string if no data."""
    summary = query_summary(conn, days=7)
    if summary["total_messages"] == 0:
        return "SKIP: no interactions in the past 7 days"

    # Compare to prior week for trend
    prior_start, _ = date_range(days=14)
    this_start, _  = date_range(days=7)
    prev_count = conn.execute(
        "SELECT COUNT(*) as c FROM messages WHERE timestamp BETWEEN ? AND ?",
        [prior_start, this_start],
    ).fetchone()["c"]

    trend_pct = None
    if prev_count > 0:
        delta = summary["total_messages"] - prev_count
        pct = round(delta / prev_count * 100)
        trend_pct = "+{}%".format(pct) if pct >= 0 else "{}%".format(pct)

    top_skill = None
    if summary["by_skill"]:
        top_skill = max(summary["by_skill"], key=summary["by_skill"].__getitem__)

    return {
        "period": "last 7 days",
        "total_messages": summary["total_messages"],
        "by_user": summary["by_user"],
        "by_channel": summary["by_channel"],
        "top_skill": top_skill,
        "by_skill": summary["by_skill"],
        "top_tools": summary["top_tools"][:5],
        "cost": summary["cost"],
        "avg_response_ms": summary["avg_response_ms"],
        "trend_vs_prior_week": trend_pct,
    }


# -- Main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Homer analytics: sync session logs and run usage queries."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sync",          action="store_true",
                      help="Sync session logs into SQLite (incremental)")
    mode.add_argument("--summary",       action="store_true",
                      help="Usage summary for a time period")
    mode.add_argument("--breakdown",     metavar="DIM",
                      choices=["tool", "skill", "user", "channel"],
                      help="Usage breakdown by dimension (tool|skill|user|channel)")
    mode.add_argument("--cost-report",   action="store_true",
                      help="Cost report for a time period")
    mode.add_argument("--weekly-report", action="store_true",
                      help="Weekly usage report (used by heartbeat)")
    mode.add_argument("--trend",         action="store_true",
                      help="Daily message and cost trend")

    parser.add_argument("--days",  type=int, default=7,
                        help="Number of days to look back (default: 7)")
    parser.add_argument("--month", metavar="YYYY-MM",
                        help="Month to report on, overrides --days (e.g. 2026-03)")
    parser.add_argument("--user",  metavar="NAME",
                        help="Filter by user name (e.g. alex)")

    args = parser.parse_args()

    try:
        conn = open_db()

        if args.sync:
            result = run_sync(conn)
            print(json.dumps(result, indent=2))
            return

        # All query modes auto-sync first so data is always fresh
        run_sync(conn)

        if args.summary:
            result = query_summary(conn, days=args.days, user=args.user, month=args.month)
        elif args.breakdown:
            result = query_breakdown(conn, dimension=args.breakdown, days=args.days)
        elif args.cost_report:
            result = query_cost_report(conn, days=args.days, month=args.month)
        elif args.trend:
            result = query_daily_trend(conn, days=args.days, user=args.user)
        elif args.weekly_report:
            result = query_weekly_report(conn)
            if isinstance(result, str) and result.startswith("SKIP:"):
                print(result)
                return
        else:
            parser.print_help()
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
