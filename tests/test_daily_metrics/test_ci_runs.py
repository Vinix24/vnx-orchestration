"""ADR-097: exercise the writer with only the gh transport replaced."""
import copy
import importlib
import json
from datetime import date, datetime, timezone

import pytest

DAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 25, 4, tzinfo=timezone.utc)


def run_row(attempt=1, **changes):
    return dict(id=7, run_attempt=attempt, event="pull_request", status="completed",
                name="CI", head_sha="a" * 40, repository={"full_name": "org/repo"},
                pull_requests=[{"number": 3}], conclusion="success",
                run_started_at=f"2026-11-{22 + attempt}T23:58:00Z",
                updated_at=f"2026-11-{23 + attempt}T00:00:00Z", **changes)


def job(id=1, start="23:58:10", end="23:59:00", conclusion="success"):
    return dict(id=id, name=f"job-{id}", conclusion=conclusion,
                started_at=f"2026-11-23T{start}Z", completed_at=f"2026-11-23T{end}Z")


@pytest.fixture
def writer():
    return importlib.import_module("daily_metrics.sources.ci_runs")


def transport(rows, jobs=None, override=None):
    calls = []

    def runner(args, cwd):
        calls.append(args)
        endpoint = args[2]
        if override:
            response = override(args, len(calls))
            if response is not None:
                return response
        if args[1] == "repo":
            data = {"nameWithOwner": "org/repo"}
        elif "/jobs?" in endpoint:
            batch = jobs if jobs is not None else [job()]
            if jobs is None and "/attempts/2/" in endpoint:
                batch = json.loads(json.dumps(batch).replace("2026-11-23", "2026-11-24"))
            data = {"total_count": len(batch), "jobs": batch}
        elif "/attempts/" in endpoint:
            data = run_row(int(endpoint.rsplit("/", 1)[1]))
        else:
            data = {"total_count": len(rows), "workflow_runs": rows}
        return 0, json.dumps(data), ""
    return runner, calls


def execute(writer, tmp_path, rows=None, jobs=None, override=None, **options):
    runner, calls = transport(rows if rows is not None else [run_row()], jobs, override)
    result = writer.write_runs("vnx-dev", DAY, date(2026, 11, 24), now=NOW,
                               project_root=tmp_path, state_dir=tmp_path, runner=runner,
                               **options)
    path = tmp_path / "ci_runs.ndjson"
    records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return result, records, calls


def test_attempts_days_sources_and_idempotence(writer, tmp_path):
    result, records, calls = execute(writer, tmp_path, [run_row(2)])
    assert [(r["run_attempt"], r["day"]) for r in records] == [(1, "2026-11-23"), (2, "2026-11-24")]
    assert records[0]["wall_seconds"]["value"] == 120
    assert records[0]["queue_seconds"]["value"] == 10
    assert records[1]["queue_seconds"]["value"] == 10
    assert "/attempts/1" in records[0]["wall_seconds"]["source"]
    assert records[0]["failed_tests"]["unknown_reason"] == "junit_not_configured"
    assert result["coverage"] == "complete" and result["calls"] == len(calls)
    assert execute(writer, tmp_path, [run_row(2)])[0]["written"] == 0
    assert execute(writer, tmp_path, [run_row(2)])[0]["calls"] == 2
    assert len(execute(writer, tmp_path, [run_row(2)])[1]) == 2


@pytest.mark.parametrize("jobs,winner", [
    ([job(1, end="23:59:00"), job(2, start="23:58:50", end="23:59:01")], 2),
    ([job(2), job(1, start="23:58:20")], 2),
    ([job(2), job(1)], 1),
    ([job(1), job(2, end="23:59:50", conclusion="skipped")], 1),
])
def test_critical_path(writer, tmp_path, jobs, winner):
    assert execute(writer, tmp_path, jobs=jobs)[1][0]["critical_path_job"]["value"]["id"] == winner


@pytest.mark.parametrize("field", ["completed_at", "started_at", "conclusion"])
@pytest.mark.parametrize("bad", [None, "", "nonsense", {}, "MISSING"])
def test_unproven_job_is_not_excluded(writer, tmp_path, field, bad):
    broken = job(2)
    broken[field] = bad
    if bad == "MISSING":
        del broken[field]
    record = execute(writer, tmp_path, jobs=[job(), broken])[1][0]
    assert record["critical_path_job"]["value"] is None


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_excluded_events(writer, tmp_path, event):
    row = run_row()
    row["event"] = event
    result, records, _ = execute(writer, tmp_path, [row])
    assert not records and result["coverage"] == "complete"


@pytest.mark.parametrize("field", ["event", "status"])
@pytest.mark.parametrize("bad", [None, "", "alien", {}, "MISSING"])
def test_unknown_population_stays_incomplete(writer, tmp_path, field, bad):
    row = run_row()
    row[field] = bad
    if bad == "MISSING":
        del row[field]
    result, records, _ = execute(writer, tmp_path, [row])
    assert result["coverage"] == "incomplete" and result["population_total"] is None
    assert not records


