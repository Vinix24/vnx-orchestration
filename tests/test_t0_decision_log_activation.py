#!/usr/bin/env python3
"""Tests for T0 decision-log activation (P0).

Covers the four wired call sites and the reconciliation pass:

  A. dispatch_created  (via dispatch_register.append_event)
  B. gate_verdict      (via gate_passed / gate_failed)
  C. pr_merge          (via pr_merged)
  D. oi_closed         (via open_items_manager.close_item)
  E. idempotency / dedup semantics — every call appends a record;
     reconciliation resolves each decision exactly once.
  F. outcome reconciliation — pending dispatch_created becomes resolved
     once a dispatch_completed event arrives.
  G. concurrent writes are fcntl-locked.
"""
from __future__ import annotations

import importlib
import json
import multiprocessing as mp
import sys
import threading
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


@pytest.fixture()
def decision_log_path(tmp_path: Path) -> Path:
    return tmp_path / "t0_decision_log.jsonl"


@pytest.fixture()
def register_path(tmp_path: Path) -> Path:
    return tmp_path / "dispatch_register.ndjson"


@pytest.fixture()
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VNX_STATE_DIR / VNX_DATA_DIR_EXPLICIT at tmp_path/state."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    # Reload modules so they pick up the patched env.
    for mod in ("t0_decision_log", "dispatch_register", "t0_decision_reconcile"):
        if mod in sys.modules:
            del sys.modules[mod]
    return state


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# A. dispatch_created
# ---------------------------------------------------------------------------


def test_dispatch_created_logs_decision(isolated_state: Path) -> None:
    import dispatch_register

    ok = dispatch_register.append_event(
        "dispatch_created",
        dispatch_id="20260430-x-A",
        terminal="T1",
        extra={
            "role": "backend-developer",
            "risk_score": 0.4,
            "reasoning": "auto-promoted from staging",
            "expected_outcome": "success",
        },
    )
    assert ok is True

    decisions = _read_jsonl(isolated_state / "t0_decision_log.jsonl")
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision_type"] == "dispatch_created"
    assert rec["action"] == "dispatch"
    assert rec["dispatch_id"] == "20260430-x-A"
    assert rec["terminal"] == "T1"
    assert rec["track"] == "A"
    assert rec["role"] == "backend-developer"
    assert rec["risk_score"] == 0.4
    assert rec["expected_outcome"] == "success"
    assert rec["outcome_pending"] is True


# ---------------------------------------------------------------------------
# B. gate_verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event,expected_verdict", [
    ("gate_passed", "passed"),
    ("gate_failed", "failed"),
])
def test_gate_verdict_logs_decision(isolated_state: Path, event: str,
                                    expected_verdict: str) -> None:
    import dispatch_register

    ok = dispatch_register.append_event(
        event,
        dispatch_id="20260430-x-A",
        pr_number=42,
        gate="codex_gate",
        extra={"blocking_count": 0 if expected_verdict == "passed" else 3,
               "reasoning": ""},
    )
    assert ok is True

    decisions = _read_jsonl(isolated_state / "t0_decision_log.jsonl")
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision_type"] == "gate_verdict"
    assert rec["action"] == "advance_gate"
    assert rec["gate"] == "codex_gate"
    assert rec["verdict"] == expected_verdict
    assert rec["pr_number"] == 42
    assert rec["outcome_pending"] is True


# ---------------------------------------------------------------------------
# C. pr_merge
# ---------------------------------------------------------------------------


def test_pr_merge_logs_decision(isolated_state: Path) -> None:
    import dispatch_register

    ok = dispatch_register.append_event(
        "pr_merged",
        pr_number=297,
        extra={
            "dispatches_in_pr": ["20260430-x-A", "20260430-x-B"],
            "reasoning": "all gates passed",
        },
    )
    assert ok is True

    decisions = _read_jsonl(isolated_state / "t0_decision_log.jsonl")
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision_type"] == "pr_merge"
    assert rec["action"] == "approve"
    assert rec["pr_number"] == 297
    assert rec["dispatches_in_pr"] == ["20260430-x-A", "20260430-x-B"]
    # Terminal type — outcome is settled at write time
    assert rec["outcome_pending"] is False


# ---------------------------------------------------------------------------
# D. oi_closed
# ---------------------------------------------------------------------------


