"""run_code.py host-runner streaming client (run_via_socket)."""

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
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line, _, rest = data.partition(b"\n")
                header = json.loads(line)
                need = header["script_size"] + sum(sz for _, sz in header["files"])
                body = rest
                while len(body) < need:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                captured["header"] = header
                captured["script"] = body[: header["script_size"]]
                off = header["script_size"]
                files = {}
                for name, sz in header["files"]:
                    files[name] = body[off:off + sz]
                    off += sz
                captured["files"] = files
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(rc, "ALLOWED_CODE_DIR", tmp)
    sock = tmp_path / "run.sock"
    monkeypatch.setattr(rc, "SANDBOX_SOCKET", sock)
    return tmp, sock


def test_streams_script_and_data_and_parses_response(workspace):
    tmp, sock = workspace
    script = tmp / "go.py"
    script.write_text("print('hi')")
    (tmp / "a.csv").write_text("x,y\n1,2\n")
    (tmp / "sub").mkdir()
    (tmp / "sub" / "b.txt").write_text("nested")

    captured = {}
    t = _fake_runner(sock, captured, {"output": "ok\n", "stderr": "", "exit_code": 0})
    result = rc.run_via_socket(script, "analyze the csv")
    t.join(timeout=5)

    assert result == {"output": "ok\n", "stderr": "", "exit_code": 0}
    assert captured["header"]["intent"] == "analyze the csv"
    assert captured["script"] == b"print('hi')"
    # data files streamed (script itself excluded), with relative names
    assert captured["files"]["a.csv"] == b"x,y\n1,2\n"
    assert captured["files"]["sub/b.txt"] == b"nested"
    assert "go.py" not in captured["files"]  # the script is not duplicated as data
    assert not script.exists()  # cleaned up


def test_symlinked_data_file_is_skipped(workspace, tmp_path):
    tmp, sock = workspace
    script = tmp / "go.py"
    script.write_text("x")
    secret = tmp_path / "secret"
    secret.write_text("SECRET")
    (tmp / "link").symlink_to(secret)  # symlink in our tmp/ — skipped, not streamed
    captured = {}
    t = _fake_runner(sock, captured, {"output": "", "stderr": "", "exit_code": 0})
    rc.run_via_socket(script, "i")
    t.join(timeout=5)
    assert "link" not in captured["files"]


def test_unreachable_socket_returns_error(workspace):
    tmp, sock = workspace
    script = tmp / "go.py"
    script.write_text("x")
    # no server bound
    result = rc.run_via_socket(script, "i")
    assert result["exit_code"] == -1
    assert "unreachable" in result["error"].lower()


def test_oversized_script_rejected(workspace, monkeypatch):
    tmp, sock = workspace
    monkeypatch.setattr(rc, "_MAX_SCRIPT_BYTES", 4)
    script = tmp / "go.py"
    script.write_text("way too long")
    result = rc.run_via_socket(script, "i")
    assert result["exit_code"] == -1
    assert "too large" in result["error"]
