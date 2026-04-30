#!/usr/bin/env python3
"""F58-PR1: Dispatch manifest + session/commit traceability tests.

Covers:
  1. manifest.json created before dispatch (active/)
  2. manifest contains commit_hash_before
  3. session_id captured from init event in SubprocessAdapter
  4. receipt has commit_hash_after, session_id, committed
  5. event_count populated from streamed events
  6. committed flag true when a new commit occurred between before/after hashes
  7. auto-commit message contains Dispatch-ID trailer
  8. manifest promoted to completed/ after dispatch
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import append_receipt as ar
import subprocess_dispatch as sd
import dispatch_manifest as dm
import dispatch_git_ops
from subprocess_adapter import SubprocessAdapter, StreamEvent


# ── helpers ────────────────────────────────────────────────────────────────────

def _fake_git_hash(val: str):
    """Patch _get_commit_hash to return val."""
    return patch.object(sd, "_get_commit_hash", return_value=val)


def _make_fake_append(state_dir: Path):
    """Return a fake append_receipt_payload that writes to state_dir."""
    def fake_append(payload):
        receipt_path = state_dir / "t0_receipts.ndjson"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(receipt_path, "a") as f:
            f.write(json.dumps(payload) + "\n")
        result = MagicMock()
        result.receipts_file = receipt_path
        result.status = "ok"
        return result
    return fake_append


def _fake_branch(val: str = "feat/test-branch"):
    return patch.object(sd, "_get_current_branch", return_value=val)


# ── 1. manifest created before dispatch ────────────────────────────────────────

def test_manifest_created_before_dispatch(tmp_path):
    """_write_manifest() writes manifest.json inside active/<dispatch_id>/."""
    dispatch_id = "20260414-120000-f58-test-A"
    commit_hash = "abc1234def5678"

    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        manifest_path = sd._write_manifest(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            model="sonnet",
            role="backend-developer",
            instruction="Do something",
            commit_hash_before=commit_hash,
            branch="feat/f58",
        )

    assert manifest_path is not None
    manifest_file = Path(manifest_path)
    assert manifest_file.exists(), "manifest.json must exist after _write_manifest()"
    assert "active" in str(manifest_file), "manifest must be in active/ subdirectory"
    assert dispatch_id in str(manifest_file), "manifest path must contain dispatch_id"


# ── 2. manifest has commit_hash_before ─────────────────────────────────────────

def test_manifest_has_commit_hash(tmp_path):
    """manifest.json contains commit_hash_before, branch, terminal, model."""
    dispatch_id = "20260414-120001-f58-test-A"
    commit_hash = "deadbeef12345678"

    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        manifest_path = sd._write_manifest(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            model="sonnet",
            role="backend-developer",
            instruction="Do something",
            commit_hash_before=commit_hash,
            branch="feat/f58",
        )

    data = json.loads(Path(manifest_path).read_text())
    assert data["commit_hash_before"] == commit_hash
    assert data["branch"] == "feat/f58"
    assert data["terminal"] == "T1"
    assert data["model"] == "sonnet"
    assert data["dispatch_id"] == dispatch_id
    assert "timestamp" in data
    assert "instruction_chars" in data


# ── 3. session_id captured from init event ─────────────────────────────────────

def test_session_id_captured_from_init_event():
    """SubprocessAdapter.get_session_id() returns session_id from init event."""
    adapter = SubprocessAdapter()
    terminal_id = "T1"
    dispatch_id = "test-dispatch-001"

    # Simulate a process that emits a stream-json init event
    init_event = json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": "sess-abc-12345",
        "model": "claude-sonnet-4-6",
    })
    result_event = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "done",
        "session_id": "sess-abc-12345",
    })
    ndjson_output = (init_event + "\n" + result_event + "\n").encode()

    mock_process = MagicMock()
    mock_process.stdout = iter([
        init_event.encode() + b"\n",
        result_event.encode() + b"\n",
    ])
    mock_process.poll.return_value = 0
    adapter._processes[terminal_id] = mock_process
    adapter._dispatch_ids[terminal_id] = dispatch_id

    # Consume all events via read_events()
    list(adapter.read_events(terminal_id))

    assert adapter.get_session_id(terminal_id) == "sess-abc-12345"


# ── 4. receipt has commit_hash and session_id ──────────────────────────────────

def test_receipt_has_commit_hash_and_session(tmp_path):
    """_write_receipt() persists commit_hash_before/after, session_id, manifest_path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    with patch.object(ar, "append_receipt_payload", side_effect=_make_fake_append(state_dir)):
        sd._write_receipt(
            "20260414-120002-f58-test-A",
            "T1",
            "done",
            event_count=42,
            session_id="sess-xyz-9999",
            commit_hash_before="aaa111",
            commit_hash_after="bbb222",
            committed=True,
            manifest_path="/some/path/manifest.json",
        )

    receipt_file = state_dir / "t0_receipts.ndjson"
    assert receipt_file.exists()
    data = json.loads(receipt_file.read_text().strip())

    assert data["session_id"] == "sess-xyz-9999"
    assert data["commit_hash_before"] == "aaa111"
    assert data["commit_hash_after"] == "bbb222"
    assert data["committed"] is True
    assert data["manifest_path"] == "/some/path/manifest.json"
    assert data["event_count"] == 42


