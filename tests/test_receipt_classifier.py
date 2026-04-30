"""Tests for the adaptive receipt classifier core (ARC-3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

import receipt_classifier as rc  # noqa: E402
from classifier_providers.base import ClassifierProvider, ClassifierResult  # noqa: E402


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------


@pytest.fixture
def env_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.delenv("VNX_RECEIPT_CLASSIFIER_ENABLED", raising=False)
    monkeypatch.delenv("VNX_RECEIPT_CLASSIFIER_MODE", raising=False)
    monkeypatch.delenv("VNX_RECEIPT_CLASSIFIER_PROVIDER", raising=False)
    monkeypatch.delenv("VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD", raising=False)
    return state


def _make_receipt(status="success", event_type="task_complete", dispatch_id="DISP-1"):
    return {
        "timestamp": "2026-04-30T12:00:00Z",
        "event_type": event_type,
        "event": event_type,
        "dispatch_id": dispatch_id,
        "task_id": dispatch_id + "-task",
        "terminal": "T1",
        "status": status,
        "report_path": "/tmp/report.md",
    }


class _StubProvider(ClassifierProvider):
    name = "stub"

    def __init__(self, payload, *, error=None, cost_usd=0.0):
        self.payload = payload
        self.error = error
        self.cost_usd = cost_usd
        self.calls = []

    def classify(self, prompt, max_tokens=1500):
        self.calls.append(prompt)
        return ClassifierResult(
            raw_response=self.payload if isinstance(self.payload, str) else json.dumps(self.payload),
            parsed_json=self.payload if isinstance(self.payload, dict) else None,
            cost_usd=self.cost_usd,
            latency_ms=10,
            provider=self.name,
            error=self.error,
        )


# ----------------------------------------------------------------------
# Case A: env var off → no classification
# ----------------------------------------------------------------------


def test_disabled_env_no_op(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "0")
    assert rc.is_enabled() is False
    assert rc.trigger_receipt_classifier_async(_make_receipt()) is None
    # queue must not be created
    assert not (env_state / rc.QUEUE_FILE_NAME).exists()


# ----------------------------------------------------------------------
# Case B: per_receipt mode → fires async
# ----------------------------------------------------------------------


def test_per_receipt_mode_spawns_async(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_MODE", "per_receipt")

    spawned = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            spawned["args"] = args
            spawned["kwargs"] = kwargs

            class _Stdin:
                def write(self_inner, data):
                    spawned["stdin"] = data

            self.stdin = _Stdin()

    with patch("receipt_classifier.subprocess.Popen", _FakePopen):
        action = rc.trigger_receipt_classifier_async(_make_receipt())

    assert action == "fired_per_receipt"
    assert "stdin" in spawned
    assert b"DISP-1" in spawned["stdin"]


# ----------------------------------------------------------------------
# Case C: batch mode → appends to queue, doesn't spawn
# ----------------------------------------------------------------------


def test_batch_mode_appends_to_queue(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_MODE", "batch")

    with patch("receipt_classifier.subprocess.Popen") as popen:
        action = rc.trigger_receipt_classifier_async(_make_receipt())
    assert action == "queued_batch"
    popen.assert_not_called()

    queue_file = env_state / rc.QUEUE_FILE_NAME
    assert queue_file.exists()
    line = queue_file.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["receipt"]["dispatch_id"] == "DISP-1"


# ----------------------------------------------------------------------
# Case D: failures_direct → fires for failures, queues successes
# ----------------------------------------------------------------------


def test_failures_direct_fires_for_failures(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_MODE", "failures_direct")

    with patch("receipt_classifier.subprocess.Popen") as popen:
        action = rc.trigger_receipt_classifier_async(
            _make_receipt(status="failed", event_type="task_failed")
        )
        assert action == "fired_failure_direct"
        assert popen.called

        # success should NOT spawn — should queue instead
        popen.reset_mock()
        action = rc.trigger_receipt_classifier_async(_make_receipt(status="success"))
        assert action == "queued_success_for_batch"
        popen.assert_not_called()

    assert (env_state / rc.QUEUE_FILE_NAME).exists()


# ----------------------------------------------------------------------
# Case E: cost budget exhausted → skip with warning
# ----------------------------------------------------------------------


def test_classify_skipped_when_budget_exhausted(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD", "0.10")

    rc.track_cost("haiku", 0.20)  # blow the budget

    assert rc.is_budget_exhausted() is True
    result = rc.classify_receipt(_make_receipt(), provider=_StubProvider({"impact_class": "trivial"}))
    assert result["status"] == "skipped"
    assert result["reason"] == "budget_exhausted"


def test_track_cost_resets_each_day(env_state):
    rc.track_cost("haiku", 0.05)
    entry1 = rc._read_cost()
    assert entry1.spent_usd == pytest.approx(0.05)
    assert entry1.calls == 1

    rc.track_cost("haiku", 0.07)
    entry2 = rc._read_cost()
    assert entry2.spent_usd == pytest.approx(0.12)
    assert entry2.calls == 2


# ----------------------------------------------------------------------
# Case F + G: provider routing
# ----------------------------------------------------------------------


def test_provider_haiku_invokes_correct_cli(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_PROVIDER", "haiku")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"impact_class":"trivial","suggested_edit":null}', stderr=""
        )

    with patch("classifier_providers.haiku_provider.subprocess.run", side_effect=fake_run):
        result = rc.classify_receipt(_make_receipt())
    assert result["status"] == "ok"
    assert captured["cmd"][0] == "claude"
    assert "--print" in captured["cmd"]


def test_provider_ollama_invokes_correct_cli(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_PROVIDER", "ollama")
    monkeypatch.setenv("VNX_OLLAMA_MODEL", "llama3.1:8b")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"impact_class":"trivial","suggested_edit":null}', stderr=""
        )

    with patch("classifier_providers.ollama_provider.subprocess.run", side_effect=fake_run):
        result = rc.classify_receipt(_make_receipt())
    assert result["status"] == "ok"
    assert captured["cmd"] == ["ollama", "run", "llama3.1:8b"]


# ----------------------------------------------------------------------
# Case H: trivial-class edit NOT queued
# ----------------------------------------------------------------------


def test_trivial_edit_not_queued(env_state):
    payload = {
        "domain": "governance",
        "outcome_class": "success",
        "impact_class": "trivial",
        "suggested_edit": {
            "target_file": "CLAUDE.md",
            "edit_type": "add_line",
            "content": "minor tweak",
            "rationale": "cosmetic",
            "confidence": 0.95,
        },
    }
    result = rc.classify_receipt(_make_receipt(), provider=_StubProvider(payload))
    assert result["status"] == "ok"
    assert result["queued_edit_ids"] == []
    assert not (env_state / rc.PENDING_EDITS_FILE_NAME).exists()


def test_low_confidence_significant_not_queued(env_state):
    payload = {
        "impact_class": "policy_change",
        "suggested_edit": {
            "target_file": "CLAUDE.md",
            "edit_type": "replace",
            "content": "new policy",
            "rationale": "weak signal",
            "confidence": 0.5,
        },
    }
    result = rc.classify_receipt(_make_receipt(), provider=_StubProvider(payload))
    assert result["queued_edit_ids"] == []


# ----------------------------------------------------------------------
# Case I: significant-class edit IS queued
# ----------------------------------------------------------------------


def test_significant_edit_queued_to_pending_edits(env_state):
    payload = {
        "domain": "governance",
        "outcome_class": "failure",
        "impact_class": "policy_change",
        "recurring_pattern_observed": "T0 skipped gate",
        "suggested_edit": {
            "target_file": ".claude/terminals/T0/CLAUDE.md",
            "edit_type": "add_line",
            "content": "Always run gate before merge.",
            "rationale": "Repeated gate skip detected.",
            "confidence": 0.92,
        },
    }
    result = rc.classify_receipt(_make_receipt(), provider=_StubProvider(payload))
    assert result["status"] == "ok"
    assert len(result["queued_edit_ids"]) == 1

    pending_file = env_state / rc.PENDING_EDITS_FILE_NAME
    assert pending_file.exists()
    data = json.loads(pending_file.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    record = data[0]
    assert record["impact_class"] == "policy_change"
    assert record["edit"]["confidence"] == 0.92
    assert record["status"] == "pending"
    assert record["source_dispatch_id"] == "DISP-1"


# ----------------------------------------------------------------------
# should_classify sampling
# ----------------------------------------------------------------------


def test_should_classify_skips_non_outcome_events():
    assert rc.should_classify({"event_type": "ack"}) is False
    assert rc.should_classify({"event_type": "dispatch_sent"}) is False
    assert rc.should_classify({"event_type": "task_complete"}) is True
    assert rc.should_classify({"event_type": "task_failed"}) is True


def test_should_classify_rejects_non_dict():
    assert rc.should_classify(None) is False
    assert rc.should_classify("string") is False


# ----------------------------------------------------------------------
# Queue drain
# ----------------------------------------------------------------------


def test_drain_queue_returns_and_truncates(env_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
    monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_MODE", "batch")

    for i in range(3):
        rc.trigger_receipt_classifier_async(_make_receipt(dispatch_id=f"DISP-{i}"))

    drained = rc.drain_queue()
    assert len(drained) == 3
    assert drained[0]["dispatch_id"] == "DISP-0"

    queue_file = env_state / rc.QUEUE_FILE_NAME
    assert queue_file.exists()
    assert queue_file.read_text(encoding="utf-8") == ""

    # second drain returns nothing
    assert rc.drain_queue() == []


# ----------------------------------------------------------------------
# Batch helper end-to-end (no provider needed for skipped path)
# ----------------------------------------------------------------------


def test_classify_batch_skips_when_empty(env_state):
    result = rc.classify_batch([])
    assert result["status"] == "skipped"
    assert result["reason"] == "no_receipts"


def test_classify_batch_queues_significant(env_state):
    payload = {
        "impact_class": "new_skill",
        "suggested_edit": {
            "target_file": ".claude/skills/new.md",
            "edit_type": "add_line",
            "content": "new skill body",
            "rationale": "Pattern observed across many receipts",
            "confidence": 0.85,
        },
    }
    receipts = [_make_receipt(dispatch_id=f"DISP-{i}") for i in range(3)]
    result = rc.classify_batch(receipts, provider=_StubProvider(payload, cost_usd=0.001))
    assert result["status"] == "ok"
    assert result["batch"] is True
    assert len(result["queued_edit_ids"]) == 1
    cost_file = env_state / rc.COST_FILE_NAME
    assert cost_file.exists()
    cost_data = json.loads(cost_file.read_text(encoding="utf-8"))
    assert cost_data["spent_usd"] == pytest.approx(0.001)
    assert cost_data["calls"] == 1
