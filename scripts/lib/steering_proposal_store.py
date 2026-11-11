"""The proposal store of the operator-steering track (ADR-072 decisions 9 and 14).

``state/steering_proposal_decisions.ndjson`` is the truth: append-only, fsynced, every append on a line
boundary. ``proposal_id = sha256(project_id | oorzaak | artefact_soort | doelpad)[:16]``; the text is a
revision under that id. The derived projection and reopening a rejected proposal come with ``reconcile``
(OB-5a-b). ``approved`` applies nothing; ``applied`` needs an ``applied_ref``. Every read and write is
scoped by ``project_id`` (ADR-007) and takes an exclusive ``fcntl.flock``. An unreadable record file
raises ``StoreUnreadable``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

DECISIONS_FILENAME = "steering_proposal_decisions.ndjson"
LOCK_FILENAME = ".steering_proposals.lock"
REVISION_DECISIONS = frozenset({"proposed", "revised"})
OPERATOR_DECISIONS = frozenset({"approved", "rejected", "applied"})
RECORD_DECISIONS = REVISION_DECISIONS | OPERATOR_DECISIONS
_REQUIRED_TEXT = ("oorzaak", "artefact_soort", "doelpad", "voorstel")


class StoreUnreadable(ValueError):
    """The record file is not a valid, consistent sequence of records."""


class ProposalInvalid(ValueError):
    """A proposal or argument cannot be stored; nothing was written."""


class DecisionRefused(ValueError):
    """The decision does not fit the state of the proposal."""


class ProposalNotFound(LookupError):
    """No proposal with that id exists for this project."""


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _require_project(project_id: Any) -> str:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ProposalInvalid("project_id is required")
    return project_id.strip()


def proposal_id(project_id: str, oorzaak: str, artefact_soort: str, doelpad: str) -> str:
    key = "|".join((project_id, oorzaak, artefact_soort, doelpad))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _stamp(now: Optional[str]) -> str:
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _parse_ts(stamp) is None:
        raise ProposalInvalid("now is not an ISO timestamp with a timezone")
    return stamp


def _parse_ts(value: Any) -> Optional[datetime]:
    """A timezone-aware timestamp, or None when absent or malformed."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):  # vnx-silent-except: absent or malformed is None, never a pass
        return None
    return parsed if parsed.tzinfo is not None else None


