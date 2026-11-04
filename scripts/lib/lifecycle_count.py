"""ADR-045 K-9: the lifecycle count of one store, read-only.

Five defects over this project's ``v3`` lines — a start without an end past its
deadline plus the sweep's grace, an end without a start, a double end, a
projection conflict, an unknown provenance — paired on ``(project_id,
dispatch_id, attempt)`` inside the ``writer_sha`` cohort. It decides nothing:
``ledger_health``'s four checks keep their exit-code fold and this sits beside
them, and what cannot be measured is ``unmeasured`` with a reason, never zero.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from coordination_db import db_path_from_state_dir, get_connection
from dispatch_lifecycle import (  # its table, its grace, its parser: never a copy
    ABANDON_GRACE_SECONDS, END_INTENT_DIRNAME, END_STATE_TARGET, PROJECTION_ROUTES,
    SWEEP_HEARTBEAT_FILENAME, _as_utc, deadline_for,
)
from ledger_end_line import end_line
from ndjson_io import NdjsonCorruptionError, read_ndjson_strict
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE
from vnx_paths import project_id_from_state_dir

LEDGER_FILENAME = "t0_receipts.ndjson"
STATUS_MEASURED, STATUS_UNMEASURED, COHORT_ALL = "measured", "unmeasured", "all_v3"
DEFECTS = ("start_without_end", "end_without_start", "double_end", "projection_conflict",
           "provenance_unknown")
KEY_FIELDS = ("project_id", "dispatch_id", "attempt")
GIT_TIMEOUT_SECONDS = 10.0  # a git that hangs must never hang the count
DEFAULT_REPO = Path(__file__).resolve().parent  # the checkout that writes the lines


class Unmeasured(Exception):
    """This store, or the git it needs, cannot be measured; the reason travels on."""


def _git(repo: Path, *args: str) -> int:
    """The exit code: ``0`` yes, ``1`` no, anything else is not an answer (never a guessed no)."""
    try:  # one helper, one timeout: a git that hangs must never hang the count
        probe = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                               timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unmeasured(f"git could not run in {repo}: {exc}") from exc
    return probe.returncode


@lru_cache(maxsize=None)
def _known(repo: str, sha: str) -> bool:
    return _git(Path(repo), "cat-file", "-e", f"{sha}^{{commit}}") == 0


@lru_cache(maxsize=None)
def _in_cohort(repo: str, activation: str, sha: str) -> bool:
    code = _git(Path(repo), "merge-base", "--is-ancestor", activation, sha)
    if code not in (0, 1):
        raise Unmeasured(f"git merge-base {activation} {sha} answered {code}: not an answer")
    return code == 0


def _verdict(line: dict, repo: str, activation, project: str):
    """``None`` for another project, else ``"out"``, ``"unknown"`` or ``"in"``."""
    pid = line.get("project_id")
    if isinstance(pid, str) and pid.strip() and pid.strip() != project:
        return None
    sha = line.get("writer_sha")
    if not isinstance(sha, str) or not sha.strip():
        return "unknown"  # a v3 line without provenance is not a known line
    if not _known(repo, sha.strip()):
        return "unknown"
    return "out" if activation and not _in_cohort(repo, activation, sha.strip()) else "in"


def _key(line: dict, loose: bool = False):
    """``(project_id, dispatch_id, attempt)``; ``None`` when unpairable, unless ``loose``."""
    values = [line.get(name) for name in KEY_FIELDS]
    pid = values[0].strip() if isinstance(values[0], str) and values[0].strip() else None
    did = values[1].strip() if isinstance(values[1], str) and values[1].strip() else None
    attempt = (values[2] if isinstance(values[2], int) and not isinstance(values[2], bool)
               and values[2] >= 1 else None)
    return (pid, did, attempt) if loose or (pid and did and attempt) else None


def _records(state_dir: Path, repo: str, activation, project: str):
    """In-scope v3 starts and ends by key, the unknown-provenance keys, the problems.

    Records come back grouped by dispatch id, so an attempt's one end is read
    through ``ledger_end_line.end_line`` without rescanning the ledger.
    """
    path = state_dir / LEDGER_FILENAME
    try:
        lines = read_ndjson_strict(path)  # a torn tail is no record; a hole is no evidence
    except NdjsonCorruptionError as exc:
        raise Unmeasured(f"{exc}; the count is not measurable") from exc
    except OSError as exc:
        raise Unmeasured(f"cannot read {path}: {exc}") from exc
    starts, ends, unknown, problems, records = {}, {}, [], [], {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        records.setdefault(str(line.get("dispatch_id") or "").strip(), []).append(line)
        version = line.get("schema_version")
        if isinstance(version, bool) or version != 3:
            continue  # no backfill: a legacy line is never paired and never counted
        verdict = _verdict(line, repo, activation, project)
        if verdict is None:
            continue  # another project's line, whatever its event type
        if verdict == "unknown":  # every v3 line of this project carries its provenance
            key = _key(line, loose=True)  # the attempt it names, else None: never a guess
            if key not in unknown:
                unknown.append(key)
        event = str(line.get("event_type") or "").strip()
        if verdict == "out" or event not in (DISPATCH_STARTED_EVENT_TYPE,
                                             DISPATCH_FINISHED_EVENT_TYPE):
            continue  # pairing is start/end only; "out" already left the cohort
        key = _key(line)
        if key is None:
            problems.append(f"unkeyable {event}: dispatch_id={line.get('dispatch_id')!r}")
            continue
        if event == DISPATCH_STARTED_EVENT_TYPE:
            starts.setdefault(key, line)
        else:
            ends.setdefault(key, []).append(line)
    return starts, ends, unknown, problems, records


def _defects(state_dir: Path, starts: dict, ends: dict, records: dict, evaluated: datetime,
             problems: list) -> dict:
    found = {name: [] for name in DEFECTS}  # rule 3: a list of keys per defect

    rows, db_path, pairs = {}, db_path_from_state_dir(state_dir), {k[:2] for k in [*starts, *ends]}
    try:
        if pairs and db_path.is_file():
            with get_connection(state_dir) as conn:
                for project, dispatch in sorted(pairs):
                    row = conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ? "
                                       "AND project_id = ?", (dispatch, project)).fetchone()
                    if row is not None: rows[(project, dispatch)] = dict(row)
    except Exception as exc:  # vnx-silent-except: an unreadable store is a refusal, never "no row"
        raise Unmeasured(f"cannot read {db_path}: {exc}") from exc
    for key, start in starts.items():
        if key in ends: continue
        deadline = deadline_for(state_dir, start, rows.get(key[:2]))[0]
        try:
            started = _as_utc(start.get("timestamp"))
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
            continue
        if started is None:
            problems.append(f"{key}: start line has no timestamp")
        elif evaluated > started + timedelta(seconds=deadline + ABANDON_GRACE_SECONDS):
            found["start_without_end"].append(key)
    for key, lines in ends.items():
        if len(lines) > 1: found["double_end"].append(key)
        end = end_line(records.get(key[1], ()), *key) or lines[-1]  # the fabric's own reader
        if key not in starts: found["end_without_start"].append(key)
        target = END_STATE_TARGET.get(str(end.get("end_state") or "").strip())
        if target is None:
            problems.append(f"{key}: end_state {end.get('end_state')!r} has no projection")
        elif key[:2] in rows:
            row = rows[key[:2]]
            attempt, state = row.get("attempt_count"), str(row.get("state") or "").strip()
            if attempt == key[2] and not isinstance(attempt, bool) and state != target \
                    and state not in PROJECTION_ROUTES[target]:
                found["projection_conflict"].append(key)
    return found


def _sweep_age(state_dir: Path, evaluated: datetime):
    """The heartbeat K-6 writes: ``(age_seconds, None)``, or ``(None, reason)`` when absent."""
    path = state_dir / END_INTENT_DIRNAME / SWEEP_HEARTBEAT_FILENAME
    try:
        written = _as_utc(json.loads(path.read_text(encoding="utf-8")).get("finished_at"))
    except FileNotFoundError:
        return None, f"no sweep heartbeat at {path}"
    except (OSError, ValueError, AttributeError) as exc:
        return None, f"unreadable sweep heartbeat {path}: {exc}"
    return (round((evaluated - written).total_seconds(), 3), None) if written else \
        (None, f"sweep heartbeat {path} carries no finished_at")


def _unmeasured(reason: str) -> dict:
    """Fail-closed: every count is ``None`` — there is no zero here to mistake for clean."""
    return {"status": STATUS_UNMEASURED, "reason": reason, "cohort": None,
            "counts": dict.fromkeys(DEFECTS), "findings": {name: [] for name in DEFECTS},
            "sweep_heartbeat_age_seconds": None, "sweep_heartbeat_reason": reason,
            "problems": [reason]}


def briefing_line(count: dict) -> str:
    """The one SessionStart line: the five numbers and the heartbeat's age."""
    if count.get("status") != STATUS_MEASURED:
        return f"lifecycle: unmeasured ({count.get('reason') or 'not measured'}); sweep unknown"
    counts, age = count.get("counts") or {}, count.get("sweep_heartbeat_age_seconds")
    sweep = (f"{int(age)}s" if isinstance(age, (int, float)) and not isinstance(age, bool)
             else f"unknown ({count.get('sweep_heartbeat_reason') or 'no reason'})")
    return (f"lifecycle: {counts.get('start_without_end')} start-without-end, "
            f"{counts.get('end_without_start')} end-without-start, "
            f"{counts.get('double_end')} double, {counts.get('projection_conflict')} conflict, "
            f"{counts.get('provenance_unknown')} provenance-unknown; sweep {sweep}")


