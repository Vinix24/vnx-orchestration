"""The proposal store of the operator-steering track (ADR-072 decisions 9 and 14).

``state/steering_proposal_decisions.ndjson`` is the truth: append-only, fsynced, every append on a line
boundary. ``proposal_id = sha256(project_id | oorzaak | artefact_soort | doelpad)[:16]``; the text is a
revision under that id. ``state/steering_proposals.json`` is derived from the records after every commit
and read, and never read as a source. A rejected proposal reopens (``reopened`` record, status ``pending``)
only on a prompt whose ``ts`` is after its ``decided_at``; otherwise the revision is kept and counted as
``suppressed_rejected`` (ADR-072 decision 14). A new revision of an approved or applied proposal is stored
and the status stays: the approval stays bound to its own revision. ``approved`` applies nothing;
``applied`` needs an ``applied_ref``. Every read and write is scoped by ``project_id`` (ADR-007) and takes
an exclusive ``fcntl.flock``. An unreadable record file raises ``StoreUnreadable``.

ADR-084 for the three ``PERMISSION_KINDS``: a rejection reopens only on ``reopen_with_note`` or a changed
``handelingsklasse``, never on fresh evidence. A notification (``MELDING_KIND``) is only ``acknowledged``, and
it suppresses every permission proposal with its ``oorzaak`` for good: ``_apply`` refuses every step but a
``suppressed`` record (status ``suppressed_hard_stop``), so every writer and every replay holds these rules.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

PROJECTION_FILENAME = "steering_proposals.json"
SCHEMA = "steering_proposals/1"
DECISIONS_FILENAME = "steering_proposal_decisions.ndjson"
LOCK_FILENAME = ".steering_proposals.lock"
REVISION_DECISIONS = frozenset({"proposed", "revised", "reopened"})
OPERATOR_DECISIONS = frozenset({"approved", "rejected", "applied", "acknowledged"})
RECORD_DECISIONS = REVISION_DECISIONS | OPERATOR_DECISIONS | {"suppressed"}
OUTCOMES = ("added", "revised", "unchanged", "reopened", "suppressed_rejected", "suppressed_hard_stop", "applied_with_hard_stop")
PERMISSION_KINDS = frozenset({"permissie", "bouwer_allowlist", "bouwgewoonte"})
MELDING_KIND = "alleen_melding"
KLASSEN = frozenset(f"K{n}" for n in range(1, 9))
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


def _prompt_ids(value: Any) -> List[str]:
    """A list of non-empty strings; a bare string or any other item is refused."""
    if not isinstance(value, list) or not all(isinstance(i, str) and i.strip() for i in value):
        raise ProposalInvalid("evidence_prompt_ids is a list of non-empty strings")
    return list(value)


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
    klasse = proposal.get("handelingsklasse")
    if klasse is not None or fields["artefact_soort"] == "permissie":
        if klasse not in KLASSEN:
            raise ProposalInvalid("handelingsklasse is one of K1..K8 (required for permissie)")
        fields["handelingsklasse"] = klasse
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
    try:
        _prompt_ids(record.get("evidence_prompt_ids", []))
    except ProposalInvalid as exc:
        raise StoreUnreadable(f"{where}: {exc}") from exc
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
                               "recorded_at": record["decided_at"],
                               **({"handelingsklasse": record["handelingsklasse"]} if record.get("handelingsklasse") else {})})


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
    if entry["artefact_soort"] in PERMISSION_KINDS and decision != "suppressed" and _melding_for(state, entry["project_id"], entry["oorzaak"]):
        raise DecisionRefused(f"a stored hard stop blocks {decision} on this permission proposal")
    if decision in REVISION_DECISIONS:
        if decision == "reopened" and entry["status"] != "rejected":
            raise DecisionRefused("only a rejected proposal reopens")
        operator_note = _text(record.get("reason")) and _text(record.get("decided_by")).lower() not in ("", "system")
        if decision == "reopened" and entry["artefact_soort"] in PERMISSION_KINDS and not (operator_note or _class_change(entry, record)):
            raise DecisionRefused("a rejected permission proposal reopens only on an operator note or a changed class")
        _add_revision(entry, record)
        if decision == "reopened":
            entry.update(status="pending", decided_at=None, decided_revision=None)
    elif decision == "suppressed":
        if entry["artefact_soort"] not in PERMISSION_KINDS or entry["status"] not in ("pending", "approved"):
            raise DecisionRefused("only a pending or approved permission proposal is suppressed")
        entry.update(status="suppressed_hard_stop")
    elif (decision == "acknowledged") != (entry["artefact_soort"] == MELDING_KIND):
        raise DecisionRefused("a notification is only acknowledged, and only a notification is")
    elif decision in ("approved", "rejected", "acknowledged"):
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


def _project(state_dir: Path, state: Mapping[str, Mapping[str, Any]]) -> None:
    """Write the derived projection when it differs from the replayed state (temp file plus replace)."""
    path = Path(state_dir) / PROJECTION_FILENAME
    body = json.dumps({"schema": SCHEMA, "proposals": [state[k] for k in sorted(state)]},
                      indent=2, ensure_ascii=False, sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == body:
            return
    except (OSError, ValueError):  # vnx-silent-except: the projection is derived, a missing or unreadable one is rewritten
        pass
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:  # vnx-atomic-write: temp file, fsync, replace
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _commit(state_dir: Path, state: Dict[str, Dict[str, Any]], records: Sequence[Mapping[str, Any]]) -> None:
    """Validate as the read path does, fold, append, project; every refusal comes before the write."""
    for record in records:
        try:
            _validate_record("new record", dict(record))
        except StoreUnreadable as exc:
            raise DecisionRefused(f"record refused: {exc}") from exc
        _apply(state, record)
    if records:
        _append(state_dir, records)
    _project(state_dir, state)


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
        _project(state_dir, state)
        return [state[k] for k in sorted(state) if state[k]["project_id"] == project_id]


def read_proposals(state_dir: Path, project_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(proposals, decisions)`` of this project, replayed from the records, strictly read-only.

    Never writes: no ``steering_proposals.json`` projection, no lock file. A reader (ADR-076 decision 14) uses
    this instead of ``proposals()``, which rewrites the derived projection when it differs. The records file is
    append-only and every append lands on a line boundary, so a lock-free read sees a consistent prefix.
    """
    project_id = _require_project(project_id)
    records = _read_records(state_dir)
    state = _replay(records)
    entries = [state[k] for k in sorted(state) if state[k]["project_id"] == project_id]
    return entries, [r for r in records if r["project_id"] == project_id]


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
                 evidence_prompt_ids: Optional[Sequence[str]] = None, reopen: bool = False,
                 now: Optional[str] = None) -> Dict[str, Any]:
    """Append one revision record: ``proposed`` for a new identity, ``revised`` for a known one,
    ``reopened`` (``reopen=True``, rejected proposals only). Returns the record. Whether a batch entry
    deserves a revision is ``reconcile``."""
    project_id = _require_project(project_id)
    item = _normalise(project_id, proposal)
    stamp = _stamp(now)
    ids = _prompt_ids(list(evidence_prompt_ids) if isinstance(evidence_prompt_ids, (list, tuple))
                      else evidence_prompt_ids) if evidence_prompt_ids is not None else [e["prompt_id"] for e in item["evidence"]]
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(item["proposal_id"])
        decision, revision = ("proposed", 1) if entry is None else ("reopened" if reopen else "revised",
                                                                    len(entry["revisions"]) + 1)
        record = _record(project_id, item["proposal_id"], revision, decision, stamp, evidence_prompt_ids=ids,
                         **{n: item[n] for n in (*_REQUIRED_TEXT, "handelingsklasse") if n in item})
        owed = _suppress(json.loads(json.dumps(state)), project_id, item, stamp, dict.fromkeys(OUTCOMES, 0)) \
            if item["artefact_soort"] == MELDING_KIND else []  # a notification suppresses from every writer
        _commit(state_dir, state, [record, *owed])
    return record


