"""impact_ledger: collect (fake gh runner, tmp_path ledger), ADR-074 besluit 7 and 8."""

from __future__ import annotations

import json
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


def test_read_ledger_names_an_unreadable_line(tmp_path):
    path = ledger.ledger_path(tmp_path)
    path.write_text('{"run_id": 1, "run_attempt": 1}\nnot json\n[1]\n', encoding="utf-8")
    assert [e.get("_unreadable") for e in ledger.read_ledger(path)] == [None, 2, 3]
