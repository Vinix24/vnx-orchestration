#!/usr/bin/env python3
"""d3filter D2 (dlv-a82252463c87): a collapsed ingest is visible (B12)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


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
def env(tmp_path, approve_learning_persist):
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    fake = _FakePaths(state_dir, tmp_path / "repo")
    approve_learning_persist(state_dir)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop(persist=True)
        loop.conn.execute(
            "CREATE TABLE IF NOT EXISTS prevention_rules ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, tag_combination TEXT, rule_type TEXT, "
            "description TEXT, recommendation TEXT, confidence REAL, created_at TEXT, "
            "triggered_count INTEGER, source_dispatch_id TEXT)"
        )
        loop.conn.commit()
        yield loop, state_dir
    loop.conn.close()


def _rule(rid, agent, terminal="T1", **extra):
    rule = {"id": rid, "status": "approved", "pattern": "same failure",
            "prevention": "do x", "confidence": 0.8,
            "terminal_constraint": terminal, "agent_constraint": agent}
    rule.update(extra)
    return rule


def _run(loop, state_dir, rules):
    (state_dir / "pending_rules.json").write_text(
        json.dumps({"pending_rules": rules}), encoding="utf-8")
    loop.ingest_approved_rules()
    return json.loads((state_dir / "pending_rules.json").read_text())["pending_rules"]


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


def test_other_terminal_is_a_new_row(env):
    loop, state_dir = env
    out = _run(loop, state_dir, [_rule("a", "claude", "T1"), _rule("b", "claude", "T2")])
    assert [r["ingest_result"] for r in out] == ["inserted", "inserted"]
    assert len(_rows(loop)) == 2


def test_non_approved_rule_is_untouched(env):
    loop, state_dir = env
    out = _run(loop, state_dir, [_rule("a", "claude", status="pending")])
    assert "ingest_result" not in out[0]
    assert out[0]["status"] == "pending"
    assert _rows(loop) == []
