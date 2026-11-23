"""vnx learning decide-rule through the CLI and bin/vnx (loop D3, split 2)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prevention_rule_store as store  # noqa: E402
from vnx_cli import _engine  # noqa: E402
from vnx_cli.main import main as vnx_main  # noqa: E402

NOW = "2026-11-10T12:00:00+00:00"
CRITERION = "dispatches_with_class:timeout drops below 2 per week"
STOP = "stop when a worker is blocked twice by the rule"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A tmp project whose data root is under tmp_path; returns (project_dir, state_dir, project_id)."""
    for key in [k for k in os.environ if k.startswith("VNX_")] + ["PROJECT_ROOT"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_PROJECT_ID", "tproj")
    project = tmp_path / "proj"
    project.mkdir()
    state = _engine.resolve_data_root(project) / "state"
    state.mkdir(parents=True)
    return project, state, _engine.derive_project_id(project)


def _queue(state, project_id, *patterns):
    proposals = [
        {"pattern": p, "terminal_constraint": "T1", "failure_class": "timeout", "prevention": "prevent",
         "confidence": 0.8, "occurrence_count": 3, "source_dispatch_ids": ["d-1", "d-2"]}
        for p in patterns
    ]
    store.queue_rules(state, proposals, project_id=project_id, now=NOW)
    return [store.rule_id(p) for p in proposals]


def _run(project, *argv) -> int:
    # --project-dir goes after the verb: the subparser's own default "." would override it.
    with patch.object(sys, "argv", ["vnx", "learning", argv[0], "--project-dir", str(project), *argv[1:]]):
        try:
            vnx_main()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    return 0


def _decide(project, rule_ids, decision, *extra):
    argv = ["decide-rule", "--decision", decision, "--reason", "r", "--by", "v"]
    for rid in rule_ids:
        argv += ["--rule-id", rid]
    return _run(project, *argv, *extra)


def _approve(project, rule_ids):
    return _decide(project, rule_ids, "approved", "--effect-criterion", CRITERION, "--stop-condition", STOP)


def _records(state):
    return store.decisions(state)


def _status(state, rid):
    return next(r["status"] for r in store.load_queue(state) if r["id"] == rid)


def test_learning_is_reachable_through_bin_vnx(env):
    project, _state, _pid = env
    result = subprocess.run(
        [str(REPO_ROOT / "bin" / "vnx"), "learning", "status", "--project-dir", str(project)],
        capture_output=True, text=True, timeout=120, env=dict(os.environ), cwd=str(project),
    )
    assert result.returncode == 0, result.stderr
    assert "Learning loop status" in result.stdout


def test_reject_writes_one_record_with_a_minted_approval_id(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, [rid], "rejected") == 0
    (record,) = _records(state)
    assert record["decision"] == "rejected" and record["rule_id"] == rid
    assert re.fullmatch(r"apr-[0-9a-f]{12}", record["approval_id"])
    assert "bundle_ref" not in record
    assert record["baseline_metric"] == "dispatches_with_class:timeout" and record["baseline_value"] == 3
    assert record["rollout_scope"] == "project" and record["project_id"] == pid
    assert _status(state, rid) == "rejected"


def test_approve_carries_bundle_criteria_and_recovery_path(env):
    project, state, pid = env
    a, b = _queue(state, pid, "p1", "p2")
    assert _approve(project, [a, b]) == 0
    recs = _records(state)
    assert len(recs) == 2
    assert {r["bundle_ref"] for r in recs} == {store.mint_bundle_ref([a, b], recs[0]["decided_at"])}
    assert all(re.fullmatch(r"bundle-[0-9a-f]{12}", r["bundle_ref"]) for r in recs)
    assert all(r["effect_criterion"] == CRITERION and r["stop_condition"] == STOP for r in recs)
    assert all(r["recovery_path"] == store.RECOVERY_PATH for r in recs)
    assert store.approved_decision(state, a)["approval_id"] == recs[0]["approval_id"] or \
        store.approved_decision(state, a)["approval_id"] == recs[1]["approval_id"]
    assert _status(state, a) == _status(state, b) == "approved"


@pytest.mark.parametrize("extra", [
    [], ["--effect-criterion", CRITERION], ["--stop-condition", STOP],
    ["--effect-criterion", " ", "--stop-condition", STOP],
])
def test_approve_without_both_criteria_is_exit_2_and_writes_nothing(env, extra):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, [rid], "approved", *extra) == 2
    assert _records(state) == [] and _status(state, rid) == "pending"


def test_unknown_decision_and_empty_text_are_exit_2(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, [rid], "maybe") == 2
    assert _run(project, "decide-rule", "--rule-id", rid, "--decision", "rejected", "--reason", " ", "--by", "v") == 2
    assert _run(project, "decide-rule", "--rule-id", rid, "--decision", "rejected", "--reason", "r", "--by", "") == 2
    assert _records(state) == []


def test_identical_repeat_is_exit_0_without_a_second_record(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, [rid], "rejected") == 0
    assert _decide(project, [rid], "rejected") == 0
    assert len(_records(state)) == 1


def test_unknown_rule_id_is_exit_1_and_a_bundle_is_all_or_nothing(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, ["rule-00000000"], "rejected") == 1
    assert _decide(project, [rid, "rule-00000000"], "rejected") == 1
    assert _records(state) == [] and _status(state, rid) == "pending"


def test_conflicting_decision_is_exit_1(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    assert _decide(project, [rid], "rejected") == 0
    assert _approve(project, [rid]) == 1
    assert len(_records(state)) == 1 and _status(state, rid) == "rejected"


def test_an_unreadable_record_or_queue_is_exit_1(env):
    project, state, pid = env
    (rid,) = _queue(state, pid, "p1")
    (state / store.DECISIONS_FILENAME).write_text("not json\n")
    assert _decide(project, [rid], "rejected") == 1
    (state / store.DECISIONS_FILENAME).unlink()
    (state / store.QUEUE_FILENAME).write_text("{")
    assert _decide(project, [rid], "rejected") == 1
