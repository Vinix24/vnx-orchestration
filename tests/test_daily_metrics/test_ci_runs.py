"""ADR-097: exercise the writer with only the gh transport replaced."""
import copy
import importlib
import json
import subprocess
from datetime import date, datetime, timezone

import pytest

DAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 25, 4, tzinfo=timezone.utc)

def run_row(attempt=1, **changes):
    return dict(id=7, run_attempt=attempt, event="pull_request", status="completed",
                created_at="2026-11-23T23:58:00Z",
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
            data = {"nameWithOwner": "org/repo", "createdAt": "2026-11-01T00:00:00Z"}
        elif "/commits/" in endpoint:
            data = {"pull_requests": []}
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
                               project_root=tmp_path, state_dir=tmp_path / "state", runner=runner,
                               **options)
    path = tmp_path / "state/ci_runs.ndjson"
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
    assert "event=pull_request&created=2026-11-01T00:00:00Z..2026-11-24T23:59:59Z" in calls[1][2]
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

def test_old_run_with_recent_rerun_is_read(writer, tmp_path):
    row = dict(run_row(2), run_started_at="2026-11-01T00:00:00Z")
    result, records, _ = execute(writer, tmp_path, [row])
    assert result["coverage"] == "complete" and len(records) == 2

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
def test_stopped_reads_preserve_completed_attempt_and_retry_unknown(writer, tmp_path, reason):
    def override(args, count):
        if count == 5 and reason == "rate_limited":
            return 1, "", "HTTP 403 API rate limit exceeded"
    result, records, calls = execute(writer, tmp_path, [run_row(2)], override=override,
                                     max_calls=4 if reason == "call_budget_exhausted" else 50)
    assert len(records) == 2 and len(calls) == (4 if reason == "call_budget_exhausted" else 5)
    assert records[0]["queue_seconds"]["value"] == 10
    assert all(records[1][f]["unknown_reason"] == reason for f in writer.FIELDS)
    beacon = json.loads((tmp_path / "health/ci_runs.json").read_text())["details"]
    assert beacon["project_id"] == "vnx-dev" and beacon["reason"] == reason
    assert len(beacon["skipped_attempts"]) == 1 and result["coverage"] == "incomplete"
    assert execute(writer, tmp_path, [run_row(2)])[0]["coverage"] == "complete"
    assert len(writer.existing_records(tmp_path / "state/ci_runs.ndjson")) == 2

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
        writer.write_runs(bad, DAY, DAY, now=NOW, project_root=tmp_path, state_dir=tmp_path / "state")

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
    result = collect_ci_runs("vnx-dev", NOW, tmp_path / "state", project_root=tmp_path, runner=runner)
    assert result["reason"] == "writer_failed" and result["coverage"] == "incomplete"
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "fail"
    blocked = tmp_path / "blocked"
    blocked.write_text("file instead of directory")
    result = collect_ci_runs("..", NOW, blocked / "state", project_root=tmp_path, runner=runner)
    assert result["beacon_error"] and '"writer_failed"' in capsys.readouterr().err

def test_nightly_configuration_and_shared_window(tmp_path):
    from daily_metrics.collect import collect_ci_runs
    runner, calls = transport([run_row(2)])
    result = collect_ci_runs("vnx-dev", NOW, tmp_path / "state", project_root=tmp_path, runner=runner)
    assert result["written"] == 2 and result["calls"] == len(calls)
    assert result["range"] == ["2026-11-11", "2026-11-24"]

@pytest.mark.parametrize("failure_at", ["repo", "jobs"])
@pytest.mark.parametrize("reader", ["all_beacons", "t0_health"])
def test_collection_failure_reaches_health_readers(tmp_path, failure_at, reader):
    from daily_metrics.collect import collect_ci_runs
    from health_beacon import all_beacons
    from build_t0_state import _build_system_health

    def response(args, count):
        if (failure_at == "repo" and args[1] == "repo") or (failure_at == "jobs" and "/jobs?" in args[2]):
            return 1, "", "HTTP 502"
    runner, _ = transport([run_row()], override=response)
    result = collect_ci_runs("vnx-dev", NOW, tmp_path / "state", project_root=tmp_path, runner=runner)
    assert result["coverage"] == "incomplete"
    if reader == "all_beacons":
        beacons = all_beacons(tmp_path)
        assert beacons["ci_runs"]["details"] == result
    else:
        health = _build_system_health(
            tmp_path / "state", True, expected_beacon_components=(),
            daemon_liveness={"overall": "ok"}, launchd_liveness={"overall": "ok"},
            processor_liveness={"overall": "ok"},
        )
        beacons = health["beacon_health"]["beacons"]
        assert health["beacon_health"]["overall"] == "fail"
        assert beacons["ci_runs"]["reason"] == result["reason"]
    assert beacons["ci_runs"]["health"] == "fail"
    assert (tmp_path / "health/ci_runs.json").is_file()


