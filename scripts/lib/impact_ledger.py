"""Impact shadow ledger: the CI artifacts of the shadow, kept locally (ADR-074 besluit 7 and 8).

``collect`` reads the completed runs of workflow ``VNX CI`` through ONE injectable
``gh`` runner and appends one ``impact_ledger/1`` line per run attempt to
``state/impact_shadow.ndjson``.  The ``impact-shadow`` artifact and the Profile A timing come from
that run.  The ``impact-shadow-job`` artifact and the job timing come from the same run when its job
list holds the shadow job (the shape before the job moved) or when the run is not a pull_request run (written
at once), otherwise from the newest run of workflow ``Impact shadow`` on the same head sha whose job record has
the same base_sha and candidate_sha as the shadow record (``shadow_run_id`` and ``shadow_run_attempt`` stamp the
pair; ADR-098).  A shadow run that is not completed, or absent while the VNX CI run is under two hours old, writes no
line: the attempt is reported as ``shadow_run_pending`` and collected again next time.  GitHub deletes artifacts after 90 days; the ledger
is the evidence, the artifact only the way in.  ``report`` is pure over the ledger
lines: what counted, what did not (by name), the reduced share, the misses of the
reduced attempts, the red runs and the wall clock.

Fail-closed: an artifact that is absent is written down as ``missing``; any other
failure of ``gh`` (rate limit, Cloudflare 403, network) aborts that attempt without a
line, so it is collected again next time and never freezes as a hole.
"""

from __future__ import annotations

import fcntl
import json
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional


def _record_schemas() -> tuple:
    """The two record schemas, imported from scripts/ci/impact_shadow.py (their single definition)."""
    ci_dir = str(Path(__file__).resolve().parent.parent / "ci")
    if ci_dir not in sys.path:
        sys.path.insert(0, ci_dir)
    import impact_shadow
    return impact_shadow.SCHEMA, impact_shadow.JOB_SCHEMA


SCHEMA = "impact_ledger/1"
LEDGER_NAME = "impact_shadow.ndjson"
WORKFLOW = "VNX CI"
SHADOW_WORKFLOW = "Impact shadow"
# A VNX CI run without any shadow run is pending this long; after that the hole is written down.
SHADOW_RUN_GRACE = timedelta(hours=2)
SHADOW_RUN_FIELDS = "databaseId,attempt,status,conclusion,createdAt"
PROFILE_A_JOB = "Profile A (doctor + core tests)"
# Since C3 the job above is only the aggregator: these five are what it waits for
# (pinned to its `needs` in .github/workflows/vnx-ci.yml by a test).
PROFILE_A_PART_JOBS = ("Profile A guards",) + tuple(f"Profile A shard ({k}/4)" for k in range(1, 5))
SHADOW_JOB = "Impact shadow (runs only S, never required)"
SHADOW_ARTIFACT, JOB_ARTIFACT = "impact-shadow", "impact-shadow-job"
# The record each artifact carries at its root: scripts/ci/impact_shadow_step.sh writes
# OUT_DIR/record.json (--output "$OUT_DIR/record.json"), scripts/ci/impact_shadow_job.sh
# writes OUT_DIR/job.json. The job artifact also holds evidence/args/run.json and more JSON.
_SHADOW_RECORD_SCHEMA, _JOB_RECORD_SCHEMA = _record_schemas()
ARTIFACT_RECORDS = {SHADOW_ARTIFACT: ("record.json", _SHADOW_RECORD_SCHEMA),
                    JOB_ARTIFACT: ("job.json", _JOB_RECORD_SCHEMA)}
RUN_FIELDS = "databaseId,attempt,headSha,headBranch,event,conclusion,createdAt"
DONE_CONCLUSIONS = ("success", "failure")

Runner = Callable[[list], str]


class RunnerError(RuntimeError):
    """``gh`` exited non-zero; ``stderr`` is what it said."""

    def __init__(self, argv: list, returncode: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv[:4])} exited {returncode}: {stderr.strip()[:200]}")
        self.stderr = stderr


