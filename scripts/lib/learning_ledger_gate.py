"""ADR-065 §6: the typed verdict ``ledger_verdict/1`` on the ledger the learning loop reads.

A thin projection of ``compute_lifecycle_count`` and ``attempt_verdict`` (no second reader).
Seven rules, top first, first hit wins; see ``_evaluate``. Fail closed: an unknown value, a
missing field, an ``OSError`` or any exception from a building block gives ``refused`` with
the reason, never ``ok`` and never a raise. A ``legacy`` dispatch is no evidence and never a
reason to refuse. The verdict file (``write_verdict``/``read_verdict``) is added in split 2.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dispatch_lifecycle import _as_utc
from ledger_attempt_state import (ATTEMPT_KINDS, KIND_DEAD_NO_END, KIND_ENDED, KIND_LEGACY,
                                  LEDGER_FILENAME, MODE_V3, attempt_verdict, read_ledger)
from lifecycle_count import (COHORT_ALL, DEFAULT_REPO, DEFECTS, STATUS_MEASURED,
                             _verdict as _cohort_membership, compute_lifecycle_count)
from receipt_outcome import noise_reason
from receipt_schema import DISPATCH_STARTED_EVENT_TYPE

SCHEMA = "ledger_verdict/1"
ACTIVATION_ENV = "VNX_LEDGER_ACTIVATION_COMMIT"
STATUS_OK, STATUS_PENDING, STATUS_REFUSED = "ok", "pending", "refused"
SWEEP_MAX_AGE_SECONDS = 600  # the sweep ticks every cycle: older means the processor stalled
PENDING_MAX_AGE = timedelta(hours=24)
DEFECT_RULES = ("end_without_start", "double_end", "projection_conflict", "provenance_unknown")

REASON_OK, REASON_PENDING = STATUS_OK, STATUS_PENDING
REASON_UNMEASURED, REASON_PROCESSOR_STALLED = "unmeasured", "processor_stalled"
REASON_NOISE_IN_COHORT, REASON_EMPTY_COHORT = "noise_in_cohort", "empty_cohort"
REASON_STALE_IN_FLIGHT, REASON_LEDGER_UNREADABLE = "stale_in_flight", "ledger_unreadable"
REASON_INVALID_INPUT, REASON_EVALUATION_FAILED = "invalid_input", "evaluation_failed"

_FIELDS = frozenset({"schema", "evaluated_at", "project_id", "cohort", "status", "reason_code",
                     "reason", "counts"})
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

def _verdict(evaluated: datetime, project: Any, cohort: Any, status: str, code: str,
             reason: str, counts: Any = None) -> Dict[str, Any]:
    return {"schema": SCHEMA, "evaluated_at": evaluated.strftime(_TS_FORMAT),
            "project_id": project, "cohort": cohort, "status": status, "reason_code": code,
            "reason": reason, "counts": counts if isinstance(counts, dict) else {}}

def _defect_counts(count: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """The five defect counts as non-negative ints, or None: a bool, None or string is no count."""
    raw = count.get("counts")
    parsed = {name: raw.get(name) for name in DEFECTS} if isinstance(raw, dict) else {}
    ok = parsed and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                        for v in parsed.values())
    return parsed if ok else None

def _cohort_lines(records: Any, activation: Optional[str], project: str) -> list:
    """lifecycle_count's cohort: this project's v3 lines the activation keeps."""
    return [line for line in records if isinstance(line, dict) and line.get("schema_version") == 3
            and _cohort_membership(line, str(DEFAULT_REPO), activation, project)
            not in (None, "out")]

def _cohort_attempts(state_dir: Path, project: str, records: Any, cohort_lines: list):
    """(ended count, unfinished attempts, error text for an unknown attempt kind or '')."""
    dispatch_ids = dict.fromkeys(
        str(line.get("dispatch_id") or "").strip() for line in cohort_lines
        if line.get("event_type") == DISPATCH_STARTED_EVENT_TYPE
        and str(line.get("lifecycle_mode") or "").strip() == MODE_V3)
    ended, unfinished = 0, []
    for dispatch_id in filter(None, dispatch_ids):
        attempt = attempt_verdict(state_dir, project, dispatch_id, records=records)
        if attempt.kind not in ATTEMPT_KINDS:
            return ended, unfinished, f"{dispatch_id}: unknown attempt kind {attempt.kind!r}"
        if attempt.kind == KIND_ENDED:
            ended += 1
        elif attempt.kind != KIND_LEGACY:
            unfinished.append(attempt)
    return ended, unfinished, ""

def _stale_in_flight(unfinished: list, evaluated: datetime) -> str:
    """The reason the first in-flight attempt is stale (dead, unreadable start, future, >24h), or ''."""
    for attempt in unfinished:
        try:
            started = _as_utc((attempt.start_line or {}).get("timestamp"))
        except ValueError:
            started = None
        where = f"{attempt.dispatch_id} attempt {attempt.attempt}"
        if attempt.kind == KIND_DEAD_NO_END:
            return f"{where} is dead without an end"
        if started is None or not timedelta(0) <= evaluated - started <= PENDING_MAX_AGE:
            return f"{where} start time is unreadable, in the future or older than 24h"
    return ""

def _evaluate(state_dir: Path, project_id: Any, activation_commit: Optional[str],
              evaluated: datetime) -> Dict[str, Any]:
    project = project_id.strip() if isinstance(project_id, str) else ""
    activation = os.environ.get(ACTIVATION_ENV, "") if activation_commit is None \
        else activation_commit
    activation = activation.strip() or None if isinstance(activation, str) else None
    cohort = activation or COHORT_ALL

    def verdict(status: str, code: str, reason: str, counts: Any = None) -> Dict[str, Any]:
        return _verdict(evaluated, project or None, cohort, status, code, reason, counts)

    def refuse(code: str, reason: str, counts: Any = None) -> Dict[str, Any]:
        return verdict(STATUS_REFUSED, code, reason, counts)

    if not project:
        return refuse(REASON_INVALID_INPUT, "no project id: the verdict is read per project")
    count = compute_lifecycle_count(state_dir, project_id=project, activation_commit=activation,
                                    now=evaluated)
    if not isinstance(count, dict):
        return refuse(REASON_INVALID_INPUT, "the lifecycle count is not a mapping")
    if isinstance(count.get("cohort"), str) and count["cohort"].strip():
        cohort = count["cohort"].strip()
    if count.get("status") != STATUS_MEASURED:  # rule 1: only the one positive status is measured
        return refuse(REASON_UNMEASURED,
                      f"lifecycle count status {count.get('status')!r}: {count.get('reason')}")
    counts = _defect_counts(count)
    if counts is None:
        return refuse(REASON_INVALID_INPUT, "the lifecycle count carries no usable counts")
    for name in DEFECT_RULES:  # rule 2
        if counts[name] > 0:
            return refuse(name, f"{counts[name]} {name} in cohort {cohort}", counts)
    age = count.get("sweep_heartbeat_age_seconds")  # rule 3: no/negative/NaN age is stalled
    if (isinstance(age, bool) or not isinstance(age, (int, float))
            or not 0 <= age <= SWEEP_MAX_AGE_SECONDS):
        return refuse(REASON_PROCESSOR_STALLED,
                      f"sweep heartbeat age {age!r} (limit {SWEEP_MAX_AGE_SECONDS}s): "
                      f"{count.get('sweep_heartbeat_reason') or 'no reason'}", counts)
    try:
        records = read_ledger(state_dir)
    except OSError as exc:
        return refuse(REASON_LEDGER_UNREADABLE, f"cannot read the ledger: {exc}", counts)
    cohort_lines = _cohort_lines(records, activation, project)
    noisy = sum(1 for line in cohort_lines if noise_reason(line, project) == "pytest")
    if noisy:  # rule 4
        return refuse(REASON_NOISE_IN_COHORT,
                      f"{noisy} source=pytest receipt(s) among the cohort's receipts", counts)
    ended, unfinished, bad = _cohort_attempts(state_dir, project, records, cohort_lines)
    if bad:
        return refuse(REASON_INVALID_INPUT, bad, counts)
    counts = dict(counts, ended=ended, in_flight=len(unfinished))
    if ended == 0:  # rule 5
        return refuse(REASON_EMPTY_COHORT, "no ended attempt in the cohort: it proves nothing",
                      counts)
    unpaired = counts["start_without_end"] - len(unfinished)  # rule 6
    if unpaired > 0:
        return refuse(REASON_STALE_IN_FLIGHT, f"{unpaired} start(s) without an end that "
                      "attempt_verdict does not explain", counts)
    stale = _stale_in_flight(unfinished, evaluated)
    if stale:
        return refuse(REASON_STALE_IN_FLIGHT, stale, counts)
    if unfinished:
        return verdict(STATUS_PENDING, REASON_PENDING,
                       f"{len(unfinished)} attempt(s) in flight, none older than 24h", counts)
    return verdict(STATUS_OK, REASON_OK, f"{ended} ended attempt(s), no defects", counts)

def _fingerprint(state_dir: Path):
    """(size, mtime_ns, inode) of the ledger file, or None when it does not exist."""
    try:
        stat = (state_dir / LEDGER_FILENAME).stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ino

def _evaluate_stable(state_dir: Path, project_id: Any, activation_commit: Optional[str],
                     evaluated: datetime) -> Dict[str, Any]:
    """``_evaluate`` against one unchanged ledger: the count and the attempt reads are separate
    reads, so a changed fingerprint means they may disagree. Retry once, then refuse."""
    for _ in range(2):
        before = _fingerprint(state_dir)
        result = _evaluate(state_dir, project_id, activation_commit, evaluated)
        if _fingerprint(state_dir) == before:
            return result
    project = project_id.strip() if isinstance(project_id, str) else None
    return _verdict(evaluated, project or None, result.get("cohort"), STATUS_REFUSED,
                    REASON_EVALUATION_FAILED,
                    "the ledger changed between the defect count and the attempt read")

def ledger_verdict(state_dir, project_id, *, activation_commit=None, now=None) -> Dict[str, Any]:
    """The verdict ``ledger_verdict/1`` for one project's ledger. Never raises."""
    project = project_id if isinstance(project_id, str) else None
    try:
        evaluated = _as_utc(now) or datetime.now(timezone.utc)
    except (ValueError, TypeError) as exc:
        return _verdict(datetime.now(timezone.utc), project, None, STATUS_REFUSED,
                        REASON_INVALID_INPUT, f"unusable clock: {exc}")
    try:
        return _evaluate_stable(Path(state_dir), project_id, activation_commit, evaluated)
    except Exception as exc:  # fail closed: a raising building block is a refusal with its reason
        return _verdict(evaluated, project, activation_commit or None, STATUS_REFUSED,
                        REASON_EVALUATION_FAILED, f"{type(exc).__name__}: {exc}")
