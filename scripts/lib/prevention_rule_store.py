"""The one store of the prevention-rule proposal queue (``state/pending_rules.json``).

ADR-065 ("De store is de enige producent en de enige schrijver"): this module is
the single writer of the queue. A write takes an exclusive ``fcntl.flock`` on
``state_dir/.prevention_rules.lock``, reads the queue, merges and replaces the
file atomically. An unreadable queue is never read as an empty one: it raises
``QueueUnreadable`` and the file stays byte-equal, so a corrupt or half-written
file can no longer erase the operator decisions in it.

A decided rule never returns to ``pending`` and its evidence never changes. A
proposal activates nothing: this module does no database access.

Follows in later splits: D3 adds ``decide``, ``mint_approval_id``,
``approved_decision`` and ``reconcile`` to this module, and split 2 of D2 moves
the queue write of ``update_terminal_constraints`` in ``scripts/learning_loop.py``
to ``queue_rules`` here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

QUEUE_FILENAME = "pending_rules.json"
LOCK_FILENAME = ".prevention_rules.lock"
SCHEMA = "pending_rule/1"
STATUS_PENDING = "pending"
DECIDED_STATUSES = frozenset({"approved", "rejected", "ingested", "deactivated"})
KNOWN_STATUSES = DECIDED_STATUSES | {STATUS_PENDING}
MAX_SOURCE_DISPATCH_IDS = 20
_SEP = chr(0x1F)
_NONE_SENTINELS = frozenset({"", "none", "null", "unknown", "n/a", "na", "-"})


class QueueUnreadable(ValueError):
    """The queue file exists but is not a valid ``{"pending_rules": [...]}`` object."""


def _scope(value: Any) -> str:
    """A scope field as a stripped string, "" when absent, not a string or a sentinel."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return "" if text.lower() in _NONE_SENTINELS else text


def _failure_class(proposal: Mapping[str, Any]) -> str:
    value = proposal.get("failure_class")
    return value.strip() if isinstance(value, str) else ""


def rule_id(proposal: Mapping[str, Any]) -> str:
    """``rule-`` plus 8 hex of sha1 over pattern, terminal scope, agent scope and
    failure class, joined by a separator that cannot occur in them. Equal exactly
    when the group key of the learning loop is equal."""
    parts = (
        str(proposal.get("pattern") or ""),
        _scope(proposal.get("terminal_constraint")) or "any",
        _scope(proposal.get("agent_constraint")),
        _failure_class(proposal),
    )
    return f"rule-{hashlib.sha1(_SEP.join(parts).encode()).hexdigest()[:8]}"


def proposal_hash(proposal: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of the five fields a decision binds to."""
    body = {
        "pattern": str(proposal.get("pattern") or ""),
        "terminal_constraint": _scope(proposal.get("terminal_constraint")) or "any",
        "agent_constraint": _scope(proposal.get("agent_constraint")),
        "prevention": str(proposal.get("prevention") or ""),
        "failure_class": _failure_class(proposal),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_queue(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QueueUnreadable(f"{path}: unreadable queue ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("pending_rules"), list):
        raise QueueUnreadable(f"{path}: not an object with a 'pending_rules' list")
    rules = data["pending_rules"]
    if not all(isinstance(rule, dict) for rule in rules):
        raise QueueUnreadable(f"{path}: 'pending_rules' holds an entry that is not an object")
    return rules


def load_queue(state_dir: Path) -> List[Dict[str, Any]]:
    """The queue, read-only. A missing file is an empty list. No lock: the file is
    only ever replaced atomically."""
    return _read_queue(Path(state_dir) / QUEUE_FILENAME)


def _dispatch_ids(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)) or not all(isinstance(v, str) and v for v in value):
        raise ValueError("source_dispatch_ids must be a list of non-empty strings")
    return sorted(set(value))


def _build_entry(proposal: Mapping[str, Any], project_id: str, now: str) -> Dict[str, Any]:
    if not isinstance(proposal, Mapping):
        raise ValueError("a proposal must be a mapping")
    for key in ("pattern", "prevention"):
        if not isinstance(proposal.get(key), str) or not proposal[key].strip():
            raise ValueError(f"proposal {key!r} must be a non-empty string")
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("proposal 'confidence' must be a number")
    count = proposal.get("occurrence_count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("proposal 'occurrence_count' must be a positive integer")
    return {
        "schema": SCHEMA,
        "id": rule_id(proposal),
        "proposal_hash": proposal_hash(proposal),
        "failure_class": _failure_class(proposal),
        "occurrence_count": count,
        "source_dispatch_ids": _dispatch_ids(proposal.get("source_dispatch_ids"))[:MAX_SOURCE_DISPATCH_IDS],
        "status": STATUS_PENDING,
        "created_at": now,
        "project_id": project_id,
        "source": "learning_loop",
        "rule_type": "failure_prevention",
        "pattern": proposal["pattern"],
        "terminal_constraint": _scope(proposal.get("terminal_constraint")) or "any",
        "agent_constraint": _scope(proposal.get("agent_constraint")) or None,
        "prevention": proposal["prevention"],
        "confidence": confidence,
    }


def _merge_pending(existing: Dict[str, Any], entry: Dict[str, Any]) -> bool:
    """Grow the evidence of a pending entry. True when anything changed."""
    try:
        have = _dispatch_ids(existing.get("source_dispatch_ids"))
    except ValueError:
        # Unreadable evidence stays as it is: no merge on a malformed entry.
        return False
    union = sorted(set(have) | set(entry["source_dispatch_ids"]))[:MAX_SOURCE_DISPATCH_IDS]
    old_count = existing.get("occurrence_count")
    if isinstance(old_count, bool) or not isinstance(old_count, int):
        return False
    count = max(old_count, entry["occurrence_count"])
    if union == existing.get("source_dispatch_ids") and count == old_count:
        return False
    existing["source_dispatch_ids"] = union
    existing["occurrence_count"] = count
    return True


def _write_atomic(path: Path, rules: Sequence[Dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pending_rules": list(rules)}, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # The temp file is removed on any failure so none is left behind.
        tmp.unlink(missing_ok=True)
        raise


def queue_rules(
    state_dir: Path,
    proposals: Sequence[Mapping[str, Any]],
    *,
    project_id: str,
    now: str,
) -> Dict[str, int]:
    """Merge ``proposals`` into the queue under an exclusive lock and write it
    atomically. Returns the counts ``added``, ``merged`` and ``unchanged``.

    A new id is added as ``pending``. A ``pending`` entry keeps its ``created_at``
    and grows only its evidence. Any other entry (decided, unknown status or no
    ``schema``) is left byte-equal and counted as ``unchanged``.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    if not isinstance(now, str) or not now:
        raise ValueError("now must be a non-empty string")
    entries = [_build_entry(p, project_id, now) for p in proposals]
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    counts = {"added": 0, "merged": 0, "unchanged": 0}
    with open(state_dir / LOCK_FILENAME, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            path = state_dir / QUEUE_FILENAME
            rules = _read_queue(path)
            by_id = {rule.get("id"): rule for rule in rules}
            for entry in entries:
                existing = by_id.get(entry["id"])
                if existing is None:
                    rules.append(entry)
                    by_id[entry["id"]] = entry
                    counts["added"] += 1
                elif existing.get("schema") == SCHEMA and existing.get("status") == STATUS_PENDING:
                    counts["merged" if _merge_pending(existing, entry) else "unchanged"] += 1
                else:
                    counts["unchanged"] += 1
            if counts["added"] or counts["merged"]:
                _write_atomic(path, rules)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return counts
