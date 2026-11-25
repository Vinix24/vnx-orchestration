"""impact_ledger: collect (fake gh runner, tmp_path ledger), ADR-074 besluit 7 and 8."""

from __future__ import annotations

import json
import subprocess
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
    """Answers the four gh calls collect makes; artifacts are written where `-D` points.

    The layout is the real one: impact-shadow holds record.json, impact-shadow-job holds job.json
    plus the nested evidence/args/run.json run stamp.  ``raises`` maps a call (``"list"``,
    ``("download", run_id)``) to an exception the runner raises.
    """

    def __init__(self, runs, artifacts, jobs=None, fail_download=None, raises=None, shadow_runs=None):
        self.runs, self.artifacts, self.jobs = runs, artifacts, jobs or {}
        self.shadow_runs = shadow_runs or {}  # head sha -> the answer of `gh run list --workflow "Impact shadow"`
        self.fail_download, self.raises, self.calls = fail_download, raises or {}, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": REPO})
        if argv[:3] == ["gh", "run", "list"] and argv[argv.index("--workflow") + 1] == "Impact shadow":
            assert argv[argv.index("--event") + 1] == "pull_request"
            return json.dumps(self.shadow_runs.get(argv[argv.index("--commit") + 1], []))
        if argv[:3] == ["gh", "run", "list"]:
            if "list" in self.raises:
                raise self.raises["list"]
            return json.dumps(self.runs)
        if argv[:3] == ["gh", "run", "download"]:
            run_id, name, target = int(argv[3]), argv[5], argv[7]
            if ("download", run_id) in self.raises:
                raise self.raises[("download", run_id)]
            if self.fail_download == run_id:
                raise ledger.RunnerError(argv, 1, "HTTP 403: Attention Required")
            record = self.artifacts.get((run_id, name))
            if record is None:
                raise ledger.RunnerError(argv, 1, "no valid artifacts found to download")
            if name == "impact-shadow-job":
                stamp = Path(target, "evidence", "args", "run.json")
                stamp.parent.mkdir(parents=True)
                stamp.write_text(json.dumps({"run_id": str(run_id), "run_attempt": record.get("run_attempt")}), encoding="utf-8")
                Path(target, "job.json").write_text(json.dumps(record), encoding="utf-8")
            else:
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


def _sharded_jobs(*, drop=None, aggregator_end="10:07:00", shard_start="10:00:30", bad_time=None):
    spec = {ledger.PROFILE_A_JOB: ("10:06:00", aggregator_end)}
    spec["Profile A guards"] = ("10:00:10", "10:03:00")
    for k in range(1, 5):
        spec[f"Profile A shard ({k}/4)"] = (shard_start if k == 2 else "10:01:00", "10:05:30")
    jobs = [{"name": ledger.SHADOW_JOB, "started_at": "2026-11-13T10:00:00Z",
             "completed_at": "2026-11-13T10:05:00Z", "conclusion": "success"}]
    for name, (start, end) in spec.items():
        if name != drop:
            job = {"name": name, "started_at": f"2026-11-13T{start}Z", "completed_at": f"2026-11-13T{end}Z",
                   "conclusion": "success"}
            if name == bad_time:
                job["started_at"] = "not-a-time"
            jobs.append(job)
    return {"jobs": jobs}


def _timing_of(tmp_path, jobs):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1)}, jobs={1: jobs})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    return entry


def test_sharded_profile_a_spans_the_six_jobs_not_the_aggregator_alone(tmp_path):
    entry = _timing_of(tmp_path, _sharded_jobs())
    assert entry["profile_a_job"] == {"started_at": "2026-11-13T10:00:10Z", "completed_at": "2026-11-13T10:07:00Z",
                                      "conclusion": "success", "seconds": 410, "shape": "sharded", "jobs": 6}
    assert entry["missing"] == []


