"""Characterisation of ``consolidator.run_dream_cycle`` (golf schoon R04).

Pins the observable behaviour: result dict, pending-review file, event order, the
``dream_cycles`` row, the stdout line, and the skip/timeout/failure exits. Every store
lives under ``tmp_path``; only the provider call is replaced.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "scripts/lib", "scripts/dream"):
    sys.path.insert(0, str(REPO_ROOT / sub))

import consolidator  # noqa: E402

PROJECT = "vnx-dev"
CONSOLIDATION = {
    "merged": [{"a": 1}, {"a": 2}], "dropped": [{"id": 1}], "archived": [], "flagged": [{"f": 1}],
    "summary": "s",
}
SCHEMA = """
CREATE TABLE dream_cycles (cycle_id TEXT NOT NULL, project_id TEXT NOT NULL, started_at TEXT,
    completed_at TEXT, status TEXT, provider TEXT NOT NULL DEFAULT 'kimi', insights_input INTEGER,
    merged_count INTEGER, dropped_count INTEGER, archived_count INTEGER, flagged_count INTEGER,
    operator_reviewed INTEGER DEFAULT 0, report_path TEXT, PRIMARY KEY (cycle_id, project_id));
CREATE TABLE success_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    title TEXT, description TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE antipatterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    title TEXT, description TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP);
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    boundary = tmp_path / "boundary.json"
    boundary.write_text(json.dumps({"version": 1}), encoding="utf-8")
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(boundary))
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(tmp_path / "switch" / "providers.json"))
    monkeypatch.setenv("VNX_DREAM_SCHEDULER_ENABLED", "1")
    monkeypatch.delenv("VNX_DREAM_PROVIDER", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_DREAM_PROVIDER", raising=False)
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "ok")
    data_root = tmp_path / ".vnx-data"
    (data_root / "state").mkdir(parents=True)
    (data_root / "state" / "t0_receipts.ndjson").write_text(
        json.dumps({"dispatch_id": "d", "status": "success",
                    "timestamp": consolidator._utc_timestamp()}) + "\n")
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO success_patterns (project_id, title) VALUES (?, 'seeded')", (PROJECT,))
    conn.commit()
    conn.close()
    calls: list[tuple] = []
    behaviour = {"raise": None}

    def fake_dispatch(patterns, project_id, provider, timeout, data_root=None):
        calls.append((patterns, project_id, provider, timeout))
        if behaviour["raise"] is not None:
            raise behaviour["raise"]
        return CONSOLIDATION

    monkeypatch.setattr(consolidator, "_dispatch_consolidation", fake_dispatch)

    def events():
        out = []
        for path in sorted((data_root / "events" / "dream").glob("*.ndjson")):
            out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        return out

    def rows():
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT cycle_id, project_id, status, provider, insights_input,"
                                " merged_count, dropped_count, archived_count, flagged_count,"
                                " report_path FROM dream_cycles").fetchall()
        finally:
            conn.close()

    def run(**kwargs):
        return consolidator.run_dream_cycle(PROJECT, db_path, data_root=data_root, **kwargs)

    from types import SimpleNamespace
    return SimpleNamespace(run=run, events=events, rows=rows, calls=calls, behaviour=behaviour,
                           data_root=data_root, db_path=db_path)


def test_completed_cycle_result_review_events_row_and_stdout(world, capsys):
    result = world.run()
    out = capsys.readouterr().out
    assert out.startswith(f"dream run: cycle={result['cycle_id']} project={PROJECT}")
    assert set(result) == {
        "cycle_id", "probe_health", "input_count", "restricted_rows_dropped", "merged_count",
        "dropped_count", "archived_count", "flagged_count", "review_path"}
    assert result["probe_health"] == "ok" and result["input_count"] == 1
    zero = {"antipatterns": 0, "intelligence_injections": 0, "success_patterns": 0}
    assert result["restricted_rows_dropped"] == zero
    assert (result["merged_count"], result["dropped_count"],
            result["archived_count"], result["flagged_count"]) == (2, 1, 0, 1)
    review_path = Path(result["review_path"])
    assert review_path == world.data_root / "state" / "dream" / f"{result['cycle_id']}-pending-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    assert review["requires_operator_review"] is True and "mode" not in review
    assert review["consolidation"] == CONSOLIDATION and review["input_count"] == 1
    assert review["probe_health"] == "ok" and review["created_at"]
    assert [e["event_type"] for e in world.events()] == ["dream_cycle_started", "dream_cycle_completed"]
    started, completed = world.events()
    assert started["provider"] == "deepseek" and started["probe_health"] == "ok"
    assert completed["merged_count"] == 2 and completed["review_path"] == str(review_path)
    assert completed["input_count"] == 1 and completed["restricted_rows_dropped"] == zero
    assert world.rows() == [(result["cycle_id"], PROJECT, "completed", "deepseek", 1, 2, 1, 0, 1,
                             str(review_path))]


