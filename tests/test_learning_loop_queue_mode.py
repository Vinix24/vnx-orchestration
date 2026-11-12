"""Queue mode of the learning loop (loop D2, split 2 of 2).

Queue mode (``VNX_LEARNING_LOOP_QUEUE=1``) is shadow plus the operator queue: the
proposals reach ``pending_rules.json`` through the prevention-rule store, in shadow
and persist mode and also when the probe gate is degraded. It writes no database
row and activates nothing. Real code paths against tmp stores only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402
import prevention_rule_store as store  # noqa: E402

DEGRADED = {"action": "degraded", "probe_health": "degraded", "detail": "ignore rate 0.75"}
RUN = {"action": "run", "probe_health": "ok", "detail": "test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.delenv(ll.QUEUE_ENV, raising=False)
    monkeypatch.delenv(ll.PERSIST_ENV, raising=False)
    # queue mode reads ledger_source_check first (D7, tests/test_learning_loop_ledger_gate.py)
    monkeypatch.setattr(ll, "_ledger_verdict", lambda *_: {"status": "ok", "reason_code": "ok"})
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    paths = {
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(tmp_path / "repo"),
        "VNX_DATA_DIR": str(state_dir.parent),
        "PROJECT_ROOT": str(tmp_path / "repo"),
    }
    with patch.object(ll, "ensure_env", return_value=paths):
        yield state_dir


def _receipts(state_dir: Path, ids=("d-1", "d-2", "d-3")) -> None:
    now = datetime.now(timezone.utc)
    lines = [
        json.dumps({
            "status": "failed", "failure_reason": "Exhausted 3 retries", "terminal": "T1",
            "provider": "claude_code", "dispatch_id": dispatch_id,
            "timestamp": (now - timedelta(hours=2, minutes=n)).isoformat(),
        })
        for n, dispatch_id in enumerate(ids)
    ]
    (state_dir / "t0_receipts.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _queue_file(state_dir: Path) -> Path:
    return state_dir / "pending_rules.json"


def _rows(state_dir: Path) -> list:
    return json.loads(_queue_file(state_dir).read_text())["pending_rules"]


def _shadow_cycle(gate=RUN):
    loop = ll.LearningLoop(persist=False)
    with patch.object(ll, "evaluate_activation_gate", return_value=gate):
        report = loop.daily_learning_cycle()
    loop.conn.close()
    return report


def _degraded_cycle(state_dir, approve_learning_persist):
    approve_learning_persist(state_dir)
    loop = ll.LearningLoop(persist=True)
    with patch.object(ll, "evaluate_activation_gate", return_value=DEGRADED), \
         patch.object(loop, "update_confidence_scores") as confidence, \
         patch.object(loop, "persist_to_intelligence_db") as persist_db, \
         patch.object(loop, "archive_unused_patterns") as archive, \
         patch("health_beacon.HealthBeacon") as beacon:
        report = loop.daily_learning_cycle()
    loop.conn.close()
    for mock in (confidence, persist_db, archive):
        mock.assert_not_called()
    return report, beacon


def test_shadow_with_queue_writes_the_proposal_row(env, monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env)
    report = _shadow_cycle()
    (row,) = _rows(env)
    assert row["failure_class"] in ll.ELIGIBLE_CLASSES
    assert row["source_dispatch_ids"] == ["d-1", "d-2", "d-3"]
    assert report["statistics"]["proposal_count"] == 1


def test_shadow_without_queue_writes_nothing(env):
    _receipts(env)
    _shadow_cycle()
    assert not _queue_file(env).exists()


def test_one_dispatch_is_not_queued(env, monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env, ids=("d-1",))
    _shadow_cycle()
    assert not _queue_file(env).exists()


@pytest.mark.parametrize("rule_class", [None, "", "unclassified", "not-a-class"])
def test_unknown_or_ineligible_class_is_not_queueable(rule_class):
    rule = {"pattern": "p", "prevention": "x", "confidence": 0.5, "occurrence_count": 5, "failure_class": rule_class}
    assert ll._is_queueable(rule) is False


@pytest.mark.parametrize("count", [None, 1, True, "3"])
def test_eligible_class_needs_two_dispatches(count):
    cls = sorted(ll.ELIGIBLE_CLASSES)[0]
    assert ll._is_queueable({"failure_class": cls, "occurrence_count": count}) is False
    assert ll._is_queueable({"failure_class": cls, "occurrence_count": 2}) is True


def test_degraded_with_queue_queues_and_activates_nothing(env, monkeypatch, approve_learning_persist):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env)
    report, beacon = _degraded_cycle(env, approve_learning_persist)
    assert report["status"] == "degraded"
    assert report["statistics"]["proposal_count"] >= 1
    assert len(_rows(env)) == 1
    assert beacon.return_value.heartbeat.call_args.kwargs["status"] == "degraded"


def test_degraded_without_queue_is_unchanged(env, approve_learning_persist):
    _receipts(env)
    report, _ = _degraded_cycle(env, approve_learning_persist)
    assert report["status"] == "degraded"
    assert report["statistics"] == {}
    assert not _queue_file(env).exists()


def test_a_decided_id_does_not_return_to_pending(env, monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env)
    _shadow_cycle()
    rule_id = _rows(env)[0]["id"]
    store.decide(
        env, [rule_id], "rejected", reason="not now", decided_by="operator",
        project_id="vnx-dev", now="2026-11-11T08:00:00+00:00",
    )
    _shadow_cycle()
    assert _rows(env)[0]["status"] == "rejected"


@pytest.mark.parametrize("content", ["{not json", '{"pending_rules": [1]}'])
def test_unreadable_queue_stays_byte_equal_and_degrades_the_run(env, monkeypatch, content):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env)
    _queue_file(env).write_text(content, encoding="utf-8")
    before = _queue_file(env).read_bytes()
    with patch("health_beacon.HealthBeacon") as beacon:
        report = _shadow_cycle()
    assert _queue_file(env).read_bytes() == before
    assert report["status"] == "degraded"
    call = beacon.return_value.heartbeat.call_args.kwargs
    assert call["status"] == "degraded"
    assert call["details"]["reason"] == report["reason"]


def test_unreadable_queue_in_the_degraded_branch_returns_the_reason(env, monkeypatch, approve_learning_persist):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _receipts(env)
    _queue_file(env).write_text("{not json", encoding="utf-8")
    report, beacon = _degraded_cycle(env, approve_learning_persist)
    assert report["status"] == "degraded"
    assert report["reason"]
    assert beacon.return_value.heartbeat.call_args.kwargs["details"]["reason"] == report["reason"]
    assert _queue_file(env).read_text(encoding="utf-8") == "{not json"
