"""The one store of the prevention-rule proposal queue (``state/pending_rules.json``).

ADR-065 ("De store is de enige producent en de enige schrijver"): this module is
the single writer of the queue. A write takes an exclusive ``fcntl.flock`` on
``state_dir/.prevention_rules.lock``, reads the queue, merges and replaces the
file atomically. An unreadable queue is never read as an empty one: it raises
``QueueUnreadable`` and the file stays byte-equal, so a corrupt or half-written
file can no longer erase the operator decisions in it.

A decided rule never returns to ``pending`` and its evidence never changes. A
proposal activates nothing: this module does no database access.

Decisions (loop D3): ``state/prevention_rule_decisions.ndjson`` is the truth, the
``status`` in the queue file is a projection of it. ``decide`` appends the record
(fsynced) first and projects second; ``reconcile`` repairs a projection that lags
after a crash. This module is the only producer of ``approval_id`` and
``bundle_ref`` (``mint_approval_id``, ``mint_bundle_ref``). Loop D9 adds the
``deactivated`` record (``record_deactivation``).

The queue write of ``update_terminal_constraints`` in ``scripts/learning_loop.py``
goes through ``queue_rules`` here: it has no writer of its own left.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

QUEUE_FILENAME = "pending_rules.json"
LOCK_FILENAME = ".prevention_rules.lock"
SCHEMA = "pending_rule/1"
STATUS_PENDING = "pending"
DECIDED_STATUSES = frozenset({"approved", "rejected", "ingested", "deactivated"})
KNOWN_STATUSES = DECIDED_STATUSES | {STATUS_PENDING}
DECISIONS_FILENAME = "prevention_rule_decisions.ndjson"
DECISIONS = frozenset({"approved", "rejected"})
RECORD_DECISIONS = DECISIONS | {"deactivated"}
ROLLOUT_SCOPE = "project"
RECOVERY_PATH = "scripts/deactivate_pattern_activation.py --activation-id <id> --reason <tekst>"
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


def _optional_fields(proposal: Mapping[str, Any]) -> Dict[str, Any]:
    """``signal`` and ``source_oi_ids`` of an open-item proposal; nothing for a
    proposal without them, so such a row is byte-equal to one written before."""
    extra: Dict[str, Any] = {}
    if "signal" in proposal:
        signal = proposal["signal"]
        if not isinstance(signal, str) or not signal.strip():
            raise ValueError("proposal 'signal' must be a non-empty string")
        extra["signal"] = signal
    if "source_oi_ids" in proposal:
        oi_ids = proposal["source_oi_ids"]
        if not isinstance(oi_ids, (list, tuple)) or not all(isinstance(v, str) and v for v in oi_ids):
            raise ValueError("source_oi_ids must be a list of non-empty strings")
        extra["source_oi_ids"] = list(oi_ids)
    return extra


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
        **_optional_fields(proposal),
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


class DecisionsUnreadable(ValueError):
    """The decision record has a line that is not a valid decision object."""


class RuleNotFound(LookupError):
    """A rule id is not in the queue."""


class DecisionRefused(ValueError):
    """A decision cannot be recorded: conflicting, not pending, another project or
    a proposal that no longer matches its hash."""


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def mint_approval_id(project_id: str, rule_id: str, decision: str, decided_at: str, decided_by: str) -> str:
    """``apr-`` plus 12 hex of sha256 over the five fields. The only place an
    approval id is formed."""
    return "apr-" + _digest(project_id, rule_id, decision, decided_at, decided_by)


def mint_bundle_ref(rule_ids: Sequence[str], decided_at: str) -> str:
    """``bundle-`` plus 12 hex of sha256 over the sorted rule ids of one call and
    the decision time. The only place a bundle ref is formed."""
    return "bundle-" + _digest(",".join(sorted(rule_ids)), decided_at)


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / LOCK_FILENAME, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _parse_record(path: Path, number: int, line: str) -> Dict[str, Any]:
    try:
        record = json.loads(line)
    except ValueError as exc:
        raise DecisionsUnreadable(f"{path}:{number}: not JSON ({exc})") from exc
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("rule_id"), str)
        or not record["rule_id"]
        or record.get("decision") not in RECORD_DECISIONS
    ):
        raise DecisionsUnreadable(f"{path}:{number}: not a decision record")
    return record


def decisions(state_dir: Path) -> List[Dict[str, Any]]:
    """Every decision record in file order. A missing file is an empty list; a line
    that is not a valid record raises ``DecisionsUnreadable`` (never skipped)."""
    path = Path(state_dir) / DECISIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise DecisionsUnreadable(f"{path}: unreadable ({exc})") from exc
    return [_parse_record(path, n, ln) for n, ln in enumerate(text.split("\n"), 1) if ln.strip()]


def _latest(records: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {record["rule_id"]: record for record in records}


def latest_decision(state_dir: Path, rule_id: str) -> Optional[Dict[str, Any]]:
    """The latest decision record of ``rule_id`` of any kind; None when there is none."""
    record = _latest(decisions(state_dir)).get(rule_id)
    return None if record is None else dict(record)


def approved_decision(state_dir: Path, rule_id: str) -> Optional[Dict[str, Any]]:
    """The latest decision of ``rule_id`` when it is ``approved``; None when there is
    none or the latest one is a rejection or deactivation. Readers of the record
    (D5, D9) call this and ask for nothing the record does not carry."""
    record = latest_decision(state_dir, rule_id)
    return record if record is not None and record["decision"] == "approved" else None


def _project(rules: List[Dict[str, Any]], latest: Mapping[str, Mapping[str, Any]]) -> int:
    changed = 0
    for rule in rules:
        record = latest.get(rule.get("id"))
        if record is None or rule.get("status") == record["decision"]:
            continue
        if rule.get("status") == "ingested" and record["decision"] == "approved":
            continue  # ingest is downstream of the approval: it stays ingested
        rule["status"] = record["decision"]
        changed += 1
    return changed


def _reconcile_locked(state_dir: Path) -> int:
    path = state_dir / QUEUE_FILENAME
    if not path.exists():
        return 0
    rules = _read_queue(path)
    changed = _project(rules, _latest(decisions(state_dir)))
    if changed:
        _write_atomic(path, rules)
    return changed


def reconcile(state_dir: Path) -> int:
    """Set the queue ``status`` to the latest decision record of each rule (the
    record is the truth). Returns the number of entries changed."""
    state_dir = Path(state_dir)
    with _locked(state_dir):
        return _reconcile_locked(state_dir)


def _baseline(entry: Mapping[str, Any]) -> Dict[str, Any]:
    count = entry.get("occurrence_count")
    fclass = entry.get("failure_class")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1 or not isinstance(fclass, str) or not fclass:
        return {"baseline_metric": None, "baseline_value": None}
    return {"baseline_metric": f"dispatches_with_class:{fclass}", "baseline_value": count}


def _check_entry(entry: Mapping[str, Any], project_id: str, decision: str, prior: Optional[Mapping[str, Any]]) -> bool:
    """True when the entry takes a new decision, False for an identical repeat."""
    if entry.get("project_id") != project_id:
        raise DecisionRefused(f"{entry.get('id')}: queue entry belongs to another project")
    if entry.get("proposal_hash") != proposal_hash(entry):
        raise DecisionRefused(f"{entry.get('id')}: the proposal no longer matches its proposal_hash")
    if prior is None:
        if entry.get("status") != STATUS_PENDING:
            raise DecisionRefused(f"{entry.get('id')}: status {entry.get('status')!r} has no decision record")
        return True
    if prior["decision"] == decision:
        return False
    if prior["decision"] == "deactivated" and decision == "approved":
        return True
    raise DecisionRefused(f"{entry.get('id')}: already {prior['decision']}, cannot become {decision}")


def _build_record(
    entry: Mapping[str, Any], decision: str, reason: str, decided_by: str, project_id: str,
    now: str, bundle: Optional[str], effect_criterion: Optional[str], stop_condition: Optional[str],
) -> Dict[str, Any]:
    approval_id = mint_approval_id(project_id, entry["id"], decision, now, decided_by)
    approved = decision == "approved"
    record: Dict[str, Any] = {
        "decision_id": "dec-" + _digest(approval_id),
        "approval_id": approval_id,
        "rule_id": entry["id"],
        "proposal_hash": entry["proposal_hash"],
        "decision": decision,
        "reason": reason,
        "decided_by": decided_by,
        "decided_at": now,
        "project_id": project_id,
        "rollout_scope": ROLLOUT_SCOPE,
        "failure_class": entry.get("failure_class"),
        **_baseline(entry),
        "effect_criterion": effect_criterion if approved else None,
        "stop_condition": stop_condition if approved else None,
        "recovery_path": RECOVERY_PATH if approved else None,
    }
    if approved:
        record["bundle_ref"] = bundle
    return record


def _append_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    payload = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records)
    with open(path, "a", encoding="utf-8") as handle:  # vnx-atomic-write: append-only record, fsynced
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def decide(
    state_dir: Path,
    rule_ids: Sequence[str],
    decision: str,
    *,
    reason: str,
    decided_by: str,
    project_id: str,
    now: str,
    effect_criterion: Optional[str] = None,
    stop_condition: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Record one decision per rule id as one bundle, under the lock.

    Returns the records newly written (an identical repeat writes nothing). All
    rule ids are checked before anything is written, so a bundle is all or nothing:
    ``RuleNotFound`` for an id not in the queue, ``DecisionRefused`` for a conflict.
    The record is appended and fsynced first; the queue projection follows.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    texts = {"reason": reason, "decided_by": decided_by, "project_id": project_id, "now": now}
    for name, value in texts.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if decision == "approved":
        for name, value in (("effect_criterion", effect_criterion), ("stop_condition", stop_condition)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required for an approval")
    ids = sorted(set(rule_ids))
    if not ids or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("rule_ids must hold at least one non-empty string")
    state_dir = Path(state_dir)
    with _locked(state_dir):
        _reconcile_locked(state_dir)
        path = state_dir / QUEUE_FILENAME
        rules = _read_queue(path)
        by_id = {rule.get("id"): rule for rule in rules}
        latest = _latest(decisions(state_dir))
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise RuleNotFound(", ".join(missing))
        fresh = [i for i in ids if _check_entry(by_id[i], project_id, decision, latest.get(i))]
        bundle = mint_bundle_ref(fresh, now) if decision == "approved" else None
        records = [
            _build_record(by_id[i], decision, reason, decided_by, project_id, now, bundle, effect_criterion, stop_condition)
            for i in fresh
        ]
        if records:
            _append_records(state_dir / DECISIONS_FILENAME, records)
            _project(rules, _latest(records))
            _write_atomic(path, rules)
    return records


def record_deactivation(state_dir: Path, activation: Mapping[str, Any], *, reason: str, decided_by: str,
                        now: str) -> Dict[str, Any]:
    """Record that ``activation`` (a ``pattern_activation`` row) is switched off, under the lock.

    The rule's latest record must be the approval the activation carries, in its project:
    ``DecisionRefused`` otherwise. A repeat (the latest record already deactivates this
    activation) returns that record and writes nothing. The record, with its own minted
    ``approval_id``, is appended and fsynced first; the queue status ``deactivated`` follows."""
    for name, value in (("reason", reason), ("decided_by", decided_by), ("now", now)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    project_id, rid, act = activation["project_id"], activation["rule_id"], activation["activation_id"]
    state_dir = Path(state_dir)
    with _locked(state_dir):
        _reconcile_locked(state_dir)
        latest = _latest(decisions(state_dir)).get(rid)
        if latest is not None and latest["decision"] == "deactivated" and latest.get("activation_id") == act:
            return dict(latest)
        if latest is None or latest["decision"] != "approved" or latest.get("project_id") != project_id \
                or latest.get("approval_id") != activation["approval_id"]:
            raise DecisionRefused(f"{rid}: the latest record is not the approval {act} carries")
        approval_id = mint_approval_id(project_id, rid, "deactivated", now, decided_by)
        record = {"decision_id": "dec-" + _digest(approval_id), "approval_id": approval_id, "rule_id": rid,
                  "proposal_hash": latest.get("proposal_hash"), "decision": "deactivated", "reason": reason,
                  "decided_by": decided_by, "decided_at": now, "project_id": project_id,
                  "rollout_scope": ROLLOUT_SCOPE, "failure_class": latest.get("failure_class"),
                  "activation_id": act, "deactivates_approval_id": latest["approval_id"]}
        _append_records(state_dir / DECISIONS_FILENAME, [record])
        _reconcile_locked(state_dir)
    return record

