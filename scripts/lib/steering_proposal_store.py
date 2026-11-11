"""The proposal store of the operator-steering track (ADR-072 decisions 9 and 14).

``state/steering_proposal_decisions.ndjson`` is the truth: append-only, fsynced first. The
projection ``state/steering_proposals.json`` is rebuilt from the records and never read as
input, so a torn projection cannot make the store forget a decision.

* ``proposal_id = sha256(project_id | oorzaak | artefact_soort | doelpad)[:16]``; the text is
  a revision under that id. A rejected proposal returns to ``pending`` only through a
  ``reopened`` record; the batch rule for when is ``reconcile`` (OB-5a-b).
* ``approved`` applies nothing; ``applied`` needs an ``applied_ref``. Every read and write is
  scoped by ``project_id`` (ADR-007). Writes take an exclusive ``fcntl.flock`` (the pattern of
  ``prevention_rule_store.py``). An unreadable record file raises ``StoreUnreadable``.
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

PROJECTION_FILENAME = "steering_proposals.json"
DECISIONS_FILENAME = "steering_proposal_decisions.ndjson"
LOCK_FILENAME = ".steering_proposals.lock"
SCHEMA = "steering_proposals/1"
REVISION_DECISIONS = frozenset({"proposed", "revised", "reopened"})
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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> Optional[datetime]:
    """A timezone-aware timestamp, or None when absent or malformed (never 'new')."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _normalise(project_id: str, proposal: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(proposal, Mapping):
        raise ProposalInvalid("a proposal is a mapping")
    fields = {name: _text(proposal.get(name)) for name in _REQUIRED_TEXT}
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise ProposalInvalid("proposal misses " + ", ".join(missing))
    if fields["oorzaak"] == "insufficient_evidence":
        raise ProposalInvalid("insufficient_evidence is no proposal")
    evidence = proposal.get("evidence") or []
    if not isinstance(evidence, Sequence) or isinstance(evidence, str):
        raise ProposalInvalid("evidence is a list of {prompt_id, ts}")
    items = [e for e in evidence if isinstance(e, Mapping) and _text(e.get("prompt_id"))]
    fields["evidence"] = [{"prompt_id": _text(e["prompt_id"]), "ts": e.get("ts")} for e in items]
    fields["proposal_id"] = proposal_id(project_id, fields["oorzaak"], fields["artefact_soort"], fields["doelpad"])
    return fields



