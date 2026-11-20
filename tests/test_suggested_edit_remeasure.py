"""The 14-day effect re-measurement writes kept, revert_proposed or unmeasurable and opens a point (D3d)."""

import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import apply_suggested_edits as ase
import operator_queue as oq
import operator_queue_dream_analyzer as oqa
import suggested_edit_remeasure as rem

PID = "vnx-dev"
RULE = "Reviewers keep finding a missing fail-closed check on evidence reads"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
APPLIED = NOW - timedelta(days=15)


def _z(ts):
    return ts.isoformat().replace("+00:00", "Z")


def _entry(**over):
    entry = {"id": 1, "dispatch_id": "dlv-edit1", "target": "agents/x/CLAUDE.md", "content": f"- {RULE}",
             "status": "applied", "applied_at": _z(APPLIED), "remeasure_at": _z(APPLIED + timedelta(days=14)),
             "baseline": {"window_days": 14, "status": "ok", "count": 1, "reason": None}}
    entry.update(over)
    return entry


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "review_gates").mkdir(parents=True)
    monkeypatch.setattr(oq, "state_dir_for", lambda project_id: tmp_path / "queue")
    for name, value in [("STATE_DIR", state), ("HISTORY_PATH", state / "edit_history.json"), ("_project_id", lambda: PID)]:
        monkeypatch.setattr(ase, name, value)
    return state


def _data(state, *, after_dispatches, rounds):
    """Pattern dispatches: one before the apply, ``after_dispatches`` after it. ``rounds``: (days from apply, verdict)."""
    ids = ["d-before"] + [f"d-after{i}" for i in range(after_dispatches)]
    with closing(sqlite3.connect(state / "quality_intelligence.db")) as conn, conn:
        conn.execute("CREATE TABLE antipatterns (project_id TEXT, title TEXT, description TEXT, "
                     "source_dispatch_ids TEXT, last_seen TEXT)")
        conn.execute("INSERT INTO antipatterns VALUES (?,?,?,?,?)", (PID, "t", RULE, json.dumps(ids), _z(NOW)))
    stamps = [("d-before", APPLIED - timedelta(days=5))] + [(f"d-after{i}", APPLIED + timedelta(days=3)) for i in range(after_dispatches)]
    (state / "t0_receipts.ndjson").write_text("".join(
        json.dumps({"dispatch_id": d, "timestamp": _z(t), "project_id": PID}) + "\n" for d, t in stamps))
    if rounds is not None:
        (state / "review_gates" / "rounds.ndjson").write_text("".join(json.dumps({
            "project_id": PID, "pr_number": 100 + i, "head_sha": f"sha{i}", "seat": "codex_gate", "reviewer": "codex",
            "takeover_from": "", "verdict": verdict, "blocking_count": 1 if verdict == "fail" else 0, "report_path": "",
            "dispatch_id": f"r{i}", "recorded_at": _z(APPLIED + timedelta(days=days))}) + "\n"
            for i, (days, verdict) in enumerate(rounds)))


def _run(state, *entries):
    ase._save_history(list(entries))
    code = ase.cmd_remeasure()
    return code, json.loads((state / "edit_history.json").read_text())


def _points():
    return [p for p in oq.read_state(PID, state_dir=oq.state_dir_for(PID))["points"].values() if p["bron"] == 4]


def test_better_or_equal_is_kept_with_one_point(env):
    _data(env, after_dispatches=0, rounds=[(-3, "fail"), (4, "pass")])
    code, hist = _run(env, _entry())
    assert code == 0 and hist[0]["outcome"] == "kept"
    assert hist[0]["measured"]["count"] == 0 and hist[0]["rework"]["before"] == 1 and hist[0]["rework"]["after"] == 0
    assert hist[0]["measured_at"]
    (point,) = _points()
    assert point["ref"] == "effect:dlv-edit1" and "kept" in point["vraag"]


def test_worse_is_revert_proposed(env):
    _data(env, after_dispatches=2, rounds=[(2, "fail"), (5, "fail")])
    code, hist = _run(env, _entry())
    assert code == 0 and hist[0]["outcome"] == "revert_proposed"
    assert len(_points()) == 1


@pytest.mark.parametrize("baseline", [
    {"window_days": 14, "status": "unknown", "count": None, "reason": "no antipattern row"},
    {"window_days": 14, "status": "ok", "count": None},
    {"window_days": 14, "status": "ok", "count": "1"},
    {"window_days": 14, "status": "ok", "count": True},
    {"window_days": 14, "status": "OK", "count": 1},
    "garbage", None])
