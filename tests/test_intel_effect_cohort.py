"""Effect PR-6: the cohort command (classes, dedup, controls, re-derivation, maturity, attrition, coverage, floor).

RC is ``init_schema`` plus the real migrations to 0038, without the state CHECK the live stores lack. The
writer's ``arm_for_dispatch`` stamps the arms; QI is empty (decision 4). Temp stores only, never ~/.vnx-data.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config_store_db
import coordination_db
import intel_effect_cohort as co
import intelligence_selector as sel
from fixtures.live_store_fixture import drop_state_check
from migrations.auto_apply import auto_apply

_ROOT = Path(__file__).resolve().parent.parent
DECISION_7 = (
    "HARDE GRENS: een verdict bestaat alleen bij n ≥ 200 opgeloste dispatches per arm. De grens is de "
    "constante `MIN_PER_ARM = 200` in de code en is door geen vlag, omgevingsvariabele of configuratierij "
    "te verlagen. De deadline 01-12-2026 verlengt het venster en houdt de track open. Onder de grens is de "
    "uitkomst altijd `geen verdict` (exit 3, reden `below_floor`), nooit een verdict bij lagere n.")
WINDOW = {"window_id": "w1", "salt": "s1", "control_fraction": 0.5,
          "opened_utc": "2026-11-01T00:00:00Z", "hash_version": 1}
DAY1, DAY2 = "2026-11-02T00:00:00.000Z", "2026-11-03T00:00:00.000Z"
AS_OF = datetime(2026, 11, 20, tzinfo=timezone.utc)
_ROW1 = "WHERE id = (SELECT min(id) FROM intelligence_injections)"
_STUCK = ("UPDATE dispatches SET state = 'proposed' WHERE dispatch_id IN (SELECT dispatch_id FROM "
          "intelligence_injections WHERE project_id = 'p1' AND ab_arm = 'control' ORDER BY id LIMIT ?)")
_CFG = f"UPDATE project_config SET config_value = ? WHERE project_id = 'p1' AND config_key = '{co.WINDOW_KEY}'"


def _exec(db: Path, sql: str, rows) -> None:
    """One statement over ``rows`` in one committed transaction."""
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.executemany(sql, rows)


@pytest.fixture
def rc(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")  # the writer's flag: arm_for_dispatch stamps only when on
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")  # the migration chain refuses a store without a tenant
    coordination_db.init_schema(tmp_path)
    path = tmp_path / "runtime_coordination.db"
    assert auto_apply(path)[-1] == 38
    drop_state_check(path)
    sqlite3.connect(str(tmp_path / "quality_intelligence.db")).close()
    with closing(sqlite3.connect(str(path))) as conn, conn:
        config_store_db.ensure_config_tables(conn)
    _exec(path, "INSERT INTO project_config (project_id, config_key, config_value, updated_by) VALUES (?, ?, ?, 't')",
          [(pid, co.WINDOW_KEY, json.dumps(WINDOW)) for pid in ("p1", "p2")])
    return path


def _at(hours):
    return (AS_OF - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(rc, ids, pid="p1", state="completed", at=DAY1, stamp=True, task="coding_interactive"):
    """One injection row per id, stamped as the writer does; a dispatches row unless state is None."""
    _exec(rc, "INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, "
              "injected_at, project_id, ab_arm, ab_window) "
              "VALUES ('inj-' || lower(hex(randomblob(8))), ?, 'dispatch_create', ?, ?, ?, ?, ?)",
          [(did, task, at, pid, *(sel.arm_for_dispatch(did, pid, WINDOW) if stamp else (None, None)))
           for did in ids])
    if state is not None:
        _exec(rc, "INSERT OR REPLACE INTO dispatches (dispatch_id, project_id, state, created_at) VALUES (?, ?, ?, ?)",
              [(did, pid, state, at) for did in ids])


def _balanced(n, pid="p1"):
    """Door ids, the first n per arm under the writer's hash."""
    arm_of = {did: sel.arm_for_dispatch(did, pid, WINDOW)[0] for did in (f"dlv-{i:05d}" for i in range(3 * n))}
    ids = sorted(did for arm in co.ARMS for did in [d for d, a in arm_of.items() if a == arm][:n])
    assert len(ids) == 2 * n
    return ids