def default_runner(argv: list, cwd: Optional[Path] = None) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(argv, -1, f"timed out after {exc.timeout}s") from exc
    except OSError as exc:
        raise RunnerError(argv, -1, f"cannot run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise RunnerError(argv, done.returncode, done.stderr)
    return done.stdout


def parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_since(text: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """``Nd`` (N days back), a date or an ISO datetime; ValueError on anything else."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    if text.endswith("d") and text[:-1].isdigit():
        return now - timedelta(days=int(text[:-1]))
    parsed = parse_time(text)
    if parsed is None:
        raise ValueError(f"--since {text!r}: use Nd, YYYY-MM-DD or an ISO datetime")
    return parsed


def ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER_NAME


def read_ledger(path: Path) -> list:
    """Every line as a dict; a line that does not parse becomes ``{"_unreadable": <line number>}``."""
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        entries.append(parsed if isinstance(parsed, dict) else {"_unreadable": number})
    return entries


def entry_key(entry: dict) -> tuple:
    return (entry.get("run_id"), entry.get("run_attempt"))


def append_entry(path: Path, entry: dict) -> bool:
    """Append one whole line unless its (run_id, run_attempt) stands already; True when written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True) + "\n"
    # vnx-atomic-write: append-only ledger under flock
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for raw in handle.read().splitlines():
                try:
                    existing = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(existing, dict) and entry_key(existing) == entry_key(entry):
                    return False
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    return True


def _download(runner: Runner, run_id, name: str):
    """``(record, problem)`` of artifact ``name``: the record is exactly its root record.json/job.json.

    ``problem`` is None for a valid record, ``"missing"`` when the run has no such artifact or the
    file is not there, ``"invalid_schema"`` when the file is unreadable, not an object or carries
    another schema.  Never a glob, never the first JSON file found.
    """
    filename, schema = ARTIFACT_RECORDS[name]
    with tempfile.TemporaryDirectory(prefix="impact-ledger-") as tmp:
        try:
            runner(["gh", "run", "download", str(run_id), "-n", name, "-D", tmp])
        except RunnerError as exc:
            if "no valid artifact" in exc.stderr.lower():
                return None, "missing"
            raise
        target = Path(tmp) / filename
        if not target.is_file():
            return None, "missing"
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "invalid_schema"
        if not isinstance(data, dict) or data.get("schema") != schema:
            return None, "invalid_schema"
        return data, None


def discover(runner: Runner, limit: int, event: Optional[str] = None) -> tuple:
    """``(repo, runs)``: the completed ``VNX CI`` runs, newest first, optionally of one event."""
    repo = json.loads(runner(["gh", "repo", "view", "--json", "nameWithOwner"]))["nameWithOwner"]
    argv = ["gh", "run", "list", "--workflow", WORKFLOW, "--status", "completed", "--limit", str(limit), "--json", RUN_FIELDS]
    runs = json.loads(runner(argv + (["--event", event] if event else [])))
    if not isinstance(runs, list):
        raise ValueError("gh run list did not return a list")
    return repo, runs


def fetch_jobs(runner: Runner, repo: str, run_id, attempt) -> list:
    """The job list of one attempt; a payload that is not a job list gives ``[]``."""
    jobs = json.loads(runner(["gh", "api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"]))
    jobs = jobs.get("jobs") if isinstance(jobs, dict) else None
    return jobs if isinstance(jobs, list) else []


def timing(jobs: list, name: str) -> Optional[dict]:
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            start, end = parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
            seconds = int((end - start).total_seconds()) if start and end else None
            return {"started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
                    "conclusion": job.get("conclusion"), "seconds": seconds}
    return None


def profile_a_timing(jobs: list) -> tuple:
    """``(timing, reason)`` of the whole Profile A.

    On the C3 shape (any guards or shard job present) the span runs from the earliest start of the
    aggregator, the guards job and the four shards to the aggregator's completion; a missing job or an
    unparseable time gives ``(None, reason)``, never a partial span. Without those jobs it is the
    aggregator's own ``timing`` as before.
    """
    named = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    if not any(name in named for name in PROFILE_A_PART_JOBS):
        single = timing(jobs, PROFILE_A_JOB)
        if single is None:
            return None, "job:profile-a"
        return {**single, "shape": "single", "jobs": 1}, None
    names = (PROFILE_A_JOB,) + PROFILE_A_PART_JOBS
    absent = [n for n in names if n not in named]
    if absent:
        return None, "job:profile-a:incomplete:" + ",".join(absent)
    starts = [parse_time(named[n].get("started_at")) for n in names]
    ends = parse_time(named[PROFILE_A_JOB].get("completed_at"))
    unparsed = [n for n, t in zip(names, starts) if t is None] + ([] if ends else [PROFILE_A_JOB])
    if unparsed:
        return None, "job:profile-a:unparseable_time:" + ",".join(unparsed)
    first = min(zip(starts, names))[1]
    seconds = int((ends - min(starts)).total_seconds())
    if seconds < 0:
        return None, "job:profile-a:negative_span"
    aggregator = named[PROFILE_A_JOB]
    return {"started_at": named[first].get("started_at"), "completed_at": aggregator.get("completed_at"),
            "conclusion": aggregator.get("conclusion"), "seconds": seconds,
            "shape": "sharded", "jobs": len(names)}, None


def _attributed(downloaded, attempt, label: str, missing: list):
    """The record when it is this attempt's; a record stamped with another attempt is not its evidence."""
    record, problem = downloaded
    if record is None:
        missing.append(f"artifact:{label}" + (f":{problem}" if problem != "missing" else ""))
        return None
    if str(record.get("run_attempt")) != str(attempt):
        missing.append(f"artifact:{label}:attempt_mismatch")
        return None
    return record


class ShadowRunPending(RuntimeError):
    """The shadow run of this attempt may still arrive or finish: no ledger line yet."""


def _same_candidate(shadow: Optional[dict], job: Optional[dict]) -> bool:
    """True when the job record tested the merge candidate the VNX CI shadow record names (base and candidate sha)."""
    return all(isinstance(shadow.get(k), str) and shadow.get(k) and shadow.get(k) == job.get(k)
               for k in ("base_sha", "candidate_sha"))


def _shadow_run(runner: Runner, run: dict, shadow: Optional[dict], now: datetime) -> tuple:
    """``(shadow run, its job download)`` for ``run``; ``(None, None)`` when there is none and it is time to give up.

    The runs are the ``Impact shadow`` runs on the head sha, newest first.  With a shadow record on the VNX CI
    side the first run whose job record has the same base_sha and candidate_sha wins (main may have moved between
    two runs on one head sha); without one the newest run wins.  Raises ShadowRunPending when the newest run is
    not completed, or when nothing pairs and the VNX CI run is under SHADOW_RUN_GRACE old (or its age cannot be
    read: no positive evidence that it is old).  After the grace a candidate that never matched gives the
    download problem ``candidate_mismatch``.
    """
    argv = ["gh", "run", "list", "--workflow", SHADOW_WORKFLOW, "--commit", str(run.get("headSha")),
            "--event", "pull_request", "--json", SHADOW_RUN_FIELDS]
    found = json.loads(runner(argv))
    if not isinstance(found, list):
        raise ValueError("gh run list did not return a list")
    runs = sorted((r for r in found if isinstance(r, dict) and isinstance(r.get("databaseId"), int) and isinstance(r.get("attempt"), int)),
                  key=lambda r: (r["databaseId"], r["attempt"]), reverse=True)
    created = parse_time(run.get("createdAt"))
    too_young = created is None or now - created < SHADOW_RUN_GRACE
    if not runs:
        if too_young:
            raise ShadowRunPending("shadow_run_pending")
        return None, None
    if runs[0].get("status") != "completed":
        raise ShadowRunPending("shadow_run_pending")
    if not (isinstance(shadow, dict) and shadow.get("base_sha") and shadow.get("candidate_sha")):
        return runs[0], None
    mismatched = False
    for candidate in runs:
        if candidate.get("status") != "completed":
            continue
        download = _download(runner, candidate["databaseId"], JOB_ARTIFACT)
        if download[0] is None:
            continue
        if _same_candidate(shadow, download[0]):
            return candidate, download
        mismatched = True
    if not mismatched:
        return runs[0], None
    if too_young:
        raise ShadowRunPending("shadow_run_pending")
    return None, (None, "candidate_mismatch")


def _job_evidence(runner: Runner, repo: str, run: dict, jobs: list, shadow: Optional[dict], now: datetime) -> dict:
    """Where the job artifact and the job timing of this attempt come from, and what they are.

    The old shape (the VNX CI jobs hold the shadow job) reads the run itself.  So does a run whose event is
    not ``pull_request`` (a push on main): it has no shadow job and never gets a paired run, so no lookup.
    Otherwise the job comes from the paired shadow run.
    """
    event = run.get("event")
    unpaired = isinstance(event, str) and event != "pull_request"  # an unreadable event is not positive evidence
    if timing(jobs, SHADOW_JOB) is not None or unpaired:
        source, source_jobs, paired, download = (run["databaseId"], run["attempt"]), jobs, (None, None), None
    else:
        found, download = _shadow_run(runner, run, shadow, now)
        if found is None:
            return {"download": download or (None, "missing"), "attempt": run["attempt"], "timing": None, "pair": (None, None)}
        source = (found["databaseId"], found["attempt"])
        source_jobs, paired = fetch_jobs(runner, repo, *source), source
    return {"download": download or _download(runner, source[0], JOB_ARTIFACT), "attempt": source[1],
            "timing": timing(source_jobs, SHADOW_JOB), "pair": paired}


def _entry(runner: Runner, repo: str, run: dict, now: datetime) -> dict:
    run_id, attempt = run["databaseId"], run["attempt"]
    jobs = fetch_jobs(runner, repo, run_id, attempt)
    missing = []
    shadow = _attributed(_download(runner, run_id, SHADOW_ARTIFACT), attempt, "impact-shadow", missing)
    evidence = _job_evidence(runner, repo, run, jobs, shadow, now)
    job = _attributed(evidence["download"], evidence["attempt"], "impact-shadow-job", missing)
    profile_a, profile_a_problem = profile_a_timing(jobs)
    shadow_job = evidence["timing"]
    if profile_a is None:
        missing.append(profile_a_problem)
    if shadow_job is None:
        missing.append("job:impact-shadow")
    return {
        "schema": SCHEMA, "run_id": run_id, "run_attempt": attempt, "head_sha": run.get("headSha"),
        "branch": run.get("headBranch"), "event": run.get("event"), "run_conclusion": run.get("conclusion"),
        "created_at": run.get("createdAt"), "collected_at": now.isoformat(),
        "shadow": shadow, "job": job, "profile_a_job": profile_a, "shadow_job": shadow_job, "missing": missing,
        "shadow_run_id": evidence["pair"][0], "shadow_run_attempt": evidence["pair"][1],
    }


def collect(state_dir: Path, runner: Runner = default_runner, since: Optional[datetime] = None,
            limit: int = 50, now: Optional[datetime] = None) -> dict:
    """Append the attempts not yet in the ledger; returns ``{"added": [...], "skipped": [...], "failed": [...]}``."""
    now = now or datetime.now(timezone.utc)
    path = ledger_path(state_dir)
    result = {"added": [], "skipped": [], "failed": []}
    try:
        repo, runs = discover(runner, limit)
    except (RunnerError, ValueError, KeyError, TypeError) as exc:
        # No ledger line: discovery failed, so nothing is known about any attempt.
        result["failed"].append({"attempt": "discovery", "error": str(exc)})
        return result
    stored = {entry_key(e) for e in read_ledger(path)}
    for run in runs:
        name = f"{run.get('databaseId')}/{run.get('attempt')}"
        created = parse_time(run.get("createdAt"))
        if since and (created is None or created < since):
            continue
        if (run.get("databaseId"), run.get("attempt")) in stored:
            result["skipped"].append(name)
            continue
        try:
            written = append_entry(path, _entry(runner, repo, run, now))
        except ShadowRunPending as exc:
            result["failed"].append({"attempt": name, "error": str(exc)})
            continue
        except (RunnerError, ValueError, KeyError, TypeError) as exc:
            result["failed"].append({"attempt": name, "error": str(exc)})
            continue
        result["added" if written else "skipped"].append(name)
    return result


def _name(entry: dict) -> str:
    return f"{entry.get('run_id')}/{entry.get('run_attempt')}"


def _exclusion(entry: dict) -> Optional[str]:
    """Why an attempt does not count, or None when it does.  Only positive evidence counts."""
    if "_unreadable" in entry:
        return "unreadable"
    shadow = entry.get("shadow")
    if not isinstance(shadow, dict):
        named = [m for m in (entry.get("missing") or []) if (str(m).startswith("artifact:impact-shadow")
                 and not str(m).startswith("artifact:impact-shadow-job")) or str(m).startswith("artifact:profile-a-evidence")]
        return f"missing:{named[0][len('artifact:'):]}" if named else "missing:impact-shadow"
    if shadow.get("reconstructible") is not True:
        named = shadow.get("unreadable_inputs")
        return "onreconstrueerbaar" + (f":{','.join(map(str, named))}" if isinstance(named, list) and named else "")
    if shadow.get("attempt_valid") is not True:
        reasons = shadow.get("invalid_reasons")
        names = sorted({str(r.get("reason")) for r in reasons if isinstance(r, dict)}) if isinstance(reasons, list) else []
        return f"invalid:{','.join(names) or 'attempt_valid_not_true'}"
    if entry.get("run_conclusion") not in DONE_CONCLUSIONS:
        return "not_completed"
    profile_a = entry.get("profile_a_job")
    if not isinstance(profile_a, dict) or not profile_a.get("conclusion"):
        return "missing:job:profile-a"
    if profile_a.get("conclusion") not in DONE_CONCLUSIONS:
        return "not_completed"
    sets = shadow.get("sets")
    if not isinstance(sets, dict) or not isinstance(sets.get("misses"), list) or not isinstance(sets.get("F"), list):
        return "invalid:sets_unreadable"
    return None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _wall_clock(counted: list) -> dict:
    rows = []
    for entry in counted:
        job, profile_a = entry.get("job"), entry.get("profile_a_job") or {}
        if (isinstance(job, dict) and job.get("ran") is True and job.get("complete") is True
                and _is_number(job.get("wall_seconds")) and _is_number(profile_a.get("seconds"))):
            rows.append((_name(entry), job["wall_seconds"], profile_a["seconds"]))
    shadow, full = [r[1] for r in rows], [r[2] for r in rows]
    return {"n": len(rows), "attempts": [r[0] for r in rows],
            "shadow_job_seconds": {"median": statistics.median(shadow) if rows else None, "sum": sum(shadow)},
            "profile_a_seconds": {"median": statistics.median(full) if rows else None, "sum": sum(full)}}


def _source(entry: dict) -> str:
    """``backtest`` only when the line says so; a line without ``source`` is live."""
    return "backtest" if entry.get("source") == "backtest" else "live"


def _summary(lines: list) -> dict:
    counted, excluded = [], []
    for entry in lines:
        reason = _exclusion(entry)
        if reason is None:
            counted.append(entry)
        else:
            if "_unreadable" in entry:
                label = f"line {entry['_unreadable']}"
                name = f"backtest {label}" if _source(entry) == "backtest" else label
            else:
                name = _name(entry)
            excluded.append({"attempt": name, "reason": reason})
    reduced = [e for e in counted if e["shadow"].get("reduced") is True]
    red = [e for e in counted if e["shadow"]["sets"]["F"]]
    misses = [{"test_id": test, "run_id": e.get("run_id"), "run_attempt": e.get("run_attempt")}
              for e in reduced for test in e["shadow"]["sets"]["misses"]]
    return {
        "attempts_in_ledger": len(lines),
        "counted": [_name(e) for e in counted],
        "excluded": excluded,
        "reduced_share": {"reduced": len(reduced), "counted": len(counted),
                          "fraction": round(len(reduced) / len(counted), 4) if counted else None},
        "misses": misses,
        "red_runs": {"counted": len(red), "reduced": len([e for e in red if e in reduced])},
        "wall_clock": _wall_clock(counted),
    }


def report(lines: list) -> dict:
    """Every count over live and backtest lines together, and again per source (``by_source``).

    An attempt that stands in both the live and the backtest ledger counts once, as live.
    """
    live = [e for e in lines if _source(e) == "live"]
    stored = {entry_key(e) for e in live if "_unreadable" not in e}
    backtest = [e for e in lines if _source(e) == "backtest"]
    superseded = [_name(e) for e in backtest if "_unreadable" not in e and entry_key(e) in stored]
    backtest = [e for e in backtest if "_unreadable" in e or entry_key(e) not in stored]
    return {"schema": "impact_report/1", **_summary(live + backtest), "backtest_superseded_by_live": superseded,
            "by_source": {"live": _summary(live), "backtest": _summary(backtest)}}


def filter_since(lines: list, since: Optional[datetime]) -> list:
    """Lines created on or after ``since``; a line whose date cannot be read stays (it is named, not hidden)."""
    if since is None:
        return lines
    kept = []
    for entry in lines:
        created = parse_time(entry.get("created_at"))
        if created is None or created >= since:
            kept.append(entry)
    return kept