def test_single_job_profile_a_is_what_timing_gave_plus_the_shape(tmp_path):
    entry = _timing_of(tmp_path, _jobs(40))
    assert entry["profile_a_job"] == {"started_at": "2026-11-13T10:00:00Z", "completed_at": "2026-11-13T10:40:00Z",
                                      "conclusion": "success", "seconds": 2400, "shape": "single", "jobs": 1}


@pytest.mark.parametrize("drop", [ledger.PROFILE_A_JOB, "Profile A guards", "Profile A shard (3/4)"])
def test_incomplete_sharded_run_gives_no_timing_and_names_the_missing_job(tmp_path, drop):
    entry = _timing_of(tmp_path, _sharded_jobs(drop=drop))
    assert entry["profile_a_job"] is None and entry["missing"] == [f"job:profile-a:incomplete:{drop}"]


@pytest.mark.parametrize("name", [ledger.PROFILE_A_JOB, "Profile A shard (1/4)"])
def test_sharded_job_without_a_parseable_time_gives_no_partial_span(tmp_path, name):
    entry = _timing_of(tmp_path, _sharded_jobs(bad_time=name))
    assert entry["profile_a_job"] is None and entry["missing"] == [f"job:profile-a:unparseable_time:{name}"]


@pytest.mark.parametrize("field", ["completed_at", None])
def test_sharded_aggregator_without_completion_or_with_a_negative_span_is_none(field):
    jobs = _sharded_jobs()["jobs"]
    aggregator = next(j for j in jobs if j["name"] == ledger.PROFILE_A_JOB)
    if field:
        aggregator[field] = None
        expected = f"job:profile-a:unparseable_time:{ledger.PROFILE_A_JOB}"
    else:
        aggregator["completed_at"] = "2026-11-13T09:00:00Z"
        expected = "job:profile-a:negative_span"
    assert ledger.profile_a_timing(jobs) == (None, expected)


def test_profile_a_part_jobs_are_the_needs_of_the_aggregator_by_name():
    import yaml
    jobs = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github/workflows/vnx-ci.yml").read_text())["jobs"]
    aggregator = next(j for j in jobs.values() if j.get("name") == ledger.PROFILE_A_JOB)
    assert sorted(jobs[k]["name"] for k in aggregator["needs"]) == sorted(ledger.PROFILE_A_PART_JOBS)


def test_non_dict_jobs_and_no_jobs_give_no_timing():
    assert ledger.profile_a_timing([]) == (None, "job:profile-a")
    assert ledger.profile_a_timing(["x", None]) == (None, "job:profile-a")


def test_collect_names_a_missing_profile_a_job(tmp_path):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1)}, jobs={1: _jobs(profile_a=False)})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    assert entry["profile_a_job"] is None and entry["missing"] == ["job:profile-a"]


def test_read_ledger_names_an_unreadable_line(tmp_path):
    path = ledger.ledger_path(tmp_path)
    path.write_text('{"run_id": 1, "run_attempt": 1}\nnot json\n[1]\n', encoding="utf-8")
    assert [e.get("_unreadable") for e in ledger.read_ledger(path)] == [None, 2, 3]


def test_collect_stores_job_json_not_the_nested_run_stamp(tmp_path):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1, wall=777)})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    assert entry["job"]["schema"] == "impact_shadow_job/1" and entry["job"]["wall_seconds"] == 777
    assert entry["missing"] == []


def test_collect_names_a_record_with_another_schema(tmp_path):
    wrong = dict(_job(1), schema="impact_shadow_job/2")
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): wrong})
    ledger.collect(tmp_path, gh)
    (entry,) = _lines(tmp_path)
    assert entry["job"] is None and entry["missing"] == ["artifact:impact-shadow-job:invalid_schema"]


def _gh_with_shadow_file(filename, payload):
    class Gh(FakeGh):
        def __call__(self, argv):
            if argv[:3] == ["gh", "run", "download"] and argv[5] == "impact-shadow":
                Path(argv[7], filename).write_text(payload, encoding="utf-8")
                return ""
            return super().__call__(argv)

    return Gh([_run(1)], {(1, "impact-shadow-job"): _job(1)})


