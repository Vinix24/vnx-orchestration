#!/usr/bin/env python3
"""Tests for canonical receipt append helper and runtime writer integration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"


def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    return env


def _run_append(
    tmp_path: Path,
    payload: str,
    extra_args: Optional[List[str]] = None,
    extra_env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(APPEND_SCRIPT)]
    if extra_args:
        args.extend(extra_args)

    env = _build_env(tmp_path)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        args,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _build_receipt(index: int = 1) -> dict:
    return {
        "timestamp": f"2026-02-11T10:00:{index:02d}Z",
        "event_type": "task_complete",
        "event": "task_complete",
        "dispatch_id": f"DISP-{index:03d}",
        "task_id": f"TASK-{index:03d}",
        "terminal": "T1",
        "status": "success",
        "source": "pytest",
    }


def test_append_receipt_rejects_malformed_json(tmp_path: Path):
    result = _run_append(tmp_path, '{"timestamp":')

    assert result.returncode == 10
    assert '"code":"invalid_json"' in result.stderr


def test_append_receipt_persists_valid_receipt_once(tmp_path: Path):
    receipt = _build_receipt(index=7)

    result = _run_append(tmp_path, json.dumps(receipt))
    assert result.returncode == 0

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = receipts_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    stored = json.loads(lines[0])
    assert stored["dispatch_id"] == receipt["dispatch_id"]
    assert stored["event_type"] == "task_complete"
    assert "provenance" in stored
    assert "session" in stored
    assert stored["session"]["terminal"] == "T1"
    assert "git_ref" in stored["provenance"]


def test_append_receipt_session_id_prefers_receipt_metadata_over_env(tmp_path: Path):
    receipt = _build_receipt(index=21)
    receipt["metadata"] = {"session_id": "report-session-123"}

    result = _run_append(
        tmp_path,
        json.dumps(receipt),
        extra_env={"CLAUDE_SESSION_ID": "env-session-999"},
    )
    assert result.returncode == 0

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    stored = json.loads(receipts_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["session"]["session_id"] == "report-session-123"


def test_append_receipt_session_id_falls_back_to_gemini_current_file(tmp_path: Path, monkeypatch):
    receipt = _build_receipt(index=22)

    home_dir = tmp_path / "home"
    gemini_current = home_dir / ".gemini" / "sessions" / "current"
    gemini_current.parent.mkdir(parents=True, exist_ok=True)
    gemini_current.write_text("gemini-session-abc\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home_dir))

    result = _run_append(tmp_path, json.dumps(receipt))
    assert result.returncode == 0

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    stored = json.loads(receipts_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["session"]["session_id"] == "gemini-session-abc"


def test_session_id_uses_panes_json_provider_for_standard_terminal(tmp_path: Path):
    """T3 mapped to codex_cli in panes.json must resolve CODEX_SESSION_ID, not CLAUDE_SESSION_ID.

    Regression guard for Codex finding: env_mapping hard-coded all standard terminals
    (T0-T3) to CLAUDE_SESSION_ID, ignoring the provider in panes.json.
    """
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    panes_json = state_dir / "panes.json"
    panes_json.write_text(
        json.dumps({"T3": {"model": "gpt-5.2-codex", "provider": "codex_cli"}}),
        encoding="utf-8",
    )

    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-PANES-CODEX-001",
        "terminal": "T3",
    }

    result = _run_append(
        tmp_path,
        json.dumps(receipt),
        extra_env={
            "CODEX_SESSION_ID": "codex-panes-session-xyz",
            "CLAUDE_SESSION_ID": "claude-should-not-be-used",
        },
    )
    assert result.returncode == 0, f"append failed: {result.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    stored = json.loads(receipts_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["session"]["session_id"] == "codex-panes-session-xyz", (
        f"Expected codex session ID from panes.json provider, got {stored['session']['session_id']!r}. "
        "Terminal T3 with codex_cli provider must read CODEX_SESSION_ID, not CLAUDE_SESSION_ID."
    )


def test_session_id_priority4_prefers_provider_session_file(tmp_path: Path, monkeypatch):
    """Priority 4 must try the resolved-provider's session file before other providers.

    T3 → codex_cli via panes.json: ~/.codex/sessions/current is checked before
    ~/.gemini/sessions/current and ~/.claude/sessions/current.
    Regression guard: old code had codex first globally, but that was a coincidence;
    new code must be deterministic based on resolved provider.
    """
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    panes_json = state_dir / "panes.json"
    panes_json.write_text(
        json.dumps({"T3": {"model": "gpt-5.2-codex", "provider": "codex_cli"}}),
        encoding="utf-8",
    )

    home_dir = tmp_path / "home"
    codex_current = home_dir / ".codex" / "sessions" / "current"
    codex_current.parent.mkdir(parents=True, exist_ok=True)
    codex_current.write_text("codex-file-session-abc\n", encoding="utf-8")
    # Also write a Claude session file to verify it is NOT chosen
    claude_current = home_dir / ".claude" / "sessions" / "current"
    claude_current.parent.mkdir(parents=True, exist_ok=True)
    claude_current.write_text("claude-file-session-wrong\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home_dir))

    receipt = {
        "timestamp": "2026-04-28T12:00:01Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-PANES-CODEX-002",
        "terminal": "T3",
    }

    result = _run_append(tmp_path, json.dumps(receipt))
    assert result.returncode == 0, f"append failed: {result.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    stored = json.loads(receipts_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert stored["session"]["session_id"] == "codex-file-session-abc", (
        f"Expected codex file session for T3 with codex_cli provider, got {stored['session']['session_id']!r}."
    )


def test_append_receipt_skips_duplicate_idempotency_key(tmp_path: Path):
    receipt = _build_receipt(index=9)
    payload = json.dumps(receipt)

    first = _run_append(tmp_path, payload)
    second = _run_append(tmp_path, payload)

    assert first.returncode == 0
    assert second.returncode == 0
    assert '"code":"duplicate_receipt_skipped"' in second.stderr

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = receipts_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_append_receipt_concurrent_writers_no_corruption(tmp_path: Path):
    payloads = [json.dumps(_build_receipt(index=i)) for i in range(1, 41)]

    def run_one(payload: str) -> subprocess.CompletedProcess:
        return _run_append(tmp_path, payload)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run_one, payloads))

    assert all(result.returncode == 0 for result in results)

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = [line for line in receipts_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(payloads)

    parsed = [json.loads(line) for line in lines]
    dispatch_ids = {entry["dispatch_id"] for entry in parsed}
    assert len(dispatch_ids) == len(payloads)


def test_runtime_writers_use_append_receipt_helper_and_no_direct_append():
    script_expectations = {
        "receipt_notifier.sh": {
            "must_contain": ["append_receipt.py"],
            "must_not_contain": [">> \"$RECEIPTS_FILE\""],
        },
        "report_watcher.sh": {
            "must_contain": ["append_receipt.py"],
            "must_not_contain": [">> \"$RECEIPTS_FILE\""],
        },
        "heartbeat_ack_monitor.py": {
            "must_contain": ["append_receipt_payload"],
            "must_not_contain": ["with open(self.receipts_file, 'a')"],
        },
    }

    for script_name, expectation in script_expectations.items():
        content = (SCRIPTS_DIR / script_name).read_text(encoding="utf-8")
        for required in expectation["must_contain"]:
            assert required in content, f"{script_name} missing helper reference: {required}"
        for forbidden in expectation["must_not_contain"]:
            assert forbidden not in content, f"{script_name} still has direct append: {forbidden}"


def test_active_runtime_receipt_writers_have_no_direct_t0_append():
    scripts_dir = SCRIPTS_DIR
    allowlist = (
        "archived_",
        "archive/",
        "lib/",
        "state/",
        "__pycache__/",
    )

    direct_append_pattern = re.compile(r">>\s+.*t0_receipts\.ndjson")
    offenders: List[str] = []

    for path in scripts_dir.rglob("*"):
        if path.is_dir() or path.suffix not in {".sh", ".py"}:
            continue

        rel = path.relative_to(scripts_dir).as_posix()
        if any(token in rel for token in allowlist):
            continue

        content = path.read_text(encoding="utf-8", errors="ignore")
        if direct_append_pattern.search(content):
            offenders.append(rel)

    assert offenders == []
