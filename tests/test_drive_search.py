"""
Tests for drive_search.py — gogcli wrapper for Drive search.

Mocks gogcli.run — no real binary or token required.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import drive_search as ds
import gogcli


def test_search_returns_matching_files(monkeypatch):
    payload = {"files": [
        {"id": "abc", "name": "Car Insurance 2025.pdf",
         "mimeType": "application/pdf", "modifiedTime": "2025-01-01"},
    ]}
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: payload)
    
    results = ds.search("tok", "car insurance", 10)
    assert len(results) == 1
    assert results[0]["id"] == "abc"
    assert results[0]["type"] == "pdf"


def test_search_empty_results(monkeypatch):
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {"files": []})
    results = ds.search("tok", "nonexistent", 10)
    assert results == []


def test_search_limit_respected(monkeypatch):
    files = [
        {"id": f"id{i}", "name": f"file{i}.pdf",
         "mimeType": "application/pdf", "modifiedTime": ""}
        for i in range(10)
    ]
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: {"files": files})
    results = ds.search("tok", "file", 3)
    assert len(results) == 3


def test_search_includes_path_from_index_when_available(monkeypatch):
    payload = {"files": [
        {"id": "known-id", "name": "doc.pdf",
         "mimeType": "application/pdf", "modifiedTime": ""},
    ]}
    monkeypatch.setattr(gogcli, "run", lambda *a, **kw: payload)
    monkeypatch.setattr(ds, "load_index", lambda: {"known-id": "family_docs/docs/doc.pdf"})
    
    results = ds.search("tok", "doc", 10)
    assert results[0]["path"] == "family_docs/docs/doc.pdf"


def test_search_query_escapes_single_quotes(monkeypatch):
    captured = []
    monkeypatch.setattr(gogcli, "run", lambda token, *args: captured.append(args) or {"files": []})
    
    ds.search("tok", "Alex's doc", 10)
    # find --query in args
    query_arg = ""
    for i, arg in enumerate(captured[0]):
        if arg == "--query":
            query_arg = captured[0][i+1]
            break
            
    assert "\\'" in query_arg
    assert "Alex's" not in query_arg
