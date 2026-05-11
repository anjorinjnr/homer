"""Pin the guest exec allowlist to specific scripts.

Before this change the regex was ``tools/\\w+\\.py`` — any of the ~70 scripts
in tools/ was reachable from a guest LLM via the exec tool. Now it's an
explicit per-script list. This test parses the rendered config templates
and asserts the allowed set matches what the guest AGENTS.md actually needs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
TEMPLATES = [
    REPO_ROOT / "config" / "guest_config.json.template",
    REPO_ROOT / "config" / "guest_config.hosted.json.template",
]

# The full set of tools the guest AGENTS.md tells the LLM to invoke.
# Anything outside this set should be unreachable from guest exec.
EXPECTED_GUEST_SCRIPTS = frozenset({
    "escalate.py",
    "deliver_escalation.py",
    "event_manage.py",
    "accumulate_context.py",
    "pending_reply.py",
})


def _parse_allow_patterns(template_path: Path) -> list[str]:
    """Pull allowPatterns from the rendered JSON template.

    Templates contain ${VAR} substitutions — both inside JSON strings (valid)
    and as bare values for booleans like ${CHANNEL_GUEST_TELEGRAM_ENABLED}
    (invalid JSON pre-substitution). Replace bare ${...} placeholders with
    `false` so the file parses, then yank allowPatterns.
    """
    text = template_path.read_text(encoding="utf-8")
    text = re.sub(r":\s*\$\{[A-Z_]+\}", ": false", text)
    config = json.loads(text)
    return config["tools"]["exec"]["allowPatterns"]


@pytest.mark.parametrize("template_path", TEMPLATES, ids=lambda p: p.name)
def test_allowlist_is_per_script_not_wildcard(template_path):
    patterns = _parse_allow_patterns(template_path)
    assert all("\\w+" not in p for p in patterns), (
        "Found wildcard \\w+ in allowPatterns — guest can call any script in tools/."
    )


@pytest.mark.parametrize("template_path", TEMPLATES, ids=lambda p: p.name)
def test_allowlist_matches_expected_scripts(template_path):
    patterns = _parse_allow_patterns(template_path)
    matched = set()
    for script in EXPECTED_GUEST_SCRIPTS:
        if any(re.search(rf"/{re.escape(script)}\\\.py", p) or script.replace(".py", "") in p for p in patterns):
            matched.add(script)
    assert matched == EXPECTED_GUEST_SCRIPTS, (
        f"Expected {EXPECTED_GUEST_SCRIPTS}, allowlist covers {matched}. "
        f"Patterns: {patterns}"
    )


def test_blocked_tools_match_agents_md_dont_use_list():
    """Pin every tool the guest AGENTS.md tells the LLM not to use.

    Sources from context/.guest_workspace/AGENTS.md:
      * "Do NOT use read_file, list_dir, write_file, or edit_file."
      * "Do NOT use web_search, web_fetch, or other external tools…"

    AGENTS.md is just a prompt — Adam's session showed the LLM ignored it.
    The blocked_tools list is the actual gate.
    """
    import sys
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    src = (REPO_ROOT / "tools" / "build_context.py").read_text()
    must_be_blocked = (
        "read_file", "write_file", "edit_file", "list_dir",
        "web_search", "web_fetch",
    )
    missing = [t for t in must_be_blocked if f'"{t}"' not in src]
    assert not missing, f"AGENTS.md says don't use these but they're not in blocked_tools: {missing}"


@pytest.mark.parametrize("template_path", TEMPLATES, ids=lambda p: p.name)
def test_no_dangerous_scripts_in_allowlist(template_path):
    """Spot-check that scripts the guest must NEVER reach are absent."""
    patterns = " ".join(_parse_allow_patterns(template_path))
    forbidden = [
        "manage_event_guest.py",  # Lists / mutates other guests' rosters
        "manage_interaction.py",  # Closes scopes
        "generate_invite.py",     # Reads event status, generates invite image
        "rsvp_invite.py",         # Generates RSVP links
        "calendar_add.py",        # Mutates calendar
        "drive_read.py",          # Drive access
        "gmail_send.py",          # Outbound email
        "scope_store.py",         # Direct scope DB access
        "context_updater.py",     # Writes household context
    ]
    for f in forbidden:
        assert f not in patterns, f"Forbidden script {f} appears in {template_path.name}"
