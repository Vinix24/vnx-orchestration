"""Learning loop as a nightly phase: shadow by default, cutoff, beacon, doctor.

Real code paths against tmp stores only. No provider, claude or launchd process
is started and nothing touches ~/.vnx-data.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402
import vnx_doctor  # noqa: E402

NOW = datetime.now(timezone.utc)
PRE_CUTOFF = "2026-09-01T10:00:00+00:00"
BEACON = "learning_loop_nightly_beacon.json"

_SCHEMA = """
CREATE TABLE pattern_usage (
    pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0, ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP, last_offered TIMESTAMP, confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, pattern_data TEXT NOT NULL, code_example TEXT,
    prerequisites TEXT, outcomes TEXT, success_rate REAL DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0, avg_completion_time INTEGER,
    confidence_score REAL DEFAULT 0.0, source_dispatch_ids TEXT, source_receipts TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP, last_used DATETIME,
    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP, valid_until DATETIME);
CREATE TABLE antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, pattern_data TEXT NOT NULL, problem_example TEXT,
    why_problematic TEXT NOT NULL, better_alternative TEXT,
    occurrence_count INTEGER DEFAULT 0, avg_resolution_time INTEGER,
    severity TEXT DEFAULT 'medium', source_dispatch_ids TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP, last_seen DATETIME,
    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP, valid_until DATETIME);
CREATE TABLE prevention_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    tag_combination TEXT NOT NULL, rule_type TEXT NOT NULL, description TEXT NOT NULL,
    recommendation TEXT NOT NULL, confidence REAL DEFAULT 0.0, created_at TEXT NOT NULL,
    triggered_count INTEGER DEFAULT 0, last_triggered TEXT, source_dispatch_id TEXT,
    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP, valid_until DATETIME);
"""


def _failure(task: str, ts: str) -> dict:
    return {
        "task_id": task,
        "status": "failed",
        "provider": "claude",
        "terminal": "T1",
        "failure_reason": "ImportError: module gone",
        "timestamp": ts,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp state dir with a seeded DB and receipts, ensure_env patched onto it."""
    monkeypatch.delenv("VNX_LEARNING_LOOP_PERSIST", raising=False)
    monkeypatch.delenv("VNX_LEARNING_LOOP_CUTOFF", raising=False)
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
        "confidence, last_used, updated_at) VALUES ('p-used','Used','h1',3,1.0,?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
        "ignored_count, confidence, last_offered) VALUES ('p-ign','Ignored','h2',0,1,1.0,?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
        "confidence) VALUES ('p-stale','Stale','h3',0,0.1)"
    )
    conn.commit()
    conn.close()

    receipts = [
        _failure("t-old", PRE_CUTOFF),
        _failure("t-new-1", NOW.isoformat()),
        _failure("t-new-2", (NOW - timedelta(minutes=5)).isoformat()),
    ]
    (state_dir / "t0_receipts.ndjson").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8"
    )
    with patch.object(ll, "ensure_env", return_value=paths):
        yield state_dir, paths


