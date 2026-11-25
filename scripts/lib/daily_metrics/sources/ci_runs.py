"""ADR-097 append-only attempt observations. Called once per nightly window.

No collect(ctx): these records are not ADR-063 merged-PR aggregate metrics.
Readers must take a shared flock on the ledger; writers hold EX across validation,
deduplication and all short writes. A failed/partial line blocks later appends.
"""
import fcntl
import json
import math
import os
import re
from pathlib import Path

from daily_metrics import days
from daily_metrics.github_fetch import RunReader, RunReadFailure, parse_timestamp
from health_beacon import HealthBeacon
from vnx_paths import resolve_central_data_dir

FIELDS = ("wall_seconds", "queue_seconds", "critical_path_job", "conclusion", "failed_tests", "retry_pass_tests")
COMMON = {"source_missing", "source_invalid", "rate_limited", "call_budget_exhausted", "local_gate_unmapped"}
TEST_REASONS = {"junit_not_configured", "junit_artifact_missing", "junit_unreadable", "junit_attempt_unverified"}
CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale", "startup_failure"}
STATUSES = {"completed", "queued", "in_progress", "requested", "waiting", "pending"}
EVENTS = {"pull_request", "push", "schedule", "workflow_dispatch"}
REPOSITORY = r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9_][\w.-]*"

def positive(value):
    return type(value) is int and value > 0

def identity(record):
    return tuple(record[k] for k in ("repository", "project_id", "run_id", "run_attempt"))

def validate_record(record):
    """Strict ci_run/1 validation, including every wrapper; raises ValueError."""
    if record.get("schema") != "ci_run/1":
        raise ValueError("unsupported ci_run schema")
    resolve_central_data_dir(record.get("project_id"))
    if not re.fullmatch(REPOSITORY, str(record.get("repository", ""))):
        raise ValueError("invalid repository")
    if not all(positive(record.get(k)) for k in ("run_id", "run_attempt")):
        raise ValueError("unresolved attempt identity")
    if record.get("pr_id") is None:
        if record.get("pr_id_unknown_reason") not in {"source_missing", "source_invalid"}:
            raise ValueError("unexplained missing PR identity")
    elif not positive(record["pr_id"]) or record.get("pr_id_unknown_reason") is not None:
        raise ValueError("invalid PR identity")
    if not re.fullmatch(r"[0-9a-f]{40}", str(record.get("head_sha", ""))):
        raise ValueError("invalid head_sha")
    start = parse_timestamp(record.get("run_started_at"))
    if (start is None or record.get("day") != start.date().isoformat()
            or parse_timestamp(record.get("collected_at")) is None
            or record.get("event") != "pull_request" or record.get("status") != "completed"):
        raise ValueError("invalid attempt population/time")
    for name in FIELDS:
        field = record.get(name)
        if not isinstance(field, dict) or set(field) != {"value", "source", "unknown_reason"}:
            raise ValueError(f"invalid wrapper: {name}")
        value, reason = field["value"], field["unknown_reason"]
        allowed = COMMON | (TEST_REASONS if name in FIELDS[4:] else set())
        if name == "retry_pass_tests":
            allowed = allowed | {"retry_history_unavailable"}
        if not isinstance(field["source"], str) or not field["source"].strip():
            raise ValueError(f"missing source: {name}")
        if value is None:
            valid = isinstance(reason, str) and reason in allowed
        elif reason is not None:
            valid = False
        elif name.endswith("seconds"):
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        elif name == "conclusion":
            valid = isinstance(value, str) and value in CONCLUSIONS
        elif name == "critical_path_job":
            valid = (isinstance(value, dict) and positive(value.get("id"))
                     and isinstance(value.get("name"), str) and bool(value["name"]))
        else:
            valid = isinstance(value, list) and all(isinstance(v, str) and v for v in value) and value == sorted(set(value))
        if not valid:
            raise ValueError(f"invalid field: {name}")
    json.dumps(record, allow_nan=False)

