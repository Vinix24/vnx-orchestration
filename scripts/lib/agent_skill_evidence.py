"""The skill materialisation record as receipt evidence (ADR-059 decision 15, skilldisc R5).

Reads only ``<state_dir>/agent_skill_materializations/<dispatch_id>/<provider>.attempt-<n>.json``
of the dispatch and provider the caller names, never the environment. A refusal is
``unavailable`` with its reason, never ``ok``. Attempts go highest first: one that fails the
identity test is provably not this receipt's and is passed over; a malformed one stops the
read, because whose attempt it is and how it ended cannot be known.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent_skill_materialize import RECORD_DIR, RECORD_SCHEMA

NO_RECORD = "no_record"
RECORD_MALFORMED = "record_malformed"
IDENTITY_MISMATCH = "identity_mismatch"
PROJECT_MISMATCH = "project_mismatch"
SESSION_MISMATCH = "session_mismatch"
UNKNOWN_STATUS = "unknown_status"

# Record status -> receipt status (decision 19e); its keys are exactly the writer's STATUSES (a test pins it).
STATUS_MAP = {"ok": "ok", "partial": "partial", "skipped": "unavailable", "error": "error"}
EVIDENCE_STATUSES = ("ok", "partial")
MAX_RECORD_BYTES = 1 << 20


def read_materialization(state_dir: Optional[Path], dispatch_id: Optional[str], provider: Optional[str], *,
                         project_id: Optional[str], session_id: Optional[str]) -> Dict[str, Any]:
    """``{status, reason, attempt}`` of the highest attempt passing the identity test, plus its ``manifest``
    for ``ok``/``partial``; otherwise the refusal with its reason."""
    attempts = _attempt_files(state_dir, dispatch_id, provider)
    if isinstance(attempts, str):
        return _refusal(attempts)
    passed_over: Optional[Tuple[str, int]] = None
    for attempt, path in attempts:
        record, raw = _load_record(path, attempt)
        if record is None:
            return _refusal(RECORD_MALFORMED, attempt)
        reason = _identity_refusal(record, dispatch_id, provider, project_id, session_id)
        if reason is None:
            return _evidence(record, attempt, raw, provider)
        passed_over = passed_over or (reason, attempt)
    return _refusal(*passed_over)  # every attempt failed the identity test: the highest one's reason


def _refusal(reason: str, attempt: Optional[int] = None) -> Dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "attempt": attempt}


def _single_name(value: Any) -> bool:
    """A non-empty string naming exactly one path component, never ``.`` or ``..``."""
    return isinstance(value, str) and value not in ("", ".", "..") and value == Path(value).name


def _attempt_files(state_dir: Any, dispatch_id: Any, provider: Any) -> Union[List[Tuple[int, Path]], str]:
    """This dispatch's attempt files for *provider*, highest first, matched exactly (never globbed); else a reason."""
    if not state_dir or not _single_name(dispatch_id) or not _single_name(provider):
        return NO_RECORD
    folder = Path(state_dir) / RECORD_DIR / dispatch_id
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return NO_RECORD
    except OSError:
        return RECORD_MALFORMED  # something stands at the folder name that cannot be listed
    pattern = re.compile(re.escape(provider) + r"\.attempt-([1-9][0-9]*)\.json")
    found = [(int(m.group(1)), folder / name) for name in names if (m := pattern.fullmatch(name))]
    return sorted(found, reverse=True) or NO_RECORD


def _load_record(path: Path, attempt: int) -> Tuple[Optional[Dict[str, Any]], bytes]:
    """The record and its bytes when it is a complete regular file (no link, no FIFO) of the writer's form."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_RECORD_BYTES + 1) if regular else b""
        finally:
            os.close(fd)
        record = json.loads(raw.decode("utf-8")) if regular and len(raw) <= MAX_RECORD_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None, b""
    return (record, raw) if _well_formed(record, attempt) else (None, b"")


def _well_formed(record: Any, attempt: int) -> bool:
    """The writer's form (a bool is no number); skipped/error name a reason, ok/partial at least one skill."""
    if not isinstance(record, dict) or "session_id" not in record:
        return False
    skills, reason, status = record.get("skills"), record.get("reason"), record.get("status")
    return (type(record.get("schema")) is int and record["schema"] == RECORD_SCHEMA
            and type(record.get("attempt")) is int and record["attempt"] == attempt
            and isinstance(skills, list) and all(isinstance(entry, dict) for entry in skills)
            and record.get("manifest_sha256") == hashlib.sha256(json.dumps(skills, sort_keys=True).encode()).hexdigest()
            and (reason is None or isinstance(reason, str))
            and not (status in ("skipped", "error") and not reason)
            and not (status in EVIDENCE_STATUSES and not skills))


def _identity_refusal(record: Dict[str, Any], dispatch_id: str, provider: str,
                      project_id: Optional[str], session_id: Optional[str]) -> Optional[str]:
    """Why this record is not this receipt's (decision 19c), or ``None`` when it is."""
    if record.get("dispatch_id") != dispatch_id or record.get("provider") != provider:
        return IDENTITY_MISMATCH
    if not (isinstance(project_id, str) and project_id and record.get("project_id") == project_id):
        return PROJECT_MISMATCH
    recorded = record.get("session_id")
    if recorded is not None and not (isinstance(session_id, str) and session_id and recorded == session_id):
        return SESSION_MISMATCH
    return None


def _evidence(record: Dict[str, Any], attempt: int, raw: bytes, provider: str) -> Dict[str, Any]:
    """Map the chosen record's status (decision 19e); only ``ok``/``partial`` carry a manifest."""
    status = record.get("status")
    mapped = STATUS_MAP.get(status) if isinstance(status, str) else None
    if mapped is None:
        return _refusal(UNKNOWN_STATUS, attempt)
    out: Dict[str, Any] = {"status": mapped, "reason": record.get("reason"), "attempt": attempt}
    if mapped in EVIDENCE_STATUSES:
        out["manifest"] = {"status": mapped, "source": "materialized", "provider": provider,
                           "role": record.get("role"), "attempt": attempt,
                           "record_sha256": hashlib.sha256(raw).hexdigest(),
                           "manifest_sha256": record["manifest_sha256"], "skills": record["skills"]}
        if out["reason"] is not None:
            out["manifest"]["reason"] = out["reason"]
    return out
