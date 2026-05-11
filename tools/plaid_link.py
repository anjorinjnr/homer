#!/usr/bin/env python3
"""
plaid_link.py — One-time Plaid Link setup to connect a bank/institution to Homer.

Opens a local browser flow to authenticate, then exchanges the public_token for
an access_token and saves it to secrets/.env.

Usage:
    python tools/plaid_link.py --institution ally    # default
    python tools/plaid_link.py --institution chase

Supported institutions and the env var each saves to:
    ally   → PLAID_ACCESS_TOKEN_ALLY
    chase  → PLAID_ACCESS_TOKEN_CHASE

Requires in secrets/.env:
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV (sandbox|production)
"""

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from plaid_utils import get_plaid_client

REPO_ROOT = Path(__file__).parent.parent.resolve()
ENV_FILE = REPO_ROOT / "secrets" / ".env"

PORT = 8765
CALLBACK_PATH = "/callback"

INSTITUTIONS = {
    "ally":  {"label": "Ally",  "env_key": "PLAID_ACCESS_TOKEN_ALLY"},
    "chase": {"label": "Chase", "env_key": "PLAID_ACCESS_TOKEN_CHASE"},
}

LINK_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Homer — Connect {label} via Plaid</title>
  <style>
    body {{ font-family: sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; }}
    button {{ padding: 12px 24px; font-size: 16px; cursor: pointer; background: #0070f3; color: white;
              border: none; border-radius: 6px; }}
    button:disabled {{ background: #999; cursor: default; }}
    #status {{ margin-top: 20px; color: #444; }}
  </style>
</head>
<body>
  <h2>Connect {label} to Homer</h2>
  <p>Click below to authenticate with your {label} account via Plaid.</p>
  <button id="link-button">Connect {label}</button>
  <p id="status"></p>

  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    const handler = Plaid.create({{
      token: '{link_token}',
      onSuccess: function(public_token, metadata) {{
        document.getElementById('status').textContent = 'Authenticating... please wait.';
        document.getElementById('link-button').disabled = true;
        fetch('{callback_url}', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{public_token: public_token, metadata: metadata}})
        }})
        .then(r => r.json())
        .then(data => {{
          document.getElementById('status').textContent = data.message;
        }})
        .catch(err => {{
          document.getElementById('status').textContent = 'Error: ' + err;
        }});
      }},
      onExit: function(err, metadata) {{
        if (err) {{
          document.getElementById('status').textContent = 'Exited: ' + (err.error_message || JSON.stringify(err));
        }}
      }}
    }});
    document.getElementById('link-button').addEventListener('click', function() {{
      handler.open();
    }});
  </script>
</body>
</html>
"""


def load_env() -> dict:
    if not ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE} not found. Copy from secrets/.env.template and fill in values.")
        sys.exit(1)
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def create_link_token(client) -> str:
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode

    request = LinkTokenCreateRequest(
        products=[Products("transactions")],
        client_name="Homer Household Agent",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(
            client_user_id=os.environ.get("HOMER_HOUSEHOLD_ID", "homer-default"),
        ),
    )
    response = client.link_token_create(request)
    return response["link_token"]


def exchange_public_token(client, public_token: str) -> str:
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response["access_token"]


def save_access_token(access_token: str, env_key: str) -> None:
    content = ENV_FILE.read_text()
    lines = [l for l in content.splitlines() if not l.startswith(f"{env_key}=")]
    lines.append(f"{env_key}={access_token}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)
    print(f"✓ {env_key} saved to {ENV_FILE}")


def run_link_flow(institution: str) -> None:
    info = INSTITUTIONS[institution]
    label = info["label"]
    env_key = info["env_key"]

    env = load_env()
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET"):
        if not env.get(var):
            print(f"ERROR: {var} not set in secrets/.env")
            sys.exit(1)

    print(f"Creating Plaid link token for {label}...")
    try:
        client = get_plaid_client(env)
    except ImportError:
        print("ERROR: Run: pip install plaid-python")
        sys.exit(1)
    link_token = create_link_token(client)
    print(f"  Link token: {link_token[:20]}...")

    callback_url = f"http://localhost:{PORT}{CALLBACK_PATH}"
    result = {"public_token": None, "done": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def do_GET(self):
            if self.path == "/":
                html = LINK_HTML.format(label=label, link_token=link_token, callback_url=callback_url)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(html.encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == CALLBACK_PATH:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                result["public_token"] = body.get("public_token")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"message": "✓ Token received. You can close this tab."}).encode())

                result["done"].set()
            else:
                self.send_response(404)
                self.end_headers()

    server = HTTPServer(("localhost", PORT), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    url = f"http://localhost:{PORT}/"
    print(f"\nOpening Plaid Link at {url}")
    print(f"Sign in with your {label} credentials in the browser window.")
    print("Waiting for authentication...\n")
    webbrowser.open(url)

    result["done"].wait()
    server.shutdown()

    public_token = result["public_token"]
    if not public_token:
        print("ERROR: No public token received.")
        sys.exit(1)

    print("Exchanging public token for access token...")
    access_token = exchange_public_token(client, public_token)
    save_access_token(access_token, env_key)
    print(f"\n✓ {label} connected. Run: python tools/plaid_fetch.py --institution {institution} --balances")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Connect a bank to Homer via Plaid.")
    parser.add_argument(
        "--institution",
        choices=list(INSTITUTIONS.keys()),
        default="ally",
        help="Institution to connect (default: ally)",
    )
    args = parser.parse_args()
    run_link_flow(args.institution)
