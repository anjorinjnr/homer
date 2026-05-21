#!/usr/bin/env python3
"""
build_context.py — Assembles Homer's context files into nanobot's MEMORY.md.

nanobot loads MEMORY.md from the workspace on every agent call. This script
combines the relevant context files into a single MEMORY.md so Homer always
has household context injected.

Always loaded:
  - household.md  (people, preferences, location)
  - property.md        (home systems, maintenance)
  - projects.md        (active projects)

On-demand (loaded when --include-finance / --include-health):
  - finance.md
  - health.md

Run:
  python tools/build_context.py              # core + property + projects
  python tools/build_context.py --all        # everything
  python tools/build_context.py --include-finance

Called automatically by:
  - scripts/setup.sh (initial build)
  - tools/context_updater.py (after every approved update)
"""

import argparse
import json
import os
import re
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).parent.parent.resolve()
# Env overrides match context_updater.py so subprocess tool calls, sim runs,
# and hosted instances can redirect context reads/writes without code edits.
CONTEXT_DIR = Path(os.environ.get("HOMER_CONTEXT_DIR") or (REPO_ROOT / "context"))
USER_CONTEXT_DIR = Path(
    os.environ.get("HOMER_USER_CONTEXT_DIR") or (CONTEXT_DIR / "user_context")
)
WORKSPACE_DIR = CONTEXT_DIR / ".nanobot_workspace"
CAPABILITIES_MANIFEST_PATH = REPO_ROOT / "config" / "capabilities.yaml"
FEATURES_PATH = USER_CONTEXT_DIR / "features.yaml"
CAPABILITY_BLOCK_RE = re.compile(
    r"[ \t]*<!--\s*CAPABILITY:\s*([A-Za-z0-9_]+)\s*-->\n?"
    r"(.*?)"
    r"[ \t]*<!--\s*/CAPABILITY\s*-->\n?",
    re.DOTALL,
)

# Sentinel: when the manifest is missing entirely (not just empty), every
# capability block is treated as enabled. This preserves bare-VPS parity —
# a deployment without a manifest behaves as if no gating existed at all.
# Distinct from set() (which means "manifest present, everything disabled").
ALL_CAPABILITIES_ENABLED = object()

# HOMER_HOME: used to generate correct tool paths in AGENTS.md.
# Resolves automatically from the script location — works on both dev and VPS.
# Can be overridden via HOMER_HOME env var if needed.
HOMER_HOME = os.environ.get("HOMER_HOME") or str(REPO_ROOT)
HOMER_VENV = f"{HOMER_HOME}/.venv/bin/python"
HOMER_TOOLS = f"{HOMER_HOME}/tools"
HOMER_WORKSPACE = str(WORKSPACE_DIR)
NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
GUEST_NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "guest_config.json"
AGENT_DIR = REPO_ROOT / "agent"

TEMPLATE_VARS = {}  # populated after HOMER_* vars are set — see _get_template_vars()

ANNOUNCEMENTS_MARKER = "## Announcements"


def _get_template_vars() -> dict:
    shared_instructions_path = AGENT_DIR / "SHARED_INSTRUCTIONS.md"
    shared_instructions = ""
    if shared_instructions_path.exists():
        shared_instructions = shared_instructions_path.read_text(encoding="utf-8")

    # Resolve primary user (admin) from context/users.yaml.
    # Falls back to parsing household.md if users.yaml doesn't exist yet.
    primary_user = ""
    users_path = CONTEXT_DIR / "users.yaml"
    if users_path.exists():
        try:
            from tools.users_loader import iter_users, load_users
            for _sym, record in iter_users(load_users(users_path)):
                if record.get("role") == "admin":
                    primary_user = record.get("display_name", "")
                    break
        except Exception as e:
            import sys
            print(f"⚠️  WARNING: Failed to parse context/users.yaml: {e}",
                  file=sys.stderr)

    # Fallback: parse household.md for "- **Primary user**: Name"
    if not primary_user:
        household_path = USER_CONTEXT_DIR / "household.md"
        if not household_path.exists():
            household_path = CONTEXT_DIR / "household.md"
        if household_path.exists():
            for line in household_path.read_text(encoding="utf-8").splitlines():
                if line.strip().lower().startswith("- **primary"):
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        primary_user = parts[1].strip().split("(")[0].strip()
                    break

    if not primary_user:
        import sys
        print("⚠️  WARNING: No admin user found in context/users.yaml or household.md. "
              "Templates using {PRIMARY_USER} will resolve to empty string.",
              file=sys.stderr)

    return dict(
        HOMER_HOME=HOMER_HOME,
        HOMER_VENV=HOMER_VENV,
        HOMER_TOOLS=HOMER_TOOLS,
        HOMER_WORKSPACE=HOMER_WORKSPACE,
        SHARED_INSTRUCTIONS=shared_instructions,
        PRIMARY_USER=primary_user,
        # Portal URL for chat-driven pointers (e.g. MCP setup link). Hosted
        # default is empty — set PORTAL_BASE_URL in env; local
        # dev overrides via PORTAL_BASE_URL env var. Same default as
        # tools/link_account.py so prompt + tool stay in sync.
        PORTAL_BASE_URL=os.environ.get("PORTAL_BASE_URL", ""),
    )


