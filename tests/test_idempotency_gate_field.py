#!/usr/bin/env python3
"""Tests that gate and pr_number are included in the idempotency key.

Before the fix: IDEMPOTENCY_FIELDS omitted 'gate' and 'pr_number'. Gate request
receipts for the same dispatch_id/terminal/event_type/source shared an identical
key, so only the first (of codex + gemini + claude_github) persisted within the
5-minute cache window.

After the fix: 'gate' and 'pr_number' are in IDEMPOTENCY_FIELDS. Each gate
request produces a distinct key and all three persist.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path

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


def _run_append(tmp_path: Path, payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(APPEND_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )


def test_idempotency_distinct_gates(tmp_path: Path):
    """3 review_gate_request receipts with same dispatch_id but different gate → all 3 persist."""
    base = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "review_gate_request",
        "event": "review_gate_request",
        "dispatch_id": "DISP-GATE-IDEM-001",
        "terminal": "T1",
        "source": "governance",
        "pr_number": "278",
    }

    gates = ["codex", "gemini", "claude_github"]
    for gate in gates:
        r = _run_append(tmp_path, json.dumps({**base, "gate": gate}))
        assert r.returncode == 0, f"gate={gate} append failed:\n{r.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    assert receipts_file.exists(), "receipts file not created"
    lines = [ln for ln in receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3, (
        f"Expected 3 distinct gate receipts but got {len(lines)}. "
        "If only 1 line, 'gate' is still not in IDEMPOTENCY_FIELDS."
    )
    stored_gates = {json.loads(ln).get("gate") for ln in lines}
    assert stored_gates == {"codex", "gemini", "claude_github"}


def test_idempotency_same_gate_deduped(tmp_path: Path):
    """Same dispatch_id + same gate → duplicate is dropped (idempotency still works)."""
    receipt = {
        "timestamp": "2026-04-28T10:01:00Z",
        "event_type": "review_gate_request",
        "event": "review_gate_request",
        "dispatch_id": "DISP-GATE-IDEM-002",
        "terminal": "T1",
        "source": "governance",
        "pr_number": "278",
        "gate": "codex",
    }
    payload = json.dumps(receipt)

    first = _run_append(tmp_path, payload)
    second = _run_append(tmp_path, payload)

    assert first.returncode == 0
    assert second.returncode == 0
    assert '"code":"duplicate_receipt_skipped"' in second.stderr, (
        "Second identical gate receipt should be deduped"
    )

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = [ln for ln in receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, "Duplicate gate receipt should not create a second entry"


def test_idempotency_existing_receipts_backward_compat(tmp_path: Path):
    """Non-gate receipts (no gate/pr_number fields) still deduplicate correctly.

    Backward compat: adding gate/pr_number to IDEMPOTENCY_FIELDS uses skip-if-None
    logic, so existing receipts without those fields produce the same hash as before.
    """
    receipt = {
        "timestamp": "2026-04-28T10:02:00Z",
        "event_type": "task_complete",
        "event": "task_complete",
        "dispatch_id": "DISP-BACK-COMPAT-001",
        "task_id": "TASK-001",
        "terminal": "T1",
        "source": "pytest",
        "status": "success",
    }
    payload = json.dumps(receipt)

    first = _run_append(tmp_path, payload)
    second = _run_append(tmp_path, payload)

    assert first.returncode == 0
    assert second.returncode == 0
    assert '"code":"duplicate_receipt_skipped"' in second.stderr, (
        "Non-gate receipt should still deduplicate correctly"
    )
