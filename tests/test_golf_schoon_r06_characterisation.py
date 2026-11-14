"""Characterisation of ``LearningLoop.daily_learning_cycle`` (golf schoon R06).

Pins the observable behaviour of the cycle before and after the extract-method
refactor: return value, stdout step lines and their order, the order of the
side-effecting steps, the heartbeat, and the statistics written to the report.
Real temporary dirs and a real SQLite store; only the beacon is patched.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402

from test_learning_loop_shadow_nightly import _SCHEMA, _failure, NOW  # noqa: E402

_GATE_RUN = {"action": "run", "probe_health": "ok", "detail": "test"}
_STEP_METHODS = (
    "extract_used_patterns", "extract_ignored_patterns", "update_confidence_scores",
    "_propose_rules", "archive_unused_patterns", "save_pattern_metrics",
    "persist_to_intelligence_db", "ingest_approved_rules", "_supersede_stale_patterns",
    "generate_learning_report",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_LEARNING_LOOP_PERSIST", raising=False)
    monkeypatch.delenv("VNX_LEARNING_LOOP_CUTOFF", raising=False)
    monkeypatch.delenv("VNX_LEARN_PERSIST", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    paths = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(tmp_path / "repo"),
        "VNX_DATA_DIR": str(state_dir.parent),
        "PROJECT_ROOT": str(tmp_path / "repo"),
    }
    conn = sqlite3.connect(state_dir / "quality_intelligence.db")
    conn.executescript(_SCHEMA)
    now = NOW.isoformat()
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
        "confidence, last_used, updated_at) VALUES ('p-used','Used','h1',3,1.0,?,?)", (now, now))
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
        "ignored_count, confidence, last_offered) VALUES ('p-ign','Ignored','h2',0,1,1.0,?)", (now,))
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
        "confidence) VALUES ('p-stale','Stale','h3',0,0.1)")
    conn.commit()
    conn.close()
    receipts = [
        _failure("t-old", "2026-09-01T10:00:00+00:00"),
        _failure("t-new-1", NOW.isoformat()),
        _failure("t-new-2", (NOW - timedelta(minutes=5)).isoformat()),
    ]
    (state_dir / "t0_receipts.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    with patch.object(ll, "ensure_env", return_value=paths):
        yield state_dir, paths


@pytest.fixture
def persist_loop(env, monkeypatch, approve_learning_persist):
    """A persist-mode loop (approval for the empty change set, ledger green)."""
    state_dir, _ = env
    approve_learning_persist(state_dir)
    monkeypatch.setenv("VNX_LEARNING_LOOP_PERSIST", "1")
    monkeypatch.setattr(ll, "_ledger_verdict", lambda *_: {"status": "ok", "reason_code": "ok"})
    loop = ll.LearningLoop()
    yield loop
    loop.conn.close()


def _record_order(loop):
    order = []
    for name in _STEP_METHODS:
        real = getattr(loop, name)

        def wrapper(*a, _n=name, _r=real, **k):
            order.append(_n)
            return _r(*a, **k)

        setattr(loop, name, wrapper)
    return order


def _run(loop, capsys, gate=_GATE_RUN, **kwargs):
    with patch.object(ll, "evaluate_activation_gate", return_value=gate):
        report = loop.daily_learning_cycle(**kwargs)
    return report, capsys.readouterr().out


def test_shadow_cycle_step_order_and_stdout(env, capsys):
    loop = ll.LearningLoop()
    order = _record_order(loop)
    report, out = _run(loop, capsys)
    loop.conn.close()
    assert order == list(_STEP_METHODS)
    steps = re.findall(r"Step (\d(?:\.\d)?):", out)
    assert steps == ["1", "2", "3", "4", "5", "5.5", "5.6", "5.7", "5.8", "6"]
    assert "[DAILY (24h) / SHADOW]" in out
    assert "Shadow mode — reconcile skipped" in out
    assert "Shadow mode — reason evaluator skipped" in out
    assert re.search(r"Learning cycle completed in \d+\.\d\d seconds", out)
    assert "  • Proposals (pending rules): 1" in out
    assert out.rstrip().endswith("=" * 60)


def test_shadow_cycle_report_and_statistics(env, capsys):
    loop = ll.LearningLoop()
    report, _ = _run(loop, capsys)
    loop.conn.close()
    assert report["mode"] == "shadow"
    assert report["cutoff"] == loop.cutoff.isoformat()
    assert report["statistics"] is loop.learning_stats
    assert report["shadow"] is loop.shadow
    assert "ledger_verdict" not in report
    assert "status" not in report
    stats = report["statistics"]
    assert stats["proposal_count"] == 1
    assert stats["injection_tuning_proposals"] == 0
    assert stats["receipts_read"] == 3
    assert stats["receipts_after_cutoff"] == 2
    assert stats["receipts_skipped"] >= 1
    assert stats["patterns_used"] == 1
    assert loop.shadow["steps_not_run"][-2:] == [
        "reconcile_pattern_confidence", "run_reason_evaluator_and_propose"]
    assert loop.shadow["archival_candidates"][0]["pattern_id"] == "p-stale"


def test_from_history_label_and_window(env, capsys):
    loop = ll.LearningLoop()
    with patch.object(loop, "_propose_rules", wraps=loop._propose_rules) as spy:
        _, out = _run(loop, capsys, from_history=True)
    loop.conn.close()
    spy.assert_called_once_with(True)
    assert "[FULL HISTORY / SHADOW]" in out


def test_dormant_gate_returns_before_anything_runs(persist_loop, capsys):
    loop = persist_loop
    order = _record_order(loop)
    gate = {"action": "dormant", "probe_health": None, "detail": "off"}
    report, out = _run(loop, capsys, gate=gate)
    assert report == {"status": "dormant", "gate": gate, "statistics": {}}
    assert order == []
    assert out == "💤 Learning loop dormant (VNX_LEARNING_LOOP_ENABLED=0) — no-op, no beacon\n"


def test_degraded_gate_uses_degraded_cycle(persist_loop, capsys):
    loop = persist_loop
    gate = {"action": "degraded", "probe_health": "produces_crap", "detail": "x"}
    sentinel = {"status": "degraded", "marker": 1}
    with patch.object(loop, "_degraded_cycle", return_value=sentinel) as dc:
        report, out = _run(loop, capsys, gate=gate, from_history=True)
    dc.assert_called_once_with(gate, True)
    assert report is sentinel
    assert "Starting Learning Cycle" not in out


def test_refused_ledger_returned_unchanged(persist_loop, capsys):
    loop = persist_loop
    refusal = {"status": "refused", "reason": "ledger"}
    with patch.object(loop, "_ledger_gate", return_value=refusal):
        report, out = _run(loop, capsys)
    assert report is refusal
    assert "Starting Learning Cycle" not in out


def test_queue_error_is_reset_each_cycle_and_degrades_report(persist_loop, capsys):
    loop = persist_loop
    loop.queue_error = "stale error from last cycle"
    with patch.object(loop, "persist_to_intelligence_db"), patch.object(loop, "_heartbeat"), \
            patch("confidence_reconcile.reconcile_pattern_confidence", return_value=0):
        report, _ = _run(loop, capsys)
    assert loop.queue_error is None and "status" not in report
    assert report["ledger_verdict"] == {"status": "ok", "reason_code": "ok"}

    def failing(from_history):
        loop.queue_error = "queue unreadable"
        return [], {"read": 0, "after_cutoff": 0, "before_cutoff": 0}

    with patch.object(loop, "_propose_rules", side_effect=failing), \
            patch.object(loop, "persist_to_intelligence_db"), \
            patch("confidence_reconcile.reconcile_pattern_confidence", return_value=0), \
            patch.object(loop, "_heartbeat") as hb:
        report, _ = _run(loop, capsys)
    assert report["status"] == "degraded" and report["reason"] == "queue unreadable"
    hb.assert_called_once_with("degraded", {"reason": "queue unreadable"})


def test_persist_cycle_heartbeat_ok_and_reconcile_order(persist_loop, monkeypatch, capsys):
    loop = persist_loop
    state_dir = loop.db_path.parent
    calls = []
    with patch.object(loop, "_heartbeat", side_effect=lambda s, d: calls.append((s, d))), \
            patch.object(loop, "persist_to_intelligence_db"), \
            patch("confidence_reconcile.reconcile_pattern_confidence", return_value=4) as rec, \
            patch("injection_effectiveness_probe.run_reason_evaluator_and_propose",
                  return_value={"ran": True, "proposals_written": 2, "proposals_generated": 3}) as rea:
        report, out = _run(loop, capsys)
    rec.assert_called_once_with(loop.db_path)
    rea.assert_called_once_with(state_dir=loop.db_path.parent)
    assert "✓ Reconciled 4 success_patterns rows" in out
    assert "2 new tuning proposal(s) queued (3 generated this run)" in out
    assert report["mode"] == "persist"
    assert "shadow" not in report
    assert report["statistics"]["injection_tuning_proposals"] == 2
    assert len(calls) == 1 and calls[0][0] == "ok"
    assert set(calls[0][1]) == {
        "elapsed_seconds", "patterns_tracked", "confidence_adjustments",
        "new_prevention_rules", "patterns_archived", "ledger_status"}
    assert calls[0][1]["ledger_status"] == "ok"


def test_persist_off_switch_and_step_errors_are_printed(persist_loop, monkeypatch, capsys):
    monkeypatch.setenv("VNX_LEARN_PERSIST", "0")
    loop = persist_loop
    with patch.object(loop, "_heartbeat"), \
            patch.object(loop, "persist_to_intelligence_db") as persist, \
            patch("confidence_reconcile.reconcile_pattern_confidence", side_effect=RuntimeError("boom")), \
            patch("injection_effectiveness_probe.run_reason_evaluator_and_propose",
                  side_effect=RuntimeError("bang")):
        report, out = _run(loop, capsys)
    persist.assert_not_called()
    assert "VNX_LEARN_PERSIST=0 — pattern persist skipped" in out
    assert "❌ Error reconciling confidence: boom" in out
    assert "❌ Error running reason evaluator: bang" in out
    assert report["statistics"]["injection_tuning_proposals"] == 0


def test_persist_reason_evaluator_flags_off_message(persist_loop, monkeypatch, capsys):
    loop = persist_loop
    state_dir = loop.db_path.parent
    with patch.object(loop, "_heartbeat"), \
            patch.object(loop, "persist_to_intelligence_db"), \
            patch("confidence_reconcile.reconcile_pattern_confidence", return_value=0), \
            patch("injection_effectiveness_probe.run_reason_evaluator_and_propose",
                  return_value={"ran": False, "proposals_written": 0, "proposals_generated": 0}):
        _, out = _run(loop, capsys)
    assert "Injection-tuning flags off — reason evaluator skipped" in out


def test_superseded_patterns_line_only_when_queued(env, capsys):
    loop = ll.LearningLoop()
    with patch.object(loop, "_supersede_stale_patterns", return_value=3):
        _, out = _run(loop, capsys)
    assert "✓ 3 patterns queued for operator review" in out
    with patch.object(loop, "_supersede_stale_patterns", return_value=0):
        _, out = _run(loop, capsys)
    loop.conn.close()
    assert "queued for operator review" not in out.split("Step 5.6")[1].split("Step 5.7")[0].replace(
        "Queuing stale patterns for operator-gated supersede...", "")
