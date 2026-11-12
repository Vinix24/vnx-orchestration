#!/usr/bin/env python3
"""tests/test_learning_status_inventory.py — loop D4: `vnx learning status` shows the
corpus, the three queues and the access path. Every fixture is built under
``tmp_path``; no test opens ~/.vnx-data or a database. The first test is the old-code
red run: it calls ``_cmd_status`` (present on the base) and asserts the four new
blocks, so it fails as an assertion, never an ImportError.
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(_REPO_ROOT))
from vnx_cli.commands import learning  # noqa: E402
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
@pytest.fixture
def inventory():
    import learning_inventory  # noqa: PLC0415 — the red run must import nothing new
    return learning_inventory
def _line_starting(text, prefix):
    return next((line for line in text.splitlines() if line.startswith(prefix)), None)
def _run_status(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(learning, "_resolve_state_dir", lambda _pd: state_dir)
    rc = learning._cmd_status(argparse.Namespace(project_dir=str(tmp_path)))
    out, err = capsys.readouterr()
    return rc, out, err, state_dir
def _receipt(dispatch_id, status, stamp, **extra):
    body = {"dispatch_id": dispatch_id, "status": status,
            "timestamp": stamp.isoformat() if isinstance(stamp, datetime) else stamp}
    body.update(extra)
    return body
def _write_receipts(state_dir, *lines):
    state_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    (state_dir / "t0_receipts.ndjson").write_text(text + "\n", encoding="utf-8")
def _write_json(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
def test_status_prints_the_four_blocks_and_keeps_the_counters(tmp_path, capsys, monkeypatch):
    rc, out, _, _ = _run_status(tmp_path, monkeypatch, capsys)
    assert rc == 0
    for block in ("corpus:", "queues:", "access:", "decisions:"):
        assert _line_starting(out, block) is not None, f"missing {block} in:\n{out}"
    for counter in ("Pending rules (awaiting operator approval): 0",
                    "Approved rules (ready for ingest):          0",
                    "Pending archival candidates:                0",
                    "Pending supersede candidates (G-L4 gated):  0",
                    "Handled (approved/dismissed, out of queue): 0",
                    "Pending skill refinements:                  0"):
        assert counter in out
def test_corpus_class_distribution_noise_and_coverage(tmp_path, inventory):
    state_dir = tmp_path / "state"
    inside, outside = NOW - timedelta(hours=1), NOW - timedelta(hours=48)
    _write_receipts(
        state_dir,
        _receipt("d-timeout", "failed", inside, failure_reason="deadline exceeded 10m"),
        _receipt("d-merged", "failed", inside, event_type="dispatch_finished"),
        _receipt("d-merged", "failed", inside, event_type="subprocess_completion",
                 failure_reason="boom"),
        _receipt("d-empty", "failed", inside),
        _receipt("d-pytest", "failed", inside, source="pytest"),
        _receipt("d-foreign", "failed", inside, project_id="other-project"),
        "not-json",
        _receipt("d-old", "failed", outside),
    )
    summary = inventory.corpus_summary(state_dir, "tproj", now=NOW)
    assert summary["error"] is None and summary["dispatches"] == 3
    assert summary["class_counts"]["timeout"] == 1
    assert summary["class_counts"]["failed"] == 1
    assert summary["class_counts"]["unclassified"] == 1
    assert summary["noise"] == 2
    assert summary["noise_reasons"] == {"pytest": 1, "foreign_project": 1}
    assert summary["unreadable"] == 1
    assert summary["coverage"] == pytest.approx(2 / 3, abs=5e-5)
def test_corpus_fails_closed_without_usable_evidence(tmp_path, inventory):
    missing = inventory.corpus_summary(tmp_path / "state", "tproj", now=NOW)
    assert missing["error"] == "missing" and missing["coverage"] is None
    state_dir = tmp_path / "state"
    _write_receipts(state_dir, _receipt("d-no-ts", "failed", "not-a-time"),
                    _receipt("d-empty", "failed", NOW - timedelta(hours=1)))
    summary = inventory.corpus_summary(state_dir, "tproj", now=NOW)
    assert summary["unreadable"] == 1 and summary["dispatches"] == 1
    assert summary["coverage"] == 0.0  # one unclassified group is not a pass
def test_queues_count_pending_and_age_of_oldest(tmp_path, inventory):
    state_dir = tmp_path / "state"
    _write_json(state_dir / "pending_rules.json", {"pending_rules": [
        {"id": "rule-1", "status": "pending", "created_at": "2026-10-30T12:00:00+00:00"},
        {"id": "rule-2", "status": "pending", "created_at": "2026-11-09T12:00:00+00:00"},
        {"id": "rule-3", "status": "approved", "created_at": "2026-06-01T00:00:00+00:00"}]})
    _write_json(state_dir / "pending_archival.json", {"pending_archival": [
        {"pattern_id": "pat-1", "status": "pending", "queued_at": "2026-06-16T08:00:00Z"}]})
    _write_json(state_dir / "pending_edits.json", {"edits": [
        {"id": 1, "status": "pending", "suggested_at": "2026-11-01T00:00:00Z"},
        {"id": 2, "status": "pending"}]})
    summary = inventory.queues_summary(state_dir, now=NOW)
    assert summary["proposals"]["pending"] == 2 and summary["proposals"]["age_days"] == 11
    assert summary["archival"]["age_days"] == (
        NOW - datetime(2026, 6, 16, 8, tzinfo=timezone.utc)).days
    assert summary["suggest-edits"]["pending"] == 2
    assert summary["suggest-edits"]["without_timestamp"] == 1
    assert summary["suggest-edits"]["age_days"] == (
        NOW - datetime(2026, 11, 1, tzinfo=timezone.utc)).days
def test_queues_missing_is_empty_and_malformed_is_unreadable(tmp_path, inventory):
    missing = inventory.queues_summary(tmp_path / "state", now=NOW)
    for key in ("proposals", "archival", "suggest-edits"):
        assert missing[key]["pending"] == 0 and missing[key]["error"] is None
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "pending_archival.json").write_text("{not json", encoding="utf-8")
    (state_dir / "pending_rules.json").write_text("[1, 2]", encoding="utf-8")
    summary = inventory.queues_summary(state_dir, now=NOW)
    assert summary["archival"]["pending"] is None and "unreadable" in summary["archival"]["error"]
    assert summary["proposals"]["pending"] is None and summary["proposals"]["error"]
def test_access_lists_the_three_paths_and_the_antipatterns_marker(tmp_path, inventory):
    state_dir = tmp_path / "state"
    summary = inventory.access_summary(state_dir, tmp_path)
    assert [entry["queue"] for entry in summary["paths"]] == [
        "proposals", "archival", "suggest-edits"]
    assert all(entry["command"] for entry in summary["paths"])
    assert summary["antipatterns_exists"] is False
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "antipatterns.md").write_text("# Prevention\n", encoding="utf-8")
    assert inventory.access_summary(state_dir, tmp_path)["antipatterns_exists"] is True
def _seed_decision(state_dir):
    import prevention_rule_store as store  # noqa: PLC0415
    proposals = [{"pattern": "p1", "terminal_constraint": "T1", "failure_class": "timeout",
                  "prevention": "prevent", "confidence": 0.8, "occurrence_count": 3,
                  "source_dispatch_ids": ["d-1", "d-2"]}]
    store.queue_rules(state_dir, proposals, project_id="tproj", now=NOW.isoformat())
    store.decide(state_dir, [store.rule_id(proposals[0])], "rejected", reason="r",
                 decided_by="v", project_id="tproj", now=NOW.isoformat())
def test_decisions_count_kind_and_render_queue_mode(tmp_path, inventory, monkeypatch):
    state_dir = tmp_path / "state"
    _seed_decision(state_dir)
    monkeypatch.setenv("VNX_LEARNING_LOOP_QUEUE", "1")
    summary = inventory.decisions_summary(state_dir, queue_mode=inventory._queue_mode())
    assert summary["counts"] == {"rejected": 1} and summary["total"] == 1
    lines = []
    inventory.print_status_blocks(state_dir, tmp_path, project_id="tproj", now=NOW,
                                  out=lines.append)
    text = "\n".join(lines)
    assert _line_starting(text, "decisions:").endswith("queue mode: on")
    assert "does not exist" in text  # the antipatterns marker
    assert "  archival: 0 pending (file missing)" in text
def test_decisions_unreadable_is_reported_and_rendered_as_unreadable(tmp_path, inventory):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "prevention_rule_decisions.ndjson").write_text("not-json\n", encoding="utf-8")
    summary = inventory.decisions_summary(state_dir, queue_mode=False)
    assert summary["counts"] is None and summary["error"]
    lines = []
    inventory.print_status_blocks(state_dir, tmp_path, project_id="tproj", now=NOW,
                                  out=lines.append)
    line = _line_starting("\n".join(lines), "decisions:")
    assert line is not None and "UNREADABLE" in line