def compute_lifecycle_count(state_dir, *, project_id=None, activation_commit=None, repo=None,
                            now=None) -> dict:
    """Count the five defects over this project's ``v3`` lines. Read-only.

    ``activation_commit`` narrows the cohort (without it every ``v3`` line is in
    and ``cohort`` says ``all_v3``), ``repo`` is the checkout the provenance is
    read against, ``now`` the evaluation instant (absent: the real clock).
    """
    state_path = Path(state_dir)
    evaluated = _as_utc(now) or datetime.now(timezone.utc)
    project = (project_id or project_id_from_state_dir(state_path)
               or os.environ.get("VNX_PROJECT_ID", "")).strip()
    if not project:
        return _unmeasured(f"no project id for {state_path}: the count is read per project")
    activation = (activation_commit or "").strip() or None
    repo = str(Path(repo) if repo is not None else DEFAULT_REPO)
    try:
        if _git(Path(repo), "rev-parse", "--git-dir"):
            return _unmeasured(f"{repo} is not a git repository: provenance cannot be measured")
        if activation and _git(Path(repo), "rev-parse", "--verify", "--quiet",
                               f"{activation}^{{commit}}"):
            return _unmeasured(f"activation commit {activation} does not resolve")
        starts, ends, unknown, problems, records = _records(
            state_path, repo, activation, project)
        found = _defects(state_path, starts, ends, records, evaluated, problems)
        if problems:  # a problem is not a measurement: no zero may stand for it
            raise Unmeasured("; ".join(problems))
    except Unmeasured as exc:
        return _unmeasured(str(exc))
    found["provenance_unknown"] = unknown
    counts = {name: len(found[name]) for name in DEFECTS}
    age, age_reason = _sweep_age(state_path, evaluated)
    return {"status": STATUS_MEASURED, "cohort": activation or COHORT_ALL,
            "evaluated_at": evaluated.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": dict(counts, total=sum(counts.values())), "findings": found,
            "sweep_heartbeat_age_seconds": age, "sweep_heartbeat_reason": age_reason,
            "problems": problems}