def test_collect_refuses_a_record_that_is_not_an_object_or_not_json(tmp_path):
    for label, payload in (("array", "[1, 2]"), ("text", "not json")):
        state = tmp_path / label
        ledger.collect(state, _gh_with_shadow_file("record.json", payload))
        (entry,) = _lines(state)
        assert entry["shadow"] is None and entry["missing"] == ["artifact:impact-shadow:invalid_schema"]


def test_collect_names_an_artifact_without_its_record_file(tmp_path):
    ledger.collect(tmp_path, _gh_with_shadow_file("other.json", json.dumps(_shadow(1))))
    (entry,) = _lines(tmp_path)
    assert entry["shadow"] is None and entry["missing"] == ["artifact:impact-shadow"]


def test_collect_download_timeout_fails_that_attempt_and_goes_on(tmp_path):
    timeout = ledger.RunnerError(["gh", "run", "download"], -1, "timed out after 300s")
    gh = FakeGh([_run(1), _run(2)], {(2, "impact-shadow"): _shadow(2), (2, "impact-shadow-job"): _job(2)},
                raises={("download", 1): timeout})
    result = ledger.collect(tmp_path, gh)
    assert result["added"] == ["2/1"] and [f["attempt"] for f in result["failed"]] == ["1/1"]
    assert [e["run_id"] for e in _lines(tmp_path)] == [2]


def test_default_runner_turns_timeout_and_oserror_into_runner_errors(monkeypatch):
    for error, wanted in ((subprocess.TimeoutExpired(["gh"], 300), "timed out"), (FileNotFoundError("gh"), "cannot run gh")):
        def run(argv, _error=error, **kwargs):
            assert kwargs.get("timeout")
            raise _error

        monkeypatch.setattr(ledger.subprocess, "run", run)
        try:
            ledger.default_runner(["gh", "run", "list"])
        except ledger.RunnerError as exc:
            assert wanted in str(exc)
        else:
            raise AssertionError("no RunnerError")


def test_collect_discovery_failure_writes_no_line_and_names_the_failure(tmp_path):
    failure = ledger.RunnerError(["gh", "run", "list"], -1, "cannot run gh: OSError")
    result = ledger.collect(tmp_path, FakeGh([_run(1)], {}, raises={"list": failure}))
    assert result["added"] == [] and result["skipped"] == []
    assert result["failed"][0]["attempt"] == "discovery" and "cannot run gh" in result["failed"][0]["error"]
    assert not ledger.ledger_path(tmp_path).exists()


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


def test_report_excludes_an_unreadable_or_mismatched_shadow_record_by_name():
    invalid = _entry(1, None)
    invalid["missing"] = ["artifact:impact-shadow:invalid_schema", "artifact:impact-shadow-job"]
    mismatch = _entry(2, None)
    mismatch["missing"] = ["artifact:impact-shadow-job", "artifact:impact-shadow:attempt_mismatch"]
    only_job = _entry(3, None)
    only_job["missing"] = ["artifact:impact-shadow-job:invalid_schema"]
    rep = ledger.report([invalid, mismatch, only_job])
    assert rep["counted"] == []
    assert {e["attempt"]: e["reason"] for e in rep["excluded"]} == {
        "1/1": "missing:impact-shadow:invalid_schema", "2/1": "missing:impact-shadow:attempt_mismatch",
        "3/1": "missing:impact-shadow"}


def _backtest(entry):
    return dict(entry, source="backtest")


