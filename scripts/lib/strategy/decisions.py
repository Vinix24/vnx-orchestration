"""Append-only NDJSON log of operator and T0 decisions.

Layer 1 strategic state — decisions.ndjson lives at
``.vnx-data/strategy/decisions.ndjson``.  Every decision is one JSON
object per line.  Concurrent writers are safe: ``record_decision`` holds
an exclusive ``fcntl.flock`` while writing so lines cannot interleave.

Decision-ID format
------------------
- Operator decision:  ``OD-YYYY-MM-DD-NNN``
- T0 decision:        ``TD-YYYY-MM-DD-NNN``

where YYYY-MM-DD is the calendar date (UTC) and NNN is a zero-padded
3-digit sequence number (001, 002, …) that the caller assigns.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DECISION_ID_RE = re.compile(
    r"^(OD|TD)-(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})-(?P<seq>\d{3})$"
)
_DEFAULT_RELATIVE_PATH = Path(".vnx-data/strategy/decisions.ndjson")

REQUIRED_FIELDS: tuple[str, ...] = ("decision_id", "scope", "ts", "rationale")


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------
class DecisionValidationError(ValueError):
    """Raised when a decision entry fails schema validation."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    decision_id: str
    scope: str
    ts: str
    rationale: str
    supersedes: str | None = None
    evidence_path: str | None = None


# ---------------------------------------------------------------------------
# Path resolution (mirrors roadmap.py pattern)
# ---------------------------------------------------------------------------
def _git_root_from(start: Path) -> Path | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return Path(out).resolve() if out else None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _default_path() -> Path:
    here = Path(__file__).resolve().parent
    root = _git_root_from(here) or _git_root_from(Path.cwd().resolve())
    if root is None:
        env_root = os.environ.get("VNX_CANONICAL_ROOT")
        root = Path(env_root).resolve() if env_root else Path.cwd().resolve()
    return root / _DEFAULT_RELATIVE_PATH


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_decision_id(decision_id: str) -> None:
    """Raise DecisionValidationError if decision_id does not match the schema."""
    m = _DECISION_ID_RE.match(decision_id)
    if not m:
        raise DecisionValidationError(
            f"invalid decision_id '{decision_id}': must match OD-YYYY-MM-DD-NNN "
            "or TD-YYYY-MM-DD-NNN (e.g. OD-2026-05-06-001)"
        )
    month = int(m.group("month"))
    day = int(m.group("day"))
    if not (1 <= month <= 12):
        raise DecisionValidationError(
            f"invalid decision_id '{decision_id}': month {month:02d} is out of range 01-12"
        )
    if not (1 <= day <= 31):
        raise DecisionValidationError(
            f"invalid decision_id '{decision_id}': day {day:02d} is out of range 01-31"
        )


def _validate_entry(data: dict) -> None:
    """Raise DecisionValidationError on schema violations."""
    for field in REQUIRED_FIELDS:
        if field not in data or data[field] is None:
            raise DecisionValidationError(f"missing required field '{field}'")
        if isinstance(data[field], str) and not data[field].strip():
            raise DecisionValidationError(f"required field '{field}' must not be empty")
    _validate_decision_id(data["decision_id"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def _decision_to_dict(d: Decision) -> dict:
    record: dict = {
        "decision_id": d.decision_id,
        "scope": d.scope,
        "ts": d.ts,
        "rationale": d.rationale,
    }
    if d.supersedes is not None:
        record["supersedes"] = d.supersedes
    if d.evidence_path is not None:
        record["evidence_path"] = d.evidence_path
    return record


def _dict_to_decision(data: dict) -> Decision:
    return Decision(
        decision_id=data["decision_id"],
        scope=data["scope"],
        ts=data["ts"],
        rationale=data["rationale"],
        supersedes=data.get("supersedes"),
        evidence_path=data.get("evidence_path"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def record_decision(
    decision_id: str,
    scope: str,
    rationale: str,
    supersedes: str | None = None,
    evidence_path: str | None = None,
    *,
    path: Path | None = None,
) -> Decision:
    """Append a decision to the log and return the persisted Decision object.

    Acquires an exclusive file lock (``fcntl.LOCK_EX``) before writing so
    concurrent callers (T0 + background hooks) cannot interleave lines.

    Raises ``DecisionValidationError`` on schema violations before any I/O.
    """
    ts = datetime.now(timezone.utc).isoformat()
    record: dict = {
        "decision_id": decision_id,
        "scope": scope,
        "ts": ts,
        "rationale": rationale,
    }
    if supersedes is not None:
        record["supersedes"] = supersedes
    if evidence_path is not None:
        record["evidence_path"] = evidence_path

    _validate_entry(record)

    target = Path(path) if path is not None else _default_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"

    with target.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    return Decision(
        decision_id=decision_id,
        scope=scope,
        ts=ts,
        rationale=rationale,
        supersedes=supersedes,
        evidence_path=evidence_path,
    )


def recent_decisions(n: int = 10, *, path: Path | None = None) -> list[Decision]:
    """Return the last *n* decisions in chronological order (oldest first).

    Returns an empty list when the file does not exist or contains no valid
    entries.  Malformed lines are silently skipped.
    """
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        return []

    valid_lines = [
        line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    tail = valid_lines[-n:] if n > 0 else []

    result: list[Decision] = []
    for line in tail:
        try:
            data = json.loads(line)
            result.append(_dict_to_decision(data))
        except (json.JSONDecodeError, KeyError):
            continue
    return result


def supersedes_chain(decision_id: str, *, path: Path | None = None) -> list[Decision]:
    """Walk the supersedes pointer chain backward from *decision_id* to the root.

    Returns the chain ordered from root to the given decision (oldest first),
    so ``chain[0]`` is the original decision and ``chain[-1]`` is the most
    recent one that ultimately supersedes all earlier entries.

    Returns an empty list when the file does not exist or *decision_id* is not
    found.  A cycle guard prevents infinite loops on malformed data.
    """
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        return []

    all_decisions: dict[str, Decision] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            d = _dict_to_decision(data)
            all_decisions[d.decision_id] = d
        except (json.JSONDecodeError, KeyError):
            continue

    if decision_id not in all_decisions:
        return []

    chain: list[Decision] = []
    current_id: str | None = decision_id
    visited: set[str] = set()
    while current_id is not None:
        if current_id in visited:
            break
        visited.add(current_id)
        d = all_decisions.get(current_id)
        if d is None:
            break
        chain.append(d)
        current_id = d.supersedes

    # chain is [decision_id → … → root]; reverse so root comes first
    chain.reverse()
    return chain


__all__ = [
    "Decision",
    "DecisionValidationError",
    "REQUIRED_FIELDS",
    "record_decision",
    "recent_decisions",
    "supersedes_chain",
]
