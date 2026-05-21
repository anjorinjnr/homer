#!/usr/bin/env python3
"""
feedback_submit.py — Tenant-facing feedback channel.

Users invoke `/feedback` (or naturally say they have feedback) and Homer collects:
  - category: bug | feature | kudos
  - message: free text
  - optional: anonymized excerpt of recent conversation for debug/repro

Submissions are filed as GitHub issues in a team-internal repo configured
via the HOMER_FEEDBACK_REPO env var (kept separate from the open-source
homer repo so user feedback stays private). They live OUTSIDE the tenant's
instance — the tenant container only holds an outbound-queue marker if
network upload fails. No tenant can read another tenant's feedback.

Anonymization (when --include-conversation):
  - emails  → <email>
  - phones  → <phone>
  - household member names from USER.md AND context/users.yaml → <name>
    (longer first names also redact substring matches, so listing a
    diminutive like "Mira" also catches the longer form "Almira")
  - the household_id itself is sent as a label, not in the body

Last 20 turns OR 8 KB (whichever's smaller) are excerpted from the active session
JSONL file.

Usage:
    feedback_submit.py --category bug --message "homer hangs when I ask for the weather"
    feedback_submit.py --category feature --message "support recurring chores"
    feedback_submit.py --category kudos --message "morning briefing nailed today!"

    # With conversation excerpt:
    feedback_submit.py --category bug --message "..." \\
        --include-conversation --session-file <path>

    # Dry-run (no network, prints assembled payload):
    feedback_submit.py --dry-run --category bug --message "..." [--include-conversation --session-file <path>]

Required env (when not --dry-run):
    HOMER_FEEDBACK_TOKEN   GitHub PAT with `issues:write` scope only
    HOMER_FEEDBACK_REPO    GitHub repo (owner/name) to file issues against
    HOMER_HOUSEHOLD_ID     Tenant identifier (becomes a label)

Optional env:
    HOMER_WORKSPACE        used to resolve session file when --session-file omitted

Output: single JSON line on stdout.
    {"ok": true,  "issue_url": "...", "issue_number": 123}
    {"ok": false, "error": "...", "queued_path": "..."}   # network failure → local queue
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
# Bootstrap REPO_ROOT on sys.path so `from tools.users_loader import ...`
# resolves whether this file runs as a script or is imported under pytest.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GITHUB_API = "https://api.github.com"

VALID_CATEGORIES = ("bug", "feature", "kudos")
CATEGORY_EMOJI = {"bug": "🐛", "feature": "💡", "kudos": "💚"}

CONVERSATION_TURN_LIMIT = 20
CONVERSATION_BYTE_LIMIT = 8 * 1024

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Matches +1 (412) 555-1212, 412-555-1212, +14125551212. Requires either a
# country-code prefix or a separator between groups so we don't redact every
# 10-digit run (timestamps, order numbers, event IDs).
PHONE_RE = re.compile(
    r"(?:\+\d{1,2}[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}\b"
    r"|\+\d{10,12}\b"
)


# ── Anonymization ────────────────────────────────────────────────────────────

def name_pattern_for(names):
    """Build a single regex matching any of the given names.

    Single-word names of 4+ chars match as a substring within a word —
    so listing a diminutive like `Mira` also redacts the longer form
    `Almira`. Useful when the household lists a shortened form but the
    agent surfaces the full legal name from a calendar/email lookup.
    Shorter names and multi-word phrases use a strict word-boundary
    match because substring matching on 2-3 char names ("Bo") would
    clobber common English words.
    """
    cleaned = sorted({n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True)
    if not cleaned:
        return None
    parts = []
    for n in cleaned:
        escaped = re.escape(n)
        if " " not in n and len(n) >= 4:
            parts.append(rf"\b\w*{escaped}\w*\b")
        else:
            parts.append(rf"\b{escaped}\b")
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


def _add_name(names, candidate):
    """Add a candidate name + its first-token form, dropping all-caps acronyms."""
    candidate = (candidate or "").strip()
    if not candidate or len(candidate) < 2 or candidate.isupper():
        return
    names.add(candidate)
    first = candidate.split()[0]
    if first != candidate and len(first) >= 2 and not first.isupper():
        names.add(first)


def load_household_names(workspace_dir):
    """Pull plausible household member names out of USER.md.

    Recognized line formats (all anchored to a `-` or `*` bullet):
      - `Name (born YYYY)`              — child / family entry
      - `Name, role` / `Name — role`    — adult entry
      - `Name: role`                    — adult entry
      - `**Field**: Name`               — primary-user / labelled entry
        (e.g. `- **Name**: Jamie`, `- **Primary user**: Mira Smith`)
      - `Name`                          — bare bullet under a `## Children` /
        `## Adults` / `## Family members` heading (no terminator)

    Both the full leader ("Mira Smith") AND its first token ("Mira") are
    returned so either form gets redacted. Tokens that look like all-caps
    acronyms (HVAC) are dropped. Anonymization is best-effort, not a guarantee.
    """
    user_md = workspace_dir / "USER.md"
    if not user_md.exists():
        return []
    try:
        text = user_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    title_case = r"[A-Z][a-z][a-zA-Z'\-]*(?:\s+[A-Z][a-z][a-zA-Z'\-]*)?"
    # `- Name (born ...)`, `- Name, role`, `- Name — role`, `- Name: role`.
    # `[,(:—]` deliberately excludes plain `-` (avoids "Pre-emergent…").
    bullet_terminated = re.compile(rf"^\s*[-*]\s+({title_case})\s*[,(:—]")
    # `- **Field**: Name` — bold key, e.g. `- **Name**: Jamie`.
    bold_field = re.compile(rf"^\s*[-*]\s+\*\*[^*\n]+\*\*\s*[:=]\s*({title_case})")
    # Bare `- Name` lines under a person-listing heading (no terminator). Only
    # the lines following a `## Children`/`## Adults`/`## Family` heading are
    # eligible — otherwise every Title Case bullet anywhere would be eaten.
    person_heading = re.compile(
        r"^\s*##+\s+(children|kids|adults|parents|family(?:\s+members)?|members|household)\b",
        re.IGNORECASE,
    )
    next_heading = re.compile(r"^\s*##+\s+")
    bare_bullet = re.compile(rf"^\s*[-*]\s+({title_case})\s*$")

    # Some USER.md rows contain a literal "\n" instead of a real newline (an
    # authoring quirk in household.md). Split on both so we don't miss rows.
    raw_lines = []
    for line in text.splitlines():
        raw_lines.extend(line.split("\\n"))

    names = set()
    in_person_section = False
    for line in raw_lines:
        if next_heading.match(line):
            in_person_section = bool(person_heading.match(line))
        m = bullet_terminated.match(line)
        if m:
            _add_name(names, m.group(1))
            continue
        m = bold_field.match(line)
        if m:
            _add_name(names, m.group(1))
            continue
        if in_person_section:
            m = bare_bullet.match(line)
            if m:
                _add_name(names, m.group(1))

    return sorted(names)


def load_users_yaml_names(context_dir):
    """Pull canonical display names from `context/users.yaml` (the user
    registry).

    USER.md is best-effort — it depends on whichever bullet conventions the
    household has typed into household.md. users.yaml is the structured source
    every multi-tenant deployment writes through `manage_users.py`, so it
    catches the household members that USER.md formatting misses (e.g. a
    spouse logged via `- name: Carla` with no presence in USER.md bullets).
    """
    users_yaml = context_dir / "users.yaml"
    if not users_yaml.exists():
        return []
    try:
        # Defer the loader import: keep this module loadable on hosts
        # without PyYAML (the loader hard-imports yaml).
        from tools.users_loader import iter_users, load_users
        data = load_users(users_yaml)
    except Exception:
        return []
    names = set()
    for _symbol, record in iter_users(data):
        _add_name(names, record.get("display_name"))
    return sorted(names)


def collect_household_names(workspace_dir, context_dir):
    """Union of names from USER.md and context/users.yaml."""
    seen = set(load_household_names(workspace_dir))
    seen.update(load_users_yaml_names(context_dir))
    return sorted(seen)


def anonymize(text, name_pattern=None):
    if not text:
        return text
    text = EMAIL_RE.sub("<email>", text)
    text = PHONE_RE.sub("<phone>", text)
    if name_pattern is not None:
        text = name_pattern.sub("<name>", text)
    return text


# ── Session excerpt ──────────────────────────────────────────────────────────

def resolve_session_file(explicit_path, workspace_dir):
    if explicit_path:
        p = Path(explicit_path)
        return p if p.exists() else None
    sessions_dir = workspace_dir / "sessions"
    if not sessions_dir.exists():
        return None
    candidates = sorted(
        (p for p in sessions_dir.glob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def excerpt_session(session_path, name_pattern=None,
                    turn_limit=CONVERSATION_TURN_LIMIT,
                    byte_limit=CONVERSATION_BYTE_LIMIT):
    """Return an anonymized markdown excerpt of the tail of a session JSONL.

    A "turn" is a single record (user / assistant / tool). Records without
    role+content are skipped. Tool args/results are summarized as `[tool: name]`
    so we don't leak filesystem paths or oauth flows.
    """
    if session_path is None:
        return "_(no session file resolved)_"
    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, FileNotFoundError) as e:
        return f"_(could not read session: {e})_"

    records = []
    for raw in lines[-(turn_limit * 4):]:  # 4× headroom for tool records
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    rendered = []
    for rec in records[-turn_limit:]:
        role = rec.get("role")
        ts = rec.get("timestamp", "")
        if role == "user":
            content = anonymize(rec.get("content", "") or "", name_pattern)
            rendered.append(f"[{ts}] user: {content}")
        elif role == "assistant":
            content = anonymize(rec.get("content", "") or "", name_pattern)
            tool_calls = rec.get("tool_calls") or []
            if tool_calls:
                names = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in tool_calls
                )
                content = (content + f"  [tools: {names}]").strip()
            if content:
                rendered.append(f"[{ts}] assistant: {content}")
        elif role == "tool":
            name = rec.get("name", "tool")
            rendered.append(f"[{ts}] tool({name}): <output omitted>")

    body = "\n".join(rendered)
    encoded = body.encode("utf-8")
    if len(encoded) > byte_limit:
        body = "…(truncated)…\n" + encoded[-byte_limit:].decode("utf-8", errors="replace").lstrip()
    return body or "_(session was empty)_"


# ── GitHub upload ────────────────────────────────────────────────────────────

def assemble_issue(category, message, household_id, conversation_block=None,
                   submitted_at=None):
    submitted_at = submitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title_msg = message.strip().splitlines()[0][:80]
    title = f"{CATEGORY_EMOJI[category]} [{category}] {title_msg}"

    parts = [
        f"**Submitted:** {submitted_at}",
        "",
        "## Message",
        "",
        message.strip(),
    ]
    if conversation_block:
        parts.extend([
            "",
            "## Conversation excerpt (anonymized)",
            "",
            "```",
            conversation_block,
            "```",
        ])
    body = "\n".join(parts)

    labels = [f"feedback:{category}"]
    if household_id:
        labels.append(f"tenant:{household_id}")
    return {"title": title, "body": body, "labels": labels}


def post_issue(repo, token, payload, timeout=15):
    url = f"{GITHUB_API}/repos/{repo}/issues"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "homer-feedback/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body


def queue_locally(workspace_dir, payload, reason):
    queue_dir = workspace_dir / "feedback_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time())}_{payload['title'][:40].replace('/', '_')}.json"
    path = queue_dir / fname
    record = {"reason": reason, "payload": payload, "queued_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(description="Submit Homer feedback (bug/feature/kudos).")
    p.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    p.add_argument("--message", required=True, help="Feedback text from the user.")
    p.add_argument("--include-conversation", action="store_true",
                   help="Attach an anonymized excerpt of the recent session.")
    p.add_argument("--session-file", default=None,
                   help="Path to the session JSONL. Defaults to most-recent in workspace/sessions/.")
    p.add_argument("--workspace", default=None,
                   help="Workspace dir. Defaults to $HOMER_WORKSPACE or repo's context/.nanobot_workspace.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print assembled payload as JSON, no network call.")
    args = p.parse_args(argv)

    if not args.message.strip():
        p.error("--message must contain non-whitespace text")

    workspace_dir = Path(
        args.workspace
        or os.environ.get("HOMER_WORKSPACE")
        or REPO_ROOT / "context" / ".nanobot_workspace"
    ).resolve()
    context_dir = Path(
        os.environ.get("HOMER_CONTEXT_DIR") or REPO_ROOT / "context"
    ).resolve()

    conversation_block = None
    if args.include_conversation:
        names = collect_household_names(workspace_dir, context_dir)
        pattern = name_pattern_for(names)
        session_path = resolve_session_file(args.session_file, workspace_dir)
        conversation_block = excerpt_session(session_path, name_pattern=pattern)

    household_id = os.environ.get("HOMER_HOUSEHOLD_ID", "").strip()
    payload = assemble_issue(
        args.category, args.message, household_id, conversation_block=conversation_block,
    )

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "payload": payload}, indent=2))
        return 0

    repo = os.environ.get("HOMER_FEEDBACK_REPO", "").strip()
    if not repo:
        queued = queue_locally(workspace_dir, payload, "missing HOMER_FEEDBACK_REPO")
        print(json.dumps({"ok": False, "error": "HOMER_FEEDBACK_REPO not configured (set to your team-internal owner/repo)",
                          "queued_path": str(queued)}))
        return 1
    token = os.environ.get("HOMER_FEEDBACK_TOKEN", "").strip()
    if not token:
        queued = queue_locally(workspace_dir, payload, "missing HOMER_FEEDBACK_TOKEN")
        print(json.dumps({"ok": False, "error": "HOMER_FEEDBACK_TOKEN not configured",
                          "queued_path": str(queued)}))
        return 1

    last_err = None
    body = None
    for attempt in range(2):
        try:
            body = post_issue(repo, token, payload)
            break
        except urllib.error.HTTPError as e:
            # 4xx is a deterministic config/auth/validation problem: retrying
            # won't help, and queueing locally just hides it from the operator.
            if 400 <= e.code < 500:
                print(json.dumps({"ok": False, "error": f"github api {e.code}: {e.reason}"}))
                return 3
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(1)
    if body is None:
        queued = queue_locally(workspace_dir, payload, f"upload failed: {last_err}")
        print(json.dumps({"ok": False, "error": f"upload failed: {last_err}",
                          "queued_path": str(queued)}))
        return 2

    print(json.dumps({
        "ok": True,
        "issue_url": body.get("html_url"),
        "issue_number": body.get("number"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