def append_records(path, records):
    """Idempotent under the ledger flock; unreadable/corrupt state raises."""
    for record in records:
        validate_record(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        seen = set()
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as source:
            for line in source:
                if not line.endswith("\n"):
                    raise ValueError("partial ci_run line")
                existing = json.loads(line)
                validate_record(existing)
                seen.add(identity(existing))
        written = 0
        for record in records:
            key = identity(record)
            if key in seen:
                continue
            data = (json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n").encode()
            append_line(fd, data)
            seen.add(key)
            written += 1
        return written
    finally:
        os.close(fd)

def append_line(fd, data):
    """Roll back only this uncommitted tail on I/O failure, while holding EX."""
    offset = os.lseek(fd, 0, os.SEEK_END)
    try:
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise OSError("ci_run short write made no progress")
            data = data[count:]
        os.fsync(fd)
    except OSError:
        os.ftruncate(fd, offset)
        os.fsync(fd)
        raise

def existing_records(path):
    """Validated snapshot for avoiding repeated API reads, never written back."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}  # No ledger yet is a new collection, not missing GitHub evidence.
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        fcntl.flock(fd, fcntl.LOCK_SH)
        records = {}
        for line in source:
            if not line.endswith("\n"):
                raise ValueError("partial ci_run line")
            record = json.loads(line)
            validate_record(record)
            records[identity(record)] = record
        return records

def wrapper(source, value=None, reason=None):
    return {"value": value, "source": source, "unknown_reason": reason}

def elapsed(start, end):
    if start in (None, "") or end in (None, ""):
        return None, "source_missing"
    first, last = parse_timestamp(start), parse_timestamp(end)
    if first is None or last is None or last < first:
        return None, "source_invalid"
    return (last - first).total_seconds(), None

def job_fields(row, jobs, source, error):
    starts, terminal, qerror, cerror = [], [], error, error
    for job in jobs:
        conclusion = job.get("conclusion")
        valid_conclusion = isinstance(conclusion, str) and conclusion in CONCLUSIONS
        if not valid_conclusion:
            qerror = cerror = "source_missing" if conclusion in (None, "") else "source_invalid"
        start = job.get("started_at")
        if conclusion == "skipped" and start in (None, ""):
            continue
        seconds, reason = elapsed(row.get("run_started_at"), start)
        if reason:
            qerror = reason
        else:
            starts.append(seconds)
        if conclusion == "skipped":
            continue
        duration, reason = elapsed(start, job.get("completed_at"))
        if reason or not positive(job.get("id")) or not isinstance(job.get("name"), str) or not job["name"]:
            cerror = reason or "source_invalid"
        else:
            terminal.append((parse_timestamp(job["completed_at"]), duration, -job["id"], job))
    queue = wrapper(source, reason=qerror or (None if starts else "source_missing"))
    critical = wrapper(source, reason=cerror or (None if terminal else "source_missing"))
    if queue["unknown_reason"] is None:
        queue["value"] = min(starts)
    if critical["unknown_reason"] is None:
        chosen = max(terminal, key=lambda item: item[:3])[3]
        critical["value"] = {"id": chosen["id"], "name": chosen["name"]}
    return queue, critical

def make_record(project, repo, row, now, source, jobs, error, artifact):
    prs = row.get("pull_requests")
    pr = prs[0].get("number") if isinstance(prs, list) and len(prs) == 1 and isinstance(prs[0], dict) else None
    pr_reason = None if positive(pr) else "source_missing" if prs in (None, []) else "source_invalid"
    start = parse_timestamp(row.get("run_started_at"))
    record = dict(schema="ci_run/1", project_id=project, repository=repo,
                  pr_id=pr if positive(pr) else None, pr_id_unknown_reason=pr_reason,
                  head_sha=row.get("head_sha"), run_id=row.get("id"),
                  run_attempt=row.get("run_attempt"), event=row.get("event"), status=row.get("status"),
                  run_started_at=row.get("run_started_at"), day=start.date().isoformat() if start else None,
                  collected_at=now.isoformat())
    wall, reason = elapsed(row.get("run_started_at"), row.get("updated_at"))
    record["wall_seconds"] = wrapper(source + " timestamps", wall, reason)
    value = row.get("conclusion")
    known = isinstance(value, str) and value in CONCLUSIONS
    record["conclusion"] = wrapper(source + " conclusion", value if known else None,
                                    None if known else "source_missing" if value in (None, "") else "source_invalid")
    record["queue_seconds"], record["critical_path_job"] = job_fields(row, jobs, source + "/jobs", error)
    for name in FIELDS[4:]:
        record[name] = wrapper(source + f" artifact={artifact!r}; reader=M3b",
                               reason="source_missing" if artifact else "junit_not_configured")
    if error in {"rate_limited", "call_budget_exhausted"}:
        for name in FIELDS:
            record[name] = wrapper(record[name]["source"], reason=error)
    validate_record(record)
    return record

def discover_attempts(reader, project, repo, rows, first, last, workflows, issues, skipped, existing):
    attempts = []
    for row in rows:
        event, status = row.get("event"), row.get("status")
        if not isinstance(event, str) or event not in EVENTS or not isinstance(status, str) or status not in STATUSES:
            issues.append(f"source_invalid: population run {row.get('id')}")
            continue
        if event in {"push", "schedule", "workflow_dispatch"}:
            continue
        if workflows and (not isinstance(row.get("name"), str) or not row["name"]):
            issues.append(f"source_missing: workflow run {row.get('id')}")
            continue
        if workflows and row["name"] not in workflows:
            continue
        start, count = parse_timestamp(row.get("run_started_at")), row.get("run_attempt")
        if start is None or not positive(count):
            issues.append(f"source_invalid: attempt identity run {row['id']}")
            continue
        updated = parse_timestamp(row.get("updated_at"))
        if start.date() < first and (count == 1 or (updated is not None and updated.date() < first)):
            continue
        for number in range(1, count + 1):
            saved = existing.get((repo, project, row["id"], number))
            if saved is not None:
                if saved["pr_id"] is None:
                    issues.append(f"{saved['pr_id_unknown_reason']}: stored run {row['id']} has unresolved PR identity")
                if saved["head_sha"] != row.get("head_sha"):
                    issues.append(f"source_invalid: stored head contradicts run {row['id']}")
                elif first.isoformat() <= saved["day"] <= last.isoformat():
                    attempts.append((saved, None, None))
                continue
            source = f"repos/{repo}/actions/runs/{row['id']}/attempts/{number}"
            read_error = None
            try:
                attempt = reader.read(["api", source])
            except RunReadFailure as exc:
                read_error = str(exc)
                issues.append(f"{exc}: {source}")
                skipped.append({"run_id": row["id"], "run_attempt": number, "reason": read_error})
                # Only the latest listing has attempt-specific envelope evidence.
                if number != count:
                    continue
                attempt = row
                source += " (envelope from latest run listing)"
            repository = attempt.get("repository")
            if (attempt.get("id") != row["id"] or attempt.get("run_attempt") != number
                    or attempt.get("head_sha") != row.get("head_sha")
                    or not isinstance(repository, dict) or repository.get("full_name") != repo
                    or attempt.get("event") != "pull_request"
                    or not isinstance(attempt.get("status"), str) or attempt["status"] not in STATUSES):
                issues.append(f"source_invalid: contradictory attempt {source}")
                continue
            stamp = parse_timestamp(attempt.get("run_started_at"))
            if stamp is None:
                issues.append(f"source_invalid: attempt timestamp {source}")
            elif attempt["status"] == "completed" and first <= stamp.date() <= last:
                attempts.append((attempt, source, read_error))
    return attempts

def write_runs(project_id, from_day, to_day, *, now, project_root, state_dir=None,
               max_calls=1000, workflows="", artifact="", runner=None):
    """Collect a settled window with one budget. Return coverage and persist its beacon."""
    central = resolve_central_data_dir(project_id)
    state = Path(state_dir) if state_dir is not None else central / "state"
    if now.utcoffset() is None or from_day > to_day or not days.is_settled(to_day, now):
        raise ValueError("ci_runs requires an aware clock and settled UTC days")
    reader = RunReader(project_root, max_calls, runner)
    existing = existing_records(state / "ci_runs.ndjson")
    issues, records, skipped = [], [], []
    info = reader.read(["repo", "view", "--json", "nameWithOwner,createdAt"])
    repo, created = info.get("nameWithOwner"), parse_timestamp(info.get("createdAt"))
    if not isinstance(repo, str) or not re.fullmatch(REPOSITORY, repo):
        raise ValueError("unresolved repository")
    if created is None:
        raise ValueError("unresolved repository creation time")
    rows, error = reader.runs(repo, created, parse_timestamp(f"{to_day}T23:59:59Z"))
    if error:
        issues.append(error)
    attempts = discover_attempts(reader, project_id, repo, rows, from_day, to_day,
                                 {name.strip() for name in workflows.split(",") if name.strip()}, issues, skipped, existing)
    for row, source, read_error in attempts:
        if source is None:
            continue  # Already validated in the ledger, which append_records rechecks under EX.
        endpoint = f"repos/{repo}/actions/runs/{row['id']}/attempts/{row['run_attempt']}/jobs"
        jobs, error = reader.pages(endpoint, "jobs")
        if error and read_error is None:
            skipped.append({"run_id": row["id"], "run_attempt": row["run_attempt"], "reason": error})
        try:
            record = make_record(project_id, repo, row, now, source, jobs, read_error or error, artifact)
            if record["pr_id"] is None:
                issues.append(f"{record['pr_id_unknown_reason']}: {source}: unresolved PR identity")
            if read_error:
                for name in FIELDS[:4]:
                    record[name] = wrapper(record[name]["source"], reason=read_error)
            records.append(record)
        except ValueError as exc:
            issues.append(f"source_invalid: {source}: {exc}")
    result = dict(project_id=project_id, coverage="incomplete" if issues else "complete",
                  population_total=None if issues else len(attempts), identified_attempts=len(attempts),
                  range=[from_day.isoformat(), to_day.isoformat()], issues=issues,
                  calls=reader.calls, budget=max_calls, skipped_attempts=skipped,
                  reason=reader.stopped or ("source_invalid" if issues or skipped else None))
    result["written"] = append_records(state / "ci_runs.ndjson", records)
    HealthBeacon(state, "ci_runs", 36 * 3600).heartbeat_strict("fail" if result["reason"] else "ok", result)
    return result