def test_filter_and_configured_artifact(writer, tmp_path):
    assert not execute(writer, tmp_path, workflows="Other")[1]
    record = execute(writer, tmp_path, workflows="Other,CI", artifact="junit")[1][0]
    assert record["failed_tests"]["unknown_reason"] == "source_missing"
    assert "junit" in record["failed_tests"]["source"] and "M3b" in record["failed_tests"]["source"]


@pytest.mark.parametrize("head", [None, "b" * 40])
def test_saved_identity_cannot_clear_contradictory_head(writer, tmp_path, head):
    execute(writer, tmp_path)
    row = dict(run_row(), head_sha=head)
    result, records, _ = execute(writer, tmp_path, [row])
    assert result["coverage"] == "incomplete" and result["written"] == 0


@pytest.mark.parametrize("kind", ["wall", "queue"])
def test_negative_elapsed(writer, tmp_path, kind):
    def override(args, count):
        if kind == "wall" and args[2].endswith("/attempts/1"):
            row = run_row()
            row["updated_at"] = "2026-11-23T23:57:59Z"
            return 0, json.dumps(row), ""
    record = execute(writer, tmp_path, jobs=[job(start="23:57:59")], override=override)[1][0]
    assert record[f"{kind}_seconds"]["unknown_reason"] == "source_invalid"


@pytest.mark.parametrize("reason", ["call_budget_exhausted", "rate_limited"])
def test_stopped_jobs_are_complete_unknown_records_and_beacon(writer, tmp_path, reason):
    def override(args, count):
        if count == 5 and reason == "rate_limited":
            return 1, "", "HTTP 403 API rate limit exceeded"
    result, records, calls = execute(writer, tmp_path, [run_row(2)], override=override,
                                     max_calls=4 if reason == "call_budget_exhausted" else 50)
    assert len(records) == 2 and len(calls) == (4 if reason == "call_budget_exhausted" else 5)
    assert all(r[f]["unknown_reason"] == reason for r in records for f in writer.FIELDS)
    beacon = json.loads((tmp_path / "health/ci_runs.json").read_text())["details"]
    assert beacon["project_id"] == "vnx-dev" and beacon["reason"] == reason
    assert len(beacon["skipped_attempts"]) == 2 and result["coverage"] == "complete"


def test_incomplete_pagination_is_not_a_total(writer, tmp_path):
    def override(args, count):
        if count == 2:
            return 0, json.dumps({"total_count": 2, "workflow_runs": [run_row()]}), ""
    result, records, _ = execute(writer, tmp_path, override=override, max_calls=2)
    assert result["coverage"] == "incomplete" and result["population_total"] is None
    assert len(records) == 1
    assert all(records[0][f]["unknown_reason"] == "call_budget_exhausted" for f in writer.FIELDS)


def test_pages_and_unresolved_earlier_attempts(writer, tmp_path):
    def override(args, count):
        if "/actions/runs?" in args[2]:
            row = run_row()
            row["id"] = 7 if args[2].endswith("page=1") else 8
            return 0, json.dumps({"total_count": 2, "workflow_runs": [row]}), ""
        if args[2].endswith("/runs/8/attempts/1"):
            return 0, json.dumps(dict(run_row(), id=8)), ""
    result, records, _ = execute(writer, tmp_path, override=override)
    assert result["population_total"] == 2 and len(records) == 2
    result, records, _ = execute(writer, tmp_path / "stopped", [run_row(2)], max_calls=2)
    assert result["coverage"] == "incomplete" and len(records) == 1
    assert {s["run_attempt"] for s in result["skipped_attempts"]} == {1, 2}


@pytest.mark.parametrize("bad", [0, -1, True, "x", 1.5])
def test_invalid_budget_before_calls(writer, tmp_path, bad):
    with pytest.raises(ValueError):
        execute(writer, tmp_path, max_calls=bad)


@pytest.mark.parametrize("bad", ["..", "/tmp", "vnx-dev/../other"])
def test_project_path_refused(writer, tmp_path, bad):
    with pytest.raises(ValueError):
        writer.write_runs(bad, DAY, DAY, now=NOW, project_root=tmp_path, state_dir=tmp_path)


def test_ledger_validation_read_failure_and_short_write(writer, tmp_path, monkeypatch):
    _, records, _ = execute(writer, tmp_path)
    record = copy.deepcopy(records[0])
    record["wall_seconds"]["value"] = float("nan")
    with pytest.raises(ValueError):
        writer.append_records(tmp_path / "invalid.ndjson", [record])
    target = tmp_path / "short.ndjson"
    real_write = writer.os.write
    monkeypatch.setattr(writer.os, "write", lambda fd, data: real_write(fd, data[:11]))
    assert writer.append_records(target, records) == 1
    assert json.loads(target.read_text()) == records[0]
    with pytest.raises(OSError):
        writer.append_records(tmp_path, records)
    (tmp_path / "corrupt.ndjson").write_text("broken\n")
    with pytest.raises(ValueError):
        writer.append_records(tmp_path / "corrupt.ndjson", records)