def _settings_evidence(entry: Mapping[str, Any], applied_ref: Any, roots: Optional[Tuple[Path, Path]]) -> Dict[str, Any]:
    """ADR-086 decision 2: the read-only check of the settings file; a failed check is a DecisionRefused."""
    import permission_applied_check as check
    try:
        if roots is None:
            from project_root import resolve_project_root
            roots = (resolve_project_root(), Path.home())
        return check.verify(entry, applied_ref, *roots)
    except RuntimeError as exc:
        raise DecisionRefused(f"applied refused, path: no project root to check against ({exc})") from exc
    except check.Refused as exc:
        raise DecisionRefused(f"applied refused, {exc}") from exc


def decide(state_dir: Path, project_id: str, pid: str, decision: str, *, reason: str, decided_by: str,
           revision: Optional[int] = None, applied_ref: str = "", now: Optional[str] = None,
           settings_roots: Optional[Tuple[Path, Path]] = None) -> Dict[str, Any]:
    """Record an operator decision bound to a revision (default: the latest). The record is
    appended and fsynced. ``approved`` applies nothing. ``applied`` on an approved ``permissie`` proposal first
    passes the read-only settings check (ADR-086 decision 2; ``settings_roots`` is ``(repo, home)``, default the
    git root of the working directory and the home dir); the record then carries ``applied_evidence`` and an
    ``applied_at`` stamped after the check."""
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
        if decision == "applied" and entry["artefact_soort"] == "permissie" and entry["status"] == "approved":
            extra["applied_evidence"] = _settings_evidence(entry, applied_ref, settings_roots)
            stamp = _stamp(now)  # applied_at: the moment the record is written, after the check passed
            extra["applied_at"] = stamp
        record = _record(project_id, pid, bound, decision, stamp, reason=_text(reason), decided_by=_text(decided_by), **extra)
        _commit(state_dir, state, [record])
    return record


