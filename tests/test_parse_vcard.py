"""Tests for tools/parse_vcard.py"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.parse_vcard import parse_vcard


# --- Pure parsing tests ---

def test_parse_basic_vcard():
    content = "BEGIN:VCARD\nVERSION:3.0\nFN:Jake\nTEL;type=CELL:+15551234567\nEND:VCARD"
    result = parse_vcard(content)
    assert result["name"] == "Jake"
    assert result["phone"] == "+15551234567"


def test_parse_vcard_with_charset():
    content = "BEGIN:VCARD\nVERSION:3.0\nFN;CHARSET=UTF-8:Alex\nTEL;type=CELL:+1234567890\nEND:VCARD"
    result = parse_vcard(content)
    assert result["name"] == "Alex"
    assert result["phone"] == "+1234567890"


def test_parse_vcard_with_tel_uri():
    content = "BEGIN:VCARD\nVERSION:3.0\nFN:Sam\nTEL;VALUE=uri:tel:+447700900000\nEND:VCARD"
    result = parse_vcard(content)
    assert result["name"] == "Sam"
    assert result["phone"] == "+447700900000"


def test_parse_vcard_no_fn_returns_unknown():
    content = "BEGIN:VCARD\nVERSION:3.0\nTEL:+15559876543\nEND:VCARD"
    result = parse_vcard(content)
    assert result["name"] == "Unknown"
    assert result["phone"] == "+15559876543"


def test_parse_vcard_no_phone_returns_error():
    content = "BEGIN:VCARD\nVERSION:3.0\nFN:No Number\nEND:VCARD"
    result = parse_vcard(content)
    assert "error" in result


def test_parse_malformed_vcard_returns_error():
    result = parse_vcard("NOT A VCARD AT ALL")
    assert "error" in result


def test_parse_vcard_phone_with_formatting():
    content = "BEGIN:VCARD\nVERSION:3.0\nFN:Bob\nTEL:(555) 123-4567\nEND:VCARD"
    result = parse_vcard(content)
    assert result["name"] == "Bob"
    assert result["phone"] == "5551234567"


def test_parse_multiple_tel_uses_first():
    content = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Multi\n"
        "TEL;type=CELL:+15550001111\n"
        "TEL;type=WORK:+15550002222\n"
        "END:VCARD"
    )
    result = parse_vcard(content)
    assert result["phone"] == "+15550001111"


# --- CLI integration tests ---

def _run_cli(*args):
    tool = os.path.join(os.path.dirname(__file__), "..", "tools", "parse_vcard.py")
    return subprocess.run(
        [sys.executable, tool] + list(args),
        capture_output=True,
        text=True,
    )


def test_cli_vcard_flag():
    vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:CLI User\nTEL:+15550000000\nEND:VCARD"
    result = _run_cli("--vcard", vcard)
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["name"] == "CLI User"
    assert data["phone"] == "+15550000000"


def test_cli_file_flag():
    vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:File User\nTEL:+15551111111\nEND:VCARD"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vcf", delete=False) as f:
        f.write(vcard)
        tmp_path = f.name
    try:
        result = _run_cli("--file", tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["name"] == "File User"
        assert data["phone"] == "+15551111111"
    finally:
        os.unlink(tmp_path)


def test_cli_error_no_phone_exits_1():
    vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:No Phone\nEND:VCARD"
    result = _run_cli("--vcard", vcard)
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert "error" in data


def test_cli_missing_file_exits_1():
    result = _run_cli("--file", "/tmp/nonexistent_contact_xyz_abc.vcf")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert "error" in data


def test_cli_vcard_and_file_mutually_exclusive():
    result = _run_cli("--vcard", "x", "--file", "/tmp/foo.vcf")
    assert result.returncode != 0


def test_cli_requires_one_arg():
    result = _run_cli()
    assert result.returncode != 0
