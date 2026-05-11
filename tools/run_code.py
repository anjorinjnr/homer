#!/usr/bin/env python3
"""
run_code.py — Execute Python code in a secure Docker sandbox.

Usage:
    python tools/run_code.py --code-file {HOMER_WORKSPACE}/tmp/script.py --intent "..."

The code file must be inside {HOMER_WORKSPACE}/tmp/. Write the script there
with the write_file tool, then call this tool.

Returns JSON: {"output": "...", "stderr": "...", "exit_code": 0}
On failure:   {"error": "...", "stderr": "...", "exit_code": N}
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
ALLOWED_CODE_DIR = REPO_ROOT / "context" / ".nanobot_workspace" / "tmp"

DOCKER_IMAGE = "homer-sandbox:latest"
TIMEOUT_SECS = 30
MAX_OUTPUT_BYTES = 64 * 1024  # 64KB
CONTAINER_PREFIX = "homer-sandbox-"


def validate_code_file(path_str: str) -> Path:
    """Resolve the path and confirm it's inside the allowed tmp directory."""
    path = Path(path_str).resolve()
    allowed = ALLOWED_CODE_DIR.resolve()
    if not str(path).startswith(str(allowed) + os.sep):
        raise ValueError(
            f"Code file must be inside {allowed}/ — got: {path}"
        )
    if not path.exists():
        raise FileNotFoundError(f"Code file not found: {path}")
    return path



def _read_capped(stream, cap: int) -> bytes:
    """Read up to cap bytes, then drain the rest so the subprocess never blocks."""
    data = stream.read(cap)
    while stream.read(8192):
        pass
    return data


def _host_timezone() -> str:
    """Return the host's IANA timezone name, falling back to UTC."""
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        return tz_file.read_text().strip()
    # systemd symlink: /etc/localtime -> /usr/share/zoneinfo/America/New_York
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        parts = str(localtime.resolve()).split("zoneinfo/", 1)
        if len(parts) == 2:
            return parts[1]
    return "UTC"


def run_code(code_path: Path, intent: str) -> dict:
    print(f"run_code: {intent}", file=sys.stderr)

    # Ensure the sandbox user (non-root, different UID) can read the script.
    os.chmod(code_path, 0o644)

    run_id = uuid.uuid4().hex[:12]
    container_name = f"{CONTAINER_PREFIX}{run_id}"

    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--memory", "128m",
        "--cpus", "0.5",
        "--pids-limit", "50",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-v", f"{code_path}:/home/sandbox/script.py:ro",
        "-v", "/etc/localtime:/etc/localtime:ro",
        *(["-v", f"{ALLOWED_CODE_DIR}:/home/sandbox/data:ro"] if ALLOWED_CODE_DIR.exists() else []),
        "--env", f"TZ={_host_timezone()}",
        DOCKER_IMAGE,
        "/home/sandbox/script.py",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Read stdout and stderr concurrently, each capped at MAX_OUTPUT_BYTES.
        # Threads block on read() until the process closes the pipe (exits or is
        # killed). Data beyond the cap stays in the OS pipe buffer — it never
        # enters Python heap, preventing OOM from infinite print loops.
        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []
        t_out = threading.Thread(
            target=lambda: stdout_buf.append(_read_capped(proc.stdout, MAX_OUTPUT_BYTES))
        )
        t_err = threading.Thread(
            target=lambda: stderr_buf.append(_read_capped(proc.stderr, MAX_OUTPUT_BYTES))
        )
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            proc.kill()
            subprocess.run(["docker", "kill", container_name], capture_output=True)
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            return {
                "error": f"Execution timed out after {TIMEOUT_SECS} seconds",
                "stderr": (stderr_buf[0] if stderr_buf else b"").decode("utf-8", errors="replace"),
                "exit_code": -1,
            }

        t_out.join(timeout=5)
        t_err.join(timeout=5)

        stdout = (stdout_buf[0] if stdout_buf else b"").decode("utf-8", errors="replace")
        stderr = (stderr_buf[0] if stderr_buf else b"").decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return {
                "error": f"Script exited with code {proc.returncode}",
                "stderr": stderr,
                "exit_code": proc.returncode,
            }

        return {
            "output": stdout,
            "stderr": stderr,
            "exit_code": 0,
        }

    except FileNotFoundError:
        return {
            "error": "Docker is not available on this system",
            "stderr": "",
            "exit_code": -1,
        }

    finally:
        try:
            code_path.unlink()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Run Python code in a Docker sandbox")
    parser.add_argument("--code-file", required=True,
                        help=f"Path to script inside {ALLOWED_CODE_DIR}/")
    parser.add_argument("--intent", required=True,
                        help="Brief description of what this code does")
    args = parser.parse_args()

    try:
        code_path = validate_code_file(args.code_file)
    except (ValueError, FileNotFoundError) as e:
        print(json.dumps({"error": str(e), "exit_code": -1}))
        sys.exit(1)

    result = run_code(code_path, args.intent)
    print(json.dumps(result, indent=2))

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
