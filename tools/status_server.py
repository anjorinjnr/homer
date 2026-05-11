#!/usr/bin/env python3
"""
status_server.py — Lightweight HTTP status page for Homer services.

Serves a JSON/HTML overview of all key services on port 18800 (localhost only).
Access via SSH tunnel: ssh -L 18800:localhost:18800 root@<vps>
Or curl from the VPS: curl localhost:18800

Usage:
    python tools/status_server.py              # start server (foreground)
    python tools/status_server.py --check      # one-shot CLI status check
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE = REPO_ROOT / "context" / ".nanobot_workspace"
GUEST_WORKSPACE = REPO_ROOT / "context" / ".guest_workspace"
NANOBOT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
LOCAL_TZ = ZoneInfo("America/New_York")


def _resolve_active_model() -> str:
    """CURRENT_MODEL is only present after switch_model.py runs (homer#247).
    Fall back to nanobot config, then HOMER_DEFAULT_MODEL env var."""
    cm = WORKSPACE / "CURRENT_MODEL"
    if cm.exists():
        val = cm.read_text().strip()
        if val:
            return val
    if NANOBOT_CONFIG_PATH.exists():
        try:
            cfg = json.loads(NANOBOT_CONFIG_PATH.read_text())
            val = cfg.get("agents", {}).get("defaults", {}).get("model", "")
            if val:
                return val
        except Exception:
            pass
    return os.environ.get("HOMER_DEFAULT_MODEL", "unknown")

sys.path.insert(0, str(Path(__file__).parent))
import scope_leakage_check  # noqa: E402

SERVICES = ["homer", "homer-guest", "homer-bridge", "homer-skyvern-webhook"]


def _run(cmd: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def _git(args: list[str]) -> str:
    return _run(["git", "-C", str(REPO_ROOT)] + args)


def get_service_status(name: str) -> dict:
    is_active = _run(["systemctl", "is-active", name])
    uptime = ""
    mem = ""
    if is_active == "active":
        props = _run([
            "systemctl", "show", name,
            "--property=ActiveEnterTimestamp,MemoryCurrent",
        ])
        for line in props.split("\n"):
            if line.startswith("ActiveEnterTimestamp="):
                uptime = line.split("=", 1)[1]
            elif line.startswith("MemoryCurrent="):
                try:
                    mem_bytes = int(line.split("=", 1)[1])
                    mem = f"{mem_bytes / 1024 / 1024:.0f}MB"
                except (ValueError, IndexError):
                    pass
    return {"name": name, "status": is_active or "not-found", "since": uptime, "memory": mem}


def get_recent_logs(name: str, lines: int = 5) -> str:
    return _run(["journalctl", "-u", name, "-n", str(lines), "--no-pager", "-o", "short-iso"])


def get_status() -> dict:
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

    # Services
    services = [get_service_status(s) for s in SERVICES]

    # Version info
    homer_commit = _git(["rev-parse", "--short", "HEAD"])
    homer_date = _git(["log", "-1", "--format=%ci"])

    nanobot_commit_file = WORKSPACE / "NANOBOT_FORK_COMMIT"
    nanobot_commit = nanobot_commit_file.read_text().strip() if nanobot_commit_file.exists() else "unknown"

    active_model = _resolve_active_model()

    # Heartbeat
    heartbeat_file = WORKSPACE / "HEARTBEAT.md"
    heartbeat_age = ""
    if heartbeat_file.exists():
        mtime = heartbeat_file.stat().st_mtime
        age_min = (datetime.now().timestamp() - mtime) / 60
        heartbeat_age = f"{age_min:.0f}min ago"

    # Guest config
    guest_config_path = Path("/home/homer/.nanobot/guest_config.json")
    guest_count = {"whatsapp": 0, "telegram": 0}
    if guest_config_path.exists():
        try:
            gc = json.loads(guest_config_path.read_text())
            guest_count["whatsapp"] = len(gc.get("channels", {}).get("whatsapp", {}).get("allow_from", []))
            guest_count["telegram"] = len(gc.get("channels", {}).get("telegram", {}).get("allowFrom", []))
        except Exception:
            pass

    # Scope leakage check — guest USER.md (if present at the host path) must
    # be a stub. On container-era deploys the file lives in the portal-managed
    # tenant volume, not on the host, so "missing" is not an alert condition —
    # it just means this status endpoint isn't the right vantage point.
    guest_user_md = GUEST_WORKSPACE / "USER.md"
    leak_code, leak_details = scope_leakage_check.check(guest_user_md)

    return {
        "timestamp": now,
        "services": services,
        "version": {
            "homer_commit": homer_commit,
            "homer_commit_date": homer_date,
            "nanobot_fork_commit": nanobot_commit,
            "active_model": active_model,
        },
        "heartbeat_last_modified": heartbeat_age,
        "guests": guest_count,
        "scope_leakage": {
            "ok": leak_code != 1,  # 0=stub, 2=missing — both fine; only 1=leakage is bad
            **leak_details,
        },
    }


def render_html(status: dict) -> str:
    services_html = ""
    for s in status["services"]:
        color = "#4caf50" if s["status"] == "active" else "#f44336" if s["status"] != "not-found" else "#999"
        dot = f'<span style="color:{color}">&#9679;</span>'
        detail = f' &mdash; up since {s["since"]}, {s["memory"]}' if s["status"] == "active" else ""
        services_html += f"<tr><td>{dot} {s['name']}</td><td><b>{s['status']}</b>{detail}</td></tr>\n"

    v = status["version"]
    return f"""<!DOCTYPE html>
<html><head><title>Homer Status</title>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: monospace; max-width: 700px; margin: 40px auto; padding: 0 20px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #e94560; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  td {{ padding: 6px 12px; border-bottom: 1px solid #333; }}
  .section {{ margin: 24px 0; }}
  .label {{ color: #888; }}
</style></head>
<body>
<h1>Homer Status</h1>
<p class="label">{status['timestamp']}</p>

<div class="section">
<h2>Services</h2>
<table>{services_html}</table>
</div>

<div class="section">
<h2>Version</h2>
<table>
<tr><td class="label">Homer commit</td><td>{v['homer_commit']} ({v['homer_commit_date']})</td></tr>
<tr><td class="label">Nanobot fork</td><td>{v['nanobot_fork_commit']}</td></tr>
<tr><td class="label">Active model</td><td>{v['active_model']}</td></tr>
</table>
</div>

<div class="section">
<h2>Heartbeat</h2>
<p>Last modified: {status['heartbeat_last_modified'] or 'N/A'}</p>
</div>

<div class="section">
<h2>Guests</h2>
<p>WhatsApp: {status['guests']['whatsapp']} &nbsp; Telegram: {status['guests']['telegram']}</p>
</div>

{_render_scope_leakage_html(status['scope_leakage'])}
</body></html>"""


def _render_scope_leakage_html(leak: dict) -> str:
    status = leak.get("status", "")
    if status == "missing":
        # Host-side USER.md absent is expected on container-era deploys —
        # the per-tenant guest workspace is mounted inside the container.
        return (
            '<div class="section"><h2>Scope Isolation</h2>'
            f'<p><span style="color:#999">&#9679;</span> '
            f"n/a (container-era: guest USER.md lives in the tenant volume, "
            f"not {leak.get('path', '?')}).</p></div>"
        )
    if leak.get("ok"):
        return (
            '<div class="section"><h2>Scope Isolation</h2>'
            f'<p><span style="color:#4caf50">&#9679;</span> '
            f"USER.md is a stub ({leak.get('size_chars', 0)} chars), no scope leakage.</p></div>"
        )
    markers = ", ".join(leak.get("markers_found", []))
    return (
        '<div class="section"><h2>Scope Isolation</h2>'
        f'<p><span style="color:#f44336">&#9679;</span> '
        f"<b>SCOPE LEAKAGE DETECTED</b> in {leak.get('path', '?')}: {markers}</p></div>"
    )


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = get_status()
        if self.path == "/json" or self.headers.get("Accept", "").startswith("application/json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status, indent=2).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_html(status).encode())

    def log_message(self, format, *args):
        pass  # silence request logs


def cli_check():
    status = get_status()
    all_ok = True
    for s in status["services"]:
        icon = "+" if s["status"] == "active" else "-" if s["status"] != "not-found" else " "
        detail = f"  (up since {s['since']}, {s['memory']})" if s["status"] == "active" else ""
        print(f"  [{icon}] {s['name']}: {s['status']}{detail}")
        if s["status"] not in ("active", "not-found"):
            all_ok = False
    v = status["version"]
    print(f"\n  Homer: {v['homer_commit']} ({v['homer_commit_date']})")
    print(f"  Model: {v['active_model']}")
    print(f"  Heartbeat: {status['heartbeat_last_modified'] or 'N/A'}")
    print(f"  Guests: {status['guests']['whatsapp']} WA, {status['guests']['telegram']} TG")
    return 0 if all_ok else 1


def main():
    parser = argparse.ArgumentParser(description="Homer status server")
    parser.add_argument("--check", action="store_true", help="One-shot CLI status check")
    parser.add_argument("--port", type=int, default=18800, help="Server port (default: 18800)")
    args = parser.parse_args()

    if args.check:
        sys.exit(cli_check())

    import socket

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

    server = ReusableHTTPServer(("127.0.0.1", args.port), StatusHandler)
    print(f"Status server listening on http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
