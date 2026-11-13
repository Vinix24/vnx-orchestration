"""impact_ledger: collect (fake gh runner, tmp_path ledger), ADR-074 besluit 7 and 8."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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

    def __init__(self, runs, artifacts, jobs=None, fail_download=None, raises=None):
        self.runs, self.artifacts, self.jobs = runs, artifacts, jobs or {}
        self.fail_download, self.raises, self.calls = fail_download, raises or {}, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": REPO})
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