def test_initial_repo_rate_limit_keeps_reason(tmp_path):
    from daily_metrics.collect import collect_ci_runs

    def response(args, count):
        assert args[1] == "repo"
        return 1, "", "HTTP 429 rate limit exceeded"
    runner, calls = transport([], override=response)
    result = collect_ci_runs("vnx-dev", NOW, tmp_path / "state", project_root=tmp_path, runner=runner)
    assert result["reason"] == "rate_limited"
    assert result["coverage"] == "incomplete" and result["population_total"] is None
    assert len(calls) == 1
    beacon = json.loads((tmp_path / "health/ci_runs.json").read_text())
    assert beacon["status"] == "fail" and beacon["details"] == result

@pytest.mark.parametrize("field", ["head_sha", "repository", "run_attempt"])
def test_contradictory_or_missing_identity_is_incomplete(writer, tmp_path, field):
    def override(args, count):
        if count == 3:
            row = run_row()
            row[field] = None
            return 0, json.dumps(row), ""
    result, records, _ = execute(writer, tmp_path, override=override)
    assert result["coverage"] == "incomplete"
    assert not records

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
    records[0].pop("pr_id_unknown_reason")  # Optional on a known PR; strict validation accepts it.
    pending = copy.deepcopy(records[0])
    pending["queue_seconds"] = writer.wrapper("failed jobs read", reason="source_missing")
    retry = tmp_path / "retry.ndjson"
    assert writer.append_records(retry, [pending]) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: writer.append_records(retry, records), range(8)))
    assert sum(counts) == 1 and len(retry.read_text().splitlines()) == 2
    assert writer.append_records(retry, [pending]) == 0
    assert writer.existing_records(retry)[writer.identity(records[0])] == records[0]

@pytest.mark.parametrize("change", [{"schema": "ci_run/2"}, {"conclusion": {}},
                                    {"event": None}, {"day": "2026-11-24"}, {"pr_id": None}])
def test_strict_envelope_validation(writer, tmp_path, change):
    record = execute(writer, tmp_path)[1][0]
    record.update(change)
    with pytest.raises(ValueError):
        writer.append_records(tmp_path / "invalid.ndjson", [record])

def test_empty_pr_association_retains_unknown_observation(writer, tmp_path):
    result, records, _ = execute(writer, tmp_path, [dict(run_row(), pull_requests=[])], max_calls=2)
    assert result["coverage"] == "incomplete" and len(records) == 1
    assert records[0]["pr_id"] is None and records[0]["pr_id_unknown_reason"] == "source_missing"
    assert execute(writer, tmp_path)[0]["coverage"] == "complete"

def test_filtered_search_limit_is_partitioned_without_losing_identified_rows(tmp_path):
    from daily_metrics.github_fetch import RunReader, parse_timestamp
    start, end = parse_timestamp("2026-11-01T00:00:00Z"), parse_timestamp("2026-11-24T23:59:59Z")
    rows = [dict(id=i + 1, created_at=f"2026-11-{2 if i < 500 else 23:02d}T00:00:00Z") for i in range(1001)]
    def runner(args, cwd):
        span = args[2].split("created=")[1].split("&")[0].split("..")
        low, high = map(parse_timestamp, span)
        selected = [r for r in rows if low <= parse_timestamp(r["created_at"]) <= high]
        page = int(args[2].rsplit("page=", 1)[1])
        return 0, json.dumps({"total_count": len(selected), "workflow_runs": selected[(page-1)*100:page*100]}), ""
    found, error = RunReader(tmp_path, 20, runner).runs("org/repo", start, end)
    assert error is None and len(found) == 1001
    found, error = RunReader(tmp_path, 1, runner).runs("org/repo", start, end)
    assert error == "call_budget_exhausted" and len(found) == 100