def _snapshot(state_dir: Path) -> dict:
    return {
        str(p.relative_to(state_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(state_dir.rglob("*"))
        if p.is_file()
    }


def _run_cycle(**kwargs):
    loop = ll.LearningLoop()
    with patch.object(
        ll, "evaluate_activation_gate",
        return_value={"action": "run", "probe_health": "ok", "detail": "test"},
    ):
        report = loop.daily_learning_cycle(**kwargs)
    loop.conn.close()
    return report


# (a) shadow mode persists nothing
def test_shadow_run_leaves_db_and_state_byte_identical(env):
    state_dir, _ = env
    before = _snapshot(state_dir)
    report = _run_cycle()
    assert _snapshot(state_dir) == before
    assert report["mode"] == "shadow"
    # The loop still computed: the recurring failure became a proposal.
    assert len(report["shadow"]["proposals"]) == 1
    assert report["shadow"]["archival_candidates"][0]["pattern_id"] == "p-stale"


# (b) the flag turns persisting on
def test_persist_flag_writes_db_and_state(env, monkeypatch):
    state_dir, _ = env
    monkeypatch.setenv("VNX_LEARNING_LOOP_PERSIST", "1")
    before = _snapshot(state_dir)
    report = _run_cycle()
    after = _snapshot(state_dir)
    assert report.get("mode") == "persist"
    assert after[ "quality_intelligence.db"] != before["quality_intelligence.db"]
    assert (state_dir / "pending_rules.json").exists()
    assert (state_dir / "pending_archival.json").exists()
    rules = json.loads((state_dir / "pending_rules.json").read_text())["pending_rules"]
    assert len(rules) == 1


# (c) receipts from before the cutoff do not count
def test_receipt_before_cutoff_is_ignored_even_from_history(env):
    loop = ll.LearningLoop()
    failures = loop.extract_failure_patterns(
        start_time=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    assert sorted(f["task"] for f in failures) == ["t-new-1", "t-new-2"]
    assert loop.receipt_stats["read"] == 3
    assert loop.receipt_stats["before_cutoff"] == 1


def test_cutoff_env_override_lets_older_receipts_in(env, monkeypatch):
    monkeypatch.setenv("VNX_LEARNING_LOOP_CUTOFF", "2026-08-01T00:00:00Z")
    loop = ll.LearningLoop()
    failures = loop.extract_failure_patterns(
        start_time=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    assert len(failures) == 3


def test_unparseable_cutoff_override_raises(env, monkeypatch):
    monkeypatch.setenv("VNX_LEARNING_LOOP_CUTOFF", "gisteren")
    with pytest.raises(ValueError):
        ll.LearningLoop()


# (d) the beacon is written per run, also when the run raises
def _run_phase(state_dir: Path) -> int:
    import learning_loop_nightly

    with patch.object(
        ll, "evaluate_activation_gate",
        return_value={"action": "run", "probe_health": "ok", "detail": "test"},
    ), patch.object(learning_loop_nightly, "resolve_project_id", return_value="vnx-dev"):
        return learning_loop_nightly.run_phase(state_dir)


def test_phase_writes_ok_beacon_and_shadow_report(env):
    state_dir, _ = env
    assert _run_phase(state_dir) == 0
    beacon = json.loads((state_dir / BEACON).read_text())
    assert beacon["status"] == "ok"
    assert beacon["mode"] == "shadow"
    assert beacon["cutoff"].startswith("2026-09-28T16:41:11")
    assert beacon["started_at"] and beacon["finished_at"]
    assert beacon["error"] is None
    assert beacon["counts"]["receipts_read"] == 3
    assert beacon["counts"]["receipts_after_cutoff"] == 2
    assert beacon["counts"]["patterns"] == 2
    assert beacon["counts"]["proposals"] >= 1
    assert beacon["counts"]["skipped"] == 1
    report = json.loads((state_dir / "learning_loop_shadow_report.json").read_text())
    assert report["report"]["mode"] == "shadow"


def test_phase_writes_failed_beacon_on_exception(env):
    state_dir, _ = env
    with patch.object(ll.LearningLoop, "daily_learning_cycle", side_effect=RuntimeError("kapot")):
        assert _run_phase(state_dir) == 1
    beacon = json.loads((state_dir / BEACON).read_text())
    assert beacon["status"] == "failed"
    assert "kapot" in beacon["error"]
    assert beacon["finished_at"]


def test_phase_writes_failed_beacon_on_bad_cutoff(env, monkeypatch):
    state_dir, _ = env
    monkeypatch.setenv("VNX_LEARNING_LOOP_CUTOFF", "nonsense")
    assert _run_phase(state_dir) == 1
    beacon = json.loads((state_dir / BEACON).read_text())
    assert beacon["status"] == "failed"
    assert "VNX_LEARNING_LOOP_CUTOFF" in beacon["error"]


# (e) doctor
def _write_beacon(state_dir: Path, age_hours: float, status: str = "ok", project_id="vnx-dev"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    (state_dir / BEACON).write_text(json.dumps({
        "component": "learning_loop_nightly", "project_id": project_id,
        "started_at": ts, "finished_at": ts, "mode": "shadow", "status": status,
        "error": "boem" if status == "failed" else None,
    }))


def test_doctor_fails_on_a_37_hour_old_beacon(env):
    state_dir, paths = env
    _write_beacon(state_dir, 37)
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.FAIL


def test_doctor_passes_on_a_fresh_beacon(env):
    state_dir, paths = env
    _write_beacon(state_dir, 1)
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.PASS


def test_doctor_warns_on_failed_status(env):
    state_dir, paths = env
    _write_beacon(state_dir, 1, status="failed")
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.WARN
    assert "boem" in result.message


def test_doctor_fails_on_missing_beacon_when_job_runs_here(env):
    state_dir, paths = env
    (state_dir / "conversation_analyzer.log").write_text("[x] run\n")
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.FAIL


def test_doctor_passes_when_job_never_ran_here(env):
    _, paths = env
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.PASS


def test_doctor_ignores_a_beacon_of_another_project(env):
    state_dir, paths = env
    (state_dir / "conversation_analyzer.log").write_text("[x] run\n")
    _write_beacon(state_dir, 1, project_id="other-proj")
    (result,) = vnx_doctor.check_learning_loop_beacon(paths)
    assert result.status == vnx_doctor.FAIL


# (f) the nightly job calls the phase
_SCRIPT = _REPO_ROOT / "scripts" / "conversation_analyzer_nightly.sh"
_STUB_PHASES = (
    "quality_db_init", "conversation_analyzer", "link_sessions_dispatches",
    "generate_t0_session_brief", "governance_aggregator", "learning_loop_nightly",
    "generate_suggested_edits", "send_digest_email",
)


def _run_job(tmp_path: Path, **stub_exits: int):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "lib").symlink_to(_REPO_ROOT / "scripts" / "lib")
    shutil.copy(_SCRIPT, scripts / _SCRIPT.name)
    for phase in _STUB_PHASES:
        (scripts / f"{phase}.py").write_text(
            "import os, sys\n"
            f"print('STUB {phase} persist=' + os.environ.get('VNX_LEARNING_LOOP_PERSIST', 'unset'))\n"
            f"sys.exit(int(os.environ.get('STUB_EXIT_{phase}', '0')))\n",
            encoding="utf-8",
        )
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    pgrep = fakebin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pgrep.chmod(pgrep.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    job_env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(home),
        "VNX_DATA_DIR": str(data),
        "VNX_STATE_DIR": str(data / "state"),
    }
    job_env.update({f"STUB_EXIT_{k}": str(v) for k, v in stub_exits.items()})
    proc = subprocess.run(
        ["/bin/bash", str(scripts / _SCRIPT.name)],
        env=job_env, capture_output=True, text=True, timeout=60,
    )
    log = (data / "state" / "conversation_analyzer.log").read_text(encoding="utf-8")
    return proc, log


def test_nightly_job_runs_the_learning_loop_phase_between_analyzer_and_digest(tmp_path):
    proc, log = _run_job(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "STUB learning_loop_nightly persist=unset" in proc.stdout
    assert "Phase 2.6 complete" in log
    assert log.index("Phase 1 complete") < log.index("Phase 2.6") < log.index("Phase 4")


def test_nightly_job_warns_loudly_but_completes_when_the_phase_fails(tmp_path):
    proc, log = _run_job(tmp_path, learning_loop_nightly=1)
    assert proc.returncode == 0, proc.stderr
    assert "Phase 2.6 WARNING" in log
    assert "status=failed" in log
    assert "Nightly analysis pipeline complete" in log
