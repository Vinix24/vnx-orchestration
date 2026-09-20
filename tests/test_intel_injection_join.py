#!/usr/bin/env python3
"""Tests for the intelligence injection join + adoption-rate script.

Dispatch: 20260920-171500-intel-lus — PUNT 2 (join) + PUNT 3a (adoption).

The tests run the real script via subprocess against a synthetic store, so
they exercise the actual ATTACH-join and adoption SQL, not a re-implementation.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "intel_injection_join.py"


def _make_store(tmp_path: Path) -> Path:
    """Build a minimal quality_intelligence.db + runtime_coordination.db
    with the three joined tables populated with a known mix."""
    qi = tmp_path / "quality_intelligence.db"
    rc = tmp_path / "runtime_coordination.db"

    q = sqlite3.connect(str(qi))
    q.execute("""CREATE TABLE dispatch_pattern_offered (
        dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL,
        pattern_title TEXT NOT NULL, offered_at TEXT NOT NULL,
        PRIMARY KEY (dispatch_id, pattern_id))""")
    q.execute("""CREATE TABLE pattern_injection_outcome (
        id INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL,
        pattern_hash TEXT, used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0,1)),
        reason TEXT, evidence TEXT, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
        created_at TEXT NOT NULL, UNIQUE (project_id, dispatch_id, pattern_id))""")
    # offers: 3 dispatches, each with 1 offer
    q.execute("INSERT INTO dispatch_pattern_offered VALUES ('d1','pat-a','Pattern A','2026-09-20T10:00:00Z')")
    q.execute("INSERT INTO dispatch_pattern_offered VALUES ('d2','pat-a','Pattern A','2026-09-20T11:00:00Z')")
    q.execute("INSERT INTO dispatch_pattern_offered VALUES ('d3','pat-b','Pattern B','2026-09-20T12:00:00Z')")
    # outcomes: d1 used=1, d2 used=0 with reason, d3 none yet
    q.execute("INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, pattern_hash, used, reason, evidence, project_id, created_at) VALUES ('d1','pat-a','pat-a',1,NULL,NULL,'vnx-dev','2026-09-20T10:05:00Z')")
    q.execute("INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, pattern_hash, used, reason, evidence, project_id, created_at) VALUES ('d2','pat-a','pat-a',0,'wrong-file-affinity','ev','vnx-dev','2026-09-20T11:05:00Z')")
    q.commit()
    q.close()

    r = sqlite3.connect(str(rc))
    r.execute("""CREATE TABLE intelligence_injections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, injection_id TEXT NOT NULL UNIQUE,
        dispatch_id TEXT NOT NULL, injection_point TEXT NOT NULL,
        task_class TEXT NOT NULL, items_injected INTEGER NOT NULL DEFAULT 0,
        items_suppressed INTEGER NOT NULL DEFAULT 0, payload_chars INTEGER NOT NULL DEFAULT 0,
        items_json TEXT NOT NULL DEFAULT '[]', suppressed_json TEXT NOT NULL DEFAULT '[]',
        injected_at TEXT NOT NULL)""")
    r.execute("INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, items_injected, injected_at) VALUES ('i1','d1','dispatch_create','backend-developer',1,'2026-09-20T10:00:00Z')")
    r.execute("INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, items_injected, injected_at) VALUES ('i2','d2','dispatch_create','backend-developer',2,'2026-09-20T11:00:00Z')")
    r.commit()
    r.close()
    return qi.parent


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(cwd),
    )


class TestJoin:
    def test_join_shows_offered_used_ignored_with_reason(self, tmp_path):
        state = _make_store(tmp_path)
        qi = state / "quality_intelligence.db"
        rc = state / "runtime_coordination.db"
        proc = _run("join", "--qi", str(qi), "--rc", str(rc), "--limit", "10", cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        # header reports the totals
        assert "offers: 3" in out
        assert "outcome rows: 2" in out
        # d1 -> used yes, no reason
        assert "d1" in out
        assert "yes" in out
        # d2 -> used no, reason wrong-file-affinity
        assert "d2" in out
        assert "no" in out
        assert "wrong-file-affinity" in out
        # d3 -> no outcome row yet, used = ?
        assert "d3" in out
        # items_injected from intelligence_injections joins through (d1=1, d2=2)
        assert " 1" in out

    def test_join_without_rc_db_still_runs(self, tmp_path):
        state = _make_store(tmp_path)
        qi = state / "quality_intelligence.db"
        # Point --rc at a nonexistent path; join must still run (no injection column).
        proc = _run("join", "--qi", str(qi), "--rc",
                    str(state / "nope.db"), "--limit", "5", cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert "offers: 3" in proc.stdout


class TestAdoption:
    def test_adoption_rate_per_pattern_and_over_time(self, tmp_path):
        state = _make_store(tmp_path)
        qi = state / "quality_intelligence.db"
        proc = _run("adoption", "--qi", str(qi), "--limit", "10", cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        # overall totals
        assert "offered: 3" in out
        assert "used: 1" in out
        assert "ignored: 1" in out
        # per-pattern: pat-a offered 2, used 1, ignored 1, adopt% 50.0
        assert "pat-a" in out
        # pat-b offered 1, used 0, ignored 0, adopt% 0.0
        assert "pat-b" in out
        # 50.0 must appear (pat-a's adoption rate)
        assert "50.0" in out
        # over-time section present
        assert "Over time" in out
        # SQLite %W is 0-based ISO week; 2026-09-20 falls in W37.
        assert "2026-W37" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