def _normalise(project_id: str, proposal: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(proposal, Mapping):
        raise ProposalInvalid("a proposal is a mapping")
    fields = {name: _text(proposal.get(name)) for name in _REQUIRED_TEXT}
    missing = [name for name, value in fields.items() if not value]
    if missing or fields["oorzaak"] == "insufficient_evidence":
        raise ProposalInvalid("proposal misses " + ", ".join(missing) if missing else "insufficient_evidence is no proposal")
    evidence = proposal.get("evidence") or []
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        raise ProposalInvalid("evidence is a list of {prompt_id, ts}")
    fields["evidence"] = [{"prompt_id": _text(e["prompt_id"]), "ts": e.get("ts")}
                          for e in evidence if isinstance(e, Mapping) and _text(e.get("prompt_id"))]
    fields["proposal_id"] = proposal_id(project_id, fields["oorzaak"], fields["artefact_soort"], fields["doelpad"])
    return fields


def _validate_record(where: str, record: Any) -> Dict[str, Any]:
    """The one check a record passes on read and before it is appended."""
    if not isinstance(record, dict):
        raise StoreUnreadable(f"{where}: not a record")
    missing = [n for n in ("proposal_id", "project_id", "decision_id", "decided_by") if not _text(record.get(n))]
    if missing:
        raise StoreUnreadable(f"{where}: {missing[0]} missing")
    revision = record.get("revision")
    if record.get("decision") not in RECORD_DECISIONS or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise StoreUnreadable(f"{where}: decision or revision invalid")
    if _parse_ts(record.get("decided_at")) is None:
        raise StoreUnreadable(f"{where}: decided_at invalid")
    if record["decision"] in REVISION_DECISIONS and not all(_text(record.get(n)) for n in _REQUIRED_TEXT):
        raise StoreUnreadable(f"{where}: revision record without content")
    return record


def _parse_record(path: Path, number: int, line: str) -> Dict[str, Any]:
    try:
        return _validate_record(f"{path}:{number}", json.loads(line))
    except ValueError as exc:
        if isinstance(exc, StoreUnreadable):
            raise
        raise StoreUnreadable(f"{path}:{number}: not JSON ({exc})") from exc


def _read_records(state_dir: Path) -> List[Dict[str, Any]]:
    path = Path(state_dir) / DECISIONS_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise StoreUnreadable(f"{path}: unreadable ({exc})") from exc
    records = [_parse_record(path, n, ln) for n, ln in enumerate(text.split("\n"), 1) if ln.strip()]
    try:
        _replay(records)
    except DecisionRefused as exc:
        raise StoreUnreadable(f"{path}: records break the sequence ({exc})") from exc
    return records


def _add_revision(entry: Dict[str, Any], record: Mapping[str, Any]) -> None:
    if record["revision"] != len(entry["revisions"]) + 1:
        raise DecisionRefused("revision out of sequence")
    entry["revisions"].append({"revision": record["revision"], "voorstel": _text(record["voorstel"]),
                               "evidence_prompt_ids": list(record.get("evidence_prompt_ids") or []),
                               "recorded_at": record["decided_at"]})


def _apply(state: Dict[str, Dict[str, Any]], record: Mapping[str, Any]) -> None:
    """Fold one record into the state. Raises DecisionRefused when it does not fit."""
    pid, decision = record["proposal_id"], record["decision"]
    if decision == "proposed":
        if pid in state:
            raise DecisionRefused("proposed twice")
        state[pid] = {"proposal_id": pid, "project_id": record["project_id"], "oorzaak": _text(record["oorzaak"]),
                      "artefact_soort": _text(record["artefact_soort"]), "doelpad": _text(record["doelpad"]),
                      "status": "pending", "revisions": [], "decided_at": None, "decided_revision": None,
                      "applied_ref": None}
    entry = state.get(pid)
    if entry is None or entry["project_id"] != record["project_id"]:
        raise DecisionRefused("unknown proposal")
    if decision in REVISION_DECISIONS:
        _add_revision(entry, record)
    elif decision in ("approved", "rejected"):
        if entry["status"] != "pending" or not 1 <= record["revision"] <= len(entry["revisions"]):
            raise DecisionRefused(f"cannot {decision} a {entry['status']} proposal or a revision it does not have")
        entry.update(status=decision, decided_at=record["decided_at"], decided_revision=record["revision"])
    elif entry["status"] != "approved" or not _text(record.get("applied_ref")) or record["revision"] != entry["decided_revision"]:
        raise DecisionRefused("applied needs an approved proposal, an applied_ref and the approved revision")
    else:
        entry.update(status="applied", applied_ref=_text(record["applied_ref"]))


def _replay(records: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Dict[str, Any]] = {}
    for record in records:
        _apply(state, record)
    return state


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(state_dir) / LOCK_FILENAME, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append(state_dir: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """Runs under the lock after the whole ledger was read and replayed, so a last line without its newline
    parsed as a complete record: the payload then starts with a newline and never joins that line."""
    payload = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    with open(Path(state_dir) / DECISIONS_FILENAME, "ab+") as handle:  # vnx-atomic-write: append-only record, fsynced
        end = os.fstat(handle.fileno()).st_size
        if end and os.pread(handle.fileno(), 1, end - 1) != b"\n":
            payload = b"\n" + payload
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _commit(state_dir: Path, state: Dict[str, Dict[str, Any]], record: Mapping[str, Any]) -> None:
    """Validate as the read path does, fold, append; every refusal comes before the write."""
    try:
        _validate_record("new record", dict(record))
    except StoreUnreadable as exc:
        raise DecisionRefused(f"record refused: {exc}") from exc
    _apply(state, record)
    _append(state_dir, [record])


def _record(project_id: str, pid: str, revision: int, decision: str, now: str, **extra: Any) -> Dict[str, Any]:
    seed = "|".join((project_id, pid, str(revision), decision, now, str(extra.get("voorstel", ""))))
    record: Dict[str, Any] = {"decision_id": "sp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
                              "proposal_id": pid, "revision": revision, "decision": decision, "project_id": project_id,
                              "decided_at": now, "decided_by": "system", "reason": "", "evidence_prompt_ids": []}
    record.update(extra)
    return record


def proposals(state_dir: Path, project_id: str) -> List[Dict[str, Any]]:
    """Every proposal of this project with all revisions, replayed from the records."""
    project_id = _require_project(project_id)
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        return [state[k] for k in sorted(state) if state[k]["project_id"] == project_id]


def get(state_dir: Path, project_id: str, pid: str) -> Dict[str, Any]:
    for entry in proposals(state_dir, project_id):
        if entry["proposal_id"] == pid:
            return entry
    raise ProposalNotFound(pid)


def decisions(state_dir: Path, project_id: str) -> List[Dict[str, Any]]:
    project_id = _require_project(project_id)
    with _locked(state_dir):
        return [r for r in _read_records(state_dir) if r["project_id"] == project_id]


def add_revision(state_dir: Path, project_id: str, proposal: Mapping[str, Any], *,
                 evidence_prompt_ids: Optional[Sequence[str]] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """Append one revision record: ``proposed`` for a new identity, ``revised`` for a known one.
    Returns the record. Whether a batch entry deserves a revision is ``reconcile`` (OB-5a-b)."""
    project_id = _require_project(project_id)
    item = _normalise(project_id, proposal)
    stamp = _stamp(now)
    ids = list(evidence_prompt_ids) if evidence_prompt_ids is not None else [e["prompt_id"] for e in item["evidence"]]
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(item["proposal_id"])
        decision, revision = ("proposed", 1) if entry is None else ("revised", len(entry["revisions"]) + 1)
        record = _record(project_id, item["proposal_id"], revision, decision, stamp, evidence_prompt_ids=ids,
                         **{n: item[n] for n in _REQUIRED_TEXT})
        _commit(state_dir, state, record)
    return record


def decide(state_dir: Path, project_id: str, pid: str, decision: str, *, reason: str, decided_by: str,
           revision: Optional[int] = None, applied_ref: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Record an operator decision bound to a revision (default: the latest). The record is
    appended and fsynced. ``approved`` applies nothing."""
    project_id = _require_project(project_id)
    if decision not in OPERATOR_DECISIONS or not _text(reason) or not _text(decided_by):
        raise DecisionRefused(f"decision must be one of {sorted(OPERATOR_DECISIONS)}, with a reason and decided_by")
    stamp = _stamp(now)
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(pid)
        if entry is None or entry["project_id"] != project_id:
            raise ProposalNotFound(pid)
        bound = revision if revision is not None else len(entry["revisions"])
        extra = {"applied_ref": _text(applied_ref)} if decision == "applied" else {}
        record = _record(project_id, pid, bound, decision, stamp, reason=_text(reason), decided_by=_text(decided_by), **extra)
        _commit(state_dir, state, record)
    return record