def test_report_counts_live_and_backtest_apart_and_together():
    miss = _shadow(2, failed=["tests/b.py::t"], misses=["tests/b.py::t"])
    rep = ledger.report([_entry(1, _shadow(1, failed=["tests/a.py::t"])), _backtest(_entry(2, miss)),
                         _backtest(_entry(3, _shadow(3, reduced=False))), _backtest(_entry(4, None)), _backtest({"_unreadable": 5})])
    live, back = rep["by_source"]["live"], rep["by_source"]["backtest"]
    assert live["counted"] == ["1/1"] and live["misses"] == [] and live["red_runs"] == {"counted": 1, "reduced": 1}
    assert back["counted"] == ["2/1", "3/1"] and len(back["misses"]) == 1 and back["reduced_share"]["reduced"] == 1
    assert back["red_runs"] == {"counted": 1, "reduced": 1}
    assert {e["attempt"]: e["reason"] for e in back["excluded"]} == {"4/1": "missing:impact-shadow", "backtest line 5": "unreadable"}
    assert rep["counted"] == ["1/1", "2/1", "3/1"] and rep["attempts_in_ledger"] == 5
    assert rep["misses"] == back["misses"] and rep["red_runs"] == {"counted": 2, "reduced": 2}
    assert rep["reduced_share"] == {"reduced": 2, "counted": 3, "fraction": 0.6667} and live["attempts_in_ledger"] == 1


def test_report_treats_a_line_without_source_or_with_another_source_as_live():
    rep = ledger.report([_entry(1, _shadow(1)), dict(_entry(2, _shadow(2)), source="other")])
    assert rep["by_source"]["live"]["counted"] == ["1/1", "2/1"] and rep["by_source"]["backtest"]["counted"] == []


def test_report_names_the_unreadable_inputs_of_a_backtest_record():
    unreadable = {"schema": "impact_shadow/1", "reconstructible": False, "unreadable_inputs": ["base"]}
    rep = ledger.report([_backtest(_entry(1, unreadable)), _entry(2, _shadow(2, reconstructible=False))])
    assert [e["reason"] for e in rep["excluded"]] == ["onreconstrueerbaar", "onreconstrueerbaar:base"]


@pytest.mark.parametrize("text", ["soon", "3x", "2026-13-45"])
def test_parse_since_refuses_bad_input(text):
    with pytest.raises(ValueError):
        ledger.parse_since(text)


# --- the shadow job in its own workflow: paired with the VNX CI run by head sha ---------------

SHA = "a" * 40
NOW = ledger.parse_time("2026-11-13T09:40:00Z")


def _pair(run_id, attempt=1, status="completed"):
    return {"databaseId": run_id, "attempt": attempt, "status": status,
            "conclusion": "success" if status == "completed" else None, "createdAt": "2026-11-13T09:01:00Z"}


def _jobs_without_shadow():
    return {"jobs": [j for j in _jobs()["jobs"] if j["name"] != ledger.SHADOW_JOB]}


def _shadow_jobs(minutes=17):
    return {"jobs": [{"name": ledger.SHADOW_JOB, "started_at": "2026-11-13T10:00:00Z",
                      "completed_at": f"2026-11-13T10:{minutes:02d}:00Z", "conclusion": "success"}]}


def _new_shape(shadow_runs, artifacts=None, jobs=None, run=None):
    artifacts = artifacts if artifacts is not None else {(1, "impact-shadow"): _shadow(1), (9, "impact-shadow-job"): _job(9, 1, wall=999)}
    return FakeGh([run or {**_run(1), "event": "pull_request"}], artifacts, jobs={1: _jobs_without_shadow(), 9: _shadow_jobs(), **(jobs or {})},
                  shadow_runs={SHA: shadow_runs})


def test_new_shape_takes_the_job_record_and_timing_from_the_paired_run(tmp_path):
    result = ledger.collect(tmp_path, _new_shape([_pair(9)]), now=NOW)
    assert result["added"] == ["1/1"] and result["failed"] == []
    (entry,) = _lines(tmp_path)
    assert entry["job"]["wall_seconds"] == 999 and entry["shadow_job"]["seconds"] == 1020
    assert entry["shadow_run_id"] == 9 and entry["shadow_run_attempt"] == 1 and entry["missing"] == []


