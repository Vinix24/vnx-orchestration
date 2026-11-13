"""impact_ledger: collect (fake gh runner, tmp_path ledger) and report (pure), ADR-074 besluit 7 and 8."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import impact_ledger as ledger  # noqa: E402

REPO = "owner/repo"


def _shadow(run_id, attempt=1, *, reconstructible=True, valid=True, reduced=True, failed=(), misses=()):
    return {"schema": "impact_shadow/1", "run_id": str(run_id), "run_attempt": str(attempt),
            "reconstructible": reconstructible, "attempt_valid": valid,
            "invalid_reasons": [] if valid else [{"reason": "stale_evidence", "detail": "x"}],
            "reduced": reduced, "sets": {"F": list(failed), "misses": list(misses)}}


def _job(run_id, attempt=1, *, wall=300, ran=True, complete=True):
    return {"schema": "impact_shadow_job/1", "run_id": str(run_id), "run_attempt": str(attempt),
            "wall_seconds": wall, "ran": ran, "complete": complete}


def _jobs(profile_a_minutes=40, *, profile_a=True):
    jobs = [{"name": ledger.SHADOW_JOB, "started_at": "2026-11-13T10:00:00Z",
             "completed_at": "2026-11-13T10:05:00Z", "conclusion": "success"}]
    if profile_a:
        jobs.append({"name": ledger.PROFILE_A_JOB, "started_at": "2026-11-13T10:00:00Z",
                     "completed_at": f"2026-11-13T10:{profile_a_minutes:02d}:00Z", "conclusion": "success"})
    return {"jobs": jobs}


class FakeGh:
    """Answers the four gh calls collect makes; artifacts are written where `-D` points."""

    def __init__(self, runs, artifacts, jobs=None, fail_download=None):
        self.runs, self.artifacts, self.jobs = runs, artifacts, jobs or {}
        self.fail_download, self.calls = fail_download, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": REPO})
        if argv[:3] == ["gh", "run", "list"]:
            return json.dumps(self.runs)
        if argv[:3] == ["gh", "run", "download"]:
            run_id, name, target = int(argv[3]), argv[5], argv[7]
            if self.fail_download == run_id:
                raise ledger.RunnerError(argv, 1, "HTTP 403: Attention Required")
            record = self.artifacts.get((run_id, name))
            if record is None:
                raise ledger.RunnerError(argv, 1, "no valid artifacts found to download")
            Path(target, "record.json").write_text(json.dumps(record), encoding="utf-8")
            return ""
        if argv[:2] == ["gh", "api"]:
            run_id = int(argv[2].split("/runs/")[1].split("/")[0])
            return json.dumps(self.jobs.get(run_id, _jobs()))
        raise AssertionError(f"unexpected gh call {argv}")


def _run(run_id, attempt=1, conclusion="success"):
    return {"databaseId": run_id, "attempt": attempt, "headSha": "a" * 40, "headBranch": "main",
            "event": "push", "conclusion": conclusion, "createdAt": "2026-11-13T09:00:00Z"}


def _lines(state):
    return ledger.read_ledger(ledger.ledger_path(state))


def test_collect_two_runs_one_without_shadow_artifact(tmp_path):
    gh = FakeGh([_run(1), _run(2)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1),
                                     (2, "impact-shadow-job"): _job(2)})
    result = ledger.collect(tmp_path, gh)
    assert result["added"] == ["1/1", "2/1"] and result["failed"] == []
    first, second = _lines(tmp_path)
    assert first["schema"] == "impact_ledger/1" and first["missing"] == [] and first["shadow"]["run_id"] == "1"
    assert first["profile_a_job"]["seconds"] == 2400 and first["shadow_job"]["conclusion"] == "success"
    assert second["shadow"] is None and "artifact:impact-shadow" in second["missing"]
    assert any(call[:2] == ["gh", "api"] and REPO in call[2] for call in gh.calls)


def test_collect_is_idempotent_and_a_new_attempt_is_a_new_line(tmp_path):
    artifacts = {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1)}
    ledger.collect(tmp_path, FakeGh([_run(1)], artifacts))
    path = ledger.ledger_path(tmp_path)
    before = path.read_bytes()
    again = ledger.collect(tmp_path, FakeGh([_run(1)], artifacts))
    assert again["added"] == [] and again["skipped"] == ["1/1"] and path.read_bytes() == before
    artifacts.update({(1, "impact-shadow"): _shadow(1, 2), (1, "impact-shadow-job"): _job(1, 2)})
    ledger.collect(tmp_path, FakeGh([_run(1, 2)], artifacts))
    assert path.read_bytes().startswith(before) and len(_lines(tmp_path)) == 2
    assert path.read_bytes().splitlines()[0] == before.splitlines()[0]


def test_collect_does_not_freeze_a_gh_failure_as_missing(tmp_path):
    gh = FakeGh([_run(1), _run(2)], {(2, "impact-shadow"): _shadow(2), (2, "impact-shadow-job"): _job(2)}, fail_download=1)
    result = ledger.collect(tmp_path, gh)
    assert result["added"] == ["2/1"] and result["failed"][0]["attempt"] == "1/1"
    assert [e["run_id"] for e in _lines(tmp_path)] == [2]


def test_collect_does_not_attribute_a_record_of_another_attempt(tmp_path):
    gh = FakeGh([_run(1, 2)], {(1, "impact-shadow"): _shadow(1, 1), (1, "impact-shadow-job"): _job(1, 2)})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    assert entry["shadow"] is None and "artifact:impact-shadow:attempt_mismatch" in entry["missing"]


def test_collect_names_a_missing_profile_a_job(tmp_path):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1)}, jobs={1: _jobs(profile_a=False)})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    assert entry["profile_a_job"] is None and entry["missing"] == ["job:profile-a"]


def _entry(run_id, shadow, job=None, *, conclusion="success", profile_a_seconds=2400, profile_a_conclusion="success"):
    return {"run_id": run_id, "run_attempt": 1, "run_conclusion": conclusion, "shadow": shadow, "job": job, "missing": [],
            "profile_a_job": {"conclusion": profile_a_conclusion, "seconds": profile_a_seconds}}


def test_report_excludes_by_name_with_reason():
    rep = ledger.report([
        _entry(1, _shadow(1)),
        _entry(2, _shadow(2, reconstructible=False)),
        _entry(3, _shadow(3, valid=False)),
        _entry(4, None),
        _entry(5, _shadow(5), conclusion="cancelled"),
        _entry(6, _shadow(6), profile_a_conclusion=None),
        _entry(7, _shadow(7, reconstructible=None)),
        {"_unreadable": 9},
    ])
    assert rep["counted"] == ["1/1"]
    assert {e["attempt"]: e["reason"] for e in rep["excluded"]} == {
        "2/1": "onreconstrueerbaar", "3/1": "invalid:stale_evidence", "4/1": "missing:impact-shadow",
        "5/1": "not_completed", "6/1": "missing:job:profile-a", "7/1": "onreconstrueerbaar", "line 9": "unreadable"}


def test_report_treats_unknown_or_malformed_fields_as_not_counted():
    broken_sets = _shadow(1)
    broken_sets["sets"] = {"F": None}
    rep = ledger.report([_entry(1, broken_sets), _entry(2, _shadow(2, valid=None)), _entry(3, "not-a-dict")])
    assert rep["counted"] == []
    assert [e["reason"] for e in rep["excluded"]] == ["invalid:sets_unreadable", "invalid:stale_evidence", "missing:impact-shadow"]


def test_report_misses_only_over_reduced_attempts():
    rep = ledger.report([
        _entry(1, _shadow(1, reduced=False, failed=["tests/test_a.py::t"], misses=["tests/test_a.py::t"])),
        _entry(2, _shadow(2, reduced=True, failed=["tests/test_b.py::t"], misses=["tests/test_b.py::t"])),
    ])
    assert rep["misses"] == [{"test_id": "tests/test_b.py::t", "run_id": 2, "run_attempt": 1}]
    assert rep["reduced_share"] == {"reduced": 1, "counted": 2, "fraction": 0.5}


def test_report_counts_red_runs_on_their_own():
    rep = ledger.report([_entry(1, _shadow(1, failed=["tests/x.py::t"])), _entry(2, _shadow(2, reduced=False, failed=["tests/y.py::t"])),
                         _entry(3, _shadow(3))])
    assert rep["red_runs"] == {"counted": 2, "reduced": 1} and rep["misses"] == []


def test_report_wall_clock_leaves_out_a_job_that_did_not_run_but_still_counts_it():
    rep = ledger.report([
        _entry(1, _shadow(1), _job(1, wall=300), profile_a_seconds=2400),
        _entry(2, _shadow(2), _job(2, wall=500), profile_a_seconds=3000),
        _entry(3, _shadow(3), _job(3, wall=0, ran=False, complete=False), profile_a_seconds=9999),
        _entry(4, _shadow(4), _job(4, complete=False), profile_a_seconds=9999),
    ])
    assert len(rep["counted"]) == 4
    wall = rep["wall_clock"]
    assert wall["n"] == 2 and wall["attempts"] == ["1/1", "2/1"]
    assert wall["shadow_job_seconds"] == {"median": 400, "sum": 800}
    assert wall["profile_a_seconds"] == {"median": 2700, "sum": 5400}


def test_report_over_nothing_is_empty_not_an_error():
    rep = ledger.report([])
    assert rep["counted"] == [] and rep["reduced_share"]["fraction"] is None and rep["wall_clock"]["shadow_job_seconds"]["median"] is None


def test_read_ledger_names_an_unreadable_line(tmp_path):
    path = ledger.ledger_path(tmp_path)
    path.write_text('{"run_id": 1, "run_attempt": 1}\nnot json\n[1]\n', encoding="utf-8")
    assert [e.get("_unreadable") for e in ledger.read_ledger(path)] == [None, 2, 3]


@pytest.mark.parametrize("text", ["soon", "3x", "2026-13-45"])
def test_parse_since_refuses_bad_input(text):
    with pytest.raises(ValueError):
        ledger.parse_since(text)
