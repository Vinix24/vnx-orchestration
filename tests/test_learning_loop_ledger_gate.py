"""D7 (self-learning-loop-reactivate): the loop reads ledger_source_check before it learns.

In queue mode and persist mode ``daily_learning_cycle`` asks ``ledger_verdict/1`` about the very
ledger it learns from, before the extraction. ``refused`` stops the run with the reason and writes
nothing to the queue; ``pending`` runs on without the dispatches still in flight; a green ledger on
the next run resumes without a hand. Shadow without queue mode never reads the verdict. The real
writers fill a tmp_path ledger (the D6 fixtures); nothing touches ~/.vnx-data.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import DEFAULT, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts/lib", "scripts", "tests"):
    sys.path.insert(0, str(VNX_ROOT / sub))

# The imports below need the sys.path entries above. The K9 names include pytest fixtures
# (_hermetic_append, lifecycle_on, state_dir) that pytest finds by name in this module.
import dispatch_start
import learning_ledger_gate as gate
import learning_loop as ll
from append_receipt_internals import lifecycle as append_lifecycle
from test_k9_lifecycle_count import (
    DEADLINE, PROJECT, ROLE, START, _append_raw, _begin, _finish, _hermetic_append, _ledger,
    _sweep_heartbeat, lifecycle_on, state_dir,
)

RUN = {"action": "run", "probe_health": "ok", "detail": "test"}


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """The K9 state dir, under the per-project layout the loop resolves its project from."""
    return tmp_path / "vnx-data" / PROJECT


@pytest.fixture()
def env(state_dir, lifecycle_on, monkeypatch):
    """One ended dispatch and a fresh sweep: on its own this ledger is ``ok``."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    for name in (ll.QUEUE_ENV, ll.PERSIST_ENV, gate.ACTIVATION_ENV):
        monkeypatch.delenv(name, raising=False)
    _finish(state_dir, _begin(state_dir, "d-ok"))
    _sweep_heartbeat(state_dir, datetime.now(timezone.utc) - timedelta(seconds=60))
    root = str(state_dir.parent)
    paths = {"VNX_STATE_DIR": str(state_dir), "VNX_HOME": root, "VNX_DATA_DIR": root,
             "PROJECT_ROOT": root}
    with patch.object(ll, "ensure_env", return_value=paths):
        yield state_dir


def _failures(state_dir: Path, ids) -> None:
    now = datetime.now(timezone.utc)
    for n, dispatch_id in enumerate(ids):
        _append_raw(state_dir, {
            "event_type": "subprocess_completion", "status": "failed", "terminal": "T1",
            "failure_reason": "Exhausted 3 retries", "provider": "claude_code",
            "project_id": PROJECT, "dispatch_id": dispatch_id,
            "timestamp": (now - timedelta(minutes=30 + n)).isoformat()})


def _second_end(state_dir: Path) -> None:
    """A second end line for the ended dispatch: the ledger's ``double_end``."""
    _append_raw(state_dir, {
        "timestamp": START, "event_type": "dispatch_finished", "schema_version": 3,
        "project_id": PROJECT, "dispatch_id": "d-ok", "attempt": 1, "end_state": "completed",
        "head_sha": "d" * 40, "writer_sha": append_lifecycle.writer_sha()})


def _begin_live(state_dir: Path, dispatch_id: str) -> None:
    """A start one minute ago whose door is this very process: the real liveness reads live."""
    spec = state_dir.parent / "dispatches" / "completed" / dispatch_id / "dispatch-spec.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps({"dispatch_id": dispatch_id, "deadline_seconds": DEADLINE}))
    started = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with patch.object(dispatch_start, "_now", lambda: started):
        dispatch_start.begin(project_id=PROJECT, dispatch_id=dispatch_id, role=ROLE,
                             base_sha="a" * 40, spec_file=spec, target_slot="T1",
                             state_dir=state_dir)


def _cycle(loop=None):
    loop = loop or ll.LearningLoop(persist=False)
    with patch.object(ll, "evaluate_activation_gate", return_value=RUN), \
            patch("health_beacon.HealthBeacon") as beacon:
        report = loop.daily_learning_cycle()
    return loop, report, beacon


def _queue(state_dir: Path) -> Path:
    return state_dir / "pending_rules.json"


def _source_ids(state_dir: Path) -> list:
    (row,) = json.loads(_queue(state_dir).read_text())["pending_rules"]
    return row["source_dispatch_ids"]