def test_new_shape_attributes_the_job_record_against_the_shadow_runs_attempt(tmp_path):
    gh = _new_shape([_pair(9, 2)], {(1, "impact-shadow"): _shadow(1), (9, "impact-shadow-job"): _job(9, 2)}, jobs={})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["job"] is not None and not any("attempt_mismatch" in m for m in entry["missing"])
    assert entry["shadow_run_attempt"] == 2 and entry["run_attempt"] == 1
    assert any(c[:2] == ["gh", "api"] and "/runs/9/attempts/2/" in c[2] for c in gh.calls)


def test_new_shape_a_job_record_stamped_with_the_other_attempt_is_still_refused(tmp_path):
    gh = _new_shape([_pair(9, 2)], {(1, "impact-shadow"): _shadow(1), (9, "impact-shadow-job"): _job(9, 1)})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["job"] is None and "artifact:impact-shadow-job:attempt_mismatch" in entry["missing"]


def test_shadow_run_in_progress_writes_no_line_until_it_completes(tmp_path):
    first = ledger.collect(tmp_path, _new_shape([_pair(9, status="in_progress")]), now=NOW)
    assert first["added"] == [] and first["failed"] == [{"attempt": "1/1", "error": "shadow_run_pending"}]
    assert _lines(tmp_path) == []
    second = ledger.collect(tmp_path, _new_shape([_pair(9)]), now=NOW)
    assert second["added"] == ["1/1"] and len(_lines(tmp_path)) == 1


@pytest.mark.parametrize("created, pending", [("2026-11-13T09:10:00Z", True), ("2026-11-13T06:40:00Z", False)])
def test_no_shadow_run_is_pending_for_two_hours_and_missing_after(tmp_path, created, pending):
    gh = _new_shape([], {(1, "impact-shadow"): _shadow(1)}, run={**_run(1), "event": "pull_request", "createdAt": created})
    result = ledger.collect(tmp_path, gh, now=NOW)
    if pending:
        assert result["added"] == [] and result["failed"][0]["error"] == "shadow_run_pending" and _lines(tmp_path) == []
        return
    (entry,) = _lines(tmp_path)
    assert {"job:impact-shadow", "artifact:impact-shadow-job"} <= set(entry["missing"])
    assert entry["shadow_run_id"] is None and entry["shadow_run_attempt"] is None
    assert not any(c[:3] == ["gh", "run", "download"] and c[5] == "impact-shadow-job" for c in gh.calls)


def test_an_unreadable_created_at_without_a_shadow_run_stays_pending(tmp_path):
    gh = _new_shape([], {}, run={**_run(1), "event": "pull_request", "createdAt": None})
    result = ledger.collect(tmp_path, gh, now=NOW)
    assert result["failed"][0]["error"] == "shadow_run_pending" and _lines(tmp_path) == []


def test_two_shadow_runs_for_one_sha_use_the_newest(tmp_path):
    artifacts = {(1, "impact-shadow"): _shadow(1), (9, "impact-shadow-job"): _job(9, wall=111), (12, "impact-shadow-job"): _job(12, wall=222)}
    gh = _new_shape([_pair(9), _pair(12), _pair(10)], artifacts, jobs={12: _shadow_jobs(20)})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["shadow_run_id"] == 12 and entry["job"]["wall_seconds"] == 222 and entry["shadow_job"]["seconds"] == 1200


def test_old_shape_never_looks_up_the_shadow_workflow(tmp_path):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1), (1, "impact-shadow-job"): _job(1)})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["shadow_run_id"] is None and entry["shadow_run_attempt"] is None and entry["missing"] == []
    assert not any("Impact shadow" in c for c in gh.calls)


