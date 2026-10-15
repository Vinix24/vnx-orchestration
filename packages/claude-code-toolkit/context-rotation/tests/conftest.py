"""Shared fixtures: package paths, an isolated HOME and transcript builders."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a0"
sys.path.insert(0, str(PKG / "lib"))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read the real ~/.claude or inherit plugin switches."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONTEXT_ROTATION", raising=False)
    return home


def load_fixture(*parts):
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


def assistant_entry(total, model="claude-sonnet-5-5", sidechain=False):
    entry = load_fixture("transcript", "assistant_entry.json")
    usage = entry["message"]["usage"]
    usage["input_tokens"] = total
    usage["cache_read_input_tokens"] = 0
    usage["cache_creation_input_tokens"] = 0
    entry["message"]["model"] = model
    entry["isSidechain"] = sidechain
    return entry


def write_transcript(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def payload_for(project, transcript, session_id="sess-1", **extra):
    payload = load_fixture("payloads", "02-UserPromptSubmit.json")["payload"]
    payload.update(
        session_id=session_id, transcript_path=str(transcript), cwd=str(project)
    )
    payload.update(extra)
    return payload