def _cli(rc, *extra):
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "intel_injection_join.py"), "cohort", "--rc", str(rc), "--qi",
         str(rc.parent / "quality_intelligence.db"), "--project-id", "p1", "--as-of", AS_OF.isoformat(), *extra],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "VNX_STATE_DIR": str(rc.parent), "VNX_PROJECT_ID": "p1"})


@pytest.mark.parametrize("n, code, refused", [
    (200, 0, None),
    (199, 3, "REFUSED below_floor: control resolved 199, treatment resolved 199; the floor is 200 resolved per arm"),
])
def test_the_cli_counts_per_arm_and_refuses_below_the_floor_with_the_rule(rc, n, code, refused):
    _seed(rc, _balanced(n))
    run, data = _cli(rc), json.loads(_cli(rc, "--json").stdout)
    assert run.returncode == data["exit_code"] == code and "never a verdict" in run.stdout, run.stdout + run.stderr
    lines = run.stdout.splitlines()
    assert [line.split() for line in lines if line.split()[:1] in (["control"], ["treatment"])] == [
        [arm, str(n), "0", "0", "0", "0", "0.00%"] for arm in co.ARMS]
    assert {arm: c["resolved"] for arm, c in data["arms"].items()} == dict.fromkeys(co.ARMS, n)
    assert [line for line in lines if line.startswith("REFUSED")] == ([refused] if refused else [])
    assert [r["reason"] for r in data["refusals"]] == (["below_floor"] if refused else [])
    assert data["floor_rule"] == (DECISION_7 if refused else None) and (DECISION_7 in lines) == bool(refused)


@pytest.mark.parametrize("reason, sql, params", [
    ("unstamped_after_open", "INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, "
     "task_class, injected_at, project_id) VALUES ('x', 'dlv-x', 'p', 't', '2026-11-02T00:00:00.000Z', 'p1')", ()),
    ("assignment_not_reproducible", "UPDATE intelligence_injections SET ab_arm = CASE ab_arm WHEN 'control' "
     f"THEN 'treatment' ELSE 'control' END {_ROW1}", ()),
    ("assignment_not_reproducible", "UPDATE intelligence_injections SET ab_window = 'w1:0000000000000000'", ()),
    ("assignment_not_reproducible", _CFG, (json.dumps({**WINDOW, "salt": "s2"}),)),
    ("config_diverged", f"UPDATE intelligence_injections SET ab_window = 'w1:0000000000000000' {_ROW1}", ()),
    ("differential_attrition", _STUCK, (11,)),
    ("arm_invalid", f"UPDATE intelligence_injections SET ab_arm = 'placebo' {_ROW1}", ()),
    ("arm_invalid", f"UPDATE intelligence_injections SET ab_arm = NULL {_ROW1}", ()),
    ("unknown_states", "UPDATE dispatches SET state = 'paused' WHERE dispatch_id = 'dlv-00000'", ()),
    ("unknown_states", "DELETE FROM dispatches WHERE dispatch_id = 'dlv-00000'", ()),
    ("malformed_timestamp", f"UPDATE intelligence_injections SET injected_at = 'yesterday' {_ROW1}", ()),
    *[("window_invalid", _CFG, (bad,)) for bad in ("{not json", json.dumps({**WINDOW, "extra": 1}),
      json.dumps({k: v for k, v in WINDOW.items() if k != "salt"}), json.dumps({**WINDOW, "control_fraction": 1.0}))],
    ("window_invalid", f"DELETE FROM project_config WHERE project_id = 'p1' AND config_key = '{co.WINDOW_KEY}'", ()),
])
def test_each_refusal_gives_exit_3_with_its_reason(rc, reason, sql, params):
    _seed(rc, _balanced(200))
    _exec(rc, sql, [params])
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert cohort.exit_code == 3 and cohort.reasons[0] == reason, cohort.refusals


