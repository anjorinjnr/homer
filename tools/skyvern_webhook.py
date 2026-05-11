#!/usr/bin/env python3
"""
skyvern_webhook.py — HTTP server that receives Skyvern task completion callbacks.

Skyvern POSTs a JSON payload to this server when a browser task finishes.
This server validates the HMAC-SHA256 signature, writes the result to
{HOMER_WORKSPACE}/skyvern_results/<run_id>.json, and returns 200.

Homer checks results via: skyvern_task.py --check <run_id>

Deploy (VPS):
    # Run as a systemd service.
    {HOMER_VENV}/bin/python {HOMER_TOOLS}/skyvern_webhook.py --port 8765

Environment variables required:
    SKYVERN_API_KEY   — used to validate HMAC signature
    HOMER_WORKSPACE   — where to write result files

Expose via nginx:
    location /skyvern/webhook {
        proxy_pass http://127.0.0.1:8765/;
    }
Set SKYVERN_WEBHOOK_URL=https://<vps-host>/skyvern/webhook in secrets/.env
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SKYVERN_API_KEY = os.environ.get("SKYVERN_API_KEY", "")
REPO_ROOT = Path(__file__).parent.parent.resolve()
RESULTS_DIR = Path(os.environ.get("HOMER_WORKSPACE",
                   str(REPO_ROOT / "context" / ".nanobot_workspace"))) / "skyvern_results"


_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _validate_signature(body: bytes, signature: str) -> bool:
    if not SKYVERN_API_KEY:
        log.error("SKYVERN_API_KEY not set — rejecting webhook (fail closed)")
        return False
    expected = hmac.new(SKYVERN_API_KEY.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, {"error": "invalid Content-Length"})
            return
        if length > 5 * 1024 * 1024:
            self._respond(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)

        signature = self.headers.get("x-skyvern-signature", "")
        if not _validate_signature(body, signature):
            log.warning("Webhook rejected — bad or missing signature")
            self._respond(401, {"error": "invalid signature"})
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            log.error(f"Bad JSON in webhook body: {e}")
            self._respond(400, {"error": "bad JSON"})
            return

        run_id = payload.get("run_id", "")
        status = payload.get("status", "unknown")

        if not run_id or not _RUN_ID_RE.match(run_id):
            log.warning(f"Webhook rejected — invalid run_id: {run_id!r}")
            self._respond(400, {"error": "missing or invalid run_id"})
            return

        out = payload.get("output")

        result = {
            "status": status,
            "run_id": run_id,
            "output": out or "",
            "app_url": payload.get("app_url", ""),
        }
        if status in ("failed", "terminated"):
            result["failure_reason"] = payload.get("failure_reason", "task failed")

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_file = RESULTS_DIR / f"{run_id}.json"
        result_file.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        log.info(f"Wrote result for {run_id} (status={status}) → {result_file}")

        self._respond(200, {"ok": True})

    def _respond(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


def main():
    parser = argparse.ArgumentParser(description="Skyvern webhook receiver")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    server = HTTPServer((args.host, args.port), WebhookHandler)
    log.info(f"Skyvern webhook server listening on {args.host}:{args.port}")
    log.info(f"Results dir: {RESULTS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