def _parse_record(path: Path, number: int, line: str) -> Dict[str, Any]:
    where = f"{path}:{number}"
    try:
        record = json.loads(line)
    except ValueError as exc:
        raise StoreUnreadable(f"{where}: not JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise StoreUnreadable(f"{where}: not a record")
    for name in ("proposal_id", "project_id", "decision_id", "decided_by"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise StoreUnreadable(f"{where}: {name} missing")
    revision = record.get("revision")
    if record.get("decision") not in RECORD_DECISIONS or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise StoreUnreadable(f"{where}: decision or revision invalid")
    if _parse_ts(record.get("decided_at")) is None:
        raise StoreUnreadable(f"{where}: decided_at invalid")
    if record["decision"] in REVISION_DECISIONS and not all(_text(record.get(n)) for n in _REQUIRED_TEXT):
        raise StoreUnreadable(f"{where}: revision record without content")
    return record


def _read_records(state_dir: Path) -> List[Dict[str, Any]]:
    path = Path(state_dir) / DECISIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise StoreUnreadable(f"{path}: unreadable ({exc})") from exc
    records = [_parse_record(path, n, ln) for n, ln in enumerate(text.split("\n"), 1) if ln.strip()]
    state: Dict[str, Dict[str, Any]] = {}
    for number, record in enumerate(records, 1):
        try:
            _apply(state, record)
        except DecisionRefused as exc:
            raise StoreUnreadable(f"{path}: record {number} breaks the sequence ({exc})") from exc
    return records


def _add_revision(entry: Dict[str, Any], record: Mapping[str, Any]) -> None:
    if record["revision"] != len(entry["revisions"]) + 1:
        raise DecisionRefused("revision out of sequence")
    entry["revisions"].append({
        "revision": record["revision"],
        "voorstel": _text(record["voorstel"]),
        "evidence_prompt_ids": list(record.get("evidence_prompt_ids") or []),
        "recorded_at": record["decided_at"],
    })


def _apply(state: Dict[str, Dict[str, Any]], record: Mapping[str, Any]) -> None:
    """Fold one record into the state. Raises DecisionRefused when it does not fit."""
    pid, decision = record["proposal_id"], record["decision"]
    if decision == "proposed":
        if pid in state:
            raise DecisionRefused("proposed twice")
        state[pid] = {
            "proposal_id": pid, "project_id": record["project_id"], "oorzaak": _text(record["oorzaak"]),
            "artefact_soort": _text(record["artefact_soort"]), "doelpad": _text(record["doelpad"]),
            "status": "pending", "revisions": [], "decided_at": None, "decided_revision": None, "applied_ref": None,
        }
    entry = state.get(pid)
    if entry is None or entry["project_id"] != record["project_id"]:
        raise DecisionRefused("unknown proposal")
    status = entry["status"]
    if decision in REVISION_DECISIONS:
        if decision == "reopened" and status != "rejected":
            raise DecisionRefused("only a rejected proposal reopens")
        _add_revision(entry, record)
        if decision == "reopened":
            entry["status"] = "pending"
    elif decision in ("approved", "rejected"):
        if status != "pending" or record["revision"] > len(entry["revisions"]):
            raise DecisionRefused(f"cannot {decision} a {status} proposal")
        entry.update(status=decision, decided_at=record["decided_at"], decided_revision=record["revision"])
    elif status != "approved" or not _text(record.get("applied_ref")):
        raise DecisionRefused("applied needs an approved proposal and an applied_ref")
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
    payload = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records)
    with open(Path(state_dir) / DECISIONS_FILENAME, "a", encoding="utf-8") as handle:  # vnx-atomic-write: append-only record, fsynced
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _project(state_dir: Path, state: Mapping[str, Mapping[str, Any]]) -> None:
    """Write the projection when it differs from the replayed state (temp file plus replace)."""
    path = Path(state_dir) / PROJECTION_FILENAME
    body = json.dumps({"schema": SCHEMA, "proposals": sorted(state.values(), key=lambda e: e["proposal_id"])},
                      indent=2, ensure_ascii=False, sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == body:
            return
    except (OSError, ValueError):  # vnx-silent-except: the projection is derived, a missing or unreadable one is rewritten
        pass
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _record(project_id: str, pid: str, revision: int, decision: str, now: str, **extra: Any) -> Dict[str, Any]:
    seed = "|".join((project_id, pid, str(revision), decision, now, str(extra.get("voorstel", ""))))
    record: Dict[str, Any] = {
        "decision_id": "sp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16],
        "proposal_id": pid, "revision": revision, "decision": decision, "project_id": project_id,
        "decided_at": now, "decided_by": "system", "reason": "", "evidence_prompt_ids": [],
    }
    record.update(extra)
    return record



def proposals(state_dir: Path, project_id: str) -> List[Dict[str, Any]]:
    """Every proposal of this project with all revisions. Rebuilds the projection."""
    project_id = _require_project(project_id)
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        _project(state_dir, state)
    return [state[k] for k in sorted(state) if state[k]["project_id"] == project_id]


def get(state_dir: Path, project_id: str, pid: str) -> Dict[str, Any]:
    for entry in proposals(state_dir, project_id):
        if entry["proposal_id"] == pid:
            return entry
    raise ProposalNotFound(pid)


def decisions(state_dir: Path, project_id: str) -> List[Dict[str, Any]]:
    project_id = _require_project(project_id)
    with _locked(state_dir):
        records = _read_records(state_dir)
    return [r for r in records if r["project_id"] == project_id]



def add_revision(state_dir: Path, project_id: str, proposal: Mapping[str, Any], *, reopen: bool = False,
                 evidence_prompt_ids: Optional[Sequence[str]] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """Append one revision record: ``proposed`` for a new identity, ``revised`` for a known one,
    ``reopened`` (only valid on a rejected proposal) when ``reopen``. Returns the record. Whether
    a batch entry deserves a revision is the job of ``reconcile`` (OB-5a-b), not of this."""
    project_id = _require_project(project_id)
    item = _normalise(project_id, proposal)
    stamp = now or _now()
    if _parse_ts(stamp) is None:
        raise ProposalInvalid("now is not an ISO timestamp with a timezone")
    ids = list(evidence_prompt_ids) if evidence_prompt_ids is not None else [e["prompt_id"] for e in item["evidence"]]
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(item["proposal_id"])
        decision = "proposed" if entry is None else ("reopened" if reopen else "revised")
        revision = 1 if entry is None else len(entry["revisions"]) + 1
        content = {n: item[n] for n in _REQUIRED_TEXT}
        record = _record(project_id, item["proposal_id"], revision, decision, stamp, evidence_prompt_ids=ids, **content)
        _apply(state, record)  # raises DecisionRefused before anything is written
        _append(state_dir, [record])
        _project(state_dir, state)
    return record


def decide(state_dir: Path, project_id: str, pid: str, decision: str, *, reason: str, decided_by: str,
           revision: Optional[int] = None, applied_ref: str = "", now: Optional[str] = None) -> Dict[str, Any]:
    """Record an operator decision bound to a revision (default: the latest). The record is
    appended and fsynced first, the projection follows. ``approved`` applies nothing."""
    project_id = _require_project(project_id)
    if decision not in OPERATOR_DECISIONS:
        raise DecisionRefused(f"decision must be one of {sorted(OPERATOR_DECISIONS)}")
    if not _text(reason) or not _text(decided_by):
        raise DecisionRefused("reason and decided_by are required")
    stamp = now or _now()
    if _parse_ts(stamp) is None:
        raise ProposalInvalid("now is not an ISO timestamp with a timezone")
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(pid)
        if entry is None or entry["project_id"] != project_id:
            raise ProposalNotFound(pid)
        bound = revision if revision is not None else len(entry["revisions"])
        extra: Dict[str, Any] = {"reason": _text(reason), "decided_by": _text(decided_by)}
        if decision == "applied":
            extra["applied_ref"] = _text(applied_ref)
        record = _record(project_id, pid, bound, decision, stamp, **extra)
        _apply(state, record)  # raises DecisionRefused before anything is written
        _append(state_dir, [record])
        _project(state_dir, state)
    return record
