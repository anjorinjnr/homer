#!/usr/bin/env python3
"""
switch_model.py — Switch Homer's active LLM model without a full redeploy.

Updates ~/.nanobot/config.json and restarts the homer systemd service.
After restart, the next message will use the new model.

Usage (via Homer exec tool):
    python3 /opt/homer/tools/switch_model.py --model pro
    python3 /opt/homer/tools/switch_model.py --model flash
    python3 /opt/homer/tools/switch_model.py --model sonnet
    python3 /opt/homer/tools/switch_model.py --model haiku
    python3 /opt/homer/tools/switch_model.py --model claude   # alias for sonnet

Model presets:
    flash25       — gemini/gemini-2.5-flash (cheapest, good for simple tasks)
    flash         — gemini/gemini-3-flash-preview
    pro           — gemini/gemini-3.1-pro-preview
    sonnet        — claude-sonnet-4-6
    haiku         — claude-haiku-4-5-20251001
    claude        — alias for sonnet
    default-cheap — deepseek/deepseek-v3.2 (default-tier background work;
                    routed via litellm openrouter passthrough)
"""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
DOCKERENV = Path("/.dockerenv")

PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _discover_tier() -> tuple[str, list[str]]:
    """Best-effort sniff of which API keys are present in this container.

    Returns (tier_label, sorted_list_of_present_env_var_names).
    """
    present = [name for name in PROVIDER_ENV.values() if os.environ.get(name)]
    has_or = "OPENROUTER_API_KEY" in present
    has_byok = any(name in present for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"))
    if has_or:
        tier = "default-tier"
    elif has_byok:
        tier = "BYOK"
    else:
        tier = "unknown"
    return tier, sorted(present)


def _validate_provider_credentials(preset_name: str, provider: str) -> None:
    """Exit 1 with a clear message if the chosen preset's provider has no key."""
    env_var = PROVIDER_ENV.get(provider)
    if env_var is None:
        # Unknown provider — let it through; nanobot will surface the error.
        return
    if os.environ.get(env_var):
        return

    tier, present = _discover_tier()
    keys_str = ", ".join(present) if present else "no provider keys"
    print(
        f"ERROR: {preset_name} requires {env_var} in this container, but it's not set.\n"
        f"This container is configured for {tier} mode ({keys_str}).\n"
        f"Pick a preset matching this tier, or have the admin add the key first.",
        file=sys.stderr,
    )
    sys.exit(1)


# Canonical preset slate — every entry routes through `provider="openrouter"`
# so chat + heartbeats land on the household's OR sub-key (or BYOK OR key)
# and OpenRouter dispatches upstream based on the `<vendor>/<model>` prefix.
# `auto` defers to OR's per-request routing; `cheap` pins deepseek-v3.2;
# the nine tier×vendor slots let admins pick "how smart" × "which vendor".
#
# Kept in lockstep with homer-portal/backend/services/config_service.py
# MODEL_PRESETS — both must agree on the label set. See
# [[project_openrouter_consolidation]] for the rationale.
MODELS = {
    "auto":             {"model": "openrouter/auto",               "provider": "openrouter"},
    "cheap":            {"model": "deepseek/deepseek-v3.2",        "provider": "openrouter"},

    "gemini-fast":      {"model": "google/gemini-3-flash-preview", "provider": "openrouter"},
    "gemini-balanced":  {"model": "google/gemini-2.5-pro",         "provider": "openrouter"},
    "gemini-smart":     {"model": "google/gemini-3.1-pro-preview", "provider": "openrouter"},

    "gpt-fast":         {"model": "openai/gpt-5-mini",             "provider": "openrouter"},
    "gpt-balanced":     {"model": "openai/gpt-5",                  "provider": "openrouter"},
    "gpt-smart":        {"model": "openai/gpt-5.5",                "provider": "openrouter"},

    "claude-fast":      {"model": "anthropic/claude-haiku-4.5",    "provider": "openrouter"},
    "claude-balanced":  {"model": "anthropic/claude-sonnet-4.6",   "provider": "openrouter"},
    "claude-smart":     {"model": "anthropic/claude-opus-4.7",     "provider": "openrouter"},

    # Internal alias retained for the default-tier heartbeat path (used by
    # render_household_env when HOMER_HEARTBEAT_MODEL is unset). Same SKU
    # as `cheap`; named for clarity at the call site.
    "default-cheap":    {"model": "deepseek/deepseek-v3.2",        "provider": "openrouter"},
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Switch Homer's active model.")
    parser.add_argument("--model", required=True, choices=MODELS.keys(),
                        help="Model preset to switch to")
    parser.add_argument("--no-restart", action="store_true",
                        help="Update config only, don't restart the service")
    args = parser.parse_args()

    preset = MODELS[args.model]

    # Fail fast if the chosen preset's provider has no API key in this
    # container — better to refuse here than to write a config that boots
    # nanobot into an unauthenticated state.
    _validate_provider_credentials(args.model, preset["provider"])

    if not CONFIG_PATH.exists():
        print(f"ERROR: config not found at {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(CONFIG_PATH.read_text())
    current_model = config.get("agents", {}).get("defaults", {}).get("model", "unknown")

    config.setdefault("agents", {}).setdefault("defaults", {})
    config["agents"]["defaults"]["model"] = preset["model"]
    config["agents"]["defaults"]["provider"] = preset["provider"]

    CONFIG_PATH.write_text(json.dumps(config, indent=2))

    # Update CURRENT_MODEL in workspace so Homer knows what it's running on
    workspace = Path(config.get("agents", {}).get("defaults", {}).get(
        "workspace", "~/.nanobot/workspace"
    )).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "CURRENT_MODEL").write_text(preset["model"], encoding="utf-8")

    print(f"Model switched: {current_model} → {preset['model']}")

    if args.no_restart:
        print("Config updated. Restart homer service to apply.")
        return

    # In a container, there's no systemd — we trigger the restart by killing
    # PID 1 (tini), which exits the container; Docker's restart policy
    # (unless-stopped) brings it back up, and entrypoint.sh re-reads
    # CURRENT_MODEL on boot to render the new config.
    #
    # Deferred kill: a bare `os.kill(1, SIGTERM)` runs synchronously and
    # tears the agent process down BEFORE the current turn's reply gets
    # flushed to the user — observed in prod on 2026-05-14: "switch to
    # gemini-balanced" ran the tool, container died, the "Switching to
    # …" confirmation reply never reached WhatsApp. Spawn a detached
    # background process that sleeps a few seconds and THEN signals PID 1,
    # giving the agent loop time to finalize and send its turn output.
    # `setsid` detaches so this script's exit doesn't terminate the
    # background sleep along with its session.
    if DOCKERENV.exists():
        print(f"Done. Container will restart to apply {preset['model']}. Send your next message in ~10s.")
        sys.stdout.flush()
        try:
            subprocess.Popen(
                ["sh", "-c", "sleep 3 && kill -TERM 1"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except PermissionError:
            print("Could not schedule restart — restart the container manually.", file=sys.stderr)
        return

    # systemd is Linux-only; skip gracefully on macOS or other systems
    systemctl = subprocess.run(["which", "systemctl"], capture_output=True)
    if systemctl.returncode != 0:
        print(f"Config updated to {preset['model']}. No systemd detected — restart homer manually to apply.")
        return

    print("Restarting homer service...")
    result = subprocess.run(["sudo", "/usr/bin/systemctl", "restart", "homer"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        print(f"Config updated but service restart failed: {result.stderr.strip()}", file=sys.stderr)
        print("Restart homer manually: sudo systemctl restart homer")
        return

    print(f"Done. Homer is now using {preset['model']}. Send your next message.")


if __name__ == "__main__":
    main()