def test_provider_receives_patterns_project_provider_and_timeout(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_TIMEOUT", "42")
    world.run()
    patterns, project, provider, timeout = world.calls[0]
    assert project == PROJECT and provider == "deepseek" and timeout == 42.0
    assert [r["title"] for r in patterns["success_patterns"]] == ["seeded"]


def test_dry_run_writes_review_and_event_but_no_row(world):
    result = world.run(dry_run=True)
    assert Path(result["review_path"]).exists()
    assert world.rows() == []
    assert world.events()[-1]["event_type"] == "dream_cycle_completed"


def test_degraded_probe_runs_as_proposal_only(world, monkeypatch):
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "degraded")
    result = world.run()
    assert result["probe_health"] == "degraded"
    review = json.loads(Path(result["review_path"]).read_text(encoding="utf-8"))
    assert review["mode"] == "proposal_only" and review["probe_health"] == "degraded"


def test_scheduler_disabled_skips_before_anything_else(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_SCHEDULER_ENABLED", "0")
    result = world.run()
    assert result["status"] == "skipped" and result["reason"] == "scheduler_disabled"
    assert result["detail"] == "VNX_DREAM_SCHEDULER_ENABLED=0"
    assert world.calls == [] and world.rows() == []
    assert [e["event_type"] for e in world.events()] == ["dream_cycle_skipped"]


def test_probe_not_ok_skips_with_probe_health(world, monkeypatch):
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "produces_crap")
    result = world.run()
    assert result["reason"] == "probe_not_ok" and result["probe_health"] == "produces_crap"
    assert world.calls == [] and world.rows() == []


def test_stale_receipts_skip_as_incomplete_data(world):
    (world.data_root / "state" / "t0_receipts.ndjson").write_text("")
    result = world.run()
    assert result["status"] == "skipped" and result["reason"] == "incomplete_data"
    assert result["detail"] and world.calls == []
    assert "dream_cycle_started" not in [e["event_type"] for e in world.events()]


def test_no_patterns_skips_without_calling_the_provider(world):
    conn = sqlite3.connect(str(world.db_path))
    conn.execute("DELETE FROM success_patterns")
    conn.commit()
    conn.close()
    result = world.run()
    assert result["status"] == "skipped" and world.calls == [] and world.rows() == []


def test_timeout_returns_timeout_result_and_writes_no_review_or_row(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_TIMEOUT", "7")
    world.behaviour["raise"] = subprocess.TimeoutExpired("deepseek", 7)
    result = world.run()
    assert result == {"status": "timeout", "cycle_id": result["cycle_id"],
                      "reason": "deepseek_timeout", "provider": "deepseek", "timeout_seconds": 7}
    assert [e["event_type"] for e in world.events()] == ["dream_cycle_started", "dream_cycle_timeout"]
    assert world.rows() == []
    assert not (world.data_root / "state" / "dream").exists()


def test_failure_reraises_after_failed_event_and_leaves_connection_usable(world):
    world.behaviour["raise"] = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        world.run()
    failed = [e for e in world.events() if e["event_type"] == "dream_cycle_failed"]
    assert len(failed) == 1 and failed[0]["error"] == "boom" and failed[0]["provider"] == "deepseek"
    assert world.rows() == []
    world.behaviour["raise"] = None
    assert world.run()["merged_count"] == 2