def test_a_failing_shadow_lookup_is_a_failure_not_a_hole(tmp_path):
    class Boom(FakeGh):
        def __call__(self, argv):
            if "Impact shadow" in argv:
                raise ledger.RunnerError(argv, 1, "HTTP 403")
            return super().__call__(argv)
    gh = Boom([{**_run(1), "event": "pull_request"}], {(1, "impact-shadow"): _shadow(1)}, jobs={1: _jobs_without_shadow()})
    result = ledger.collect(tmp_path, gh, now=NOW)
    assert result["added"] == [] and "403" in result["failed"][0]["error"] and _lines(tmp_path) == []


def test_a_push_run_is_written_at_once_without_a_shadow_lookup(tmp_path):
    gh = FakeGh([_run(1)], {(1, "impact-shadow"): _shadow(1)}, jobs={1: _jobs_without_shadow()})
    result = ledger.collect(tmp_path, gh, now=NOW)
    assert result["added"] == ["1/1"] and result["failed"] == []
    (entry,) = _lines(tmp_path)
    assert entry["event"] == "push" and entry["shadow_run_id"] is None and entry["shadow_run_attempt"] is None
    assert entry["missing"] == ["artifact:impact-shadow-job", "job:impact-shadow"]
    assert not any("Impact shadow" in c for c in gh.calls)


def test_a_run_without_an_event_is_not_written_early(tmp_path):
    gh = FakeGh([{**_run(1), "event": None}], {(1, "impact-shadow"): _shadow(1)}, jobs={1: _jobs_without_shadow()}, shadow_runs={SHA: []})
    result = ledger.collect(tmp_path, gh, now=NOW)
    assert result["failed"][0]["error"] == "shadow_run_pending" and _lines(tmp_path) == []


def _identified(record, base="b" * 40, candidate="c" * 40):
    return {**record, "base_sha": base, "candidate_sha": candidate}


def test_pairing_skips_a_newer_run_that_tested_another_candidate(tmp_path):
    artifacts = {(1, "impact-shadow"): _identified(_shadow(1)),
                 (12, "impact-shadow-job"): _identified(_job(12, wall=222), candidate="d" * 40),
                 (9, "impact-shadow-job"): _identified(_job(9, wall=111))}
    gh = _new_shape([_pair(9), _pair(12)], artifacts, jobs={12: _shadow_jobs(20)})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["shadow_run_id"] == 9 and entry["job"]["wall_seconds"] == 111 and entry["missing"] == []


def test_pairing_with_no_matching_candidate_is_pending_then_a_named_hole(tmp_path):
    artifacts = {(1, "impact-shadow"): _identified(_shadow(1)), (9, "impact-shadow-job"): _identified(_job(9), base="e" * 40)}
    young = {**_run(1), "event": "pull_request", "createdAt": "2026-11-13T09:10:00Z"}
    first = ledger.collect(tmp_path, _new_shape([_pair(9)], artifacts, run=young), now=NOW)
    assert first["failed"][0]["error"] == "shadow_run_pending" and _lines(tmp_path) == []
    ledger.collect(tmp_path, _new_shape([_pair(9)], artifacts), now=NOW + ledger.SHADOW_RUN_GRACE)
    (entry,) = _lines(tmp_path)
    assert "artifact:impact-shadow-job:candidate_mismatch" in entry["missing"]
    assert entry["job"] is None and entry["shadow_run_id"] is None


def test_pairing_without_a_shadow_record_takes_the_newest_run(tmp_path):
    artifacts = {(9, "impact-shadow-job"): _identified(_job(9, wall=111)), (12, "impact-shadow-job"): _identified(_job(12, wall=222), base="e" * 40)}
    gh = _new_shape([_pair(9), _pair(12)], artifacts, jobs={12: _shadow_jobs(20)})
    ledger.collect(tmp_path, gh, now=NOW)
    (entry,) = _lines(tmp_path)
    assert entry["shadow_run_id"] == 12 and "artifact:impact-shadow" in entry["missing"]
    assert entry["job"]["wall_seconds"] == 222
