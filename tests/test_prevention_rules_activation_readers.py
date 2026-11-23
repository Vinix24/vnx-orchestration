"""Every reader of prevention_rules that returns content goes through one activation predicate (loop D8)."""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_t0_quality_digest as digest  # noqa: E402
import cached_intelligence as ci  # noqa: E402
import gather_intelligence as gi  # noqa: E402
import learning_ledger_gate as gate  # noqa: E402
import pattern_activation as pa  # noqa: E402
import quality_db_init as qdi  # noqa: E402
import tag_intelligence as ti  # noqa: E402
from intelligence_sources import failure_prevention as fp  # noqa: E402

PID_A, PID_B = "proj-a", "proj-b"
LOOP_A, LOOP_B = "loop flaky rule a", "loop flaky rule b"
LOOP = "learning_loop"


def _db(state: Path) -> Path:
    return state / "quality_intelligence.db"


def _sql(state: Path, statement: str, params=()):
    with closing(sqlite3.connect(_db(state))) as conn:
        cur = conn.execute(statement, params)
        conn.commit()
        return cur.lastrowid


def _rule(state: Path, text: str, source: str, project: str, confidence: float) -> int:
    return _sql(state, "INSERT INTO prevention_rules (tag_combination, rule_type, description, recommendation, "
                       "confidence, created_at, project_id, source) VALUES ('[\"flaky\"]', 'r', ?, ?, ?, 'now', ?, ?)",
                (text, text, confidence, project, source))


def _activate(state: Path, project: str, row: int, active: int = 1) -> None:
    names = [c for c in pa.COLUMNS if c not in ("deactivated_at", "deactivation_reason", "deactivation_approval_id")]
    values = {**{c: f"{c}-{project}-{row}" for c in names}, "project_id": project, "rule_row_id": row,
              "baseline_value": 2, "active": active}
    _sql(state, f"INSERT INTO pattern_activation ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
         [values[c] for c in names])


def _verdict(state: Path, status: str = "ok", *, project: str = PID_A, hours_ago: float = 1) -> None:
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gate.write_verdict(state, {"schema": gate.SCHEMA, "evaluated_at": at, "project_id": project, "cohort": "all_v3",
                               "status": status, "reason_code": status, "reason": "fixture", "counts": {}})


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    assert qdi.bootstrap_qi_db(_db(state)) is True
    _sql(state, "ALTER TABLE prevention_rules ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
    _sql(state, "ALTER TABLE prevention_rules ADD COLUMN source TEXT")
    for i in range(20):
        _rule(state, f"legacy flaky rule {i:02d}", "behavior_analysis", PID_A, 0.5 + i / 100)
    monkeypatch.setenv("VNX_PROJECT_ID", PID_A)
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    return state


def _read_all(state: Path) -> dict:
    """The output of each of the six readers on ``state``, as comparable text lists."""
    out = {}
    with closing(sqlite3.connect(_db(state))) as conn:
        conn.row_factory = sqlite3.Row

        def has_column(table, column):
            return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

        out["failure_prevention"] = [i.title for i in fp._query_prevention_rules(conn, [], has_column)]
        recs = digest.build_prompt_config_tuning(conn, state, {"dispatch_ids": []})
        out["t0_digest"] = [r["detail"] for r in recs]
        gatherer = object.__new__(gi.T0IntelligenceGatherer)
        gatherer.quality_db, gatherer.quality_db_path, gatherer.tag_engine = conn, _db(state), None
        out["gather_intelligence"] = gatherer.get_mined_quality_context("flaky").splitlines()
    cached = ci.CachedIntelligence()
    out["cached_intelligence"] = [r["description"] for r in cached.query_prevention_rules(["flaky"])]
    cached.conn.close()
    engine = ti.TagIntelligenceEngine(_db(state))
    out["tag_intelligence_subsets"] = [r["description"] for r in engine.query_prevention_rules(["flaky"])]
    out["tag_intelligence_all"] = [r["description"] for r in engine.query_prevention_rules()]
    engine.close()
    return out


def _shows(out: dict, text: str) -> dict:
    return {name: any(text in line for line in lines) for name, lines in out.items()}


def test_a_loop_rule_without_an_activation_is_out_and_legacy_is_unchanged(state):
    before = _read_all(state)
    assert all(before.values()), before
    _verdict(state)
    _rule(state, LOOP_A, LOOP, PID_A, 0.99)
    assert _read_all(state) == before


@pytest.mark.parametrize("status", ["ok", "pending"])
def test_an_active_activation_with_a_fresh_ok_or_pending_verdict_shows_the_loop_rule(state, status):
    before = _read_all(state)
    _activate(state, PID_A, _rule(state, LOOP_A, LOOP, PID_A, 0.99))
    _verdict(state, status)
    after = _read_all(state)
    assert all(_shows(after, LOOP_A).values()), after
    for name, lines in after.items():
        legacy = [line for line in lines if LOOP_A not in line]
        assert legacy == before[name][:len(legacy)], name


_BREAKERS = {
    "inactive": lambda s: _sql(s, "UPDATE pattern_activation SET active = 0"),
    "no_table": lambda s: _sql(s, "DROP TABLE pattern_activation"),
    "no_project_column": lambda s: _sql(s, "ALTER TABLE prevention_rules DROP COLUMN project_id"),
    "no_verdict": lambda s: (s / gate.VERDICT_FILENAME).unlink(),
    "unreadable": lambda s: (s / gate.VERDICT_FILENAME).write_text("{not json", encoding="utf-8"),
    "stale": lambda s: _verdict(s, hours_ago=40),
    "future": lambda s: _verdict(s, hours_ago=-2),
    "refused": lambda s: _verdict(s, "refused"),
    "unknown_status": lambda s: _verdict(s, "maybe"),
    "none_status": lambda s: _verdict(s, None),
    "other_project_verdict": lambda s: _verdict(s, project=PID_B),
}


@pytest.mark.parametrize("breaker", sorted(_BREAKERS))
def test_no_activation_or_no_fresh_injectable_verdict_keeps_the_loop_rule_out(state, breaker):
    before = _read_all(state)
    _activate(state, PID_A, _rule(state, LOOP_A, LOOP, PID_A, 0.99))
    _verdict(state)
    _BREAKERS[breaker](state)
    assert _read_all(state) == before


def test_a_rule_of_project_b_does_not_appear_in_project_a(state):
    row_b = _rule(state, LOOP_B, LOOP, PID_B, 0.98)
    _activate(state, PID_B, row_b)
    _activate(state, PID_A, row_b)  # an activation in A cannot switch on a row of B
    _activate(state, PID_A, _rule(state, LOOP_A, LOOP, PID_A, 0.99))
    _verdict(state, project=PID_A)
    after = _read_all(state)
    assert all(_shows(after, LOOP_A).values()), after
    assert not any(_shows(after, LOOP_B).values()), after


def test_a_database_without_a_state_dir_keeps_loop_rules_out(state):
    _activate(state, PID_A, _rule(state, LOOP_A, LOOP, PID_A, 0.99))
    _verdict(state)
    with closing(sqlite3.connect(":memory:")) as memory:
        memory.row_factory = sqlite3.Row
        with closing(sqlite3.connect(_db(state))) as disk:
            disk.backup(memory)
        titles = [i.title for i in fp._query_prevention_rules(memory, [], lambda t, c: False)]
    assert len(titles) == 10 and not any(LOOP_A in t for t in titles)