def test_unknown_or_malformed_baseline_is_unmeasurable(env, baseline):
    _data(env, after_dispatches=0, rounds=[])
    _, hist = _run(env, _entry(baseline=baseline))
    assert hist[0]["outcome"] == "unmeasurable" and len(_points()) == 1


def test_baseline_key_missing_is_unmeasurable(env):
    _data(env, after_dispatches=0, rounds=[])
    entry = _entry()
    del entry["baseline"]
    assert _run(env, entry)[1][0]["outcome"] == "unmeasurable"


def test_rounds_direction_disagreeing_is_unmeasurable_with_both_values(env):
    _data(env, after_dispatches=0, rounds=[(2, "fail"), (5, "fail")])  # patterns better, rework worse
    _, hist = _run(env, _entry())
    assert hist[0]["outcome"] == "unmeasurable" and "disagree" in hist[0]["outcome_reason"]
    assert hist[0]["measured"]["count"] == 0 and hist[0]["rework"]["after"] == 2


def test_missing_or_corrupt_rounds_ledger_is_unmeasurable(env):
    _data(env, after_dispatches=0, rounds=None)
    assert _run(env, _entry())[1][0]["outcome"] == "unmeasurable"
    (env / "review_gates" / "rounds.ndjson").write_text("{not json\n")
    _, hist = _run(env, _entry(dispatch_id="dlv-edit2"))
    assert hist[0]["outcome"] == "unmeasurable"


def test_no_data_in_window_database_is_unmeasurable(env):
    _, hist = _run(env, _entry())  # no database, no ledgers at all
    assert hist[0]["outcome"] == "unmeasurable" and "after-window unknown" in hist[0]["outcome_reason"]


def test_future_remeasure_at_is_untouched(env):
    _data(env, after_dispatches=0, rounds=[])
    entry = _entry(remeasure_at=_z(NOW + timedelta(days=3)))
    # the clock is real: a date far in the future stays in the future
    entry["remeasure_at"] = _z(datetime.now(tz=timezone.utc) + timedelta(days=3))
    _, hist = _run(env, entry)
    assert "outcome" not in hist[0] and _points() == []


def test_second_run_opens_no_second_point_and_keeps_the_outcome(env):
    _data(env, after_dispatches=0, rounds=[])
    _run(env, _entry())
    first = json.loads((env / "edit_history.json").read_text())
    assert ase.cmd_remeasure() == 0
    assert json.loads((env / "edit_history.json").read_text()) == first and len(_points()) == 1


def test_crash_after_point_before_outcome_reopens_nothing(env):
    _data(env, after_dispatches=0, rounds=[])
    entry = _entry()
    fields = rem.decide(entry, PID, env / "quality_intelligence.db", env / "t0_receipts.ndjson",
                        env / "review_gates" / "rounds.ndjson")
    assert rem.open_effect_point(PID, entry, fields, NOW) is True
    _, hist = _run(env, entry)
    assert hist[0]["outcome"] == "kept" and len(_points()) == 1


def test_unreadable_inbox_leaves_no_outcome_and_a_failure_code(env, tmp_path):
    _data(env, after_dispatches=0, rounds=[])
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / oq.LEDGER_FILENAME).write_text("not json\n")
    code, hist = _run(env, _entry())
    assert code == 1 and "outcome" not in hist[0]


def test_entry_without_dispatch_id_is_a_problem_not_an_outcome(env):
    _data(env, after_dispatches=0, rounds=[])
    code, hist = _run(env, _entry(dispatch_id=None))
    assert code == 1 and "outcome" not in hist[0]


def test_history_unreadable_is_refused(env):
    (env / "edit_history.json").write_text("{broken")
    assert ase.cmd_remeasure() == 1


def test_analyzer_sweep_leaves_an_effect_point_open(env, tmp_path):
    _data(env, after_dispatches=0, rounds=[])
    _run(env, _entry())
    (env / "pending_edits.json").write_text(json.dumps({"edits": []}))
    oqa.read_analyzer(PID, data_root=env.parent, now=NOW, state_dir=oq.state_dir_for(PID))
    (point,) = _points()
    assert point["state"] == "open" and not point["unknown_reden"]