def test_open_states_never_count_withdrawn_is_excluded_and_a_new_state_is_unknown(rc, monkeypatch):
    c = co.classify_state
    assert " ".join(map(c, ("completed", "expired", "dead_letter", "withdrawn"))) == "success failure failure excluded"
    assert c(None) == c("") == c("paused") == c(7) == "unknown"
    assert set().union(*co.CLASS_STATES.values()) == co.DISPATCH_STATES  # every state of today has a class
    _seed(rc, _balanced(200))
    for i, state in enumerate(("proposed", "failed_delivery", "timed_out", "withdrawn")):
        _seed(rc, [f"dlv-extra-{i}"], state=state)
    cohort = co.build_cohort(rc, "p1", AS_OF)
    total = {key: sum(counts[key] for counts in cohort.arms().values()) for key in co.COUNT_KEYS}
    assert total == {"resolved": 400, "unresolved": 3, "excluded": 1, "unknown": 0, "immature": 0}
    assert cohort.exit_code == 0, cohort.refusals
    monkeypatch.setattr(co, "DISPATCH_STATES", co.DISPATCH_STATES | {"paused"})
    assert c("paused") == "unknown"  # a state new in coordination_db waits for its class here


def test_a_second_project_with_the_same_ids_changes_nothing(rc):
    _seed(rc, ids := _balanced(200))
    before = co.as_dict(co.build_cohort(rc, "p1", AS_OF))
    _seed(rc, ids, pid="p2", state="proposed")
    _seed(rc, ["dlv-00000"], pid="p2", stamp=False)
    _exec(rc, "INSERT INTO dispatches (dispatch_id, project_id, state, created_at) VALUES (?, 'p2', 'completed', ?)",
          [("dlv-orphan", DAY1)])
    assert co.as_dict(co.build_cohort(rc, "p1", AS_OF)) == before
    assert "unstamped_after_open" in co.build_cohort(rc, "p2", AS_OF).reasons


def test_the_floor_is_one_constant_without_a_flag_or_an_env_read(rc):
    assert co.MIN_PER_ARM == 200 and co.floor_rule() == DECISION_7
    for extra, error in ((("--min-per-arm", "10"), "unrecognized arguments: --min-per-arm 10"),
                         (("--as-of", "yesterday"), "not an ISO timestamp: 'yesterday'")):
        run = _cli(rc, *extra)
        assert run.returncode == 2 and error in run.stderr, run.stderr
    source = Path(co.__file__).read_text(encoding="utf-8")
    assert "environ" not in source and "getenv" not in source
    # Statement-start assignments (=, +=, annotated, attribute), never ==; the "constante" docstring line is none.
    assignment = re.compile(r"^[ \t]*(?:\w+\.)?MIN_PER_ARM\b[^=\n]*=(?!=).*$", re.M)
    assigned = [(str(path.relative_to(_ROOT)), hit) for path in (_ROOT / "scripts").rglob("*.py")
                for hit in assignment.findall(path.read_text(encoding="utf-8", errors="replace"))]
    assert assigned == [("scripts/lib/intel_effect_cohort.py", "MIN_PER_ARM = 200")]


def test_dedup_keeps_the_first_row_and_a_missing_dispatches_row_is_unknown(rc):
    _seed(rc, ["dlv-a"])
    _seed(rc, ["dlv-a"], state=None, task="coding_sql")  # same injected_at: min(id) wins
    _seed(rc, ["dlv-d"], at=DAY2)
    _seed(rc, ["dlv-d"], state=None, task="coding_sql")  # earlier injected_at wins
    _seed(rc, ["dlv-b"], state=None)
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert {d.dispatch_id: d.task_class for d in cohort.dispatches} == {
        "dlv-a": "coding_interactive", "dlv-b": "coding_interactive", "dlv-d": "coding_sql"}
    assert cohort.deduplicated == 2 and "unknown_states" in cohort.reasons