def test_failed_write_rolls_back_uncommitted_tail(writer, tmp_path, monkeypatch):
    records = execute(writer, tmp_path)[1]
    path = tmp_path / "failure.ndjson"
    real_write, calls = writer.os.write, []
    def fail(fd, data):
        calls.append(1)
        return real_write(fd, data[:10]) if len(calls) == 1 else 0
    monkeypatch.setattr(writer.os, "write", fail)
    with pytest.raises(OSError):
        writer.append_records(path, records)
    assert path.read_bytes() == b""


@pytest.mark.parametrize("value", [None, "", {}, "alien", "MISSING"])
def test_conclusion_unknown_never_means_success(writer, tmp_path, value):
    def override(args, count):
        if count == 3:
            row = run_row()
            row["conclusion"] = value
            if value == "MISSING":
                del row["conclusion"]
            return 0, json.dumps(row), ""
    record = execute(writer, tmp_path, override=override)[1][0]
    assert record["conclusion"]["value"] is None



@pytest.mark.parametrize("response", [(None, "", "failed"), (1, "", "HTTP 502"),
                                       (0, "bad json", ""), (0, "[]", "")])
def test_failed_attempt_read_does_not_become_known(writer, tmp_path, response):
    def override(args, count):
        return response if count == 3 else None
    result, records, _ = execute(writer, tmp_path, override=override)
    assert result["coverage"] == "incomplete"
    assert all(records[0][name]["value"] is None for name in writer.FIELDS[:4])


def test_raising_runner_and_beacon_io_failure_are_visible(tmp_path, capsys):
    from daily_metrics.collect import collect_ci_runs
    def runner(args, cwd):
        raise OSError("gh unavailable")
    result = collect_ci_runs("vnx-dev", NOW, tmp_path, project_root=tmp_path, runner=runner)
    assert result["reason"] == "writer_failed" and result["coverage"] == "incomplete"
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "fail"
    blocked = tmp_path / "blocked"
    blocked.write_text("file instead of directory")
    result = collect_ci_runs("..", NOW, blocked, project_root=tmp_path, runner=runner)
    assert result["beacon_error"] and '"writer_failed"' in capsys.readouterr().err


def test_nightly_configuration_and_shared_window(tmp_path):
    from daily_metrics.collect import collect_ci_runs
    runner, calls = transport([run_row(2)])
    result = collect_ci_runs("vnx-dev", NOW, tmp_path, project_root=tmp_path, runner=runner)
    assert result["written"] == 2 and result["calls"] == len(calls)
    assert result["range"] == ["2026-11-11", "2026-11-24"]


@pytest.mark.parametrize("field", ["head_sha", "repository", "pull_requests", "run_attempt"])
def test_contradictory_or_missing_identity_is_incomplete(writer, tmp_path, field):
    def override(args, count):
        if count == 3:
            row = run_row()
            row[field] = None
            return 0, json.dumps(row), ""
    result, records, _ = execute(writer, tmp_path, override=override)
    assert result["coverage"] == "incomplete" and not records


def test_job_pagination_and_empty_jobs_remain_unknown(writer, tmp_path):
    def override(args, count):
        if "/jobs?" in args[2]:
            return 0, json.dumps({"total_count": 2, "jobs": [job()]}), ""
    record = execute(writer, tmp_path, override=override)[1][0]
    assert record["queue_seconds"]["unknown_reason"] == "source_invalid"
    assert record["critical_path_job"]["unknown_reason"] == "source_invalid"
    empty = tmp_path / "empty"
    record = execute(writer, empty, jobs=[])[1][0]
    assert record["critical_path_job"]["value"] is None


def test_ledger_concurrency_exact_identity_and_symlink_refusal(writer, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _, records, _ = execute(writer, tmp_path)
    other = dict(records[0], project_id="vnx-developer")
    path = tmp_path / "parallel.ndjson"
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: writer.append_records(path, records + [other]), range(8)))
    assert sum(counts) == 2 and len(path.read_text().splitlines()) == 2
    link = tmp_path / "link.ndjson"
    link.symlink_to(path)
    with pytest.raises(OSError):
        writer.append_records(link, records)


@pytest.mark.parametrize("change", [{"schema": "ci_run/2"}, {"conclusion": {}},
                                    {"event": None}, {"day": "2026-11-24"}])
def test_strict_envelope_validation(writer, tmp_path, change):
    record = execute(writer, tmp_path)[1][0]
    record.update(change)
    with pytest.raises(ValueError):
        writer.append_records(tmp_path / "invalid.ndjson", [record])
