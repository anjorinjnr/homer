"""Shared helper for tools that shell out to gogcli (https://github.com/steipete/gogcli).

Path B pattern: Python owns OAuth (via google_auth), passes per-call access
token to gogcli via env. gogcli is stateless on the host — no keyring, no
refresh tokens stored. See project_gogcli_migration in memory for context.
"""
import json
import os
import subprocess

GOG_BIN = os.environ.get("GOG_BIN", "gog")
GOG_TIMEOUT_SEC = 30


def clean_stderr(stderr: str) -> str:
    """Strip gogcli's `Note: Using direct access token...` prefix lines.

    gogcli writes that line to stderr on every access-token-mode call.
    Without filtering it, every error message we surface to Homer's LLM
    gets prepended with the noise.
    """
    keep = [l for l in stderr.splitlines() if not l.startswith("Note:")]
    cleaned = "\n".join(keep).strip()
    return cleaned or stderr.strip()


def run(token: str, *args: str) -> dict:
    """Run gogcli with the given args. Returns parsed JSON stdout.

    Raises RuntimeError with a human-readable message on:
      - missing `gog` binary
      - subprocess timeout (default 30s)
      - non-zero exit (stderr filtered to drop the Note: prefix line)
      - empty / non-JSON stdout
    """
    env = {**os.environ, "GOG_ACCESS_TOKEN": token}
    try:
        proc = subprocess.run(
            [GOG_BIN, "--json", "--no-input", *args],
            env=env, capture_output=True, text=True, check=False,
            timeout=GOG_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        raise RuntimeError(f"gogcli binary '{GOG_BIN}' not found. Install: brew install gogcli")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gogcli timed out after {GOG_TIMEOUT_SEC}s: {' '.join(args[:2])}")
    if proc.returncode != 0:
        raise RuntimeError(f"gogcli failed (exit {proc.returncode}): {clean_stderr(proc.stderr)}")
    if not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gogcli returned non-JSON output: {e}: {proc.stdout[:200]}")


def download_bytes(token: str, *args: str) -> bytes:
    """Run gogcli streaming command (e.g. `drive download --out -`) and return
    stdout bytes. Nothing is written to disk.

    gogcli's drive download with `--out -` does `io.Copy(os.Stdout, resp.Body)`
    directly from the HTTP response (verified at internal/cmd/drive.go:981-984).
    `--json` is intentionally omitted — gogcli rejects `--json --out -`.
    """
    env = {**os.environ, "GOG_ACCESS_TOKEN": token}
    try:
        proc = subprocess.run(
            [GOG_BIN, "--no-input", *args],
            env=env, capture_output=True, check=False,
            timeout=GOG_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        raise RuntimeError(f"gogcli binary '{GOG_BIN}' not found. Install: brew install gogcli")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gogcli timed out after {GOG_TIMEOUT_SEC}s: {' '.join(args[:2])}")
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise RuntimeError(f"gogcli failed (exit {proc.returncode}): {clean_stderr(stderr_text)}")
    return proc.stdout
