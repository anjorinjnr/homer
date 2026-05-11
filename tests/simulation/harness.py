"""Simulation harness: isolated AgentLoop setup, message dispatch, trajectory capture."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

# Repo paths
REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
TOOLS_DIR = REPO_ROOT / "tools"
AGENT_DIR = REPO_ROOT / "agent"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Add tools to path for imports
sys.path.insert(0, str(TOOLS_DIR))


@dataclass
class Actor:
    name: str
    role: str  # "primary" or "guest"
    phone: str
    jid: str
    channel: str
    style: str = ""


@dataclass
class BeatResult:
    beat_idx: int
    actor: str
    role: str
    scope_id: str | None
    message: str
    response: str
    tool_calls: list[dict]
    context_available: dict
    escalation_created: str | None
    latency_ms: int
    tokens: dict[str, int]
    expectations: dict[str, Any]
    passed: bool
    note: str = ""


@dataclass
class Trajectory:
    run_id: str
    model: str
    flow_name: str
    event_id: str
    started_at: str
    beats: list[BeatResult] = field(default_factory=list)
    finished_at: str = ""
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "flow_name": self.flow_name,
            "event_id": self.event_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "beats": [vars(b) for b in self.beats],
            "summary": self.summary,
        }


class SimulationHarness:
    """Sets up an isolated nanobot AgentLoop for multi-user event simulation."""

    def __init__(
        self,
        flow_path: Path,
        workspace_root: Path | None = None,
        keep_workspace: bool = False,
    ):
        self.flow = yaml.safe_load(flow_path.read_text())
        self.flow_path = flow_path
        self.keep_workspace = keep_workspace

        # Load personas
        personas_path = Path(__file__).parent / "fixtures" / "personas.yaml"
        self.personas = yaml.safe_load(personas_path.read_text())

        # Build actor map
        self.actors: dict[str, Actor] = {}
        for key, data in self.personas["actors"].items():
            self.actors[key] = Actor(
                name=data["name"],
                role=data["role"],
                phone=data["phone"],
                jid=data["jid"],
                channel=data["channel"],
                style=data.get("style", ""),
            )

        # Workspace
        self.workspace_root = workspace_root or (REPO_ROOT / "tests" / "sim_workspace")
        self.main_workspace = self.workspace_root / "main_workspace"
        self.guest_workspace = self.workspace_root / "guest_workspace"
        self.events_dir = self.workspace_root / "events"
        self.acl_file = self.events_dir / "guest_agent_acl.json"
        self.scope_db = self.workspace_root / "scopes_test.db"

        # Agent loop (set during setup)
        self._agent_loop = None
        self._patches: list[Any] = []
        self._original_env: dict[str, str | None] = {}

    async def setup(self) -> None:
        """Create isolated workspace, patch paths, initialize AgentLoop."""
        # Clean previous run
        if self.workspace_root.exists():
            shutil.rmtree(self.workspace_root)

        # Create directory structure
        self.main_workspace.mkdir(parents=True)
        self.events_dir.mkdir(parents=True)
        (self.main_workspace / "sessions").mkdir()
        (self.main_workspace / "tmp").mkdir()
        (self.main_workspace / "memory").mkdir()
        (self.main_workspace / "state").mkdir()
        (self.main_workspace / "files").mkdir()
        self.guest_workspace.mkdir(parents=True)
        (self.guest_workspace / "sessions").mkdir()
        (self.guest_workspace / "memory").mkdir()

        # Context fixtures — anonymized household data for realistic eval
        self.context_dir = self.workspace_root / "context"
        self.user_context_dir = self.context_dir / "user_context"
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.user_context_dir.mkdir(parents=True, exist_ok=True)
        fixture_ctx = FIXTURES_DIR / "context"
        if fixture_ctx.exists():
            for md_file in fixture_ctx.glob("*.md"):
                shutil.copy2(md_file, self.user_context_dir / md_file.name)
            users_yaml = fixture_ctx / "users.yaml"
            if users_yaml.exists():
                shutil.copy2(users_yaml, self.context_dir / "users.yaml")

        # Set env vars for isolation (inherited by exec subprocesses)
        self._set_env("HOMER_SCOPE_DB", str(self.scope_db))
        self._set_env("HOMER_EVENTS_DIR", str(self.events_dir))
        self._set_env("HOMER_GUEST_WORKSPACE", str(self.guest_workspace))
        self._set_env("HOMER_SIM", "1")

        # Household skill DBs — isolate to sim workspace
        state_dir = self.main_workspace / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._set_env("HOMER_EVENTS_DB", str(state_dir / "events.db"))
        self._set_env("HOMER_MAINTENANCE_DB", str(state_dir / "maintenance.db"))
        self._set_env("HOMER_MEALS_DB", str(state_dir / "meals.db"))
        self._set_env("HOMER_HEALTH_DB", str(state_dir / "health.db"))
        self._set_env("HOMER_ONBOARDING_DB", str(state_dir / "onboarding.db"))
        self._set_env("HOMER_ONBOARDING_SKIP_REBUILD", "1")
        # Redirect context_updater reads/writes to the sim fixture dir so
        # subprocess invocations of onboarding.py (and other context tools)
        # from the agent loop land in sim-land, not the real repo.
        self._set_env("HOMER_CONTEXT_DIR", str(self.workspace_root / "context"))
        self._set_env(
            "HOMER_USER_CONTEXT_DIR",
            str(self.workspace_root / "context" / "user_context"),
        )
        self._set_env("HOMER_EMAIL_APPROVALS_DB", str(state_dir / "email_approvals.db"))
        # Stable household id for tools that gate on HOMER_HOUSEHOLD_ID
        # (e.g. history_manage, history_invite). Real value doesn't matter
        # in sim — flows use mock_tools for any Supabase-bound history calls.
        self._set_env("HOMER_HOUSEHOLD_ID", "00000000-0000-0000-0000-000000000001")
        self._set_env("HOMER_INTERNAL_EMAILS", "alex.johnson@gmail.com,se.johnson@gmail.com,thejohnsons@gmail.com,@example.com")
        self._set_env("HOMER_EMAIL_ADDRESS", "homer@example.com")
        self._set_env("HOMER_EMAIL_DISPLAY_NAME", "Homer (AI Assistant)")
        self._set_env("PORTAL_BASE_URL", "https://example.com")
        # Point HOMER_HOME to the real repo root (not a worktree) so
        # build_context.py resolves {HOMER_VENV} to the actual .venv path
        homer_home = REPO_ROOT
        git_file = REPO_ROOT / ".git"
        if git_file.is_file():
            # Worktree: .git is a file pointing to the real repo
            # e.g. "gitdir: /Users/.../homer/.git/worktrees/name"
            gitdir = git_file.read_text().strip().replace("gitdir: ", "")
            homer_home = Path(gitdir).parent.parent.parent
        self._set_env("HOMER_HOME", str(homer_home))

        # Patch module-level paths in Homer tools
        self._apply_patches()

        # Run setup steps (create event, add guests) using Homer tools directly
        await self._run_setup_steps()

        # Build workspace files from agent templates
        self._build_workspace()

        # Sync ACL into main workspace (nanobot looks for it at workspace root)
        self._sync_acl()

        # Initialize nanobot AgentLoop
        await self._init_agent_loop()

    async def teardown(self) -> None:
        """Clean up agent loop and workspace."""
        if self._agent_loop:
            try:
                await self._agent_loop.close_mcp()
            except Exception:
                pass

        # Restore patches
        for p in self._patches:
            p.stop()
        self._patches.clear()

        # Restore env
        for key, original in self._original_env.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self._original_env.clear()

        if not self.keep_workspace and self.workspace_root.exists():
            shutil.rmtree(self.workspace_root)

    async def _dispatch(self, message: str, session_key: str, channel: str,
                        chat_id: str, sender_id: str) -> dict:
        """Send a message through the agent loop and collect results."""
        assert self._agent_loop is not None, "Harness not set up"

        guest_ctx = self._agent_loop._resolve_guest_agent_workspace(sender_id)
        sessions = guest_ctx[1] if guest_ctx else self._agent_loop.sessions
        session_before = sessions.get_or_create(session_key)
        msg_count_before = len(session_before.messages)

        start = time.monotonic()
        response = await self._agent_loop.process_direct(
            content=message,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        inline_text = response.content if response else ""

        session_after = sessions.get_or_create(session_key)
        new_messages = session_after.messages[msg_count_before:]
        tool_calls = self._extract_tool_calls(new_messages)

        # Determine response text and its source
        response_text = inline_text
        response_source = "inline" if inline_text else None

        # If no inline response, extract text from message tool calls.
        # When the agent calls `message` more than once in a turn (e.g. an
        # immediate ack followed by a substantive reply), join them with
        # a separator so assertions can match keywords from any of them.
        # Last call still wins for the "primary" response_text.
        if not response_text:
            message_contents = [
                tc["args"].get("content", "")
                for tc in tool_calls
                if tc["tool"] == "message" and "args" in tc
                and tc["args"].get("content")
            ]
            if message_contents:
                response_text = " || ".join(message_contents)
                response_source = "message_tool"
        if not response_text:
            response_text = "(no response)"
            response_source = "none"

        tokens = dict(self._agent_loop._last_usage) if self._agent_loop._last_usage else {}

        return {
            "response": response_text,
            "response_source": response_source,
            "raw_response": response,
            "tool_calls": tool_calls,
            "latency_ms": latency_ms,
            "tokens": tokens,
        }

    async def send_message(self, actor_key: str, message: str) -> dict:
        """Send a message as an actor and return response + metadata."""
        actor = self.actors[actor_key]

        if actor.role == "primary":
            sender_id = "user"
            chat_id = "direct"
            channel = "cli"
            session_key = "cli:direct"
        else:
            sender_id = actor.jid.split("@")[0]
            chat_id = actor.jid
            channel = actor.channel
            session_key = f"{channel}:{chat_id}"

        result = await self._dispatch(message, session_key, channel, chat_id, sender_id)
        result.pop("raw_response", None)
        return result

    async def send_heartbeat(self, message: str) -> dict:
        """Send a heartbeat message, matching production routing.

        Pre-seeds the session with HEARTBEAT.md content on cold start
        to match production where prior ticks have already read the file.
        """
        assert self._agent_loop is not None, "Harness not set up"

        sessions = self._agent_loop.sessions
        session = sessions.get_or_create("heartbeat")

        if not session.messages:
            heartbeat_path = self.main_workspace / "HEARTBEAT.md"
            if heartbeat_path.exists():
                hb_content = heartbeat_path.read_text(encoding="utf-8")
                session.messages.append({
                    "role": "user",
                    "content": f"[Heartbeat context]\n\n{hb_content}",
                })
                session.messages.append({
                    "role": "assistant",
                    "content": "Understood. I will follow the heartbeat rules and execute due tasks using only tool calls.",
                })
                sessions.save(session)

        result = await self._dispatch(message, "heartbeat", "whatsapp", "direct", "user")

        # Mirror prod: HeartbeatService.filter_heartbeat_response decides what
        # actually reaches the user. Inline EMPTY_FINAL_RESPONSE_MESSAGE and
        # STOP_INTENTIONAL_SILENCE are suppressed in prod; the harness should
        # display the suppressed state, not the raw model fallback string.
        from nanobot.heartbeat.service import filter_heartbeat_response

        raw = result.pop("raw_response", None)
        delivered = filter_heartbeat_response(raw, message, suppress_errors=False) if raw else ""

        if not delivered and result.get("response_source") == "inline":
            # Heartbeat layer suppressed the inline final. If the agent already
            # delivered via MessageTool, surface that as the user-visible text.
            message_text = next(
                (tc["args"].get("content", "") for tc in result["tool_calls"]
                 if tc["tool"] == "message" and tc.get("args", {}).get("content")),
                "",
            )
            if message_text:
                result["response"] = message_text
                result["response_source"] = "message_tool"
            else:
                result["response"] = "(silent — heartbeat suppressed)"
                result["response_source"] = "heartbeat_silent"

        return result

    def clear_session(self, role: str = "primary") -> None:
        """Clear conversation history for a session, simulating a new session.

        All primary actors share session key "cli:direct" (hardcoded in
        send_message routing), so clearing that one key resets the session
        for any primary actor.
        """
        assert self._agent_loop is not None, "Harness not set up"
        if role == "primary":
            session = self._agent_loop.sessions.get_or_create("cli:direct")
            session.clear()
            self._agent_loop.sessions.save(session)

    def rebuild_guest_context(self) -> None:
        """Rebuild the guest workspace to reflect event state changes.

        Also synthesises lid_map.json entries for LID-based personas that
        share a phone number with a phone-based persona. This simulates the
        bridge learning the phone→LID mapping on first outbound, so the
        simulation can test the full LID identity chain without a real bridge.
        """
        import build_context
        import context_inject

        # Re-apply patches in case modules were re-imported
        build_context.EVENTS_DIR = self.events_dir
        build_context.GUEST_AGENT_ACL_FILE = self.acl_file
        build_context.GUEST_AGENT_WORKSPACE_DIR = self.guest_workspace
        build_context.WORKSPACE_DIR = self.main_workspace
        context_inject.EVENTS_DIR = self.events_dir

        # Simulate bridge LID learning: for each LID persona that shares a
        # phone with a phone-based persona, write a lid_map entry so
        # build_context can include the LID in sender_map and allow_from.
        self._synthesise_lid_map()

        build_context.build_guest_agent_workspace()
        self._sync_acl()

        # Invalidate the agent loop's guest context + sender map caches so it re-reads
        if self._agent_loop:
            self._agent_loop._guest_agent_cache.clear()
            self._agent_loop._sender_map_cache.clear()

    def _synthesise_lid_map(self) -> None:
        """Write lid_map.json entries for LID-based personas.

        Simulates: Homer sends outbound to phone JID → bridge ack returns LID
        → channel writes lid_map.json. In the simulation, we derive the mapping
        from personas that share the same phone but have different JID formats.

        Writes to the simulation workspace (not ~/.nanobot/) and monkeypatches
        build_context._load_lid_map to read from there.
        """
        import re
        import build_context

        # Build phone→LID mapping from personas
        phone_actors: dict[str, Actor] = {}  # phone → phone-JID actor
        lid_actors: list[Actor] = []
        for actor in self.actors.values():
            if actor.role != "guest" or actor.channel != "whatsapp":
                continue
            if actor.jid.endswith("@lid"):
                lid_actors.append(actor)
            else:
                phone_digits = re.sub(r"[^\d]", "", actor.phone)
                phone_actors[phone_digits] = actor

        if not lid_actors:
            return

        # Write lid_map.json to the simulation workspace (isolated)
        lid_map_path = self.workspace_root / "lid_map.json"
        lid_map: dict = {}
        for actor in lid_actors:
            lid_prefix = actor.jid.split("@")[0]
            phone_digits = re.sub(r"[^\d]", "", actor.phone)
            if phone_digits in phone_actors:
                lid_map[lid_prefix] = {"phone": phone_digits}

        if lid_map:
            lid_map_json = json.dumps(lid_map, indent=2, ensure_ascii=False)
            lid_map_path.write_text(lid_map_json, encoding="utf-8")
            # Also write to both workspaces so the loop can find it
            (self.main_workspace / "lid_map.json").write_text(lid_map_json, encoding="utf-8")
            (self.guest_workspace / "lid_map.json").write_text(lid_map_json, encoding="utf-8")
            # Monkeypatch build_context to read from sim workspace instead of ~/.nanobot/
            build_context._load_lid_map = lambda: json.loads(
                lid_map_path.read_text(encoding="utf-8")
            ) if lid_map_path.exists() else {}

    def snapshot_artifacts(self, output_dir: Path) -> None:
        """Copy event files, scope DB, and workspace state into the run output dir."""
        artifacts = output_dir / "artifacts"

        # Event status files
        if self.events_dir.exists():
            events_dst = artifacts / "events"
            shutil.copytree(self.events_dir, events_dst, dirs_exist_ok=True)

        # Scope DB
        if self.scope_db.exists():
            shutil.copy2(self.scope_db, artifacts / "scopes.db")

        # Main workspace context files
        main_dst = artifacts / "main_workspace"
        main_dst.mkdir(parents=True, exist_ok=True)
        for fname in ["AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md"]:
            src = self.main_workspace / fname
            if src.exists():
                shutil.copy2(src, main_dst / fname)

        # Guest workspace context files
        guest_dst = artifacts / "guest_workspace"
        guest_dst.mkdir(parents=True, exist_ok=True)
        for fname in ["AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md"]:
            src = self.guest_workspace / fname
            if src.exists():
                shutil.copy2(src, guest_dst / fname)

        # Session logs (for deep audit)
        for label, sessions_dir in [
            ("main_sessions", self.main_workspace / "sessions"),
            ("guest_sessions", self.guest_workspace / "sessions"),
        ]:
            if sessions_dir.exists() and any(sessions_dir.iterdir()):
                dst = artifacts / label
                shutil.copytree(sessions_dir, dst, dirs_exist_ok=True)

    def get_last_scope_context(self, sender_id: str) -> str | None:
        """Return the scope-context string injected on the last dispatch for ``sender_id``.

        Populated by the ``_get_scope_context`` wrap installed in
        ``_init_agent_loop``. Returns ``None`` if no dispatch has run for
        this sender or the provider returned nothing.
        """
        return self._last_scope_context.get(sender_id)

    def get_scope_context(self, scope_id: str) -> dict:
        """Read current scope envelope for audit/trajectory capture."""
        import scope_store
        env = scope_store.get_scope(scope_id, self.scope_db)
        return env or {}

    def get_pending_escalations(self) -> list[dict]:
        """Check for pending escalations."""
        import scope_store
        return scope_store.get_pending_escalations(self.scope_db)

    # --- Internal methods ---

    def _sync_acl(self) -> None:
        """Copy ACL and sender_map into main workspace root where nanobot expects them."""
        if self.acl_file.exists():
            shutil.copy2(self.acl_file, self.main_workspace / "guest_agent_acl.json")
        # Sync sender_map.json so main agent loop can also resolve guest names
        sender_map = self.guest_workspace / "sender_map.json"
        if sender_map.exists():
            shutil.copy2(sender_map, self.main_workspace / "sender_map.json")

    def _set_env(self, key: str, value: str) -> None:
        self._original_env[key] = os.environ.get(key)
        os.environ[key] = value

    def _apply_patches(self) -> None:
        """Monkeypatch Homer tool modules to use isolated paths."""
        # Import modules to patch
        import context_inject
        # onboarding.py prefers `from tools import context_updater`, so patch
        # the `tools.*` alias (HOMER_CONTEXT_DIR / HOMER_USER_CONTEXT_DIR env
        # vars cover subprocess tool calls).
        from tools import context_updater
        import event_manage
        import manage_event_guest
        import manage_guest
        import manage_interaction
        import pending_reply
        import scope_store

        patches = [
            # event_manage
            patch.object(event_manage, "EVENTS_DIR", self.events_dir),
            # context_inject (must see sim events for guest USER.md injection)
            patch.object(context_inject, "EVENTS_DIR", self.events_dir),
            # manage_event_guest
            patch.object(manage_event_guest, "EVENTS_DIR", self.events_dir),
            # manage_guest
            patch.object(manage_guest, "ACL_FILE", self.acl_file),
            patch.object(manage_guest, "WORKSPACE_DIR", self.main_workspace),
            # scope_store default DB
            patch.object(scope_store, "DEFAULT_DB_PATH", self.scope_db),
            # context_updater — redirect household.md writes into the sim
            # fixture dir (onboarding.py uses it during setup).
            patch.object(context_updater, "CONTEXT_DIR", self.context_dir),
            patch.object(context_updater, "USER_CONTEXT_DIR", self.user_context_dir),
            # pending_reply — redirect to sim context dir; stub rebuild
            # (workspace hasn't been built yet at setup time, so rebuild is a no-op).
            patch.object(pending_reply, "PENDING_FILE", self.context_dir / "pending_replies.json"),
            patch.object(pending_reply, "_rebuild_context", lambda: None),
            # Stub out systemd restart
            patch.object(manage_guest, "restart_service", return_value=True),
            # Stub out Google Sheets API in event_manage
            patch.object(event_manage, "create_budget_sheet", return_value=None),
        ]

        # manage_guest.rebuild_context calls subprocess build_context.py.
        # Replace with our own rebuild.
        patches.append(
            patch.object(manage_guest, "rebuild_context", self._stub_rebuild_context)
        )

        # event_manage._rebuild_if_guests calls subprocess too
        patches.append(
            patch.object(event_manage, "_rebuild_if_guests", lambda event_id: None)
        )


        for p in patches:
            p.start()
            self._patches.append(p)

    def _stub_rebuild_context(self) -> None:
        """Replacement for manage_guest.rebuild_context during simulation."""
        self._build_guest_workspace()

    async def _run_setup_steps(self) -> None:
        """Execute setup steps from the flow file using Homer tools directly."""
        import io
        import event_manage
        import manage_event_guest

        # Lazy-import household skill tools only when needed
        import manage_guest
        _tool_modules: dict[str, Any] = {
            "event_manage": event_manage,
            "manage_event_guest": manage_event_guest,
            "manage_guest": manage_guest,
        }

        def _get_tool(name: str):
            if name not in _tool_modules:
                if name == "maintenance":
                    import maintenance
                    _tool_modules[name] = maintenance
                elif name == "meal_plan":
                    import meal_plan
                    _tool_modules[name] = meal_plan
                elif name == "health_records":
                    import health_records
                    _tool_modules[name] = health_records
                elif name == "manage_interaction":
                    import manage_interaction
                    _tool_modules[name] = manage_interaction
                elif name == "pending_reply":
                    import pending_reply
                    _tool_modules[name] = pending_reply
                elif name == "onboarding":
                    import onboarding
                    _tool_modules[name] = onboarding
                elif name == "history_manage":
                    import history_manage
                    _tool_modules[name] = history_manage
                elif name == "history_invite":
                    import history_invite
                    _tool_modules[name] = history_invite
                else:
                    raise ValueError(f"Unknown setup tool: {name}")
            return _tool_modules[name]

        for step in self.flow.get("setup", []):
            tool = step["tool"]
            args = step["args"]

            # Suppress tool stdout during setup (they print JSON results)
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                mod = _get_tool(tool)
                sys.argv = [tool] + args
                try:
                    mod.main()
                except SystemExit:
                    pass
            finally:
                sys.stdout = old_stdout

        sys.argv = ["simulation"]

    def _build_workspace(self) -> None:
        """Build main + guest workspace files from agent templates."""
        import build_context

        # Patch build_context paths to use sim workspace
        build_context.WORKSPACE_DIR = self.main_workspace
        build_context.EVENTS_DIR = self.events_dir
        build_context.GUEST_AGENT_WORKSPACE_DIR = self.guest_workspace
        build_context.GUEST_AGENT_ACL_FILE = self.acl_file
        build_context.CONTEXT_DIR = self.context_dir
        build_context.USER_CONTEXT_DIR = self.user_context_dir

        # Write SOUL.md and AGENTS.md from templates
        template_vars = build_context._get_template_vars()

        soul = (AGENT_DIR / "SOUL.md").read_text(encoding="utf-8")
        agents = (AGENT_DIR / "AGENTS.md").read_text(encoding="utf-8").format_map(template_vars)

        (self.main_workspace / "SOUL.md").write_text(soul, encoding="utf-8")
        (self.main_workspace / "AGENTS.md").write_text(agents, encoding="utf-8")

        # Write USER.md from anonymized context fixtures (same assembly as prod)
        user_md = build_context.build_user_context()
        (self.main_workspace / "USER.md").write_text(user_md, encoding="utf-8")

        # Write HEARTBEAT.md — use flow-provided content if available
        heartbeat_content = self.flow.get(
            "heartbeat_md",
            "# Heartbeat\n\nNo active tasks during simulation.\n",
        )
        (self.main_workspace / "HEARTBEAT.md").write_text(
            heartbeat_content, encoding="utf-8",
        )

        # Write CURRENT_MODEL
        model = self.flow.get("model", "gemini/gemini-3-flash-preview")
        (self.main_workspace / "CURRENT_MODEL").write_text(model, encoding="utf-8")

        # Copy skills from repo to main workspace (same as build_context.py main())
        skills_src = REPO_ROOT / "skills"
        if skills_src.exists():
            skills_dst = self.main_workspace / "skills"
            for skill_dir in skills_src.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    dst = skills_dst / skill_dir.name
                    dst.mkdir(parents=True, exist_ok=True)
                    (dst / "SKILL.md").write_text(
                        (skill_dir / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8"
                    )

        # Build guest workspace
        self._build_guest_workspace()

    def _build_guest_workspace(self) -> None:
        """Build guest agent workspace from templates + scope store."""
        import io
        import build_context
        import context_inject

        build_context.WORKSPACE_DIR = self.main_workspace
        build_context.EVENTS_DIR = self.events_dir
        build_context.GUEST_AGENT_WORKSPACE_DIR = self.guest_workspace
        build_context.GUEST_AGENT_ACL_FILE = self.acl_file
        build_context.CONTEXT_DIR = self.context_dir
        build_context.USER_CONTEXT_DIR = self.user_context_dir
        context_inject.EVENTS_DIR = self.events_dir

        # Suppress build_context print output
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            build_context.build_guest_agent_workspace()
        finally:
            sys.stdout = old_stdout

    async def _init_agent_loop(self) -> None:
        """Create a nanobot AgentLoop with isolated workspace and test config."""
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus
        from nanobot.config.loader import load_config

        model = self.flow.get("model", "gemini/gemini-3-flash-preview")

        # Write a minimal nanobot config for the simulation
        config_data = self._build_nanobot_config(model)
        config_path = self.workspace_root / "sim_config.json"
        config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

        config = load_config(config_path)
        config.agents.defaults.workspace = str(self.main_workspace)

        # Build provider
        from nanobot.cli.commands import _make_provider
        provider = _make_provider(config)

        bus = MessageBus()

        self._agent_loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=self.main_workspace,
            model=model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            context_window_tokens=config.agents.defaults.context_window_tokens,
            exec_config=config.tools.exec,
            restrict_to_workspace=False,
            channels_config=config.channels,
            timezone="America/New_York",
            guest_workspace=self.guest_workspace,
            scope_context_provider="scope_store:render_scope_context_for_sender",
        )

        # Record per-sender injected scope context for scope_isolation assertions.
        # Wrap _get_scope_context so tests can read back what was injected.
        self._last_scope_context: dict[str, str | None] = {}
        _orig_get_scope = self._agent_loop._get_scope_context

        def _capture(sender_id: str) -> str | None:
            result = _orig_get_scope(sender_id)
            self._last_scope_context[sender_id] = result
            return result

        self._agent_loop._get_scope_context = _capture

        # Intercept exec calls matching mock_tools patterns from the flow YAML
        self._install_exec_mocks()

        # Patch _restrict_fs_tools so the guest exec tool can still call homer
        # scripts outside the guest workspace (restrict filesystem tools but
        # leave ExecTool unrestricted — the harness controls the environment).
        self._patch_guest_exec_restriction()

    def _install_exec_mocks(self) -> None:
        """Wrap ExecTool.execute to intercept commands matching mock_tools patterns.

        Flow YAML can define mock_tools as a list of {pattern, response} dicts.
        When an exec command matches a pattern (regex), the mock response is
        returned instead of running the real command.
        """
        mock_tools = self.flow.get("mock_tools", [])
        if not mock_tools:
            return

        import re as _re
        from nanobot.agent.tools.shell import ExecTool

        compiled = [(_re.compile(m["pattern"]), m["response"]) for m in mock_tools]

        exec_tool = self._agent_loop.tools.get("exec")
        if not exec_tool or not isinstance(exec_tool, ExecTool):
            return

        original_execute = exec_tool.execute

        async def intercepted_execute(command: str, **kwargs):
            for regex, response in compiled:
                if regex.search(command):
                    return response
            return await original_execute(command, **kwargs)

        exec_tool.execute = intercepted_execute

    def _patch_guest_exec_restriction(self) -> None:
        """Replace _restrict_fs_tools so ExecTool keeps restrict_to_workspace=False.

        In production, nanobot forces restrict_to_workspace=True for guest
        messages, which blocks exec calls to scripts outside the guest workspace.
        In simulation we control the environment, so we only restrict filesystem
        tools (read/write/edit) to the guest workspace but let exec reach the
        repo tools directory.
        """
        from contextlib import contextmanager
        from nanobot.agent.tools.filesystem import _FsTool

        loop = self._agent_loop

        @contextmanager
        def _restricted(guest_workspace):
            saved = []
            for tool in loop.tools._tools.values():
                if isinstance(tool, _FsTool):
                    saved.append((tool, tool._workspace, tool._allowed_dir))
                    tool._workspace = guest_workspace
                    tool._allowed_dir = guest_workspace
                # ExecTool intentionally NOT restricted for simulation
            try:
                yield
            finally:
                for tool, ws, ad in saved:
                    tool._workspace = ws
                    tool._allowed_dir = ad

        loop._restrict_fs_tools = _restricted

    def _build_nanobot_config(self, model: str) -> dict:
        """Build a nanobot config dict for the simulation.

        Reads API keys from the real homer config or env vars.
        """
        # Try to read API keys from the real nanobot config
        real_config_path = Path.home() / ".nanobot" / "config.json"
        api_keys: dict[str, str] = {}
        if real_config_path.exists():
            try:
                real_cfg = json.loads(real_config_path.read_text())
                providers = real_cfg.get("providers", {})
                for provider_name in ("gemini", "anthropic", "openai"):
                    key = providers.get(provider_name, {}).get("apiKey", "")
                    if key and not key.startswith("${"):
                        api_keys[provider_name] = key
            except Exception:
                pass

        # Fall back to env vars
        if "gemini" not in api_keys:
            api_keys["gemini"] = os.environ.get("GEMINI_API_KEY", "")
        if "anthropic" not in api_keys:
            api_keys["anthropic"] = os.environ.get("ANTHROPIC_API_KEY", "")

        return {
            "providers": {
                "gemini": {"apiKey": api_keys.get("gemini", "")},
                "anthropic": {"apiKey": api_keys.get("anthropic", "")},
            },
            "agents": {
                "defaults": {
                    "model": model,
                    "workspace": str(self.main_workspace),
                    "timezone": "America/New_York",
                    "max_tool_iterations": 20,
                }
            },
            "channels": {
                "send_progress": False,
                "send_tool_hints": False,
            },
            "gateway": {
                "port": 0,  # not used
                "heartbeat": {"enabled": False},
            },
            "tools": {
                "exec": {
                    "enable": True,
                    "timeout": 60,
                    "allowedEnvKeys": [
                        "HOMER_SCOPE_DB",
                        "HOMER_EVENTS_DIR",
                        "HOMER_EVENTS_DB",
                        "HOMER_GUEST_WORKSPACE",
                        "HOMER_SIM",
                        "HOMER_MAINTENANCE_DB",
                        "HOMER_MEALS_DB",
                        "HOMER_HEALTH_DB",
                        "HOMER_ONBOARDING_DB",
                        "HOMER_ONBOARDING_SKIP_REBUILD",
                        "HOMER_CONTEXT_DIR",
                        "HOMER_USER_CONTEXT_DIR",
                        "HOMER_EMAIL_APPROVALS_DB",
                        "HOMER_INTERNAL_EMAILS",
                        "HOMER_EMAIL_ADDRESS",
                        "HOMER_EMAIL_DISPLAY_NAME",
                        "PORTAL_BASE_URL",
                        "HOMER_TIMEZONE",
                        "HOMER_HOME",
                        "HOMER_WORKSPACE",
                        "GEMINI_API_KEY",
                        "ANTHROPIC_API_KEY",
                    ],
                },
                "restrictToWorkspace": False,
            },
        }

    def _extract_tool_calls(self, messages: list[dict]) -> list[dict]:
        """Extract tool call info from session messages."""
        calls = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                raw_args = func.get("arguments", "")
                entry: dict[str, Any] = {
                    "tool": func.get("name", "unknown"),
                    "args_preview": raw_args[:200],
                }
                # Parse full args for all tools (needed for report inspection)
                if raw_args:
                    try:
                        entry["args"] = json.loads(raw_args)
                    except (json.JSONDecodeError, TypeError):
                        entry["args"] = {"_raw": raw_args[:500]}
                calls.append(entry)
        return calls


def check_expectations(
    response: str,
    tool_calls: list[dict],
    expectations: dict,
    harness: SimulationHarness,
    event_id: str,
    response_source: str | None = None,
    sender_id: str | None = None,
) -> dict:
    """Evaluate beat expectations against actual results.

    Returns dict with check results and overall pass/fail.
    """
    result: dict[str, Any] = {}
    passed = True

    if "scope_isolation" in expectations and sender_id is not None:
        spec = expectations["scope_isolation"]
        injected = harness.get_last_scope_context(sender_id) or ""
        must_contain = spec.get("must_contain", [])
        must_not_contain = spec.get("must_not_contain", [])
        missing = [s for s in must_contain if s not in injected]
        leaked = [s for s in must_not_contain if s in injected]
        result["scope_isolation"] = {
            "injected_len": len(injected),
            "missing": missing,
            "leaked": leaked,
        }
        if missing or leaked:
            passed = False

    if "keywords" in expectations:
        expected = expectations["keywords"]
        found = [k for k in expected if k.lower() in response.lower()]
        missing = [k for k in expected if k.lower() not in response.lower()]
        result["keywords"] = {"expected": expected, "found": found, "missing": missing}
        if missing:
            passed = False

    if "no_keywords" in expectations:
        forbidden = expectations["no_keywords"]
        leaked = [k for k in forbidden if k.lower() in response.lower()]
        result["no_keywords"] = {"forbidden": forbidden, "leaked": leaked}
        if leaked:
            passed = False

    if "escalation" in expectations:
        expect_esc = expectations["escalation"]
        escalations = harness.get_pending_escalations()
        esc_fired = len(escalations) > 0
        result["escalation"] = {"expected": expect_esc, "fired": esc_fired}
        if expect_esc != esc_fired:
            passed = False

    if "tools_used" in expectations:
        actual_tools = [tc["tool"] for tc in tool_calls]
        expected_tools = expectations["tools_used"]
        missing = [t for t in expected_tools if t not in actual_tools]
        result["tools_used"] = {
            "expected": expected_tools,
            "actual": actual_tools,
            "missing": missing,
        }
        if missing:
            passed = False

    if "tools_not_used" in expectations:
        actual_tools = [tc["tool"] for tc in tool_calls]
        forbidden_tools = expectations["tools_not_used"]
        used_forbidden = [t for t in forbidden_tools if t in actual_tools]
        result["tools_not_used"] = {"forbidden": forbidden_tools, "used": used_forbidden}
        if used_forbidden:
            passed = False

    if "rsvp" in expectations:
        rsvp_spec = expectations["rsvp"]
        guest_name = rsvp_spec["guest"]
        expected_status = rsvp_spec["status"]
        # Query the event DB directly via event_store (already patched to sim DB)
        import event_store

        guests = event_store.list_guests(event_id)
        match = None
        for g in guests:
            if g["name"].lower() == guest_name.lower():
                match = g
                break
        actual_status = match["rsvp_status"] if match else None
        rsvp_ok = actual_status == expected_status
        result["rsvp"] = {
            "guest": guest_name,
            "expected_status": expected_status,
            "actual_status": actual_status,
        }
        if not rsvp_ok:
            passed = False

    if "max_tool_calls" in expectations:
        max_allowed = expectations["max_tool_calls"]
        actual_count = len(tool_calls)
        result["max_tool_calls"] = {"max": max_allowed, "actual": actual_count}
        if actual_count > max_allowed:
            passed = False

    if "tool_sequence" in expectations:
        import re as _re
        # Check that expected patterns appear in order in the exec command args
        exec_commands = [
            tc.get("args", {}).get("command", "") for tc in tool_calls if tc["tool"] == "exec"
        ]
        all_commands = " ||| ".join(exec_commands)
        seq_result = []
        for spec in expectations["tool_sequence"]:
            pattern = spec["pattern"]
            found = bool(_re.search(pattern, all_commands))
            seq_result.append({"pattern": pattern, "found": found})
            if not found:
                passed = False
        result["tool_sequence"] = seq_result

    if "tool_sequence_forbidden" in expectations:
        import re as _re
        exec_commands = [
            tc.get("args", {}).get("command", "") for tc in tool_calls if tc["tool"] == "exec"
        ]
        all_commands = " ||| ".join(exec_commands)
        forbidden_result = []
        for spec in expectations["tool_sequence_forbidden"]:
            pattern = spec["pattern"] if isinstance(spec, dict) else spec
            found = bool(_re.search(pattern, all_commands))
            forbidden_result.append({"pattern": pattern, "found": found})
            if found:
                passed = False
        result["tool_sequence_forbidden"] = forbidden_result

    if expectations.get("no_text_response"):
        wrote_text = response_source == "inline"
        result["no_text_response"] = {"wrote_inline_text": wrote_text}
        if wrote_text:
            passed = False

    # Message-tool channel/chat_id routing assertions
    _message_keys = (
        "message_channels", "message_channels_forbidden",
        "message_chat_ids", "message_chat_ids_forbidden",
        "message_patterns", "message_patterns_forbidden",
    )
    if any(k in expectations for k in _message_keys):
        import re as _re
        message_calls = [tc for tc in tool_calls if tc["tool"] == "message"]
        actual_channels = [str(tc.get("args", {}).get("channel", "")) for tc in message_calls]
        actual_chat_ids = [str(tc.get("args", {}).get("chat_id", "")) for tc in message_calls]
        actual_contents = [str(tc.get("args", {}).get("content", "")) for tc in message_calls]

        if "message_channels" in expectations:
            required = expectations["message_channels"]
            missing = [c for c in required if c not in actual_channels]
            result["message_channels"] = {"required": required, "actual": actual_channels, "missing": missing}
            if missing:
                passed = False

        if "message_channels_forbidden" in expectations:
            forbidden = expectations["message_channels_forbidden"]
            used = [c for c in forbidden if c in actual_channels]
            result["message_channels_forbidden"] = {"forbidden": forbidden, "actual": actual_channels, "used": used}
            if used:
                passed = False

        if "message_chat_ids" in expectations:
            required_patterns = expectations["message_chat_ids"]
            unmatched = [p for p in required_patterns if not any(_re.search(p, cid) for cid in actual_chat_ids)]
            result["message_chat_ids"] = {"required_patterns": required_patterns, "actual": actual_chat_ids, "unmatched": unmatched}
            if unmatched:
                passed = False

        if "message_chat_ids_forbidden" in expectations:
            forbidden_patterns = expectations["message_chat_ids_forbidden"]
            matched = [p for p in forbidden_patterns if any(_re.search(p, cid) for cid in actual_chat_ids)]
            result["message_chat_ids_forbidden"] = {"forbidden_patterns": forbidden_patterns, "actual": actual_chat_ids, "matched": matched}
            if matched:
                passed = False

        if "message_patterns" in expectations:
            required_patterns = expectations["message_patterns"]
            unmatched = [p for p in required_patterns if not any(_re.search(p, c) for c in actual_contents)]
            result["message_patterns"] = {"required_patterns": required_patterns, "unmatched": unmatched}
            if unmatched:
                passed = False

        if "message_patterns_forbidden" in expectations:
            forbidden_patterns = expectations["message_patterns_forbidden"]
            matched = [p for p in forbidden_patterns if any(_re.search(p, c) for c in actual_contents)]
            result["message_patterns_forbidden"] = {"forbidden_patterns": forbidden_patterns, "matched": matched}
            if matched:
                passed = False

    result["pass"] = passed
    return result