@pytest.mark.parametrize("flag", ["0", None])
def test_a_closed_flag_never_fails_the_rederivation(rc, monkeypatch, flag):
    """Decision 12 closes the window with VNX_INTEL_AB_TEST=0; decision 14 re-derives from the frozen window."""
    _seed(rc, _balanced(200))
    monkeypatch.delenv("VNX_INTEL_AB_TEST") if flag is None else monkeypatch.setenv("VNX_INTEL_AB_TEST", flag)
    assert sel.arm_for_dispatch("dlv-00000", "p1", WINDOW) == (None, None)  # the writer is closed
    assert co.build_cohort(rc, "p1", AS_OF).refusals == []
    run = _cli(rc)
    assert run.returncode == 0 and "REFUSED" not in run.stdout, run.stdout + run.stderr


def test_71_hours_is_immature_and_72_hours_is_mature(rc):
    _seed(rc, ["dlv-71"], at=_at(71))
    _seed(rc, ["dlv-72s"], at=_at(72 - 1 / 3600))
    _seed(rc, ["dlv-72"], at=_at(72))
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert {d.dispatch_id: d.mature for d in cohort.dispatches} == {"dlv-71": False, "dlv-72s": False, "dlv-72": True}
    total = {key: sum(c[key] for c in cohort.arms().values()) for key in co.COUNT_KEYS}
    assert total == {"resolved": 1, "unresolved": 0, "excluded": 0, "unknown": 0, "immature": 2}


@pytest.mark.parametrize("stuck, refused", [(10, False), (11, True)])
def test_differential_attrition_refuses_above_5pp_only(rc, stuck, refused):
    _seed(rc, _balanced(200))
    _exec(rc, _STUCK, [(stuck,)])  # 10 of 200 is exactly 5 pp
    assert ("differential_attrition" in co.build_cohort(rc, "p1", AS_OF).reasons) == refused


def test_attrition_is_exact_and_an_arm_without_mature_dispatches_refuses():
    def arms(stuck, unknown=0, mature=100):
        counts = {"resolved": mature - stuck - unknown, "unresolved": stuck, "excluded": 0, "unknown": unknown}
        return {"control": {**counts, "immature": 0},
                "treatment": {"resolved": 91, "unresolved": 9, "excluded": 0, "unknown": 0, "immature": 0}}
    assert 14 / 100 - 9 / 100 > 0.05 and co.attrition_gap(arms(10, unknown=4)) is None  # floats would refuse
    assert "5.01 pp" in co.attrition_gap(arms(1401, mature=10000))
    assert "not computable" in co.attrition_gap(arms(0, mature=0))


def test_the_coverage_line_counts_every_group(rc):
    _seed(rc, ["dlv-b"], state=None)  # an injection without a dispatches row
    _seed(rc, ["dlv-e"], at="2026-10-31T00:00:00.000Z", stamp=False)  # first injection before the open
    _seed(rc, ["dlv-e"], state=None)
    _exec(rc, "INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, "
              "injected_at, project_id, ab_arm, ab_window) VALUES (?, ?, 'p', 't', ?, 'p1', 'control', 'w0:ab')",
          [("x-f", "dlv-f", DAY1), ("x-g", "dlv-g", DAY1)])  # rows of another window, dlv-f's first injection
    _seed(rc, ["dlv-f"], at=DAY2)
    _exec(rc, "INSERT INTO dispatches (dispatch_id, project_id, state, created_at) VALUES (?, ?, 'completed', ?)",
          [("dlv-c", "p1", DAY1), ("dlv-early", "p1", "2026-10-30T00:00:00Z"), ("other-c", "p1", DAY1)])
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert cohort.coverage == {"door_without_injection_row": 1, "injection_without_dispatches_row": 1,
                               "other_window_rows": 2, "first_injection_before_open": 1,
                               "first_injection_off_window": 1}
    assert "# coverage: door_without_injection_row 1, injection_without_dispatches_row 1" in co.render(cohort)