def cli_night(writer, tmp_path, monkeypatch, runner, **options):
    def process(args, **kwargs):
        code, stdout, stderr = runner(args, kwargs["cwd"])
        return subprocess.CompletedProcess(args, code, stdout, stderr)
    monkeypatch.setattr(subprocess, "run", process)
    return writer.write_runs("vnx-dev", DAY, date(2026, 11, 24), now=NOW,
                             project_root=tmp_path, state_dir=tmp_path / "state", **options)


def test_long_history_bounds_discovery_and_reserves_window_reads(writer, tmp_path, monkeypatch):
    from daily_metrics.github_fetch import parse_timestamp
    rows = [dict(run_row(), id=100 + i, created_at="2026-10-25T00:00:00Z",
                 run_started_at="2026-10-25T00:00:00Z", updated_at="2026-10-25T00:02:00Z")
            for i in range(1001)] + [run_row(2)]
    def response(args, count):
        if args[1] == "repo":
            return 0, json.dumps({"nameWithOwner": "org/repo", "createdAt": "2010-01-01T00:00:00Z"}), ""
        if "/actions/runs?" in args[2]:
            low, high = map(parse_timestamp, args[2].split("created=")[1].split("&")[0].split(".."))
            selected = sorted((r for r in rows if low <= parse_timestamp(r["created_at"]) <= high),
                              key=lambda r: r["created_at"], reverse=True)
            page = int(args[2].rsplit("page=", 1)[1])
            return 0, json.dumps({"total_count": len(selected), "workflow_runs": selected[(page-1)*100:page*100]}), ""
    runner, calls = transport(rows, override=response)
    result = cli_night(writer, tmp_path, monkeypatch, runner, max_calls=12)
    records = writer.existing_records(tmp_path / "state/ci_runs.ndjson")
    assert len(records) == 2 and all(r["queue_seconds"]["value"] == 10 for r in records.values())
    assert "created=2026-10-24T00:00:00Z..2026-11-24T23:59:59Z" in calls[1][2]
    discovery = [c[2] for c in calls if "/actions/runs?" in c[2]]
    assert len(discovery) <= 4 and "created=2026-11-09" in discovery[1]
    assert result["calls"] <= 12
    assert result["reason"] == "call_budget_exhausted"  # Unread old ranges remain visible.


@pytest.mark.parametrize("response", [(1, "", "HTTP 502"), (0, "bad json", ""),
                                      (1, "", "HTTP 429 rate limit exceeded"), "timeout"])
@pytest.mark.parametrize("endpoint", ["/jobs?", "/attempts/1"])
def test_transient_jobs_retry_next_night(writer, tmp_path, monkeypatch, response, endpoint):
    def broken(args, count):
        if endpoint in args[2]:
            if response == "timeout":
                raise subprocess.TimeoutExpired(args, 60)
            return response
    runner, _ = transport([run_row()], override=broken)
    first = cli_night(writer, tmp_path, monkeypatch, runner)
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "fail"
    assert first["skipped_attempts"]
    still_broken = cli_night(writer, tmp_path, monkeypatch, runner)
    assert still_broken["reason"] and still_broken["coverage"] == "incomplete"
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "fail"
    runner, _ = transport([run_row()])
    second = cli_night(writer, tmp_path, monkeypatch, runner)
    latest = writer.existing_records(tmp_path / "state/ci_runs.ndjson")
    assert next(iter(latest.values()))["queue_seconds"]["value"] == 10
    assert second["coverage"] == "complete" and second["reason"] is None
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "ok"
    assert cli_night(writer, tmp_path, monkeypatch, runner)["written"] == 0


def test_missing_workflow_reason_is_source_missing(writer, tmp_path, monkeypatch):
    runner, _ = transport([dict(run_row(), name=None)])
    result = cli_night(writer, tmp_path, monkeypatch, runner, workflows="CI")
    assert result["coverage"] == "incomplete" and result["reason"] == "source_missing"
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "fail"


@pytest.mark.parametrize("prs,expected", [([], None), ([{"number": 19, "head": {"sha": "a" * 39 + "b"}},
                                                     {"number": 3, "head": {"sha": "a" * 40}}], 3)])
