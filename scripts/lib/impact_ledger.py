"""Impact shadow ledger: the CI artifacts of the shadow, kept locally (ADR-074 besluit 7 and 8).

``collect`` reads the completed runs of workflow ``VNX CI`` through ONE injectable
``gh`` runner and appends one ``impact_ledger/1`` line per run attempt to
``state/impact_shadow.ndjson``.  GitHub deletes artifacts after 90 days; the ledger
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
PROFILE_A_JOB = "Profile A (doctor + core tests)"
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


def default_runner(argv: list) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
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


def _key(entry: dict) -> tuple:
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
                if isinstance(existing, dict) and _key(existing) == _key(entry):
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


def _timing(jobs: list, name: str) -> Optional[dict]:
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            start, end = parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
            seconds = int((end - start).total_seconds()) if start and end else None
            return {"started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
                    "conclusion": job.get("conclusion"), "seconds": seconds}
    return None


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


def _entry(runner: Runner, repo: str, run: dict, now: datetime) -> dict:
    run_id, attempt = run["databaseId"], run["attempt"]
    shadow = _download(runner, run_id, SHADOW_ARTIFACT)
    job = _download(runner, run_id, JOB_ARTIFACT)
    jobs = json.loads(runner(["gh", "api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"]))
    jobs = jobs.get("jobs") if isinstance(jobs, dict) else None
    jobs = jobs if isinstance(jobs, list) else []
    missing = []
    shadow = _attributed(shadow, attempt, "impact-shadow", missing)
    job = _attributed(job, attempt, "impact-shadow-job", missing)
    profile_a, shadow_job = _timing(jobs, PROFILE_A_JOB), _timing(jobs, SHADOW_JOB)
    missing += [f"job:{n}" for n, t in (("profile-a", profile_a), ("impact-shadow", shadow_job)) if t is None]
    return {
        "schema": SCHEMA, "run_id": run_id, "run_attempt": attempt, "head_sha": run.get("headSha"),
        "branch": run.get("headBranch"), "event": run.get("event"), "run_conclusion": run.get("conclusion"),
        "created_at": run.get("createdAt"), "collected_at": now.isoformat(),
        "shadow": shadow, "job": job, "profile_a_job": profile_a, "shadow_job": shadow_job, "missing": missing,
    }


def collect(state_dir: Path, runner: Runner = default_runner, since: Optional[datetime] = None,
            limit: int = 50, now: Optional[datetime] = None) -> dict:
    """Append the attempts not yet in the ledger; returns ``{"added": [...], "skipped": [...], "failed": [...]}``."""
    now = now or datetime.now(timezone.utc)
    path = ledger_path(state_dir)
    result = {"added": [], "skipped": [], "failed": []}
    try:
        repo = json.loads(runner(["gh", "repo", "view", "--json", "nameWithOwner"]))["nameWithOwner"]
        runs = json.loads(runner(["gh", "run", "list", "--workflow", WORKFLOW, "--status", "completed",
                                  "--limit", str(limit), "--json", RUN_FIELDS]))
        if not isinstance(runs, list):
            raise ValueError("gh run list did not return a list")
    except (RunnerError, ValueError, KeyError, TypeError) as exc:
        # No ledger line: discovery failed, so nothing is known about any attempt.
        result["failed"].append({"attempt": "discovery", "error": str(exc)})
        return result
    stored = {_key(e) for e in read_ledger(path)}
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
        named = [m for m in (entry.get("missing") or []) if str(m).startswith("artifact:impact-shadow")
                 and not str(m).startswith("artifact:impact-shadow-job")]
        return f"missing:{named[0][len('artifact:'):]}" if named else "missing:impact-shadow"
    if shadow.get("reconstructible") is not True:
        return "onreconstrueerbaar"
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


def report(lines: list) -> dict:
    counted, excluded = [], []
    for entry in lines:
        reason = _exclusion(entry)
        if reason is None:
            counted.append(entry)
        else:
            excluded.append({"attempt": _name(entry) if "_unreadable" not in entry else f"line {entry['_unreadable']}",
                             "reason": reason})
    reduced = [e for e in counted if e["shadow"].get("reduced") is True]
    red = [e for e in counted if e["shadow"]["sets"]["F"]]
    misses = [{"test_id": test, "run_id": e.get("run_id"), "run_attempt": e.get("run_attempt")}
              for e in reduced for test in e["shadow"]["sets"]["misses"]]
    return {
        "schema": "impact_report/1",
        "attempts_in_ledger": len(lines),
        "counted": [_name(e) for e in counted],
        "excluded": excluded,
        "reduced_share": {"reduced": len(reduced), "counted": len(counted),
                          "fraction": round(len(reduced) / len(counted), 4) if counted else None},
        "misses": misses,
        "red_runs": {"counted": len(red), "reduced": len([e for e in red if e in reduced])},
        "wall_clock": _wall_clock(counted),
    }


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