def test_a_double_end_refuses_queue_mode_leaves_the_queue_and_a_green_ledger_resumes(env,
                                                                                   monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _failures(env, ("d-1", "d-2"))
    loop, first, _ = _cycle()
    assert first.get("status") != "refused" and _source_ids(env) == ["d-1", "d-2"]
    before = _queue(env).read_bytes()
    _failures(env, ("d-3",))
    healthy = _ledger(env).read_text(encoding="utf-8")
    _second_end(env)

    _, report, beacon = _cycle(loop)

    assert (report.get("status"), report.get("reason")) == ("refused",
                                                            "ledger_source_check: double_end")
    assert report["statistics"] == {} and _queue(env).read_bytes() == before
    call = beacon.return_value.heartbeat.call_args.kwargs
    assert (call["status"], call["details"]["reason"]) == ("degraded", report["reason"])
    stored = gate.read_verdict(env)
    assert (stored["status"], stored["reason_code"]) == ("refused", "double_end")

    _ledger(env).write_text(healthy, encoding="utf-8")  # the ledger is green again
    _, resumed, _ = _cycle(loop)
    assert resumed.get("status") != "refused" and resumed["ledger_verdict"]["status"] == "ok"
    assert _source_ids(env) == ["d-1", "d-2", "d-3"]
    assert gate.read_verdict(env)["status"] == "ok"


def test_a_live_dispatch_is_pending_and_left_out_of_the_proposal(env, monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _begin_live(env, "d-live")
    _failures(env, ("d-1", "d-live", "d-2"))

    loop, report, _ = _cycle()

    assert _source_ids(env) == ["d-1", "d-2"]
    assert report.get("status") != "refused" and report["ledger_verdict"]["status"] == "pending"
    assert loop.receipt_stats["in_flight_excluded"] == 1


def test_persist_mode_refuses_before_any_step(env, approve_learning_persist):
    approve_learning_persist(env)
    _failures(env, ("d-1", "d-2"))
    _second_end(env)
    loop = ll.LearningLoop(persist=True)
    steps = ("update_confidence_scores", "persist_to_intelligence_db", "ingest_approved_rules",
             "archive_unused_patterns")
    with patch.multiple(loop, **dict.fromkeys(steps, DEFAULT)) as mocks:
        _, report, _ = _cycle(loop)

    assert (report.get("status"), report.get("reason")) == ("refused",
                                                            "ledger_source_check: double_end")
    assert not _queue(env).exists()  # the extraction did not run: persist queues through the store
    for mock in mocks.values():
        mock.assert_not_called()


def _snapshot(directory: Path) -> dict:
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def test_shadow_without_queue_never_reads_the_verdict(env):
    _failures(env, ("d-1", "d-2"))
    _second_end(env)
    before = _snapshot(env)
    _, report, _ = _cycle()

    assert report.get("status") != "refused" and "ledger_verdict" not in report
    assert _snapshot(env) == before  # no verdict file, no queue: byte-identical


@pytest.mark.parametrize("verdict, code", [
    (None, "malformed_verdict"), ({}, "malformed_verdict"), ("ok", "malformed_verdict"),
    ({"status": None, "reason_code": "ok"}, "malformed_verdict"),
    ({"status": "OK", "reason_code": "ok"}, "malformed_verdict"),
    ({"status": "fine", "reason_code": "ok"}, "malformed_verdict"),
    ({"status": "ok"}, "malformed_verdict"), ({"status": "pending", "reason_code": " "},
                                               "malformed_verdict"),
    ({"status": "refused", "reason_code": None}, "malformed_verdict"),
    ({"status": "refused", "reason_code": "empty_cohort"}, "empty_cohort"),
])
def test_only_a_well_formed_ok_or_pending_runs_on(env, monkeypatch, verdict, code):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _failures(env, ("d-1", "d-2"))
    with patch.object(ll, "_ledger_verdict", return_value=verdict):
        _, report, _ = _cycle()

    assert (report["status"], report["reason"]) == ("refused", f"ledger_source_check: {code}")
    assert not _queue(env).exists()


def test_a_verdict_that_cannot_be_stored_refuses(env, monkeypatch):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _failures(env, ("d-1", "d-2"))
    with patch.object(ll, "write_verdict", side_effect=OSError("disk full")):
        _, report, beacon = _cycle()

    assert report["reason"] == "ledger_source_check: verdict_unwritable"
    assert "disk full" in beacon.return_value.heartbeat.call_args.kwargs["details"]["detail"]
    assert not _queue(env).exists()


@pytest.mark.parametrize("target", ["read_ledger", "attempt_verdict"])
def test_an_unreadable_in_flight_set_under_pending_refuses(env, monkeypatch, target):
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _begin_live(env, "d-live")
    _failures(env, ("d-1", "d-live", "d-2"))
    real = ll.attempt_verdict
    broken = {"read_ledger": {"side_effect": OSError("boom")},
              "attempt_verdict": {"new": lambda *a, **k: replace(real(*a, **k), kind="mystery")}}
    with patch.object(ll, target, **broken[target]):
        _, report, _ = _cycle()

    assert report["reason"] == "ledger_source_check: in_flight_unreadable"
    assert not _queue(env).exists()


def test_a_transient_unreadable_queue_does_not_degrade_the_next_cycle(env, monkeypatch):
    """D2-b carry-over: ``intelligence_daemon`` keeps one loop; ``queue_error`` resets per cycle."""
    monkeypatch.setenv(ll.QUEUE_ENV, "1")
    _failures(env, ("d-1", "d-2"))
    _queue(env).write_text("{not json", encoding="utf-8")
    loop, first, _ = _cycle()
    _queue(env).unlink()  # the queue is readable again

    _, second, _ = _cycle(loop)

    assert first.get("status") == "degraded" and first.get("reason")
    assert second.get("status") != "degraded" and "reason" not in second
    assert loop.queue_error is None and _source_ids(env) == ["d-1", "d-2"]