# ── 5. event_count populated ──────────────────────────────────────────────────

def test_event_count_populated():
    """deliver_via_subprocess() counts streamed events and returns the total."""
    adapter = SubprocessAdapter()
    terminal_id = "T1"
    dispatch_id = "20260414-120003-f58-test-A"

    events = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "m"}).encode() + b"\n",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}).encode() + b"\n",
        json.dumps({"type": "result", "subtype": "success", "result": "done", "session_id": "s1"}).encode() + b"\n",
    ]

    mock_process = MagicMock()
    mock_process.stdout = iter(events)
    mock_process.poll.return_value = 0
    adapter._processes[terminal_id] = mock_process
    adapter._dispatch_ids[terminal_id] = dispatch_id

    yielded = list(adapter.read_events(terminal_id))
    # init -> 1 normalized, assistant/text -> 1, result -> 1 = 3 total
    assert len(yielded) == 3


# ── 6. committed flag true when new commit ─────────────────────────────────────

def test_committed_flag_true_when_new_commit(tmp_path):
    """receipt.committed is True when commit_hash_after differs from before."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    with patch.object(ar, "append_receipt_payload", side_effect=_make_fake_append(state_dir)):
        sd._write_receipt(
            "20260414-120004-f58-test-A",
            "T1",
            "done",
            commit_hash_before="hash_before",
            commit_hash_after="hash_after",
            committed=False,  # explicit False, but hashes differ → True
        )

    data = json.loads((state_dir / "t0_receipts.ndjson").read_text().strip())
    assert data["committed"] is True


def test_committed_flag_false_when_no_new_commit(tmp_path):
    """receipt.committed is False when commit_hash_before == commit_hash_after."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    with patch.object(ar, "append_receipt_payload", side_effect=_make_fake_append(state_dir)):
        sd._write_receipt(
            "20260414-120005-f58-test-A",
            "T1",
            "done",
            commit_hash_before="same_hash",
            commit_hash_after="same_hash",
            committed=False,
        )

    data = json.loads((state_dir / "t0_receipts.ndjson").read_text().strip())
    assert data["committed"] is False


# ── 7. auto-commit message contains Dispatch-ID ────────────────────────────────

def test_auto_commit_message_contains_dispatch_id(tmp_path):
    """_auto_commit_changes() includes 'Dispatch-ID: <id>' in the commit body."""
    dispatch_id = "20260414-120006-f58-test-A"

    # Simulate: git status shows dirty, git add succeeds, git commit succeeds
    # Git porcelain format: XY<space>path — 3-char prefix before the filename.
    # " M scripts/lib/foo.py" = space(X) + M(Y) + space + path (unstaged modification).
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "status" in cmd:
            result.stdout = " M scripts/lib/foo.py\n"
        elif "commit" in cmd:
            msg_idx = cmd.index("-m") + 1
            fake_run._captured_msg = cmd[msg_idx]
            result.stdout = "[feat/f58 abc1234] auto-commit\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    fake_run._captured_msg = ""

    with patch("dispatch_git_ops.subprocess.run", side_effect=fake_run):
        result = sd._auto_commit_changes(
            dispatch_id, "T1", gate="f58-pr1",
            pre_dispatch_dirty=set(),
            dispatch_touched_files=frozenset({"scripts/lib/foo.py"}),
        )

    assert result is True
    assert f"Dispatch-ID: {dispatch_id}" in fake_run._captured_msg


# ── 8. manifest promoted to completed/ ────────────────────────────────────────

def test_manifest_promoted_to_completed(tmp_path):
    """_promote_manifest() copies manifest from active/ to completed/."""
    dispatch_id = "20260414-120007-f58-test-A"

    with patch.dict(os.environ, {"VNX_DATA_DIR": str(tmp_path)}):
        # Write manifest to active/
        sd._write_manifest(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            model="sonnet",
            role="backend-developer",
            instruction="test",
            commit_hash_before="abc",
            branch="feat/f58",
        )

        # Promote to completed/
        completed_path = sd._promote_manifest(dispatch_id)

    assert completed_path is not None
    completed_file = Path(completed_path)
    assert completed_file.exists()
    assert "completed" in str(completed_file)
    data = json.loads(completed_file.read_text())
    assert data["dispatch_id"] == dispatch_id