def _load_capabilities_manifest() -> dict:
    """Load config/capabilities.yaml. Returns {} if missing or malformed."""
    if not CAPABILITIES_MANIFEST_PATH.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(CAPABILITIES_MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        import sys
        print(f"⚠️  WARNING: Failed to parse {CAPABILITIES_MANIFEST_PATH}: {e}",
              file=sys.stderr)
        return {}
    caps = data.get("capabilities") if isinstance(data, dict) else None
    return caps if isinstance(caps, dict) else {}


def load_enabled_capabilities():
    """Resolve which capabilities are enabled for this household.

    Returns either a `set[str]` of enabled capability names, or the
    `ALL_CAPABILITIES_ENABLED` sentinel when the manifest is missing (bare-VPS
    parity — behave as if no gating existed).

    Resolution order for each capability:
      1. An explicit `<name>: true|false` in `context/user_context/features.yaml`
         (household opt-in/opt-out).
      2. The manifest's own `default_enabled` field (ship default).
      3. Fallback to enabled if neither is set.

    Any capability named in features.yaml that isn't in the manifest is
    warned about (likely a typo) but otherwise ignored. Any manifest
    capability with a `requires_env` list whose env vars are missing is
    warned about (only when `HOMER_VERIFY_CAPABILITIES=1`) but NOT
    auto-disabled — the household intent stays the source of truth; missing
    secrets become a runtime error, not a silent feature drop.
    """
    import sys
    if not CAPABILITIES_MANIFEST_PATH.exists():
        return ALL_CAPABILITIES_ENABLED

    manifest = _load_capabilities_manifest()
    known_caps = set(manifest.keys())
    overrides: dict[str, bool] = {}
    if FEATURES_PATH.exists():
        try:
            import yaml
            raw = yaml.safe_load(FEATURES_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(k, str) and isinstance(v, bool):
                        if k not in known_caps:
                            print(f"⚠️  WARNING: {FEATURES_PATH.name} references "
                                  f"unknown capability '{k}' — ignored. "
                                  f"Known: {sorted(known_caps)}",
                                  file=sys.stderr)
                            continue
                        overrides[k] = v
        except Exception as e:
            print(f"⚠️  WARNING: Failed to parse {FEATURES_PATH}: {e}",
                  file=sys.stderr)

    def _ship_default(cap: str) -> bool:
        # Only honour explicit bools — matches the features.yaml parser, which
        # ignores non-bool values. Prevents a quoted `"false"` from coercing to
        # True and silently enabling a default-off capability.
        spec = manifest.get(cap)
        if isinstance(spec, dict):
            val = spec.get("default_enabled")
            if isinstance(val, bool):
                return val
        return True

    enabled = {cap for cap in known_caps if overrides.get(cap, _ship_default(cap))}

    # Env-var verification is opt-in because context_updater.py rebuilds the
    # workspace on every approved write; warning every run would be spammy.
    # Deploy scripts and `build_context.py --all` can set the flag to get the
    # full preflight check.
    if os.environ.get("HOMER_VERIFY_CAPABILITIES"):
        for cap in enabled:
            for var in manifest.get(cap, {}).get("requires_env") or []:
                if not os.environ.get(var):
                    print(f"⚠️  WARNING: capability '{cap}' is enabled but required "
                          f"env var {var} is not set — tools will fail at runtime.",
                          file=sys.stderr)
    return enabled


def _skills_gated_by_capabilities() -> dict[str, str]:
    """Return {skill_dir_name: owning_capability_name} for every skill the
    manifest claims. Skills not in this map are core and always load."""
    manifest = _load_capabilities_manifest()
    gated: dict[str, str] = {}
    for cap_name, spec in manifest.items():
        if not isinstance(spec, dict):
            continue
        for skill in spec.get("skills") or []:
            if isinstance(skill, str):
                gated[skill] = cap_name
    return gated


def apply_capability_markers(text: str, enabled) -> str:
    """Strip `<!-- CAPABILITY: X -->...<!-- /CAPABILITY -->` blocks.

    `enabled` is either a `set[str]` of enabled capability names or the
    `ALL_CAPABILITIES_ENABLED` sentinel (every block kept — bare-VPS path).

    Enabled capability: markers removed, inner content kept.
    Disabled capability: entire block (and its trailing newline) removed.
    Unknown capability (not in the manifest / enabled set): treated as
    disabled — fail closed.
    """
    all_on = enabled is ALL_CAPABILITIES_ENABLED
    def _replace(m: re.Match) -> str:
        cap = m.group(1)
        inner = m.group(2)
        if all_on:
            return inner
        return inner if cap in enabled else ""
    return CAPABILITY_BLOCK_RE.sub(_replace, text)


def load_template(name: str, enabled_capabilities=None) -> str:
    """Read an agent template file, apply capability markers, substitute vars.

    `enabled_capabilities` is a `set[str]` or the `ALL_CAPABILITIES_ENABLED`
    sentinel; `None` means resolve from disk now.
    """
    raw = (AGENT_DIR / name).read_text(encoding="utf-8")
    if enabled_capabilities is None:
        enabled_capabilities = load_enabled_capabilities()
    filtered = apply_capability_markers(raw, enabled_capabilities)
    return filtered.format_map(_get_template_vars())


# ── Content loaded from agent/ template files ────────────────────────────────
# Edit agent/SOUL.md, agent/AGENTS.md, agent/HEARTBEAT.md to change Homer's instructions.

ENABLED_CAPABILITIES = load_enabled_capabilities()
SOUL_CONTENT = load_template("SOUL.md", ENABLED_CAPABILITIES)
AGENTS_CONTENT = load_template("AGENTS.md", ENABLED_CAPABILITIES)
HEARTBEAT_CONTENT = load_template("HEARTBEAT.md", ENABLED_CAPABILITIES)

# Guest agent templates (loaded only if the files exist)
GUEST_AGENT_SOUL_PATH = AGENT_DIR / "GUEST_AGENT_SOUL.md"
GUEST_AGENT_AGENTS_PATH = AGENT_DIR / "GUEST_AGENT.md"
GUEST_HEARTBEAT_PATH = AGENT_DIR / "GUEST_HEARTBEAT.md"

ALWAYS_LOAD = ["household", "property", "finance"]
OPTIONAL = {"health": "health"}
EVENTS_DIR = CONTEXT_DIR / "events"
_gw_env = os.environ.get("HOMER_GUEST_WORKSPACE")
GUEST_AGENT_WORKSPACE_DIR = Path(_gw_env) if _gw_env else CONTEXT_DIR / ".guest_workspace"
GUEST_AGENT_ACL_FILE = EVENTS_DIR / "guest_agent_acl.json"


def _extract_announcement_entries(text: str) -> list[str]:
    """Extract ### blocks from the ## Announcements section of a HEARTBEAT.md string."""
    if ANNOUNCEMENTS_MARKER not in text or "## User Tasks" not in text:
        return []
    ann_pos = text.index(ANNOUNCEMENTS_MARKER)
    user_tasks_pos = text.index("## User Tasks", ann_pos)
    ann_header_end = text.index("\n", ann_pos) + 1
    section = text[ann_header_end:user_tasks_pos]
    entries = []
    for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\Z)", section, re.DOTALL):
        block = m.group(0).strip()
        if block:
            entries.append(block)
    return entries


def _parse_whats_new(content: str) -> list[dict]:
    """Parse ## YYYY-MM-DD — Title entries from WHATS_NEW.md."""
    entries = []
    for m in re.finditer(
        r"^## (\d{4}-\d{2}-\d{2} — .+?)$(.*?)(?=^## \d{4}-\d{2}-\d{2}|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    ):
        heading = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            continue
        title = heading.split(" — ", 1)[1] if " — " in heading else heading
        recipients = ""
        msg_lines = []
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("Recipients:"):
                recipients = line.split(":", 1)[1].strip()
            elif line and not line.startswith("---"):
                msg_lines.append(line)
        message = " ".join(msg_lines).strip()
        if recipients and message:
            entries.append({
                "key": heading,
                "title": title,
                "recipients": recipients,
                "message": message,
            })
    return entries


def inject_whats_new(heartbeat_path: Path, whats_new_path: Path | None = None) -> None:
    """Inject unannounced WHATS_NEW.md entries into HEARTBEAT.md as announcement blocks."""
    whats_new_file = whats_new_path or (REPO_ROOT / "WHATS_NEW.md")
    if not whats_new_file.exists():
        return

    entries = _parse_whats_new(whats_new_file.read_text(encoding="utf-8"))

    state_file = WORKSPACE_DIR / "state" / "whats_new_announced.txt"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    announced: set[str] = set()
    if state_file.exists():
        announced = set(state_file.read_text(encoding="utf-8").splitlines())

    new_entries = [e for e in entries if e["key"] not in announced]
    if not new_entries:
        return

    heartbeat = heartbeat_path.read_text(encoding="utf-8")
    if ANNOUNCEMENTS_MARKER not in heartbeat or "## User Tasks" not in heartbeat:
        return  # graceful degradation if old HEARTBEAT.md

    injected = []
    for entry in new_entries:
        # Guard against double-injection (e.g. fresh VPS with no state file)
        if entry["title"] in heartbeat:
            announced.add(entry["key"])
            continue
        block = f"### {entry['title']}\nRecipients: {entry['recipients']}\nMessage: {entry['message']}"
        user_tasks_pos = heartbeat.index("## User Tasks")
        heartbeat = heartbeat[:user_tasks_pos] + block + "\n\n" + heartbeat[user_tasks_pos:]
        announced.add(entry["key"])
        injected.append(entry)

    if injected:
        heartbeat_path.write_text(heartbeat, encoding="utf-8")

    state_file.write_text("\n".join(sorted(announced)) + "\n", encoding="utf-8")

    if injected:
        print(f"✓ Queued {len(injected)} announcement(s): {', '.join(e['title'] for e in injected)}")


def _parse_task_blocks(section: str) -> list[tuple[str, str]]:
    """Return list of (task_name, full_block) from a User Tasks section string."""
    blocks = []
    for m in re.finditer(r"(###\s+.+?)(?=\n###\s|\n##\s|\Z)", section, re.DOTALL):
        block = m.group(0).strip()
        name_m = re.match(r"###\s+(.+)", block)
        if name_m:
            blocks.append((name_m.group(1).strip(), block))
    return blocks


# Match `Key: anything` lines inside a task block. Lines that don't start
# with a Capitalized identifier (the `### heading`, blank lines, free-form
# descriptions) are intentionally skipped — overlay convention is one
# Key: Value pair per line.
_TASK_FIELD_PAT = re.compile(r"^([A-Z][\w-]*):[^\n]*$", re.MULTILINE)


def _overlay_fields(template_block: str, overlay_block: str) -> str:
    """Apply overlay's Key: Value lines onto template, preserving template's
    fields the overlay doesn't mention.

    Per-tenant overlay (`context/user_context/heartbeat_tasks.md`) used to
    fully replace the template block for a same-named task — meaning a new
    field added to the template (PR-C's `Prompt-file:` on Morning briefing,
    say) silently disappeared in production for any tenant whose overlay
    pre-dated the template change. This patch-style merge fixes that: the
    overlay supplies what it overrides or adds; everything else stays from
    the template.

    Same-key lines: overlay replaces template in place (preserves order).
    Overlay-only keys: appended in overlay's order.
    Template-only keys (the bug-fix scenario): preserved as-is.
    """
    template_fields: dict[str, str] = {}
    for m in _TASK_FIELD_PAT.finditer(template_block):
        template_fields.setdefault(m.group(1), m.group(0))

    result = template_block
    appended: list[str] = []
    seen: set[str] = set()
    for m in _TASK_FIELD_PAT.finditer(overlay_block):
        key, line = m.group(1), m.group(0)
        if key in seen:
            continue
        seen.add(key)
        if key in template_fields:
            result = result.replace(template_fields[key], line, 1)
        else:
            appended.append(line)

    if appended:
        result = result.rstrip("\n") + "\n" + "\n".join(appended) + "\n"
    return result


def _merge_system_task(template_block: str, live_block: str) -> str:
    """Use template fields but preserve Schedule, Last-run, Model, and Id from live block."""
    result = template_block
    # Stable task ID (homer#TBD): once a block has an Id (added by tasks_update.py
    # at first read), carry it forward through merges. Insert directly under the
    # ### heading so it stays at the top of the block.
    id_m = re.search(r"^Id:\s*(t_[a-z2-7]{8})\s*$", live_block, re.MULTILINE)
    if id_m:
        live_id = id_m.group(1)
        if re.search(r"^Id:\s*", result, re.MULTILINE):
            result = re.sub(r"^Id:\s*.+$", f"Id: {live_id}", result, count=1, flags=re.MULTILINE)
        else:
            result = re.sub(
                r"(###\s+[^\n]+\n)",
                rf"\1Id: {live_id}\n",
                result,
                count=1,
            )
    schedule_m = re.search(r"Schedule:\s*(.+)", live_block)
    if schedule_m:
        result = re.sub(r"Schedule:\s*.+", f"Schedule: {schedule_m.group(1).strip()}", result)
    last_run_m = re.search(r"Last-run:\s*(.+)", live_block)
    if last_run_m:
        if re.search(r"Last-run:", result):
            result = re.sub(r"Last-run:\s*.+", f"Last-run: {last_run_m.group(1).strip()}", result)
        else:
            result = re.sub(
                r"(Schedule:[^\n]+\n)",
                rf"\1Last-run: {last_run_m.group(1).strip()}\n",
                result,
            )
    model_m = re.search(r"Model:\s*(.+)", live_block)
    if model_m:
        model_val = model_m.group(1).strip()
        if re.search(r"Model:", result):
            result = re.sub(r"Model:\s*.+", f"Model: {model_val}", result)
        else:
            # Insert after Recipients line if present, otherwise after Schedule
            if re.search(r"Recipients:", result):
                result = re.sub(
                    r"(Recipients:[^\n]+\n)",
                    rf"\1Model: {model_val}\n",
                    result,
                )
            else:
                result = re.sub(
                    r"(Schedule:[^\n]+\n)",
                    rf"\1Model: {model_val}\n",
                    result,
                )
    return result


def _stamp_last_run_for_fresh_system_tasks(
    content: str, now: "datetime | None" = None,
) -> str:
    """For system tasks that lack a Last-run line, insert one stamped to the
    most recent past occurrence of the schedule pattern.

    A freshly-provisioned tenant has no live HEARTBEAT.md history, so every
    recurring system task block in the template ships with just a fixed-anchor
    Schedule (e.g. ``Schedule: 2026-01-01 07:00`` for the morning briefing).
    Nanobot's heartbeat treats "past Schedule + missing Last-run" as past-due,
    which means EVERY recurring task fires the first time the heartbeat ticks
    after boot — including the daily morning briefing fired at 10pm local time
    if that's when the user finished onboarding.

    Naive fix (Last-run = now) only delays the first fire by one Recur cycle:
    the brief would then fire 24h after onboarding (e.g. tomorrow 22:16), not
    at tomorrow's 7am. The smart stamp aligns Last-run with the schedule's
    own grid: floor(now → schedule + N·Recur) for the largest N where
    schedule + N·Recur <= now. That way ``Last-run + Recur`` lands on the
    NEXT scheduled occurrence, and the first real fire happens at the right
    time-of-day (e.g. tomorrow 7am, not tomorrow 22:16).

    Edge cases:
    - Schedule already in the future → no past occurrence; stamp Last-run =
      Schedule - Recur so the first fire still lands exactly on Schedule.
    - Recur unit we don't recognize → fall back to stamping `now` (better
      than nothing — at least the first heartbeat tick won't fire it).

    Only system tasks (`Type: system`) are touched — reminders intentionally
    rely on past-Schedule firing as a "catch-up" semantic. Tasks that already
    have a Last-run line are left alone.

    Local-now is timezone-naive in the user's TZ to match the parse format
    `_LASTRUN_VALUE_PAT` expects in nanobot/heartbeat/service.py.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    if now is None:
        tz_name = os.environ.get("HOMER_TIMEZONE")
        try:
            tz = ZoneInfo(tz_name) if tz_name else None
        except Exception:
            tz = None
        now_aware = datetime.now(tz=tz) if tz else datetime.now()
        now_naive = now_aware.replace(tzinfo=None)
    else:
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    fallback_stamp = now_naive.strftime("%Y-%m-%d %H:%M")

    # Reuse nanobot's regex constants — same patterns, single source of truth.
    from nanobot.heartbeat.service import _RECUR_PAT, _SCHED_PAT

    def _compute_stamp(block: str) -> str:
        sched_m = _SCHED_PAT.search(block)
        recur_m = _RECUR_PAT.search(block)
        if not (sched_m and recur_m):
            return fallback_stamp
        sched_raw = sched_m.group(1).strip()
        try:
            sched_dt = datetime.strptime(
                sched_raw, "%Y-%m-%d %H:%M" if " " in sched_raw else "%Y-%m-%d",
            )
            amount = int(recur_m.group(1))
            unit = recur_m.group(2).lower()
        except ValueError:
            return fallback_stamp
        delta = {
            "minute": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
        }.get(unit)
        if delta is None or delta <= timedelta(0):
            return fallback_stamp
        if now_naive < sched_dt:
            # Schedule is in the future — anchor first fire exactly on it
            # by setting Last-run = Schedule - Recur.
            target = sched_dt - delta
        else:
            steps = (now_naive - sched_dt) // delta
            target = sched_dt + steps * delta
        return target.strftime("%Y-%m-%d %H:%M")

    def _stamp_block(block: str) -> str:
        if "Type: system" not in block:
            return block
        if re.search(r"^Last-run:", block, re.MULTILINE):
            return block
        if not re.search(r"^Schedule:[^\n]+\n", block, re.MULTILINE):
            return block
        stamp = _compute_stamp(block)
        # Insert immediately after the Schedule line so field order mirrors
        # what tasks_update.py emits on tick.
        return re.sub(
            r"(^Schedule:[^\n]+\n)",
            rf"\1Last-run: {stamp}\n",
            block,
            count=1,
            flags=re.MULTILINE,
        )

    if "Type: system" not in content:
        return content
    blocks = re.split(r"(?=^###\s)", content, flags=re.MULTILINE)
    return "".join(_stamp_block(b) for b in blocks)


def _resolve_heartbeat_model_default() -> str | None:
    """Return the heartbeat-task Model preset to stamp, or None to skip stamping.

    Behaviour:
    - HOMER_MODEL_TIER must equal "default" (case-sensitive) — BYOK and managed
      tiers never get the heartbeat-default stamp.
    - HOMER_HEARTBEAT_MODEL must be set and non-empty. Its value is used
      verbatim as the Model field (typically a preset name like "default-cheap"
      or "auto"; any string is accepted because nanobot/litellm will resolve
      it the same way switch_model.py does).
    - All other tier values (byok, managed, unset) → no stamp.
    """
    if os.environ.get("HOMER_MODEL_TIER") != "default":
        return None
    preset = (os.environ.get("HOMER_HEARTBEAT_MODEL") or "").strip()
    if not preset:
        return None
    return preset


def stamp_heartbeat_model(content: str, preset: str) -> str:
    """Insert ``Model: <preset>`` into every ``## User Tasks`` block missing one.

    Idempotent: blocks that already declare a ``Model:`` line are left untouched.
    Insertion point: after the ``Schedule:`` line (matches how
    _merge_system_task adds Model when the live block has one). When a block
    has no Schedule line we fall back to inserting after the ``###`` header.
    Anything outside the ``## User Tasks`` section (e.g. ``## Completed``,
    instructional prose, ``## Announcements``) is left as-is.
    """
    marker = "## User Tasks"
    if marker not in content:
        return content
    user_start = content.index(marker)
    after = content[user_start + len(marker):]
    next_heading_m = re.search(r"\n##\s", after)
    section_end = (
        user_start + len(marker) + next_heading_m.start()
        if next_heading_m
        else len(content)
    )
    section = content[user_start:section_end]
    blocks = list(re.finditer(r"(###\s+.+?)(?=\n###\s|\n##\s|\Z)", section, re.DOTALL))
    if not blocks:
        return content

    rebuilt = section
    # Walk in reverse so earlier offsets stay valid as we splice.
    for m in reversed(blocks):
        block = m.group(0)
        if re.search(r"^Model:\s*", block, re.MULTILINE):
            continue
        rstripped = block.rstrip()
        trailing = block[len(rstripped):]
        # Prefer inserting right after Schedule: (matches _merge_system_task).
        new_block, n = re.subn(
            r"(Schedule:[^\n]*\n)",
            rf"\1Model: {preset}\n",
            rstripped,
            count=1,
        )
        if n == 0:
            # No Schedule line — append after the ### header so the field is
            # still on its own line. We append at the end of the block (just
            # before the trailing whitespace) for predictability.
            new_block = rstripped + f"\nModel: {preset}"
        rebuilt = rebuilt[:m.start()] + new_block + trailing + rebuilt[m.end():]
    return content[:user_start] + rebuilt + content[section_end:]


def merge_heartbeat(template: str, live: str, household_tasks: str = "") -> str:
    """Merge HEARTBEAT.md: system prefix + instructions from template; tasks from
    (template ⊕ per-household overlay) + live.

    - System prefix (before ## User Tasks): always from template.
    - User Tasks header/instructions text: from template.
    - System tasks (Type: system): start with template tasks, then merge in per-household
      tasks (from `household_tasks`, usually `context/user_context/heartbeat_tasks.md`).
      Per-household tasks override template tasks with the same name. Schedule/Last-run
      are preserved from live when a matching task exists.
    - Reminder tasks (no Type): always preserved from live.
    - Completed section: from live.

    The per-household overlay is how the shared `agent/HEARTBEAT.md` stays baseline
    (no household-specific content) while still letting each deployment specify its
    own Gmail/Plaid/briefing tasks.
    """
    marker = "## User Tasks"
    completed_marker = "## Completed"

    if not live or marker not in live:
        return template

    live_ann_entries = _extract_announcement_entries(live)

    # System prefix and template User Tasks section
    system_prefix = template.split(marker)[0]
    template_user_section = template[template.index(marker):]
    live_user_section = live[live.index(marker):]

    # Instructions text (between ## User Tasks header and first ### block): from template
    instructions_m = re.match(r"(## User Tasks[^\n]*\n.*?)(?=\n###|\n##\s|\Z)", template_user_section, re.DOTALL)
    user_tasks_header = instructions_m.group(0) if instructions_m else "## User Tasks\n"

    # Parse task blocks from all three sources
    template_tasks = _parse_task_blocks(template_user_section)
    live_tasks = _parse_task_blocks(live_user_section)
    household_tasks_list = _parse_task_blocks(household_tasks or "")
    live_tasks_dict = dict(live_tasks)
    household_tasks_dict = dict(household_tasks_list)

    merged_blocks: list[str] = []
    seen_names: set[str] = set()

    # System tasks: template first, then household overlay patches template
    # field-by-field. When overlay and template share a task name, overlay
    # fields override template's same-key fields and overlay-only fields are
    # appended — template-only fields survive so new fields added to
    # agent/HEARTBEAT.md don't silently drop in tenants whose overlay
    # pre-dates the template change.
    for name, template_block in template_tasks:
        if "Type: system" not in template_block:
            continue
        if name in household_tasks_dict:
            source_block = _overlay_fields(template_block, household_tasks_dict[name])
        else:
            source_block = template_block
        if name in live_tasks_dict:
            merged_blocks.append(_merge_system_task(source_block, live_tasks_dict[name]))
        else:
            merged_blocks.append(source_block)
        seen_names.add(name)

    # Household-only system tasks (not present in template)
    for name, hh_block in household_tasks_list:
        if name in seen_names or "Type: system" not in hh_block:
            continue
        if name in live_tasks_dict:
            merged_blocks.append(_merge_system_task(hh_block, live_tasks_dict[name]))
        else:
            merged_blocks.append(hh_block)
        seen_names.add(name)

    # Reminder tasks: from live (non-system only)
    for name, live_block in live_tasks:
        if "Type: system" not in live_block:
            merged_blocks.append(live_block)

    # Completed section: from live
    if completed_marker in live_user_section:
        completed_section = live_user_section[live_user_section.index(completed_marker):]
    else:
        completed_section = f"{completed_marker}\n\n"

    tasks_text = ("\n\n" + "\n\n".join(merged_blocks) + "\n\n") if merged_blocks else "\n"
    result = system_prefix + user_tasks_header + tasks_text + completed_section

    if live_ann_entries and ANNOUNCEMENTS_MARKER in result:
        user_tasks_pos = result.index(marker)
        insert = "\n\n".join(live_ann_entries) + "\n\n"
        result = result[:user_tasks_pos] + insert + result[user_tasks_pos:]

    return result


def load_file(name: str) -> str:
    path = USER_CONTEXT_DIR / f"{name}.md"
    if not path.exists():
        # Fallback to context/ root for backwards compatibility
        path = CONTEXT_DIR / f"{name}.md"
    if not path.exists():
        return f"<!-- {name}.md not found -->\n"
    return path.read_text(encoding="utf-8")


def load_active_events() -> str:
    """Load status.md from all active (non-archived) events."""
    if not EVENTS_DIR.exists():
        return ""
    parts = []
    for edir in sorted(EVENTS_DIR.iterdir()):
        sp = edir / "status.md"
        if not sp.exists():
            continue
        content = sp.read_text(encoding="utf-8")
        # Skip archived events
        if re.search(r"^Status:\s*Archived", content, re.MULTILINE | re.IGNORECASE):
            continue
        parts.append(content)
    if not parts:
        return ""
    return "# Active Events\n\n" + "\n---\n".join(parts) + "\n"


def load_pending_replies() -> str:
    """Inject pending reply tracking into USER.md so Homer knows who it's waiting on."""
    pending_file = CONTEXT_DIR / "pending_replies.json"
    if not pending_file.exists():
        return ""
    try:
        entries = json.loads(pending_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(entries, list) or not entries:
        return ""
    lines = ["# Pending Follow-ups\n\n",
             "Homer is waiting for replies from the following people:\n\n"]
    for e in entries:
        lines.append(
            f"- **{e.get('from', '?')}** re: {e.get('topic', '?')} "
            f"(id: {e.get('id', '?')}, "
            f"notify via {e.get('notify_channel', '?')} → {e.get('notify_recipient', '?')})\n"
        )
    lines.append(
        "\nWhen any listed person's next message arrives, forward it to the notify recipient "
        "via the **message** tool, then call pending_reply.py --complete --id <id>.\n"
    )
    return "".join(lines)


def build_user_context(include_finance: bool = False, include_health: bool = False) -> str:
    """Build USER.md content — household context loaded on every call."""
    parts = [f"# User Context\n<!-- Built: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n"]
    for name in ALWAYS_LOAD:
        parts.append(f"---\n")
        parts.append(load_file(name))
        parts.append("\n")
    if include_finance:
        parts.append("---\n")
        parts.append(load_file("finance"))
        parts.append("\n")
    if include_health:
        parts.append("---\n")
        parts.append(load_file("health"))
        parts.append("\n")
    # Append active events
    events_context = load_active_events()
    if events_context:
        parts.append("---\n")
        parts.append(events_context)
    # Append pending replies snapshot so Homer knows who it's waiting on without a tool call
    pending_context = load_pending_replies()
    if pending_context:
        parts.append("---\n")
        parts.append(pending_context)
    return "".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Homer's nanobot workspace files from context.")
    parser.add_argument("--all", action="store_true", help="Include all context files")
    parser.add_argument("--include-finance", action="store_true")
    parser.add_argument("--include-health", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing")
    args = parser.parse_args()

    include_finance = args.all or args.include_finance
    include_health = args.all or args.include_health

    user_context = build_user_context(include_finance=include_finance, include_health=include_health)

    if args.dry_run:
        print("=== SOUL.md ==="); print(SOUL_CONTENT)
        print("=== AGENTS.md ==="); print(AGENTS_CONTENT)
        print("=== USER.md ==="); print(user_context)
        return

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "tmp").mkdir(exist_ok=True)  # scratch dir for run_code.py scripts
    (WORKSPACE_DIR / "state").mkdir(exist_ok=True)  # operational state files
    (WORKSPACE_DIR / "files").mkdir(exist_ok=True)  # working documents
    GUEST_AGENT_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)  # sibling guest workspace
    (WORKSPACE_DIR / "SOUL.md").write_text(SOUL_CONTENT, encoding="utf-8")
    (WORKSPACE_DIR / "AGENTS.md").write_text(AGENTS_CONTENT, encoding="utf-8")
    (WORKSPACE_DIR / "USER.md").write_text(user_context, encoding="utf-8")
    # CURRENT_MODEL: intentionally NOT written here. switch_model.py is the
    # SOLE writer of this file — it represents a deliberate runtime model
    # choice that must survive container restarts. Auto-stamping it on every
    # boot caused stale values to override corrected .env settings (homer#247).
    # Readers (version.py, status_server.py, analytics_query.py, entrypoint.sh)
    # fall back to nanobot config / HOMER_DEFAULT_MODEL when the file is absent.
    # HEARTBEAT.md: merge system sections from template + per-household overlay,
    # preserve User Tasks + Completed from live. The overlay lives at
    # user_context/heartbeat_tasks.md (gitignored) and carries tasks that are
    # specific to this deployment (e.g. Gmail scan, Plaid checks, briefings).
    heartbeat_file = WORKSPACE_DIR / "HEARTBEAT.md"
    is_fresh_workspace = not heartbeat_file.exists()
    household_tasks_path = USER_CONTEXT_DIR / "heartbeat_tasks.md"
    household_tasks_text = (
        household_tasks_path.read_text(encoding="utf-8")
        if household_tasks_path.exists()
        else ""
    )
    merged = merge_heartbeat(
        HEARTBEAT_CONTENT,
        heartbeat_file.read_text(encoding="utf-8") if heartbeat_file.exists() else "",
        household_tasks_text,
    )
    # Fresh-tenant bootstrap: stamp Last-run for system tasks that don't
    # have one yet so a months-old fixed-anchor Schedule (e.g. the daily
    # 07:00 morning briefing) doesn't fire on the very first heartbeat
    # tick after provisioning. See _stamp_last_run_for_fresh_system_tasks.
    if is_fresh_workspace:
        merged = _stamp_last_run_for_fresh_system_tasks(merged)
    # Default-tier containers stamp a cheap heartbeat model on any task that
    # doesn't already declare one — keeps managed-key spend predictable while
    # still letting BYOK households use the agent default.
    hb_preset = _resolve_heartbeat_model_default()
    if hb_preset:
        merged = stamp_heartbeat_model(merged, hb_preset)
    heartbeat_file.write_text(merged, encoding="utf-8")
    inject_whats_new(heartbeat_file)

    # Skills: copy from repo skills/ to workspace skills/ (workspace overrides built-ins).
    # Skills owned by a disabled capability are skipped; everything else is core.
    skills_src = REPO_ROOT / "skills"
    gated_skills = _skills_gated_by_capabilities()
    all_on = ENABLED_CAPABILITIES is ALL_CAPABILITIES_ENABLED
    skipped_skills: list[str] = []
    copied_skills: list[str] = []
    if skills_src.exists():
        skills_dst = WORKSPACE_DIR / "skills"
        for skill_dir in skills_src.iterdir():
            if not (skill_dir.is_dir() and (skill_dir / "SKILL.md").exists()):
                continue
            owning_cap = gated_skills.get(skill_dir.name)
            if owning_cap and not all_on and owning_cap not in ENABLED_CAPABILITIES:
                skipped_skills.append(skill_dir.name)
                continue
            dst = skills_dst / skill_dir.name
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "SKILL.md").write_text(
                (skill_dir / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8"
            )
            copied_skills.append(skill_dir.name)
        print(f"✓ Skills copied: {', '.join(copied_skills)}")
        if skipped_skills:
            print(f"  Skills skipped (capability disabled): {', '.join(skipped_skills)}")

    loaded = ALWAYS_LOAD + (["finance"] if include_finance else []) + (["health"] if include_health else [])
    print(f"✓ SOUL.md, AGENTS.md, USER.md written from: {', '.join(loaded)}")
    print(f"  USER.md size: {len(user_context):,} chars")

    # Identity map: nanobot reads this to collapse channel-scoped distinct_ids
    # back to one canonical person, so the same human across WhatsApp / email /
    # telegram doesn't register as three separate household members.
    try:
        from tools.build_identity_map import write_map as _write_identity_map
    except ModuleNotFoundError:
        from build_identity_map import write_map as _write_identity_map
    identity_map_path, entry_count = _write_identity_map(
        output_path=WORKSPACE_DIR / "identity_map.json",
        users_yaml_path=CONTEXT_DIR / "users.yaml",
    )
    print(f"✓ identity_map.json → {identity_map_path.name} ({entry_count} channel entries)")

    # Guest workspace: build if guest agent templates exist
    build_guest_agent_workspace()


def build_guest_agent_workspace() -> None:
    """Build the guest_agent workspace (no household data).

    The guest agent is relationship/scope-scoped, not event-specific. USER.md
    is assembled from active scope envelopes (falling back to ACL if the scope
    store has not been initialised yet).

    Generates:
      guest_agent/SOUL.md           — from GUEST_AGENT_SOUL.md template
      guest_agent/AGENTS.md         — from GUEST_AGENT.md template
      guest_agent/USER.md           — active scope context (zero household data)
      guest_agent/skills/*/SKILL.md — from skills/*/guest/SKILL.md
    """
    if not GUEST_AGENT_SOUL_PATH.exists() or not GUEST_AGENT_AGENTS_PATH.exists():
        return  # Guest agent templates not deployed yet

    GUEST_AGENT_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    # SOUL.md and AGENTS.md from guest_agent templates
    template_vars = _get_template_vars()
    guest_agent_soul = apply_capability_markers(
        GUEST_AGENT_SOUL_PATH.read_text(encoding="utf-8"), ENABLED_CAPABILITIES
    ).format_map(template_vars)
    guest_agent_agents = apply_capability_markers(
        GUEST_AGENT_AGENTS_PATH.read_text(encoding="utf-8"), ENABLED_CAPABILITIES
    ).format_map(template_vars)
    (GUEST_AGENT_WORKSPACE_DIR / "SOUL.md").write_text(guest_agent_soul, encoding="utf-8")
    (GUEST_AGENT_WORKSPACE_DIR / "AGENTS.md").write_text(guest_agent_agents, encoding="utf-8")

    # HEARTBEAT.md for guest agent (escalation delivery polling)
    if GUEST_HEARTBEAT_PATH.exists():
        guest_heartbeat_template = apply_capability_markers(
            GUEST_HEARTBEAT_PATH.read_text(encoding="utf-8"), ENABLED_CAPABILITIES
        ).format_map(template_vars)
        guest_heartbeat_file = GUEST_AGENT_WORKSPACE_DIR / "HEARTBEAT.md"
        guest_merged = merge_heartbeat(
            guest_heartbeat_template,
            guest_heartbeat_file.read_text(encoding="utf-8") if guest_heartbeat_file.exists() else "",
        )
        hb_preset = _resolve_heartbeat_model_default()
        if hb_preset:
            guest_merged = stamp_heartbeat_model(guest_merged, hb_preset)
        guest_heartbeat_file.write_text(guest_merged, encoding="utf-8")

    # Copy guest-facing skills: skills/*/guest/SKILL.md → guest_agent/skills/*/SKILL.md
    skills_src = REPO_ROOT / "skills"
    guest_skills_copied = []
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            guest_skill_file = skill_dir / "guest" / "SKILL.md"
            if skill_dir.is_dir() and guest_skill_file.exists():
                dst = GUEST_AGENT_WORKSPACE_DIR / "skills" / skill_dir.name
                dst.mkdir(parents=True, exist_ok=True)
                (dst / "SKILL.md").write_text(guest_skill_file.read_text(encoding="utf-8"), encoding="utf-8")
                guest_skills_copied.append(skill_dir.name)
    if guest_skills_copied:
        print(f"  Guest skills copied: {', '.join(guest_skills_copied)}")

    # Inject dynamic context into scopes before building USER.md
    try:
        import context_inject
        ctx_updated = context_inject.inject_all()
        if ctx_updated:
            print(f"  Context injected for {ctx_updated} scope(s)")
    except Exception as e:
        print(f"  [warn] context injection failed: {e}")

    # USER.md — stub only. Scope context is injected per-turn by nanobot
    # (see scope_store.render_scope_context_for_sender), NOT written here.
    scopes = _get_active_scopes()
    (GUEST_AGENT_WORKSPACE_DIR / "USER.md").write_text(
        GUEST_USER_MD_STUB, encoding="utf-8"
    )

    # active_scopes.json is written empty: per-sender scope IDs come from
    # current_sender_scopes.json (written per-turn by scope_store). The
    # global list defeated the gate — an unscoped sender could discover
    # every scope ID by catting this file. Guest tools that need the
    # sender's scopes go through tools/guest_scope_guard.py.
    (GUEST_AGENT_WORKSPACE_DIR / "active_scopes.json").write_text(
        "[]", encoding="utf-8"
    )

    # Ensure sessions dir exists
    (GUEST_AGENT_WORKSPACE_DIR / "sessions").mkdir(parents=True, exist_ok=True)

    # blocked_tools.json — enforced at the execution layer in nanobot loop
    # NOTE: escalate.py and deliver_escalation.py are intentionally ALLOWED
    # for the guest agent (Phase 2 escalation flow). resolve_escalation.py is
    # main-agent-only and must remain blocked.
    # AGENTS.md tells the guest LLM not to call these; the list also enforces
    # it at the registry level so a non-compliant LLM can't reach them.
    blocked_tools = [
        # Filesystem (AGENTS.md: "Do NOT use read_file, list_dir, write_file,
        # or edit_file. Your context is pre-loaded.")
        "read_file", "write_file", "edit_file", "list_dir",
        # External (AGENTS.md: "Do NOT use web_search, web_fetch, or other
        # external tools unless the guest explicitly asks…").
        "web_search", "web_fetch",
        # Stale entries below are MCP tool names that don't actually map to
        # registered tools (the underlying scripts are gated by the exec
        # allowPattern in guest_config). Kept for documentation.
        "context_updater", "gmail_fetch", "gmail_search",
        "plaid_fetch", "calendar_fetch", "calendar_add",
        "drive_fetch", "drive_read", "drive_upload",
        "announce_update", "switch_model", "export_context",
        "tasks_update", "manage_guest", "manage_event_guest",
        "resolve_escalation",
        "scope_store",
        "manage_interaction",
    ]
    (GUEST_AGENT_WORKSPACE_DIR / "blocked_tools.json").write_text(
        json.dumps(blocked_tools, indent=2), encoding="utf-8"
    )

    # Build sender_map.json — maps phone digits and LID prefixes to guest names.
    # Guest workspace gets all scope participants.
    # Main workspace excludes participants from guest-only scopes (e.g. family_history)
    # so contributors who can't reach the main agent don't appear there.
    _GUEST_ONLY_SCOPE_TYPES = {"family_history"}
    acl = {}
    if GUEST_AGENT_ACL_FILE.exists():
        try:
            acl = json.loads(GUEST_AGENT_ACL_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    sender_map_full = _build_sender_map(scopes, acl)
    # Compute keys that appear in guest-only scopes vs other scopes.
    # A key is excluded from main only if it appears EXCLUSIVELY in guest-only
    # scopes — a contributor who is also a participant in a regular scope must
    # remain in the main sender_map.
    keys_in_guest_only: set[str] = set()
    keys_in_other: set[str] = set()
    for scope in scopes:
        target = (
            keys_in_guest_only
            if scope.get("scope_type") in _GUEST_ONLY_SCOPE_TYPES
            else keys_in_other
        )
        for p in scope.get("participants", []):
            pid = p.get("party_id", "")
            if pid:
                target.add(pid.split("@")[0])
    exclusive_guest_keys = keys_in_guest_only - keys_in_other
    sender_map_main = {k: v for k, v in sender_map_full.items() if k not in exclusive_guest_keys}

    # Always overwrite both files (or remove when empty) so stale entries from
    # a prior build don't linger.
    guest_path = GUEST_AGENT_WORKSPACE_DIR / "sender_map.json"
    main_path = WORKSPACE_DIR / "sender_map.json"
    if sender_map_full:
        guest_path.write_text(
            json.dumps(sender_map_full, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif guest_path.exists():
        guest_path.unlink()
    if sender_map_main:
        main_path.write_text(
            json.dumps(sender_map_main, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif main_path.exists():
        main_path.unlink()
    print(f"  sender_map.json: {len(sender_map_full)} entries ({len(sender_map_main)} in main)")

    # Build known_email_senders.json — used by email channel pre-LLM filter.
    # Contains household emails (from HOMER_INTERNAL_EMAILS) + guest emails.
    _build_known_email_senders(scopes)

    # Build per-scope-type guest workspaces. Each scope type with a registered
    # template gets its own SOUL/AGENTS/USER.md/skills under
    # <guest_workspace>/<subdir>/ so the nanobot guest loop can swap
    # workspaces per inbound sender (see scope_workspaces.json below).
    _build_scope_type_workspaces(scopes)

    print(f"✓ Guest agent workspace built: {GUEST_AGENT_WORKSPACE_DIR}")
    print(f"  Guest agent USER.md: stub only (scope context injected per-turn)")

    # Sync allow_from list in guest nanobot config from scope store (or ACL fallback)
    update_guest_config_allow_from()


# ---------------------------------------------------------------------------
# Per-scope-type guest workspace overlays
# ---------------------------------------------------------------------------

# Maps scope_type → (subdir name, source skill dir). Each entry's skill dir
# must contain `guest/SOUL.md` and `guest/AGENTS.md`; if either is missing
# the overlay is skipped and the sender falls through to the generic guest
# workspace.
_SCOPE_TYPE_OVERLAYS: dict[str, tuple[str, str]] = {
    "family_history": ("_family_history", "family-historian"),
}


def _build_scope_type_workspaces(scopes: list[dict]) -> None:
    """Build sibling guest workspaces for scope-types with their own SOUL/AGENTS.

    Writes ``<guest_workspace>/<subdir>/`` for each registered scope type that
    has at least one active scope. Also writes
    ``<guest_workspace>/scope_workspaces.json`` mapping sender id (phone or
    LID) → subdir name; the nanobot guest loop reads this to swap workspaces
    per inbound.

    Senders without a registered scope type fall through to the default
    guest workspace.
    """
    sender_to_subdir: dict[str, str] = {}
    written: list[str] = []

    for scope in scopes:
        scope_type = scope.get("scope_type", "")
        overlay = _SCOPE_TYPE_OVERLAYS.get(scope_type)
        if not overlay:
            continue
        subdir_name, skill_name = overlay
        skill_dir = REPO_ROOT / "skills" / skill_name / "guest"
        soul_path = skill_dir / "SOUL.md"
        agents_path = skill_dir / "AGENTS.md"
        if not soul_path.exists() or not agents_path.exists():
            continue

        ws_dir = GUEST_AGENT_WORKSPACE_DIR / subdir_name
        ws_dir.mkdir(parents=True, exist_ok=True)

        # Skill SOUL/AGENTS files include literal JSON examples (with
        # unescaped braces) so we substitute the {HOMER_VENV}/{HOMER_TOOLS}
        # placeholders by plain string replacement rather than format_map,
        # which would treat every brace pair as a placeholder.
        template_vars = _get_template_vars()
        def _interpolate(text: str) -> str:
            for k, v in template_vars.items():
                text = text.replace("{" + k + "}", str(v))
            return text
        soul = _interpolate(apply_capability_markers(
            soul_path.read_text(encoding="utf-8"), ENABLED_CAPABILITIES,
        ))
        agents = _interpolate(apply_capability_markers(
            agents_path.read_text(encoding="utf-8"), ENABLED_CAPABILITIES,
        ))
        (ws_dir / "SOUL.md").write_text(soul, encoding="utf-8")
        (ws_dir / "AGENTS.md").write_text(agents, encoding="utf-8")

        # USER.md stub — scope context still injected per-turn.
        (ws_dir / "USER.md").write_text(GUEST_USER_MD_STUB, encoding="utf-8")
        (ws_dir / "active_scopes.json").write_text("[]", encoding="utf-8")
        (ws_dir / "sessions").mkdir(parents=True, exist_ok=True)

        # Skill folder — only the skill that owns this scope-type, so the
        # agent doesn't see overlapping skill files.
        skill_src = REPO_ROOT / "skills" / skill_name / "guest" / "SKILL.md"
        if skill_src.exists():
            dst_skill_dir = ws_dir / "skills" / skill_name
            dst_skill_dir.mkdir(parents=True, exist_ok=True)
            (dst_skill_dir / "SKILL.md").write_text(
                skill_src.read_text(encoding="utf-8"), encoding="utf-8",
            )

        # blocked_tools.json — restrict to the historian's prescribed flow.
        # message + the history_*.py exec set + escalate flow + minimal fs.
        # Everything else is blocked, so the agent can't drift into
        # accumulate_context, manage_event_guest, web_search, etc.
        (ws_dir / "blocked_tools.json").write_text(
            json.dumps(_HISTORIAN_BLOCKED_TOOLS, indent=2),
            encoding="utf-8",
        )

        # Map every participant for this scope to this subdir, by all the
        # sender-id forms the WhatsApp channel might deliver.
        for p in scope.get("participants", []):
            pid = p.get("party_id", "")
            if not pid:
                continue
            sender_to_subdir[pid] = subdir_name
            if "@" in pid:
                sender_to_subdir[pid.split("@", 1)[0]] = subdir_name

        written.append(subdir_name)

    # scope_workspaces.json — the nanobot guest loop reads this to decide
    # which subdir's SOUL/AGENTS to load for an inbound sender. Empty/no
    # match = use the default guest workspace.
    sw_path = GUEST_AGENT_WORKSPACE_DIR / "scope_workspaces.json"
    if sender_to_subdir:
        sw_path.write_text(
            json.dumps(sender_to_subdir, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"  scope_workspaces.json: {len(sender_to_subdir)} senders → "
              f"{', '.join(sorted(set(sender_to_subdir.values())))}")
    elif sw_path.exists():
        sw_path.unlink()


# Tools the historian guest scope is NOT allowed to call. Mirrors the
# main guest blocked_tools list, but adds the household-management /
# generic guest tools that fight the historian flow (accumulate_context,
# event/manage_*). The agent's prescribed flow only needs `message` and
# the `history_*.py` scripts.
_HISTORIAN_BLOCKED_TOOLS = [
    "web_search", "web_fetch",
    "context_updater", "gmail_fetch", "gmail_search",
    "plaid_fetch", "calendar_fetch", "calendar_add",
    "drive_fetch", "drive_read", "drive_upload",
    "announce_update", "switch_model", "export_context",
    "tasks_update", "manage_guest", "manage_event_guest",
    "manage_interaction", "event_manage", "accumulate_context",
    "resolve_escalation",
    "scope_store",
    "manage_interaction",
]


# Intentional stub: the guest workspace no longer contains scope data. The
# per-turn hook in the nanobot fork resolves the active sender to their scope
# via scope_store.render_scope_context_for_sender and injects it as an
# ephemeral system message. Any "## Scope:" text here means the stub was
# regressed — see tests/test_build_context.py and
# tools/scope_leakage_check.py for the lockdowns.
GUEST_USER_MD_STUB = (
    "# Guest Agent Context\n\n"
    "Scope context for the active sender is injected per-turn by the nanobot "
    "gateway (see scope_store.render_scope_context_for_sender). This file is "
    "intentionally empty of scope data.\n"
)


def _get_active_scopes() -> list[dict]:
    """Return the active scope envelopes, or [] if the scope DB is missing/unreadable."""
    try:
        import sys as _sys
        _sys.path.insert(0, HOMER_TOOLS)
        import scope_store
        if scope_store.get_db_path().exists():
            return scope_store.list_active_scopes()
    except Exception as e:
        print(f"  [warn] scope_store read failed: {e}")
    return []


def _build_sender_map(scopes: list[dict], acl: dict) -> dict[str, str]:
    """Build sender_id → name mapping from scopes, ACL, and lid_map.

    Maps both phone digits (from participant party_id) and LID prefixes
    (from lid_map.json or ACL lid field) to guest names. Written to
    sender_map.json so the agent loop can resolve sender identity on
    inbound messages.

    LID mappings come from two sources:
    - lid_map.json (written by the WhatsApp channel from inbound
      MessageSource pairs at runtime)
    - ACL lid field (set by --update-lid, used for simulation/manual mapping)
    """
    sender_map: dict[str, str] = {}

    # From scope participants: extract phone digits from party_id
    for env in scopes:
        for p in env.get("participants", []):
            name = p.get("name", "")
            if not name:
                continue
            party_id = p.get("party_id", "")
            if party_id and not party_id.startswith("tg:"):
                digits = party_id.split("@")[0]
                if digits:
                    sender_map[digits] = name
            # Email address from participant (for inbound email routing)
            email = p.get("email", "")
            if email:
                sender_map[email] = name

    # From ACL: add phone digits and optional LID
    for key, info in acl.items():
        name = info.get("name", "")
        if not name:
            continue
        # Phone digits from ACL key (e.g., "14129739891@s.whatsapp.net")
        if not key.startswith("tg:"):
            digits = key.split("@")[0]
            if digits:
                sender_map[digits] = name
        # LID from ACL entry (set by --update-lid, used for simulation)
        lid = info.get("lid", "")
        if lid:
            sender_map[lid] = name

    # From lid_map.json (written by the WhatsApp channel from inbound
    # MessageSource pairs). Cross-reference phone→name from sender_map to
    # resolve LID→name.
    lid_map = _load_lid_map()
    for lid_prefix, info in lid_map.items():
        if lid_prefix in sender_map:
            continue  # Already mapped
        if isinstance(info, dict):
            name = info.get("name", "")
            phone = info.get("phone", "")
            if name:
                sender_map[lid_prefix] = name
            elif phone and phone in sender_map:
                sender_map[lid_prefix] = sender_map[phone]

    return sender_map


def _load_lid_map() -> dict:
    """Load lid_map.json from nanobot data dir (written by the WhatsApp
    channel from inbound MessageSource pairs)."""
    lid_map_path = Path.home() / ".nanobot" / "lid_map.json"
    if lid_map_path.exists():
        try:
            return json.loads(lid_map_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _build_known_email_senders(scopes: list[dict]) -> None:
    """Build known_email_senders.json for the email channel's pre-LLM filter.

    Combines household emails (from HOMER_INTERNAL_EMAILS) and guest emails
    (from scope participants).  Written to the main workspace's state dir.
    """
    known: set[str] = set()

    # Household emails from HOMER_INTERNAL_EMAILS env var
    internal = os.environ.get("HOMER_INTERNAL_EMAILS", "")
    for pattern in internal.split(","):
        pattern = pattern.strip().lower()
        if pattern and not pattern.startswith("@"):
            known.add(pattern)

    # Guest emails from scope participants
    for env in scopes:
        for p in env.get("participants", []):
            email = p.get("email", "")
            if email:
                known.add(email)

    state_dir = WORKSPACE_DIR / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "known_email_senders.json"
    out_path.write_text(json.dumps(sorted(known), indent=2), encoding="utf-8")
    print(f"  known_email_senders.json: {len(known)} addresses")


def update_guest_config_allow_from() -> None:
    """Update guest_config.json allow_from from scope store, falling back to ACL file.

    Called after building the guest workspace so the guest nanobot instance
    knows which participants it should accept messages from.
    """
    import fcntl

    if not GUEST_NANOBOT_CONFIG_PATH.exists():
        return  # Guest config not generated yet (deploy.sh creates it via sed)

    # Collect participant IDs — prefer scope store, fall back to ACL
    all_ids: list[str] = []
    try:
        import sys as _sys
        _sys.path.insert(0, HOMER_TOOLS)
        import scope_store
        all_ids = scope_store.get_all_active_participant_ids()
    except Exception:
        pass

    # Fall back to ACL if scope store returned nothing (empty DB or not initialised)
    if not all_ids and GUEST_AGENT_ACL_FILE.exists():
        try:
            acl = json.loads(GUEST_AGENT_ACL_FILE.read_text(encoding="utf-8"))
            all_ids = list(acl.keys())
        except Exception:
            pass

    wa_ids = [p.split("@")[0] for p in all_ids if p.endswith("@s.whatsapp.net")]
    tg_ids = [p.removeprefix("tg:") for p in all_ids if p.startswith("tg:")]

    # Collect email addresses for inbound email routing
    email_ids: list[str] = []
    try:
        email_ids = scope_store.get_all_active_email_addresses()
    except Exception:
        pass

    # Also include LID prefixes from lid_map.json (learned from inbound) —
    # but only for phones already authorized as guests. The lid_map is global
    # (includes primary user), so we must filter to avoid routing admin
    # messages to the guest agent.
    lid_map = _load_lid_map()
    wa_ids_set = set(wa_ids)
    for lid_prefix, info in lid_map.items():
        if isinstance(info, dict):
            phone = info.get("phone", "")
            if phone and phone in wa_ids_set and lid_prefix not in wa_ids_set:
                wa_ids.append(lid_prefix)
    if GUEST_AGENT_ACL_FILE.exists():
        try:
            acl_data = json.loads(GUEST_AGENT_ACL_FILE.read_text(encoding="utf-8"))
            for info in acl_data.values():
                lid = info.get("lid", "")
                if lid and lid not in wa_ids:
                    wa_ids.append(lid)
        except (json.JSONDecodeError, OSError):
            pass

    # Read current guest config
    try:
        with open(GUEST_NANOBOT_CONFIG_PATH, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                config = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        return

    channels = config.get("channels", {})
    wa = channels.get("whatsapp", {})
    wa["allow_from"] = wa_ids
    wa["enabled"] = bool(wa_ids)
    channels["whatsapp"] = wa
    tg = channels.get("telegram", {})
    tg["allowFrom"] = tg_ids
    tg["enabled"] = bool(tg_ids)
    channels["telegram"] = tg
    em = channels.get("email", {})
    em["allowFrom"] = email_ids
    if email_ids:
        em["enabled"] = True
    channels["email"] = em
    config["channels"] = channels

    try:
        with open(GUEST_NANOBOT_CONFIG_PATH, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
            fcntl.flock(f, fcntl.LOCK_UN)
        print(f"  Guest config: {len(wa_ids)} WhatsApp, {len(tg_ids)} Telegram, {len(email_ids)} email participants")
    except Exception as e:
        print(f"  [warn] Could not update guest config allow_from: {e}")


if __name__ == "__main__":
    main()