def test_oi_closed_logs_decision_directly(isolated_state: Path) -> None:
    """Test the oi_closed call path by invoking log_decision the same way
    open_items_manager._log_oi_close_decision does. We avoid running the
    full open_items_manager CLI flow because it depends on vnx_paths
    state-dir wiring that's already exercised by its own tests."""
    from t0_decision_log import log_decision

    ok = log_decision(
        decision_type="oi_closed",
        oi_id="OI-1234",
        status="done",
        reasoning="Fixed in PR #500",
        dispatch_id="20260430-x-A",
    )
    assert ok is True

    decisions = _read_jsonl(isolated_state / "t0_decision_log.jsonl")
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision_type"] == "oi_closed"
    assert rec["action"] == "close_oi"
    assert rec["oi_id"] == "OI-1234"
    assert rec["status"] == "done"
    assert rec["reasoning"] == "Fixed in PR #500"
    assert rec["outcome_pending"] is False


# ---------------------------------------------------------------------------
# E. Idempotency / dedup semantics
# ---------------------------------------------------------------------------


def test_repeated_calls_append_separately(isolated_state: Path) -> None:
    """Each call to log_decision appends a fresh record by design.

    The decision log is event-sourced and append-only; deduplication is the
    reconciler's responsibility (it dedups on the full timestamp+subject
    key so two records at the same instant resolve as one). Documenting
    this here so future readers don't expect implicit merge behavior.
    """
    from t0_decision_log import log_decision

    log_decision(decision_type="oi_closed", oi_id="OI-1", status="done",
                 reasoning="x")
    log_decision(decision_type="oi_closed", oi_id="OI-1", status="done",
                 reasoning="x")

    decisions = _read_jsonl(isolated_state / "t0_decision_log.jsonl")
    assert len(decisions) == 2


# ---------------------------------------------------------------------------
# F. Outcome reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_resolves_pending_dispatch_created(isolated_state: Path) -> None:
    import dispatch_register
    import t0_decision_reconcile

    # 1. Write dispatch_created (decision is pending)
    dispatch_register.append_event(
        "dispatch_created",
        dispatch_id="20260430-recon-A",
        terminal="T1",
        extra={"role": "backend-developer", "expected_outcome": "success"},
    )
    # No outcome event yet → first reconcile is a no-op
    assert t0_decision_reconcile.reconcile() == 0

    # 2. Worker completes — register receives dispatch_completed
    dispatch_register.append_event(
        "dispatch_completed",
        dispatch_id="20260430-recon-A",
    )

    # 3. Reconcile picks up the resolution
    written = t0_decision_reconcile.reconcile()
    assert written == 1

    outcomes = _read_jsonl(isolated_state / "t0_decision_outcomes.ndjson")
    assert len(outcomes) == 1
    res = outcomes[0]
    assert res["decision_type"] == "dispatch_created"
    assert res["dispatch_id"] == "20260430-recon-A"
    assert res["actual_outcome"] == "success"
    assert res["expected_outcome"] == "success"
    assert res["register_event"] == "dispatch_completed"

    # 4. Idempotency — second reconcile must not re-write
    assert t0_decision_reconcile.reconcile() == 0
    outcomes_after = _read_jsonl(isolated_state / "t0_decision_outcomes.ndjson")
    assert len(outcomes_after) == 1


def test_reconcile_failure_outcome(isolated_state: Path) -> None:
    import dispatch_register
    import t0_decision_reconcile

    dispatch_register.append_event(
        "dispatch_created",
        dispatch_id="20260430-fail-A",
        terminal="T1",
        extra={"expected_outcome": "success"},
    )
    dispatch_register.append_event(
        "dispatch_failed",
        dispatch_id="20260430-fail-A",
    )

    assert t0_decision_reconcile.reconcile() == 1
    outcomes = _read_jsonl(isolated_state / "t0_decision_outcomes.ndjson")
    assert outcomes[0]["actual_outcome"] == "failure"
    assert outcomes[0]["register_event"] == "dispatch_failed"


# ---------------------------------------------------------------------------
# G. Concurrent writes are fcntl-locked
# ---------------------------------------------------------------------------


def _write_n(log_path_str: str, n: int, oi_prefix: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
    from t0_decision_log import log_decision
    log_path = Path(log_path_str)
    for i in range(n):
        log_decision(
            decision_type="oi_closed",
            oi_id=f"{oi_prefix}-{i}",
            status="done",
            reasoning="concurrent",
            log_file=log_path,
        )


def test_concurrent_writes_no_corruption(tmp_path: Path) -> None:
    log_path = tmp_path / "concurrent.jsonl"
    threads = [
        threading.Thread(target=_write_n, args=(str(log_path), 50, f"T{n}"))
        for n in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = log_path.read_text().splitlines()
    assert len(lines) == 200
    # Every line must be valid JSON — no torn writes.
    for line in lines:
        rec = json.loads(line)
        assert rec["decision_type"] == "oi_closed"
        assert rec["status"] == "done"