def _fresh_evidence(entry: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> List[str]:
    """Prompt ids whose transcript ts is after the rejection; an absent or malformed ts never counts."""
    decided = _parse_ts(entry["decided_at"])
    stamps = [(e["prompt_id"], _parse_ts(e.get("ts"))) for e in evidence]
    return [pid for pid, ts in stamps if decided is not None and ts is not None and ts > decided]


def _melding_for(state: Mapping[str, Mapping[str, Any]], project_id: str, oorzaak: str) -> Optional[str]:
    """The proposal_id of a stored notification with this ``oorzaak`` (any status), or None."""
    return next((pid for pid, e in sorted(state.items()) if e["project_id"] == project_id
                 and e["artefact_soort"] == MELDING_KIND and e["oorzaak"] == oorzaak), None)


def _class_change(entry: Mapping[str, Any], item: Mapping[str, Any]) -> Optional[str]:
    """``old -> new`` when the class differs from the one on the rejected revision; both must be known."""
    old, new = entry["revisions"][entry["decided_revision"] - 1].get("handelingsklasse"), item.get("handelingsklasse")
    return f"handelingsklasse {old} -> {new}" if old in KLASSEN and new in KLASSEN and old != new else None


def _plan(state: Mapping[str, Mapping[str, Any]], project_id: str, item: Mapping[str, Any], now: str):
    """The outcome of one batch item and the record it needs (None for no write)."""
    pid, ids = item["proposal_id"], [e["prompt_id"] for e in item["evidence"]]
    content = {n: item[n] for n in (*_REQUIRED_TEXT, "handelingsklasse") if n in item}
    entry, permission = state.get(pid), item["artefact_soort"] in PERMISSION_KINDS
    if permission and (_melding_for(state, project_id, item["oorzaak"]) or (entry or {}).get("status") == "suppressed_hard_stop"):
        return "suppressed_hard_stop", None
    if entry is None:
        return "added", _record(project_id, pid, 1, "proposed", now, evidence_prompt_ids=ids, **content)
    revision, changed = len(entry["revisions"]) + 1, item["voorstel"] != entry["revisions"][-1]["voorstel"]
    changed = changed or item.get("handelingsklasse") != entry["revisions"][-1].get("handelingsklasse")
    rejected = entry["status"] == "rejected"
    why = (_class_change(entry, item) if permission else "new evidence after the rejection") if rejected else None
    fresh = ids if permission else _fresh_evidence(entry, item["evidence"]) if rejected else []
    if why and (permission or fresh):
        return "reopened", _record(project_id, pid, revision, "reopened", now, evidence_prompt_ids=fresh,
                                   reason=why, **content)
    outcome = "suppressed_rejected" if entry["status"] == "rejected" else "revised" if changed else "unchanged"
    if not changed:
        return outcome, None
    return outcome, _record(project_id, pid, revision, "revised", now, evidence_prompt_ids=ids, **content)


def reconcile(state_dir: Path, project_id: str, batch: Sequence[Mapping[str, Any]] = (), *,
              now: Optional[str] = None) -> Dict[str, int]:
    """Bring a batch of proposals against the store; returns the count per outcome (``OUTCOMES``).

    A new identity is added; a known one gets a revision only when its text changed; a rejected one
    reopens on evidence after the rejection, otherwise its changed text is kept as a revision and counted
    ``suppressed_rejected`` (ADR-072 decision 14). The whole batch is validated before any write; an
    empty batch only rebuilds the projection."""
    project_id = _require_project(project_id)
    items = [_normalise(project_id, proposal) for proposal in batch]
    stamp = _stamp(now)
    counts = {name: 0 for name in OUTCOMES}
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        scratch, records = json.loads(json.dumps(state)), []  # the plan folds into a copy
        for item in items:
            outcome, record = _plan(scratch, project_id, item, stamp)
            counts[outcome] += 1
            if record is not None:
                _apply(scratch, record)
                records.append(record)
            if item["artefact_soort"] == MELDING_KIND:
                records += _suppress(scratch, project_id, item, stamp, counts)
        _commit(state_dir, state, records)
    return counts


def _suppress(scratch: Dict[str, Dict[str, Any]], project_id: str, melding: Mapping[str, Any], now: str, counts: Dict[str, int]):
    """The ``suppressed`` records a notification owes the permission proposals with its ``oorzaak``."""
    oorzaak, records = melding["oorzaak"], []
    for pid, entry in sorted(scratch.items()):
        if entry["project_id"] != project_id or entry["oorzaak"] != oorzaak or entry["artefact_soort"] not in PERMISSION_KINDS:
            continue
        if entry["status"] == "applied":
            counts["applied_with_hard_stop"] += 1
        elif entry["status"] in ("pending", "approved"):
            record = _record(project_id, pid, len(entry["revisions"]), "suppressed", now, reason=melding["proposal_id"])
            _apply(scratch, record)
            records.append(record)
    return records


def reopen_with_note(state_dir: Path, project_id: str, pid: str, *, note: str, decided_by: str,
                     now: Optional[str] = None) -> Dict[str, Any]:
    """Reopen a rejected proposal on a note of the operator (ADR-084): a ``reopened`` record with the content of
    the latest revision. Refused for an empty note or ``system``; ``_apply`` refuses another status or a hard stop."""
    project_id = _require_project(project_id)
    if not _text(note) or _text(decided_by).lower() in ("", "system"):
        raise DecisionRefused("reopen_with_note needs a note and an operator as decided_by")
    stamp = _stamp(now)
    with _locked(state_dir):
        state = _replay(_read_records(state_dir))
        entry = state.get(pid)
        if entry is None or entry["project_id"] != project_id:
            raise ProposalNotFound(pid)
        latest = entry["revisions"][-1]
        content = {**{n: entry[n] for n in ("oorzaak", "artefact_soort", "doelpad")},
                   **{n: latest[n] for n in ("voorstel", "handelingsklasse") if n in latest}}
        record = _record(project_id, pid, len(entry["revisions"]) + 1, "reopened", stamp, reason=_text(note),
                         decided_by=_text(decided_by), **content)
        _commit(state_dir, state, [record])
    return record
