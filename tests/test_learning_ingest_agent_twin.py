#!/usr/bin/env python3
"""d3filter D2 (dlv-a82252463c87): a collapsed ingest is visible (B12)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_ledger_gate as gate
import learning_loop as ll  # noqa: E402
import prevention_rule_store as store
import quality_db_init as qdi

PID = "vnx-dev"


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


@pytest.fixture
def env(tmp_path, approve_learning_persist, monkeypatch):
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    fake = _FakePaths(state_dir, tmp_path / "repo")
    approve_learning_persist(state_dir)
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    assert qdi.bootstrap_qi_db(state_dir / "quality_intelligence.db") is True  # v35: pattern_activation
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate.write_verdict(state_dir, {"schema": gate.SCHEMA, "evaluated_at": at, "project_id": PID, "cohort": "all_v3",
                                   "status": "ok", "reason_code": "ok", "reason": "fixture", "counts": {}})
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop(persist=True)
        loop.conn.execute("ALTER TABLE prevention_rules ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        loop.conn.execute("ALTER TABLE prevention_rules ADD COLUMN source TEXT")
        loop.conn.commit()
        yield loop, state_dir
    loop.conn.close()


def _rule(rid, agent, terminal="T1", **extra):
    rule = {"id": rid, "status": "approved", "pattern": "same failure", "failure_class": "timeout",
            "prevention": "do x", "confidence": 0.8, "occurrence_count": 2,
            "terminal_constraint": terminal, "agent_constraint": agent}
    rule.update(extra)
    return rule


def _run(loop, state_dir, rules):
    """Queue the rules through the store and approve the ``approved`` ones in its record (loop D9)."""
    store.queue_rules(state_dir, rules, project_id=PID, now="2026-11-11T08:00:00+00:00")
    approved = [store.rule_id(r) for r in rules if r["status"] == "approved"]
    if approved:
        store.decide(state_dir, approved, "approved", reason="r", decided_by="v", project_id=PID,
                     now="2026-11-11T09:00:00+00:00", effect_criterion="e", stop_condition="s")
    loop.ingest_approved_rules()
    return store.load_queue(state_dir)


def _rows(loop):
    return loop.conn.execute("SELECT id FROM prevention_rules").fetchall()


def test_other_agent_twin_is_marked_duplicate(env, capsys):
    loop, state_dir = env
    out = _run(loop, state_dir, [_rule("a", "claude"), _rule("b", "deepseek")])
    assert [r["status"] for r in out] == ["ingested", "ingested"]
    assert len(_rows(loop)) == 1
    assert out[0]["ingest_result"] == "inserted"
    assert out[1]["ingest_result"] == "duplicate_of_existing_row"
    assert out[1]["ingest_row_id"] == out[0]["ingest_row_id"] == _rows(loop)[0][0]
    assert "1 collapsed" in capsys.readouterr().out


def test_rule_without_agent_constraint_is_inserted(env):
    loop, state_dir = env
    rule = _rule("a", None)
    del rule["agent_constraint"]
    out = _run(loop, state_dir, [rule])
    assert out[0]["ingest_result"] == "inserted"
    assert len(_rows(loop)) == 1


@pytest.mark.parametrize("extra", [{"terminal_constraint": "T2"}, {"prevention": "do y"}])
def test_other_terminal_is_a_new_row(env, extra):
    loop, state_dir = env
    out = _run(loop, state_dir, [_rule("a", "claude", "T1"), _rule("b", "deepseek", **extra)])
    assert [r["ingest_result"] for r in out] == ["inserted", "inserted"]
    assert len(_rows(loop)) == 2


def test_non_approved_rule_is_untouched(env):
    loop, state_dir = env
    out = _run(loop, state_dir, [_rule("a", "claude", status="pending")])
    assert "ingest_result" not in out[0]
    assert out[0]["status"] == "pending"
    assert _rows(loop) == []
