#!/usr/bin/env python3
"""
skyvern_task.py — Submit and check Skyvern browser automation tasks.

Homer uses this for web tasks that have no API: buying tickets, filling
forms, checking sites that require a browser. Tasks run asynchronously —
Skyvern calls back to skyvern_webhook.py when done, which writes the result
to {HOMER_WORKSPACE}/skyvern_results/<run_id>.json.

Usage:
    # Submit a task (returns immediately with run_id):
    python tools/skyvern_task.py --prompt "Buy 2 adult tickets at Zoo Atlanta for 2026-04-05" \\
        --url "https://www.zooatlanta.org" \\
        --data-file {HOMER_WORKSPACE}/tmp/skyvern_data.json

    # Check the result of a previously submitted task:
    python tools/skyvern_task.py --check tsk_v2_abc123

Output (submit):
    {"status": "submitted", "run_id": "tsk_v2_...", "app_url": "https://app.skyvern.com/runs/..."}

Output (check — completed):
    {"status": "completed", "run_id": "...", "output": {...}, "app_url": "..."}

Output (check — still running):
    {"status": "running", "run_id": "..."}

Output (check — failed):
    {"status": "failed", "run_id": "...", "failure_reason": "..."}
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

SKYVERN_API_KEY = os.environ.get("SKYVERN_API_KEY", "")
SKYVERN_WEBHOOK_URL = os.environ.get("SKYVERN_WEBHOOK_URL", "")

REPO_ROOT = Path(__file__).parent.parent.resolve()
RESULTS_DIR = Path(os.environ.get("HOMER_WORKSPACE",
                   str(REPO_ROOT / "context" / ".nanobot_workspace"))) / "state" / "skyvern_results"


def _client():
    try:
        from skyvern import Skyvern
    except ImportError:
        print(json.dumps({"error": "skyvern not installed. Run: pip install skyvern"}))
        sys.exit(1)
    if not SKYVERN_API_KEY:
        print(json.dumps({"error": "SKYVERN_API_KEY not set in environment"}))
        sys.exit(1)
    return Skyvern(api_key=SKYVERN_API_KEY)


def submit_task(prompt: str, url: str | None = None, data_file: str | None = None) -> dict:
    """Submit a task to Skyvern and return immediately with the run_id."""
    import asyncio

    client = _client()
    data = None
    if data_file:
        data_path = Path(data_file).resolve()
        tmp_dir = (Path(os.environ.get("HOMER_WORKSPACE",
                        str(REPO_ROOT / "context" / ".nanobot_workspace"))) / "tmp").resolve()
        if not str(data_path).startswith(str(tmp_dir) + os.sep) and data_path != tmp_dir:
            print(json.dumps({"error": "--data-file must be inside the workspace tmp/ directory"}))
            sys.exit(1)
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except OSError as e:
            print(json.dumps({"error": f"--data-file could not be read: {e}"}))
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"--data-file is not valid JSON: {e}"}))
            sys.exit(1)

    kwargs: dict = {
        "prompt": prompt,
        "wait_for_completion": False,
    }
    if url:
        kwargs["url"] = url
    if data:
        kwargs["data"] = data
    if SKYVERN_WEBHOOK_URL:
        kwargs["webhook_url"] = SKYVERN_WEBHOOK_URL

    async def _run():
        return await client.run_task(**kwargs)

    try:
        result = asyncio.run(_run())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    return {
        "status": "submitted",
        "run_id": result.run_id or "",
        "app_url": result.app_url or "",
        "message": "Task submitted. Homer will be notified via webhook when complete.",
    }


def check_task(run_id: str) -> dict:
    """Check the result of a previously submitted task.

    Checks the local webhook result file first; falls back to the Skyvern API.
    """
    if not _RUN_ID_RE.match(run_id):
        print(json.dumps({"error": f"Invalid run_id: {run_id!r}"}))
        sys.exit(1)

    result_file = RESULTS_DIR / f"{run_id}.json"
    if result_file.exists():
        try:
            return json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Fall back to API
    import asyncio

    client = _client()

    async def _get():
        return await client.get_run(run_id)

    try:
        result = asyncio.run(_get())
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    status = str(result.status) if result.status else "unknown"
    out = result.output
    if hasattr(out, "model_dump"):
        out = out.model_dump()

    response: dict = {
        "status": status,
        "run_id": run_id,
        "output": out or "",
        "app_url": result.app_url or "",
    }
    if status in ("failed", "terminated"):
        response["failure_reason"] = result.failure_reason or "task failed"
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit or check a Skyvern browser task.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt", help="What to do in the browser (submits a new task)")
    group.add_argument("--check", metavar="RUN_ID", help="Check the result of a submitted task")

    parser.add_argument("--url", help="Starting URL (used with --prompt)")
    parser.add_argument("--data-file", dest="data_file",
                        help="Path to a JSON file with form data (used with --prompt). "
                             "Write the file with write_file first: {HOMER_WORKSPACE}/tmp/<name>.json")
    args = parser.parse_args()

    if args.check:
        result = check_task(args.check)
    else:
        result = submit_task(args.prompt, url=args.url, data_file=args.data_file)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