def test_empty_pr_association_is_resolved_or_recorded_once(writer, tmp_path, monkeypatch, prs, expected):
    def response(args, count):
        if args[2].endswith("/attempts/1"):
            return 0, json.dumps(dict(run_row(), pull_requests=[])), ""
        if "/commits/" in args[2]:
            assert "/commits/" + "a" * 40 + "/pulls?" in args[2]
            return 0, json.dumps({"pull_requests": prs}), ""
    runner, calls = transport([dict(run_row(), pull_requests=[])], override=response)
    result = cli_night(writer, tmp_path, monkeypatch, runner)
    record = next(iter(writer.existing_records(tmp_path / "state/ci_runs.ndjson").values()))
    assert record["pr_id"] == expected
    assert record["pr_id_unknown_reason"] == ("source_missing" if expected is None else None)
    assert result["coverage"] == "complete" and result["population_total"] == 1
    assert result["reason"] is None and not result["issues"]
    assert json.loads((tmp_path / "health/ci_runs.json").read_text())["status"] == "ok"
    assert cli_night(writer, tmp_path, monkeypatch, runner)["written"] == 0
    assert len(calls) == 7  # Second night reads only repo and population.


@pytest.mark.parametrize("payload", [{}, {"pull_requests": None}, {"pull_requests": "invalid"},
                                     {"pull_requests": [{"number": 1, "head": {}}]},
                                     {"pull_requests": [{"number": n, "head": {"sha": "a" * 40}} for n in (1, 2)]}])
def test_pr_lookup_invalid_evidence_blocks(writer, tmp_path, monkeypatch, payload):
    def response(args, count):
        if args[2].endswith("/attempts/1"):
            return 0, json.dumps(dict(run_row(), pull_requests=[])), ""
        if "/commits/" in args[2]:
            return 0, json.dumps(payload), ""
    runner, _ = transport([run_row()], override=response)
    result = cli_night(writer, tmp_path, monkeypatch, runner)
    assert result["coverage"] == "incomplete" and result["reason"] == "source_invalid"
    assert result["skipped_attempts"] and result["written"] == 0


@pytest.mark.parametrize("budget,stop_page,failure,reason", [
    (4, 1, "HTTP 502", "call_budget_exhausted"),
    (5, 2, "HTTP 502", "call_budget_exhausted"),
    (10, 1, "HTTP 429 rate limit exceeded", "rate_limited"),
    (10, 2, "HTTP 429 rate limit exceeded", "rate_limited"),
    (10, 2, "HTTP 502", "source_missing"),
])
def test_pr_lookup_pagination_failure_stays_blocking(writer, tmp_path, monkeypatch, budget, stop_page, failure, reason):
    def response(args, count):
        if args[2].endswith("/attempts/1"):
            return 0, json.dumps(dict(run_row(), pull_requests=[])), ""
        if "/commits/" in args[2]:
            if args[2].endswith(f"&page={stop_page}"):
                return 1, "", failure
            prs = [{"number": n + 1, "head": {"sha": "b" * 40}} for n in range(100)]
            return 0, json.dumps({"pull_requests": prs}), ""
    runner, calls = transport([run_row()], override=response)
    result = cli_night(writer, tmp_path, monkeypatch, runner, max_calls=budget)
    assert result["reason"] == reason and result["coverage"] == "incomplete"
    assert result["skipped_attempts"] == [{"run_id": 7, "run_attempt": 1, "reason": reason}]
    assert len(calls) == min(budget, 4 + stop_page)
    if reason == "source_missing":
        assert result["written"] == 0
        return
    assert result["written"] == 1
    records = writer.existing_records(tmp_path / "state/ci_runs.ndjson")
    record = records[("org/repo", "vnx-dev", 7, 1)]
    writer.validate_record(record)
    assert all(record[name]["value"] is None and record[name]["unknown_reason"] == reason
               and record[name]["source"] for name in writer.FIELDS)
    runner, _ = transport([run_row()])
    assert cli_night(writer, tmp_path, monkeypatch, runner)["coverage"] == "complete"
    latest = writer.existing_records(tmp_path / "state/ci_runs.ndjson")
    assert len(latest) == 1 and latest[writer.identity(record)]["conclusion"]["value"] == "success"
