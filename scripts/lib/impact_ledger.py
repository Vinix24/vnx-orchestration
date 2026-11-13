"""Impact shadow ledger: the CI artifacts of the shadow, kept locally (ADR-074 besluit 7 and 8).

``collect`` reads the completed runs of workflow ``VNX CI`` through ONE injectable
``gh`` runner and appends one ``impact_ledger/1`` line per run attempt to
``state/impact_shadow.ndjson``.  GitHub deletes artifacts after 90 days; the ledger
is the evidence, the artifact only the way in.  The report over the ledger lines
(what counted, what did not, the reduced share, the misses, the red runs and the
wall clock) and the ``vnx impact`` command follow in impactselectie-in-schaduw PR 4a-2.

Fail-closed: an artifact that is absent is written down as ``missing``; any other
failure of ``gh`` (rate limit, Cloudflare 403, network) aborts that attempt without a
line, so it is collected again next time and never freezes as a hole.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SCHEMA = "impact_ledger/1"
LEDGER_NAME = "impact_shadow.ndjson"
WORKFLOW = "VNX CI"
PROFILE_A_JOB = "Profile A (doctor + core tests)"
SHADOW_JOB = "Impact shadow (runs only S, never required)"
SHADOW_ARTIFACT, JOB_ARTIFACT = "impact-shadow", "impact-shadow-job"
RUN_FIELDS = "databaseId,attempt,headSha,headBranch,event,conclusion,createdAt"

Runner = Callable[[list], str]


class RunnerError(RuntimeError):
    """``gh`` exited non-zero; ``stderr`` is what it said."""

    def __init__(self, argv: list, returncode: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv[:4])} exited {returncode}: {stderr.strip()[:200]}")
        self.stderr = stderr


def default_runner(argv: list) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
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
    """The record.json/job.json of artifact ``name``, or None when the run has no such artifact."""
    with tempfile.TemporaryDirectory(prefix="impact-ledger-") as tmp:
        try:
            runner(["gh", "run", "download", str(run_id), "-n", name, "-D", tmp])
        except RunnerError as exc:
            if "no valid artifact" in exc.stderr.lower():
                return None
            raise
        files = sorted(Path(tmp).rglob("*.json"))
        if not files:
            return None
        try:
            data = json.loads(files[0].read_text(encoding="utf-8"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


def _timing(jobs: list, name: str) -> Optional[dict]:
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            start, end = parse_time(job.get("started_at")), parse_time(job.get("completed_at"))
            seconds = int((end - start).total_seconds()) if start and end else None
            return {"started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
                    "conclusion": job.get("conclusion"), "seconds": seconds}
    return None


def _attributed(record, attempt, label: str, missing: list):
    """The record when it is this attempt's; a record stamped with another attempt is not its evidence."""
    if record is None:
        missing.append(f"artifact:{label}")
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
    repo = json.loads(runner(["gh", "repo", "view", "--json", "nameWithOwner"]))["nameWithOwner"]
    runs = json.loads(runner(["gh", "run", "list", "--workflow", WORKFLOW, "--status", "completed",
                              "--limit", str(limit), "--json", RUN_FIELDS]))
    stored = {_key(e) for e in read_ledger(path)}
    result = {"added": [], "skipped": [], "failed": []}
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
        except (RunnerError, ValueError, KeyError) as exc:
            result["failed"].append({"attempt": name, "error": str(exc)})
            continue
        result["added" if written else "skipped"].append(name)
    return result
