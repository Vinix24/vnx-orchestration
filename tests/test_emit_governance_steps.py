"""Characterisation of the three fail-open steps inside ``_emit_governance``.

Golf 2 / G2-11 moves three blocks out of ``provider_dispatch._emit_governance``
into private helpers: the receipt-role resolution (step A), the tool-call
signal aggregation (step B) and the post-receipt tail — cooldown, reachability
and the event-store clear (step C). Behaviour must not change, so these tests
were written and run green on the OLD code first and stay green across the
move.

Every case asserts on evidence the dispatch actually produced: the receipt in
``t0_receipts.ndjson``, the cooldown file, the reachability record, and the
debug/warning log records the fail-open paths emit. Fixtures follow
``test_emit_governance_retry.py`` and ``test_provider_delivery_observability.py``
— the real ``emit_dispatch_receipt`` runs against a tmp state dir, nothing about
the receipt is faked.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_dispatch as pd
from toolcall_signals import record_toolcall_event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        dispatch_id="steps-test-001",
        terminal_id="T1",
        pr_id=None,
        instruction="Reply OK.",
        model="sonnet",
        provider="claude",
        role="backend-developer",
        project_id="test-proj",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _FakeResult:
    """Minimal spawn result carrying only what _emit_governance reads."""

    def __init__(self, *, error=None, completion_text=None, timed_out=False,
                 returncode=None, token_usage=None, session_id=None):
        self.error = error
        self.completion_text = completion_text
        self.timed_out = timed_out
        self.returncode = returncode
        self.token_usage = token_usage if token_usage is not None else {"input": 0, "output": 0}
        self.session_id = session_id


def _now():
    return datetime.now(timezone.utc)


def _run_emit(monkeypatch, tmp_path, *, args, provider=None, model=None,
              status="success", result=None, event_store=None):
    """Run the real _emit_governance against tmp state/data dirs; return the ledger."""
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    result = result if result is not None else _FakeResult(returncode=0, completion_text="done")
    pd._emit_governance(
        args, provider or args.provider, model or args.model,
        result, _now(), _now(), status, event_store=event_store,
    )
    return _read_receipts(tmp_path / "state")


def _read_receipts(state_dir: Path) -> list[dict]:
    receipt_file = state_dir / "t0_receipts.ndjson"
    assert receipt_file.exists(), f"expected receipt ledger at {receipt_file}"
    lines = [ln for ln in receipt_file.read_text().splitlines() if ln.strip()]
    assert lines, "receipt ledger must be non-empty"
    return [json.loads(ln) for ln in lines]


def _receipt(receipts: list[dict], dispatch_id: str) -> dict:
    matching = [r for r in receipts if r.get("dispatch_id") == dispatch_id]
    assert len(matching) == 1, f"expected exactly 1 receipt for {dispatch_id}, got {len(matching)}"
    return matching[0]


def _debug_records(caplog, needle: str) -> list[logging.LogRecord]:
    return [
        rec for rec in caplog.records
        if rec.levelno == logging.DEBUG and needle in rec.getMessage()
        and rec.name.endswith("provider_dispatch")
    ]


class _EventStore:
    """Event store double: archive returns nothing, clear does what the test says."""

    def __init__(self, *, clear_exc: Exception | None = None, archive_exc: Exception | None = None):
        self.clear_exc = clear_exc
        self.archive_exc = archive_exc
        self.clear_calls: list[tuple] = []
        self.archive_calls: list[tuple] = []

    def archive(self, terminal_id, dispatch_id):
        self.archive_calls.append((terminal_id, dispatch_id))
        if self.archive_exc is not None:
            raise self.archive_exc
        return None

    def clear(self, terminal_id, dispatch_id=None):
        self.clear_calls.append((terminal_id, dispatch_id))
        if self.clear_exc is not None:
            raise self.clear_exc


# ---------------------------------------------------------------------------
# Step A — the receipt role: resolve, or stamp identity_unresolved
# ---------------------------------------------------------------------------

class TestStepARoleResolution:
    def test_resolved_role_lands_on_the_receipt(self, monkeypatch, tmp_path):
        args = _make_args(dispatch_id="steps-a-resolved", role=None)
        with patch("dispatch_identity.resolve_effective_role", return_value="quality-engineer") as mock_resolve:
            receipts = _run_emit(monkeypatch, tmp_path, args=args)
        mock_resolve.assert_called_once()
        assert _receipt(receipts, "steps-a-resolved")["role"] == "quality-engineer"

    def test_resolver_error_falls_open_to_identity_unresolved(self, monkeypatch, tmp_path, caplog):
        args = _make_args(dispatch_id="steps-a-failopen", role=None)
        with caplog.at_level(logging.DEBUG, logger="provider_dispatch"):
            with patch("dispatch_identity.resolve_effective_role",
                       side_effect=RuntimeError("identity store unavailable")):
                receipts = _run_emit(monkeypatch, tmp_path, args=args)
        assert _receipt(receipts, "steps-a-failopen")["role"] == "identity_unresolved"
        records = _debug_records(caplog, "role resolution failed open")
        assert records, "the fail-open path must leave its debug line"
        assert records[0].exc_info is not None, "the debug line carries exc_info=True"


# ---------------------------------------------------------------------------
# Step B — the tool-call signals: absent, aggregated, or fail-open empty
# ---------------------------------------------------------------------------

class TestStepBToolcallSignals:
    def test_signal_dir_unset_leaves_the_fields_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_TMUX_SIGNAL_DIR", raising=False)
        args = _make_args(dispatch_id="steps-b-unset")
        receipt = _receipt(_run_emit(monkeypatch, tmp_path, args=args), "steps-b-unset")
        for field in ("tool_call_count", "tool_call_failures", "tool_call_retries"):
            assert field not in receipt, f"{field} must stay omitted, never a fabricated zero"

    def test_real_signal_log_lands_its_counts_on_the_receipt(self, monkeypatch, tmp_path):
        signal_dir = tmp_path / "tmux-signal"
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, blocked=False)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "claude -p x"}}, blocked=True)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
        record_toolcall_event(signal_dir, {"tool_name": "Bash", "tool_input": {"command": "pytest"}}, blocked=False)
        monkeypatch.setenv("VNX_TMUX_SIGNAL_DIR", str(signal_dir))

        args = _make_args(dispatch_id="steps-b-present")
        receipt = _receipt(_run_emit(monkeypatch, tmp_path, args=args), "steps-b-present")

        assert receipt["tool_call_count"] == 4
        assert receipt["tool_call_failures"] == 1
        assert receipt["tool_call_retries"] == 1

    def test_aggregation_error_falls_open_to_empty_and_logs(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("VNX_TMUX_SIGNAL_DIR", str(tmp_path / "tmux-signal-broken"))
        args = _make_args(dispatch_id="steps-b-failopen")
        with caplog.at_level(logging.DEBUG, logger="provider_dispatch"):
            with patch("toolcall_signals.aggregate_toolcall_signals",
                       side_effect=RuntimeError("signal log unreadable")):
                receipt = _receipt(_run_emit(monkeypatch, tmp_path, args=args), "steps-b-failopen")
        for field in ("tool_call_count", "tool_call_failures", "tool_call_retries"):
            assert field not in receipt
        records = _debug_records(caplog, "toolcall signal aggregation failed")
        assert records, "the fail-open path must leave its debug line"
        assert records[0].exc_info is not None


# ---------------------------------------------------------------------------
# Step C — the post-receipt tail: cooldown, reachability, event-store clear
# ---------------------------------------------------------------------------

class TestStepCTail:
    def test_success_writes_no_cooldown_and_one_reachable_record(self, monkeypatch, tmp_path):
        args = _make_args(dispatch_id="steps-c-success")
        state_dir = tmp_path / "state"
        _run_emit(
            monkeypatch, tmp_path, args=args,
            result=_FakeResult(returncode=0, completion_text="done"),
        )
        assert not (state_dir / "router_lane_cooldown").exists(), "a success writes no cooldown"
        record = json.loads((state_dir / "provider_reachability" / "claude.json").read_text())
        assert record["state"] == "reachable"
        assert record["source"] == "provider_dispatch:claude"

    def test_failure_with_class_writes_cooldown_and_unreachable_record(self, monkeypatch, tmp_path):
        args = _make_args(dispatch_id="steps-c-failure", provider="deepseek-harness", model="deepseek-v4-pro")
        state_dir = tmp_path / "state"
        _run_emit(
            monkeypatch, tmp_path, args=args,
            provider="deepseek-harness", model="deepseek-v4-pro",
            status="failure",
            result=_FakeResult(returncode=1, error="API Error: 401 Unauthorized - invalid API key"),
        )
        cooldown = json.loads(
            (state_dir / "router_lane_cooldown" / "deepseek-harness.json").read_text()
        )
        assert cooldown["failure_class"] == "provider_auth_failure"
        assert "401" in cooldown["reason"]
        record = json.loads(
            (state_dir / "provider_reachability" / "deepseek-harness.json").read_text()
        )
        assert record["state"] == "unreachable"
        assert record["reason"] == "auth_401"
        assert record["source"] == "provider_dispatch:deepseek-harness"

    def test_event_store_clear_error_warns_and_never_raises(self, monkeypatch, tmp_path, caplog):
        args = _make_args(dispatch_id="steps-c-clear-error")
        store = _EventStore(clear_exc=RuntimeError("event stream already rotated"))
        with caplog.at_level(logging.WARNING, logger="provider_dispatch"):
            receipts = _run_emit(monkeypatch, tmp_path, args=args, event_store=store)
        assert store.clear_calls == [("T1", "steps-c-clear-error")]
        assert _receipt(receipts, "steps-c-clear-error")["status"] == "success"
        warnings = [
            rec for rec in caplog.records
            if rec.levelno == logging.WARNING and "event_store.clear failed" in rec.getMessage()
        ]
        assert warnings, "the clear failure must be logged at warning level, not raised"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
