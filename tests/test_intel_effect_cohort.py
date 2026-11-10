"""Effect PR-6: the cohort command (outcome classes, dedup, cohort controls, the hard floor).

RC is ``init_schema`` plus ``auto_apply`` over the real migration dir up to 0038, without the state
CHECK the live stores lack; arms are stamped by the real ``arm_for_dispatch``. QI is an empty SQLite
store: ``cohort`` reads no table of it (decision 4). Temp stores only, never ~/.vnx-data.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import config_store_db  # noqa: E402
import coordination_db  # noqa: E402
import intel_effect_cohort as co  # noqa: E402
import intel_injection_join  # noqa: E402
import intelligence_selector as sel  # noqa: E402
from migrations.auto_apply import auto_apply  # noqa: E402

from fixtures.live_store_fixture import drop_state_check  # noqa: E402

DECISION_7 = (
    "HARDE GRENS: een verdict bestaat alleen bij n ≥ 200 opgeloste dispatches per arm. De grens is de "
    "constante `MIN_PER_ARM = 200` in de code en is door geen vlag, omgevingsvariabele of configuratierij "
    "te verlagen. De deadline 01-12-2026 verlengt het venster en houdt de track open. Onder de grens is de "
    "uitkomst altijd `geen verdict` (exit 3, reden `below_floor`), nooit een verdict bij lagere n.")
WINDOW = {"window_id": "w1", "salt": "s1", "control_fraction": 0.5,
          "opened_utc": "2026-11-01T00:00:00Z", "hash_version": 1}
DAY1 = datetime(2026, 11, 2, tzinfo=timezone.utc)
AS_OF = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sql(db: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def rc(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
    monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "p1")  # the migration chain refuses a store without a tenant
    (tmp_path / "state").mkdir()
    coordination_db.init_schema(tmp_path / "state")
    path = tmp_path / "state" / "runtime_coordination.db"
    assert auto_apply(path)[-1] == 38
    drop_state_check(path)
    conn = sqlite3.connect(str(path))
    config_store_db.ensure_config_tables(conn)
    conn.executemany(
        "INSERT INTO project_config (project_id, config_key, config_value, updated_by) VALUES (?, ?, ?, 'test')",
        [(p, k, v) for p in ("p1", "p2") for k, v in ((co.WINDOW_KEY, json.dumps(WINDOW)), ("VNX_INTEL_AB_TEST", "1"))])
    conn.commit()
    conn.close()
    return path


def _seed(rc, ids, pid="p1", state="completed", at=DAY1, stamp=True, task="coding_interactive"):
    """One injection row per id, stamped as the writer does; a dispatches row unless state is None."""
    conn = sqlite3.connect(str(rc))
    try:
        for did in ids:
            arm, ab_window = sel.arm_for_dispatch(did, pid, WINDOW) if stamp else (None, None)
            n = conn.execute("SELECT count(*) FROM intelligence_injections").fetchone()[0]
            conn.execute("INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, task_class, "
                         "injected_at, project_id, ab_arm, ab_window) VALUES (?, ?, 'dispatch_create', ?, ?, ?, ?, ?)",
                         (f"inj-{n}", did, task, _iso(at), pid, arm, ab_window))
            if state is not None:
                conn.execute("INSERT OR REPLACE INTO dispatches (dispatch_id, project_id, state, created_at) "
                             "VALUES (?, ?, ?, ?)", (did, pid, state, _iso(at)))
        conn.commit()
    finally:
        conn.close()


def _balanced(n, pid="p1"):
    """Door ids, exactly n per arm under the real hash."""
    ids, counts, i = [], {"control": 0, "treatment": 0}, 0
    while min(counts.values()) < n:
        did, i = f"dlv-{i:05d}", i + 1
        arm = sel.arm_for_dispatch(did, pid, WINDOW)[0]
        if counts[arm] < n:
            counts[arm] += 1
            ids.append(did)
    return ids


def _cli(rc, *extra):
    qi = rc.parent / "quality_intelligence.db"
    sqlite3.connect(str(qi)).close()
    env = {k: v for k, v in os.environ.items() if k != "VNX_INTEL_AB_TEST"}
    env.update(VNX_STATE_DIR=str(rc.parent), VNX_PROJECT_ID="p1")  # the flag comes from project_config
    return subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "intel_injection_join.py"), "cohort", "--qi", str(qi),
         "--rc", str(rc), "--project-id", "p1", "--as-of", _iso(AS_OF), *extra],
        capture_output=True, text=True, env=env, timeout=120)


def test_cohort_exits_0_with_the_per_arm_table(rc):
    _seed(rc, _balanced(200))
    run = _cli(rc)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "counts only, never a verdict" in run.stdout and "REFUSED" not in run.stdout
    rows = [line.split() for line in run.stdout.splitlines()]
    assert [r for r in rows if r[:1] in (["control"], ["treatment"])] == [
        ["control", "200", "0", "0", "0"], ["treatment", "200", "0", "0", "0"]]
    data = json.loads(_cli(rc, "--json").stdout)
    assert data["refusals"] == [] and data["exit_code"] == 0 and data["floor_rule"] is None
    assert {arm: c["resolved"] for arm, c in data["arms"].items()} == {"control": 200, "treatment": 200}


def test_199_resolved_per_arm_is_below_floor_with_the_rule(rc):
    _seed(rc, _balanced(199))
    run = _cli(rc)
    assert run.returncode == 3, run.stdout + run.stderr
    assert "REFUSED below_floor: control resolved 199, treatment resolved 199" in run.stdout
    assert DECISION_7 in run.stdout


_ROW1 = "WHERE id = (SELECT min(id) FROM intelligence_injections)"
_CFG = f"UPDATE project_config SET config_value = ? WHERE project_id = 'p1' AND config_key = '{co.WINDOW_KEY}'"


@pytest.mark.parametrize("reason, sql, params", [
    ("unstamped_after_open", "INSERT INTO intelligence_injections (injection_id, dispatch_id, injection_point, "
     "task_class, injected_at, project_id) VALUES ('x', 'dlv-x', 'p', 't', '2026-11-02T00:00:00.000Z', 'p1')", ()),
    ("assignment_not_reproducible", "UPDATE intelligence_injections SET ab_arm = CASE ab_arm WHEN 'control' "
     f"THEN 'treatment' ELSE 'control' END {_ROW1}", ()),
    ("config_diverged", f"UPDATE intelligence_injections SET ab_window = 'w1:0000000000000000' {_ROW1}", ()),
    ("arm_invalid", f"UPDATE intelligence_injections SET ab_arm = 'placebo' {_ROW1}", ()),
    ("arm_invalid", f"UPDATE intelligence_injections SET ab_arm = NULL {_ROW1}", ()),
    ("unknown_states", "UPDATE dispatches SET state = 'paused' WHERE dispatch_id = 'dlv-00000'", ()),
    ("unknown_states", "DELETE FROM dispatches WHERE dispatch_id = 'dlv-00000'", ()),
    ("malformed_timestamp", f"UPDATE intelligence_injections SET injected_at = 'yesterday' {_ROW1}", ()),
    ("window_invalid", _CFG, ("{not json",)),
    ("window_invalid", _CFG, (json.dumps({k: v for k, v in WINDOW.items() if k != "salt"}),)),
    ("window_invalid", _CFG, (json.dumps({**WINDOW, "extra": 1}),)),
    ("window_invalid", _CFG, (json.dumps({**WINDOW, "control_fraction": 1.0}),)),
    ("window_invalid", f"DELETE FROM project_config WHERE project_id = 'p1' AND config_key = '{co.WINDOW_KEY}'", ()),
])
def test_each_refusal_gives_exit_3_with_its_reason(rc, reason, sql, params):
    _seed(rc, _balanced(200))
    _sql(rc, sql, params)
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert cohort.exit_code == 3 and cohort.reasons[0] == reason, cohort.refusals


def test_open_states_never_count_withdrawn_is_excluded_and_a_new_state_is_unknown(rc, monkeypatch):
    c = co.classify_state
    assert [c(s) for s in ("completed", "expired", "dead_letter", "withdrawn")] == [
        "success", "failure", "failure", "excluded"]
    assert c(None) == c("") == c("paused") == c(7) == "unknown"
    assert set().union(*co.CLASS_STATES.values()) == co.DISPATCH_STATES  # every state of today has a class
    _seed(rc, _balanced(200))
    for i, state in enumerate(("proposed", "failed_delivery", "timed_out", "withdrawn")):
        _seed(rc, [f"dlv-extra-{i}"], state=state)
    cohort = co.build_cohort(rc, "p1", AS_OF)
    total = {key: sum(counts[key] for counts in cohort.arms().values()) for key in co.COUNT_KEYS}
    assert total == {"resolved": 400, "unresolved": 3, "excluded": 1, "unknown": 0}
    assert cohort.exit_code == 0, cohort.refusals
    monkeypatch.setattr(co, "DISPATCH_STATES", co.DISPATCH_STATES | {"paused"})
    assert c("paused") == "unknown"  # a state new in coordination_db waits for its class here


def test_a_second_project_with_the_same_ids_changes_nothing(rc):
    ids = _balanced(200)
    _seed(rc, ids)
    before = co.as_dict(co.build_cohort(rc, "p1", AS_OF))
    _seed(rc, ids, pid="p2", state="proposed")
    _seed(rc, ["dlv-00000"], pid="p2", stamp=False)
    assert co.as_dict(co.build_cohort(rc, "p1", AS_OF)) == before
    assert "unstamped_after_open" in co.build_cohort(rc, "p2", AS_OF).reasons


def test_the_floor_is_one_constant_without_a_flag_or_an_env_read(rc):
    assert co.MIN_PER_ARM == 200 and co.floor_rule() == DECISION_7
    for argv in (["--min-per-arm", "10"], ["--as-of", "yesterday"]):
        with pytest.raises(SystemExit) as exc:
            intel_injection_join.main(["cohort", "--rc", str(rc), "--project-id", "p1", *argv])
        assert exc.value.code == 2
    source = Path(co.__file__).read_text(encoding="utf-8")
    assert "environ" not in source and "getenv" not in source
    # Assignments at a statement start (=, +=, annotated, attribute), never ==; the docstring line
    # that quotes decision 7 starts with "constante" and is not one.
    assignment = re.compile(r"^[ \t]*(?:\w+\.)?MIN_PER_ARM\b[^=\n]*=(?!=).*$", re.M)
    assigned = [(str(path.relative_to(_ROOT)), hit) for path in (_ROOT / "scripts").rglob("*.py")
                for hit in assignment.findall(path.read_text(encoding="utf-8", errors="replace"))]
    assert assigned == [("scripts/lib/intel_effect_cohort.py", "MIN_PER_ARM = 200")]


def test_dedup_keeps_the_first_row_and_a_missing_dispatches_row_is_unknown(rc):
    _seed(rc, ["dlv-a"])
    _seed(rc, ["dlv-a"], state=None, task="coding_sql")  # same injected_at: min(id) wins
    _seed(rc, ["dlv-d"], at=DAY1 + timedelta(days=1))
    _seed(rc, ["dlv-d"], state=None, task="coding_sql")  # earlier injected_at wins
    _seed(rc, ["dlv-b"], state=None)
    cohort = co.build_cohort(rc, "p1", AS_OF)
    assert {d.dispatch_id: d.task_class for d in cohort.dispatches} == {
        "dlv-a": "coding_interactive", "dlv-b": "coding_interactive", "dlv-d": "coding_sql"}
    assert cohort.deduplicated == 2 and "unknown_states" in cohort.reasons


def test_a_closed_flag_fails_every_rederivation_and_never_passes(rc, monkeypatch):
    _seed(rc, _balanced(200))
    monkeypatch.setenv("VNX_INTEL_AB_TEST", "0")
    refusals = dict(co.build_cohort(rc, "p1", AS_OF).refusals)
    assert "VNX_INTEL_AB_TEST" in refusals["assignment_not_reproducible"]
