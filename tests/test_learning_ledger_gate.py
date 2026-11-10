"""D6 (self-learning-loop-reactivate): ``ledger_verdict/1`` on real ledgers (dlv-27fbb7f85633).

The real writers fill a tmp_path state dir; the building blocks run for real and are only
wrapped where a case needs a value they cannot produce (unknown status, missing field).
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts/lib", "scripts", "tests"):
    sys.path.insert(0, str(VNX_ROOT / sub))

import dispatch_start  # noqa: E402
import learning_ledger_gate as gate  # noqa: E402
from append_receipt_internals import lifecycle as append_lifecycle  # noqa: E402
from test_k9_lifecycle_count import (  # noqa: E402,F401  (fixtures and real-writer helpers)
    DEADLINE, PROJECT, ROLE, START, _append_raw, _at, _begin, _finish, _handover,
    _hermetic_append, _insert_row, _ledger, _sweep_heartbeat, data_dir, lifecycle_on, state_dir,
)

NOW = _at(600)  # ten minutes after the START every start line carries
REAL_SHA = append_lifecycle.writer_sha()
DELETE = object()  # marks a key the wrapped building block drops
VERDICT_FILE = "learning_ledger_verdict.json"

def _verdict(state_dir, now=NOW, sweep=60, **kwargs) -> dict:
    if sweep is not None:
        _sweep_heartbeat(state_dir, now - timedelta(seconds=sweep))
    return gate.ledger_verdict(state_dir, PROJECT, now=now, **kwargs)

def _ended(state_dir, dispatch_id="d-ok") -> None:
    _finish(state_dir, _begin(state_dir, dispatch_id))

def _begin_live(state_dir, dispatch_id) -> None:
    """A start line whose door is this very process: the real liveness reads live."""
    spec = state_dir.parent / "dispatches" / "completed" / dispatch_id / "dispatch-spec.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(json.dumps({"dispatch_id": dispatch_id, "deadline_seconds": DEADLINE}))
    with patch.object(dispatch_start, "_now", lambda: START):
        dispatch_start.begin(project_id=PROJECT, dispatch_id=dispatch_id, role=ROLE,
                             base_sha="a" * 40, spec_file=spec, target_slot="T1",
                             state_dir=state_dir)

def _line(dispatch_id, event="dispatch_finished", version=3, **extra) -> dict:
    return {"timestamp": START, "event_type": event, "schema_version": version,
            "project_id": PROJECT, "dispatch_id": dispatch_id, "attempt": 1,
            "end_state": "completed", "head_sha": "d" * 40, "writer_sha": REAL_SHA, **extra}

def _refused(verdict, code) -> None:
    assert verdict["status"] == "refused" and verdict["reason_code"] == code, verdict
    assert verdict["reason"], verdict

def _wrapped(**overrides):
    real = gate.compute_lifecycle_count

    def run(*args, **kwargs):
        count = real(*args, **kwargs)
        for key, value in overrides.items():
            count.pop(key, None) if value is DELETE else count.__setitem__(key, value)
        return count
    return run

def _defect_end_without_start(sd):
    _finish(sd, _handover("d-ews"))

def _defect_double_end(sd):
    _append_raw(sd, _line("d-ok"))

def _defect_projection_conflict(sd):
    _insert_row(sd, "d-conf", state="expired")
    _finish(sd, _begin(sd, "d-conf"))

def _defect_provenance_unknown(sd):
    _begin(sd, "d-prov")
    _append_raw(sd, _line("d-prov", writer_sha="f" * 40))

def test_one_ended_and_no_defects_is_ok(state_dir, lifecycle_on):
    _ended(state_dir)

    verdict = _verdict(state_dir)

    assert verdict["status"] == "ok" and verdict["reason_code"] == "ok", verdict
    assert set(verdict) == {"schema", "evaluated_at", "project_id", "cohort", "status",
                            "reason_code", "reason", "counts"}
    assert (verdict["schema"], verdict["project_id"], verdict["cohort"], verdict["evaluated_at"]) == (
        "ledger_verdict/1", PROJECT, "all_v3", "2026-11-01T10:10:00Z")
    assert verdict["counts"]["ended"] == 1 and verdict["counts"]["in_flight"] == 0

@pytest.mark.parametrize("defect", [
    _defect_end_without_start, _defect_double_end, _defect_projection_conflict,
    _defect_provenance_unknown])
def test_each_defect_is_refused_under_its_own_name(state_dir, lifecycle_on, defect):
    _ended(state_dir)
    defect(state_dir)

    verdict = _verdict(state_dir)

    _refused(verdict, defect.__name__.removeprefix("_defect_"))
    assert verdict["counts"][verdict["reason_code"]] == 1

@pytest.mark.parametrize("sweep", [601, 86400, None])  # None: no heartbeat written at all
def test_a_stalled_or_absent_sweep_is_refused(state_dir, lifecycle_on, sweep):
    _ended(state_dir)

    _refused(_verdict(state_dir, sweep=sweep), "processor_stalled")

def test_pytest_noise_counts_only_among_the_v3_cohort(state_dir, lifecycle_on):
    _ended(state_dir)
    _append_raw(state_dir, _line("d-old", "task_complete", version=2, source="pytest"))
    assert _verdict(state_dir)["status"] == "ok"  # a legacy-schema line is not the cohort

    _append_raw(state_dir, _line("d-noise", "task_complete", source="pytest", status="failed"))

    _refused(_verdict(state_dir), "noise_in_cohort")

def test_an_empty_cohort_is_refused_and_a_legacy_dispatch_does_not_fill_it(state_dir, lifecycle_on):
    _ledger(state_dir).write_text("", encoding="utf-8")
    _refused(_verdict(state_dir), "empty_cohort")
    _append_raw(state_dir, _line("d-legacy", "task_complete", version=2, status="success"))
    _refused(_verdict(state_dir), "empty_cohort")  # a legacy dispatch is no evidence...

    _ended(state_dir)
    verdict = _verdict(state_dir)

    assert verdict["status"] == "ok" and verdict["counts"]["ended"] == 1, verdict  # ...nor a reason

def test_a_live_start_younger_than_a_day_beside_an_ended_is_pending(state_dir, lifecycle_on):
    _ended(state_dir)
    _begin_live(state_dir, "d-live")

    verdict = _verdict(state_dir)

    assert verdict["status"] == "pending" and verdict["reason_code"] == "pending", verdict
    assert verdict["counts"]["in_flight"] == 1

def test_a_dead_start_without_an_end_is_refused(state_dir, lifecycle_on):
    _ended(state_dir)
    _begin(state_dir, "d-dead")  # the recorded door start time is not this process's: dead

    _refused(_verdict(state_dir), "stale_in_flight")

def test_a_live_start_older_than_a_day_is_refused(state_dir, lifecycle_on):
    _ended(state_dir)
    _begin_live(state_dir, "d-old-live")

    _refused(_verdict(state_dir, now=_at(25 * 3600)), "stale_in_flight")

def test_a_ledger_with_a_hole_is_unmeasured_and_refused(state_dir, lifecycle_on):
    _ended(state_dir)
    with _ledger(state_dir).open("a", encoding="utf-8") as fh:
        fh.write("{this is not json}\n")
    _append_raw(state_dir, _line("d-after-the-hole"))

    _refused(_verdict(state_dir), "unmeasured")

@pytest.mark.parametrize("overrides, code", [
    ({"status": "mystery"}, "unmeasured"), ({"status": DELETE}, "unmeasured"),
    ({"counts": DELETE}, "invalid_input"), ({"counts": {"double_end": 0}}, "invalid_input"),
    ({"counts": dict.fromkeys(gate.DEFECTS, True)}, "invalid_input"),
    *[({"sweep_heartbeat_age_seconds": v}, "processor_stalled")
      for v in (DELETE, None, "5", True, -1, float("nan"))],
])
def test_an_unknown_missing_or_malformed_field_is_refused(state_dir, lifecycle_on, overrides, code):
    _ended(state_dir)

    with patch.object(gate, "compute_lifecycle_count", _wrapped(**overrides)):
        _refused(_verdict(state_dir), code)

def test_an_unexplained_start_without_an_end_is_refused(state_dir, lifecycle_on):
    _ended(state_dir)
    swe = {**dict.fromkeys(gate.DEFECTS, 0), "start_without_end": 1}
    with patch.object(gate, "compute_lifecycle_count", _wrapped(counts=swe)):
        _refused(_verdict(state_dir), "stale_in_flight")

def test_an_unknown_attempt_kind_is_refused(state_dir, lifecycle_on):
    _ended(state_dir)
    real = gate.attempt_verdict
    with patch.object(gate, "attempt_verdict",
                      lambda *a, **k: replace(real(*a, **k), kind="mystery")):
        _refused(_verdict(state_dir), "invalid_input")

@pytest.mark.parametrize("target, error, code", [
    ("read_ledger", OSError("boom"), "ledger_unreadable"),
    ("attempt_verdict", RuntimeError("boom"), "evaluation_failed")])
def test_an_error_from_a_building_block_is_refused_not_raised(state_dir, lifecycle_on, target,
                                                              error, code):
    _ended(state_dir)

    with patch.object(gate, target, side_effect=error):
        verdict = _verdict(state_dir)

    _refused(verdict, code)
    assert "boom" in verdict["reason"]

@pytest.mark.parametrize("project, now", [("  ", NOW), (None, NOW), (PROJECT, "yesterday-ish")])
def test_a_blank_project_or_an_unusable_clock_is_refused(state_dir, project, now):
    _refused(gate.ledger_verdict(state_dir, project, now=now), "invalid_input")

def test_the_activation_commit_defaults_to_the_environment(state_dir, lifecycle_on, monkeypatch):
    _ended(state_dir)
    monkeypatch.setenv("VNX_LEDGER_ACTIVATION_COMMIT", REAL_SHA)
    verdict = _verdict(state_dir)

    assert verdict["cohort"] == REAL_SHA and verdict["status"] == "ok", verdict

def _good(state_dir) -> dict:
    _ended(state_dir)
    return _verdict(state_dir)

def test_write_then_read_round_trips(state_dir, lifecycle_on):
    verdict = _good(state_dir)

    assert gate.write_verdict(state_dir, verdict) == state_dir / VERDICT_FILE
    assert gate.read_verdict(state_dir, now=NOW + timedelta(hours=1)) == verdict
    assert [p.name for p in state_dir.glob(VERDICT_FILE + "*")] == [VERDICT_FILE]  # no temp left

@pytest.mark.parametrize("text", [None, "{not json", "[]", "", '"ok"'])  # None: no file at all
def test_read_verdict_of_a_missing_or_corrupt_file_is_refused(state_dir, text):
    if text is not None:
        (state_dir / VERDICT_FILE).write_text(text, encoding="utf-8")

    _refused(gate.read_verdict(state_dir, now=NOW), "verdict_unreadable")

def test_read_verdict_of_a_stale_file_is_refused(state_dir, lifecycle_on):
    gate.write_verdict(state_dir, _good(state_dir))
    _refused(gate.read_verdict(state_dir, now=NOW + timedelta(hours=37)), "verdict_stale")
    assert gate.read_verdict(state_dir, now=NOW + timedelta(hours=35))["status"] == "ok"

@pytest.mark.parametrize("change", [
    {"schema": "ledger_verdict/2"}, {"status": "fine"}, {"evaluated_at": "yesterday"},
    {"counts": "none"}, {"reason_code": ""}, {"project_id": None}, {"extra": 1},
    {"reason": DELETE}, {"status": []}, {"status": {}}, {"schema": []}, {"counts": "x"},
])
def test_read_verdict_of_a_wrong_shape_is_refused(state_dir, lifecycle_on, change):
    verdict = {k: v for k, v in {**_good(state_dir), **change}.items() if v is not DELETE}
    (state_dir / VERDICT_FILE).write_text(json.dumps(verdict), encoding="utf-8")

    _refused(gate.read_verdict(state_dir, now=NOW), "verdict_unreadable")