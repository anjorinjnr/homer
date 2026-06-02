"""run_code.py host-runner socket client (run_via_socket)."""

import json
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import run_code as rc  # noqa: E402


def _fake_runner(sock_path, captured, response):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def serve():
        try:
            conn, _ = srv.accept()
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                captured["req"] = data
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_sends_relative_path_and_parses_response(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    script = tmp_dir / "a.py"
    script.write_text("print(1)")
    monkeypatch.setattr(rc, "ALLOWED_CODE_DIR", tmp_dir)
    sock = tmp_path / "run.sock"
    monkeypatch.setattr(rc, "SANDBOX_SOCKET", sock)

    captured = {}
    t = _fake_runner(sock, captured, {"output": "1\n", "stderr": "", "exit_code": 0})
    result = rc.run_via_socket(script, "test intent")
    t.join(timeout=5)

    req = json.loads(captured["req"])
    assert req["script"] == "a.py"          # relative to tmp/, never absolute
    assert req["intent"] == "test intent"
    assert result == {"output": "1\n", "stderr": "", "exit_code": 0}
    assert not script.exists()              # consumed host-side, then unlinked


def test_nested_relative_path(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    (tmp_dir / "sub").mkdir(parents=True)
    script = tmp_dir / "sub" / "n.py"
    script.write_text("x")
    monkeypatch.setattr(rc, "ALLOWED_CODE_DIR", tmp_dir)
    sock = tmp_path / "run.sock"
    monkeypatch.setattr(rc, "SANDBOX_SOCKET", sock)
    captured = {}
    t = _fake_runner(sock, captured, {"output": "", "stderr": "", "exit_code": 0})
    rc.run_via_socket(script, "i")
    t.join(timeout=5)
    assert json.loads(captured["req"])["script"] == "sub/n.py"


def test_unreachable_socket_returns_error(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    script = tmp_dir / "a.py"
    script.write_text("x")
    monkeypatch.setattr(rc, "ALLOWED_CODE_DIR", tmp_dir)
    monkeypatch.setattr(rc, "SANDBOX_SOCKET", tmp_path / "nope.sock")

    result = rc.run_via_socket(script, "i")
    assert result["exit_code"] == -1
    assert "unreachable" in result["error"].lower()


def test_script_outside_tmp_rejected(tmp_path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(rc, "ALLOWED_CODE_DIR", tmp_dir)
    monkeypatch.setattr(rc, "SANDBOX_SOCKET", tmp_path / "run.sock")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x")
    result = rc.run_via_socket(outside, "i")
    assert result["exit_code"] == -1
    assert "must be inside" in result["error"]
